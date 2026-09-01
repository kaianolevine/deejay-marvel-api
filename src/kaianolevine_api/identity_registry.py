"""Declared machine principals — the source of truth for who the cogs are.

Adding a machine to the fleet is a code change here and nothing else. No
manual seeding, no database access, no script run against production. The
declaration below is reconciled into the principal store on every boot, so a
deploy is what applies it.

Revocation is a code change too: delete the entry, or remove a role from one,
and the next deploy takes the grant away. That is the point of declaring it
rather than seeding it — the grant lives in git, reviewable and reversible,
instead of in whatever state someone left the database in.

Two deliberate limits on what reconciliation touches:

  * Only machines. Human principals are backfilled from wcs_user_profiles and
    granted through the admin UI; a code deploy must never silently re-grant
    or revoke a person's access.

  * Only grants it made itself. Role rows carry ``granted_by``, and
    reconciliation only ever removes rows it wrote (``identity_registry``).
    A role granted by hand for a one-off is left alone rather than being
    quietly reverted by the next unrelated deploy.

Machines are declared by **name**, never by Clerk id. The name is the thing a
human knows; the id is Clerk's business and nobody should have to go find it.

A machine binds its own id by calling ``POST /v1/identity/register`` with its
name and its own credential. The subject is taken from the verified token, so
the binding is established by the machine that holds the secret rather than
typed in by a person. Once bound it is recorded, and a different subject
claiming the same name is refused rather than silently rebinding it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from identity.store import Principal as PrincipalRow
from identity.store import PrincipalRole
from identity.store import Role as RoleRow
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger(__name__)

#: Marks role grants this module owns. Reconciliation only removes these.
GRANTED_BY = "identity_registry"

ISSUER_COGS = "https://clerk.kaianolevine.com"


@dataclass(frozen=True)
class Machine:
    """One declared machine principal, identified by name.

    ``name`` is what the cog calls itself when it registers, and what it is
    called in this file, in logs, and in the audit trail. The Clerk subject is
    discovered at registration and never written here.
    """

    name: str
    roles: tuple[str, ...]
    issuer: str = ISSUER_COGS
    notes: str = ""


# ---------------------------------------------------------------------------
# The fleet. Add a machine by adding an entry.
# ---------------------------------------------------------------------------

MACHINES: tuple[Machine, ...] = (
    # deejay-cog is the first cog with its own Clerk machine. Every other cog
    # still shares `miniappolis-cogs`, which is why the audit trail can say a
    # cog called but not which one. They move over one at a time, each by
    # adding an entry here and calling POST /v1/identity/register once.
    Machine(
        name="deejay-cog",
        roles=("catalog-ingest",),
        notes="Ingests DJ sets, tracks and live plays.",
    ),
)


def declared(name: str) -> Machine | None:
    """The declared machine with this name, or None if it is not declared.

    Registration consults this: a caller may only claim a name that already
    appears in the file, so registering can never invent a principal the fleet
    did not intend to have.
    """
    for machine in MACHINES:
        if machine.name == name:
            return machine
    return None


async def reconcile(session: AsyncSession) -> dict[str, int]:
    """Sync declared roles onto machines that have registered. Idempotent.

    Runs on every boot. It does not create principals — a principal needs a
    Clerk subject, and only the machine holding the secret can supply that, by
    registering. What this does is keep the *grants* equal to what the file
    declares, so adding or removing a role is a deploy and nothing more.

    A declared machine that has never registered simply has no row yet, which
    is not an error: it means that cog has not been given its secret and
    started up. It will pick up its roles on the deploy after it registers,
    or immediately, since registration grants declared roles itself.

    Never raises into startup: failing to grant is already fail-closed, since
    an ungranted principal is denied by the ordinary authorization path.
    """
    granted = revoked = 0
    by_name = {m.name: m for m in MACHINES}

    known_roles = set((await session.execute(select(RoleRow.name))).scalars().all())
    for machine in MACHINES:
        missing = set(machine.roles) - known_roles
        if missing:
            # Refuse rather than create: the role vocabulary comes from a
            # migration, and inventing one here would let a typo mint a role
            # that grants nothing and looks correct in the file.
            _log.error(
                "[identity] %s declares unknown role(s) %s; skipping",
                machine.name,
                sorted(missing),
            )

    machine_rows = (
        (
            await session.execute(
                select(PrincipalRow).where(PrincipalRow.kind == "machine")
            )
        )
        .scalars()
        .all()
    )

    for row in machine_rows:
        machine = by_name.get(row.display_name)
        wanted: set[str] = set()
        if machine is not None and not (set(machine.roles) - known_roles):
            wanted = set(machine.roles)

        current = {
            name: by
            for name, by in (
                await session.execute(
                    select(PrincipalRole.role_name, PrincipalRole.granted_by).where(
                        PrincipalRole.principal_id == row.id
                    )
                )
            ).all()
        }

        for name in wanted - set(current):
            session.add(
                PrincipalRole(
                    principal_id=row.id, role_name=name, granted_by=GRANTED_BY
                )
            )
            granted += 1

        # Only ever remove grants this module made. A role granted by hand for
        # a one-off is left alone rather than quietly reverted by the next
        # unrelated deploy.
        for name, granted_by in current.items():
            if name not in wanted and granted_by == GRANTED_BY:
                stale = (
                    (
                        await session.execute(
                            select(PrincipalRole).where(
                                PrincipalRole.principal_id == row.id,
                                PrincipalRole.role_name == name,
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                if stale is not None:
                    await session.delete(stale)
                    revoked += 1

    await session.commit()
    return {"granted": granted, "revoked": revoked}
