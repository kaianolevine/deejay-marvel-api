"""GET /v1/identity/whoami."""

from __future__ import annotations

import uuid

import pytest
from identity.store import Principal, PrincipalRole
from identity.types import VerifiedSubject

from kaianolevine_api import auth as auth_mod

ISSUER = "https://clerk.kaianolevine.com"


def _vs(subject: str, kind: str = "machine") -> VerifiedSubject:
    return VerifiedSubject(issuer=ISSUER, subject=subject, kind=kind)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_unknown_subject_reports_the_string_to_seed(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case this endpoint exists for: a Clerk machine with no principal.

    The caller is authenticated but unknown, and the response hands back the
    exact subject to seed rather than leaving it to be guessed.
    """
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        auth_mod, "verify_bearer", AsyncMock(return_value=_vs("mch_brand_new"))
    )
    r = await client.get("/v1/identity/whoami")
    assert r.status_code == 200
    body = r.json()
    assert body["verified"]["subject"] == "mch_brand_new"
    assert body["verified"]["kind"] == "machine"
    assert body["principal"] is None
    assert "Seed it" in body["hint"]


@pytest.mark.asyncio
async def test_known_subject_reports_principal_and_roles(
    client, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    await db_session.flush()
    pid = uuid.uuid4()
    db_session.add(
        Principal(
            id=pid,
            kind="machine",
            issuer=ISSUER,
            subject="mch_deejay_cog",
            display_name="deejay-cog",
        )
    )
    db_session.add(PrincipalRole(principal_id=pid, role_name="catalog-ingest"))
    await db_session.commit()

    monkeypatch.setattr(
        auth_mod, "verify_bearer", AsyncMock(return_value=_vs("mch_deejay_cog"))
    )
    r = await client.get("/v1/identity/whoami")
    assert r.status_code == 200
    body = r.json()
    assert body["principal"]["id"] == str(pid)
    assert body["principal"]["roles"] == ["catalog-ingest"]
    assert body["principal"]["display_name"] == "deejay-cog"


@pytest.mark.asyncio
async def test_requires_a_credential(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unauthenticated callers learn nothing."""
    from kaianolevine_api.schemas import api_error

    async def _reject(*_a, **_k):
        raise api_error(401, "unauthorized", "Valid Bearer token required")

    monkeypatch.setattr(auth_mod, "verify_bearer", _reject)
    r = await client.get("/v1/identity/whoami")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# POST /v1/identity/register — a machine binds its own Clerk subject
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_machine_registers_itself_under_a_declared_name(
    client, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No human ever handles the Clerk id.

    The machine holding the secret is the only party that needs to know its
    own subject, and it is the one calling.
    """
    from unittest.mock import AsyncMock

    from kaianolevine_api import identity_registry as reg

    await db_session.commit()

    monkeypatch.setattr(
        reg, "MACHINES", (reg.Machine(name="deejay-cog", roles=("catalog-ingest",)),)
    )
    monkeypatch.setattr(
        auth_mod, "verify_bearer", AsyncMock(return_value=_vs("mch_anything_at_all"))
    )
    r = await client.post("/v1/identity/register", json={"name": "deejay-cog"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True
    assert body["principal"]["name"] == "deejay-cog"
    assert body["principal"]["subject"] == "mch_anything_at_all"
    # Usable immediately, not after the next deploy.
    assert body["principal"]["roles"] == ["catalog-ingest"]


@pytest.mark.asyncio
async def test_undeclared_name_is_refused(
    client, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registering can never invent a principal the fleet did not intend."""
    from unittest.mock import AsyncMock

    from kaianolevine_api import identity_registry as reg

    await db_session.commit()
    monkeypatch.setattr(reg, "MACHINES", ())
    monkeypatch.setattr(
        auth_mod, "verify_bearer", AsyncMock(return_value=_vs("mch_rogue"))
    )
    r = await client.post("/v1/identity/register", json={"name": "not-a-cog"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "machine_not_declared"


@pytest.mark.asyncio
async def test_a_different_machine_cannot_steal_a_bound_name(
    client, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rebinding a name would be a privilege transfer, so it is refused."""
    from unittest.mock import AsyncMock

    from kaianolevine_api import identity_registry as reg

    await db_session.flush()
    db_session.add(
        Principal(
            id=uuid.uuid4(),
            kind="machine",
            issuer=ISSUER,
            subject="mch_the_real_one",
            display_name="deejay-cog",
        )
    )
    await db_session.commit()

    monkeypatch.setattr(
        reg, "MACHINES", (reg.Machine(name="deejay-cog", roles=("catalog-ingest",)),)
    )
    monkeypatch.setattr(
        auth_mod, "verify_bearer", AsyncMock(return_value=_vs("mch_impostor"))
    )
    r = await client.post("/v1/identity/register", json={"name": "deejay-cog"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "name_already_bound"


@pytest.mark.asyncio
async def test_re_registering_is_idempotent(
    client, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    from kaianolevine_api import identity_registry as reg

    await db_session.commit()
    monkeypatch.setattr(
        reg, "MACHINES", (reg.Machine(name="deejay-cog", roles=("catalog-ingest",)),)
    )
    monkeypatch.setattr(
        auth_mod, "verify_bearer", AsyncMock(return_value=_vs("mch_dj"))
    )
    first = await client.post("/v1/identity/register", json={"name": "deejay-cog"})
    second = await client.post("/v1/identity/register", json={"name": "deejay-cog"})
    assert first.json()["created"] is True
    assert second.json()["created"] is False
    assert second.json()["principal"]["id"] == first.json()["principal"]["id"]


@pytest.mark.asyncio
async def test_humans_cannot_register_as_machines(
    client, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    from kaianolevine_api import identity_registry as reg

    await db_session.commit()
    monkeypatch.setattr(
        reg, "MACHINES", (reg.Machine(name="deejay-cog", roles=("catalog-ingest",)),)
    )
    monkeypatch.setattr(
        auth_mod, "verify_bearer", AsyncMock(return_value=_vs("user_1", "human"))
    )
    r = await client.post("/v1/identity/register", json={"name": "deejay-cog"})
    assert r.status_code == 403
