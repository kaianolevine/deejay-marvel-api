"""Reconciliation guardrails — what a deploy may and may not change.

The declaration is the source of truth for machines, so a deploy is what
applies it. These cover the limits on that power: it touches machines only,
grants it made itself only, and refuses anything it cannot verify.
"""

from __future__ import annotations

import uuid

import pytest
from identity.apikey import API_KEY_ISSUER
from identity.store import Principal, PrincipalRole
from sqlalchemy import select

from kaianolevine_api import identity_registry as reg
from tests.conftest import DEV_ISSUER, seed_identity


async def _roles_of(session, subject: str, issuer: str = API_KEY_ISSUER) -> set[str]:
    row = (
        (
            await session.execute(
                select(Principal).where(
                    Principal.issuer == issuer, Principal.subject == subject
                )
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        return set()
    return set(
        (
            await session.execute(
                select(PrincipalRole.role_name).where(
                    PrincipalRole.principal_id == row.id
                )
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_removing_a_role_from_the_file_revokes_it(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revocation is a code change — that is the point of declaring it."""
    await seed_identity(db_session)
    monkeypatch.setattr(
        reg,
        "MACHINES",
        (reg.Machine(name="deejay-cog", roles=("catalog-ingest", "pipeline-writer")),),
    )
    await reg.reconcile(db_session)
    assert await _roles_of(db_session, "deejay-cog") == {
        "catalog-ingest",
        "pipeline-writer",
    }

    monkeypatch.setattr(
        reg, "MACHINES", (reg.Machine(name="deejay-cog", roles=("catalog-ingest",)),)
    )
    assert (await reg.reconcile(db_session))["revoked"] == 1
    assert await _roles_of(db_session, "deejay-cog") == {"catalog-ingest"}


@pytest.mark.asyncio
async def test_hand_granted_roles_are_left_alone(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A one-off grant must survive an unrelated deploy.

    Reconciliation removes only rows it wrote, identified by granted_by.
    """
    await seed_identity(db_session)
    pid = uuid.uuid4()
    db_session.add(
        Principal(
            id=pid,
            kind="machine",
            issuer=API_KEY_ISSUER,
            subject="manual-cog",
            display_name="manual-cog",
        )
    )
    db_session.add(
        PrincipalRole(
            principal_id=pid, role_name="pipeline-writer", granted_by="a-human"
        )
    )
    await db_session.commit()

    monkeypatch.setattr(reg, "MACHINES", ())
    assert (await reg.reconcile(db_session))["revoked"] == 0
    assert await _roles_of(db_session, "manual-cog") == {"pipeline-writer"}


@pytest.mark.asyncio
async def test_human_principals_are_never_touched(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A code deploy must not re-grant or revoke a person's access."""
    await seed_identity(db_session)
    pid = uuid.uuid4()
    db_session.add(Principal(id=pid, kind="human", issuer=DEV_ISSUER, subject="user_2"))
    db_session.add(
        PrincipalRole(
            principal_id=pid, role_name="catalog-ingest", granted_by=reg.GRANTED_BY
        )
    )
    await db_session.commit()

    monkeypatch.setattr(reg, "MACHINES", ())
    assert (await reg.reconcile(db_session))["revoked"] == 0
    assert await _roles_of(db_session, "user_2", DEV_ISSUER) == {"catalog-ingest"}


@pytest.mark.asyncio
async def test_unknown_role_is_refused_not_invented(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo must not mint a role that grants nothing and looks correct.

    The machine is skipped entirely rather than created with no roles, so the
    error surfaces as an absent principal rather than a silent denial later.
    """
    await seed_identity(db_session)
    monkeypatch.setattr(
        reg, "MACHINES", (reg.Machine(name="oops", roles=("catalog-injest",)),)
    )
    assert await reg.reconcile(db_session) == {
        "created": 0,
        "granted": 0,
        "revoked": 0,
    }
    assert await _roles_of(db_session, "oops") == set()


def test_declared_lookup() -> None:
    assert reg.declared("deejay-cog") is not None
    assert reg.declared("no-such-cog") is None
