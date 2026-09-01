"""GET /v1/identity/whoami — what this enforcement point sees about the caller.

Authorization is normally invisible: a caller gets a 200 or a 403 and has no
way to find out which of the four contract steps produced it. That is fine in
production and miserable when wiring a new principal, because the one string
you need — the subject Clerk actually reports — is the one string nothing
shows you.

This endpoint runs verify and resolve and reports both, without authorizing
anything. It is the tool for answering "what do I put in the principal row",
and afterwards for "why is this caller getting a 403".

It is deliberately NOT scope-guarded. Any caller holding a valid credential
can see their own identity and nothing else — no other principal, no roles
they do not hold, no store contents. Requiring a scope to discover your own
identity would make it useless for the case it exists to serve: a caller that
has no principal yet.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header
from identity.store import Issuer
from identity.store import Principal as PrincipalRow
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import auth
from .. import identity_registry as registry
from ..auth import ENFORCEMENT_POINT, resolve_principal
from ..config import Settings, get_settings
from ..database import get_db_session
from ..schemas import api_error

router = APIRouter()


@router.get(
    "/identity/whoami",
    summary="Report the caller's verified subject and resolved principal",
    description=(
        "Diagnostic. Runs verify and resolve and reports what this "
        "enforcement point sees, without making an authorization decision. "
        "`subject` is the exact string to seed into identity_principals; "
        "`principal` is null when this ecosystem has no row for that subject "
        "yet, which is the expected state for a newly created Clerk machine."
    ),
)
async def whoami(
    authorization: str | None = Header(default=None, alias="Authorization"),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    # Module-qualified so the call resolves at request time, not import time.
    subject = await auth.verify_bearer(authorization, settings)
    principal = await resolve_principal(subject, session)

    return {
        "enforcement_point": ENFORCEMENT_POINT,
        "verified": {
            "issuer": subject.issuer,
            "subject": subject.subject,
            "kind": subject.kind,
        },
        "principal": (
            None
            if principal is None
            else {
                "id": str(principal.id),
                "kind": principal.kind,
                "display_name": principal.display_name,
                "status": principal.status,
                "roles": list(principal.roles),
            }
        ),
        "hint": (
            "No principal for this subject yet. Seed it with the `subject` value above."
            if principal is None
            else "Resolved."
        ),
    }


class RegisterRequest(BaseModel):
    """What a machine says about itself when registering."""

    name: str = Field(
        ...,
        min_length=1,
        description=(
            "The machine's name as declared in identity_registry.MACHINES, "
            "e.g. `deejay-cog`. Only declared names may be claimed."
        ),
    )


@router.post(
    "/identity/register",
    summary="Register the calling machine under its declared name",
    description=(
        "A machine calls this once, with its own credential and its declared "
        "name. The API takes the Clerk subject from the verified token and "
        "binds it to that name, then grants the roles the name declares.\n\n"
        "Nobody has to look up a Clerk id: the machine holding the secret is "
        "the only party that needs to know it, and it is the one calling. "
        "Idempotent — re-registering returns the existing principal."
    ),
)
async def register(
    body: RegisterRequest,
    authorization: str | None = Header(default=None, alias="Authorization"),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """Bind this caller's Clerk subject to a declared machine name.

    Three things have to be true, and each failure is distinct:

      * the credential verifies (handled by verify_bearer, 401)
      * the name is declared in code (409) — registering can never invent a
        principal the fleet did not intend to have
      * the name is unbound, or already bound to *this* subject (409) — a
        different machine claiming a taken name is refused, never a silent
        rebind, because that would be a privilege transfer

    Roles come from the declaration, never from the request. A machine says
    who it is; it does not say what it may do.
    """
    subject = await auth.verify_bearer(authorization, settings)

    if subject.kind != "machine":
        raise api_error(403, "forbidden", "Only machine callers register this way.")

    machine = registry.declared(body.name)
    if machine is None:
        raise api_error(
            409,
            "machine_not_declared",
            f"No machine named {body.name!r} is declared. Add it to "
            "identity_registry.MACHINES and deploy.",
        )

    issuer_row = (
        (await session.execute(select(Issuer).where(Issuer.issuer == subject.issuer)))
        .scalars()
        .first()
    )
    if issuer_row is None:
        raise api_error(
            409,
            "issuer_not_registered",
            f"Issuer {subject.issuer} is trusted for authentication but has "
            "no row in identity_issuers.",
        )

    taken = (
        (
            await session.execute(
                select(PrincipalRow).where(
                    PrincipalRow.kind == "machine",
                    PrincipalRow.display_name == body.name,
                )
            )
        )
        .scalars()
        .first()
    )
    if taken is not None and taken.subject != subject.subject:
        raise api_error(
            409,
            "name_already_bound",
            f"{body.name!r} is already bound to a different Clerk subject. "
            "Refusing to rebind; unbind it deliberately if the machine was "
            "rotated.",
        )

    existing = await resolve_principal(subject, session)
    created = False
    if existing is None:
        row = PrincipalRow(
            kind="machine",
            issuer=subject.issuer,
            subject=subject.subject,
            display_name=body.name,
        )
        session.add(row)
        await session.flush()
        created = True
    else:
        row = (
            (
                await session.execute(
                    select(PrincipalRow).where(PrincipalRow.id == existing.id)
                )
            )
            .scalars()
            .first()
        )
        row.display_name = body.name

    await session.commit()

    # Grant immediately rather than waiting for the next boot, so a cog is
    # usable the moment it registers.
    result = await registry.reconcile(session)

    principal = await resolve_principal(subject, session)
    return {
        "created": created,
        "principal": {
            "id": str(principal.id),
            "name": principal.display_name,
            "kind": principal.kind,
            "subject": principal.subject,
            "roles": list(principal.roles),
            "status": principal.status,
        },
        "granted": result["granted"],
    }
