"""The declared machine registry.

Machines are declared by name in code. They bind their own Clerk subject by
registering. Reconciliation keeps the *grants* equal to what the file says,
on every boot.
"""

from __future__ import annotations

import uuid

import pytest
from identity.store import Issuer, Principal, PrincipalRole, Role, RoleScope
from sqlalchemy import select

from kaianolevine_api import identity_registry as reg

ISSUER = "https://clerk.kaianolevine.com"


async def _vocabulary(session) -> None:
    session.add(Issuer(issuer=ISSUER, jwks_url=f"{ISSUER}/.well-known/jwks.json"))
    for name, scope in [
        ("catalog-ingest", "catalog.sets.write"),
        ("pipeline-writer", "pipeline.findings.write"),
    ]:
        session.add(Role(name=name, description=name))
        session.add(RoleScope(role_name=name, scope=scope))
    await session.commit()


async def _registered(session, *, name: str, subject: str, kind: str = "machine"):
    """Stand in for a machine having called POST /v1/identity/register."""
    pid = uuid.uuid4()
    session.add(
        Principal(id=pid, kind=kind, issuer=ISSUER, subject=subject, display_name=name)
    )
    await session.commit()
    return pid


async def _roles_of(session, subject: str) -> set[str]:
    row = (
        (await session.execute(select(Principal).where(Principal.subject == subject)))
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
async def test_declared_roles_are_granted_to_a_registered_machine(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _vocabulary(db_session)
    await _registered(db_session, name="deejay-cog", subject="mch_whatever")
    monkeypatch.setattr(
        reg, "MACHINES", (reg.Machine(name="deejay-cog", roles=("catalog-ingest",)),)
    )
    result = await reg.reconcile(db_session)
    assert result["granted"] == 1
    assert await _roles_of(db_session, "mch_whatever") == {"catalog-ingest"}


@pytest.mark.asyncio
async def test_declared_but_unregistered_machine_is_a_no_op(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cog that hasn't been given its secret yet simply has no row.

    That is an expected state during rollout, not an error — reconciliation
    must not treat it as one.
    """
    await _vocabulary(db_session)
    monkeypatch.setattr(
        reg, "MACHINES", (reg.Machine(name="deejay-cog", roles=("catalog-ingest",)),)
    )
    assert await reg.reconcile(db_session) == {"granted": 0, "revoked": 0}


@pytest.mark.asyncio
async def test_reconcile_is_idempotent(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every deploy runs this; the second run must change nothing."""
    await _vocabulary(db_session)
    await _registered(db_session, name="deejay-cog", subject="mch_whatever")
    monkeypatch.setattr(
        reg, "MACHINES", (reg.Machine(name="deejay-cog", roles=("catalog-ingest",)),)
    )
    await reg.reconcile(db_session)
    assert await reg.reconcile(db_session) == {"granted": 0, "revoked": 0}


@pytest.mark.asyncio
async def test_removing_a_role_from_the_file_revokes_it(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revocation is a code change — that is the point of declaring it."""
    await _vocabulary(db_session)
    await _registered(db_session, name="deejay-cog", subject="mch_whatever")
    monkeypatch.setattr(
        reg,
        "MACHINES",
        (reg.Machine(name="deejay-cog", roles=("catalog-ingest", "pipeline-writer")),),
    )
    await reg.reconcile(db_session)
    assert await _roles_of(db_session, "mch_whatever") == {
        "catalog-ingest",
        "pipeline-writer",
    }

    monkeypatch.setattr(
        reg, "MACHINES", (reg.Machine(name="deejay-cog", roles=("catalog-ingest",)),)
    )
    assert (await reg.reconcile(db_session))["revoked"] == 1
    assert await _roles_of(db_session, "mch_whatever") == {"catalog-ingest"}


@pytest.mark.asyncio
async def test_removing_the_machine_revokes_its_grants(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _vocabulary(db_session)
    await _registered(db_session, name="deejay-cog", subject="mch_whatever")
    monkeypatch.setattr(
        reg, "MACHINES", (reg.Machine(name="deejay-cog", roles=("catalog-ingest",)),)
    )
    await reg.reconcile(db_session)
    monkeypatch.setattr(reg, "MACHINES", ())
    assert (await reg.reconcile(db_session))["revoked"] == 1
    assert await _roles_of(db_session, "mch_whatever") == set()


@pytest.mark.asyncio
async def test_hand_granted_roles_are_left_alone(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A one-off grant must not be silently reverted by an unrelated deploy."""
    await _vocabulary(db_session)
    pid = await _registered(db_session, name="manual-cog", subject="mch_manual")
    db_session.add(
        PrincipalRole(
            principal_id=pid, role_name="pipeline-writer", granted_by="a-human"
        )
    )
    await db_session.commit()

    monkeypatch.setattr(reg, "MACHINES", ())
    assert (await reg.reconcile(db_session))["revoked"] == 0
    assert await _roles_of(db_session, "mch_manual") == {"pipeline-writer"}


@pytest.mark.asyncio
async def test_human_principals_are_never_touched(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A code deploy must not re-grant or revoke a person's access."""
    await _vocabulary(db_session)
    pid = await _registered(db_session, name="somebody", subject="user_1", kind="human")
    db_session.add(
        PrincipalRole(
            principal_id=pid, role_name="catalog-ingest", granted_by=reg.GRANTED_BY
        )
    )
    await db_session.commit()

    monkeypatch.setattr(reg, "MACHINES", ())
    assert (await reg.reconcile(db_session))["revoked"] == 0
    assert await _roles_of(db_session, "user_1") == {"catalog-ingest"}


@pytest.mark.asyncio
async def test_unknown_role_is_refused_not_invented(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo must not mint a role that grants nothing and looks correct."""
    await _vocabulary(db_session)
    await _registered(db_session, name="oops", subject="mch_typo")
    monkeypatch.setattr(
        reg, "MACHINES", (reg.Machine(name="oops", roles=("catalog-injest",)),)
    )
    assert await reg.reconcile(db_session) == {"granted": 0, "revoked": 0}
    assert await _roles_of(db_session, "mch_typo") == set()


def test_declared_lookup() -> None:
    assert reg.declared("deejay-cog") is not None
    assert reg.declared("no-such-cog") is None
