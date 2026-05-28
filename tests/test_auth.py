"""Authentication — Clerk JWT verification tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaianolevine_api import auth as auth_mod
from kaianolevine_api.auth import (
    get_current_owner,
    require_wcs_admin_or_service,
    require_wcs_service,
    verify_clerk_jwt,
)
from kaianolevine_api.config import get_settings
from kaianolevine_api.models import WcsUserProfile


class _SettingsShim:
    """Minimal settings object for unit-testing auth helpers."""

    CLERK_JWKS_URL = "https://example.clerk.accounts.dev/.well-known/jwks.json"
    CLERK_ISSUER = "https://example.clerk.accounts.dev"


class _ServiceSettings:
    WCS_SERVICE_MACHINE_IDS = ["mch_wiki-cog"]


@pytest.fixture
async def db_session(async_engine) -> AsyncIterator[AsyncSession]:
    sm = async_sessionmaker(async_engine, expire_on_commit=False, autoflush=False)
    async with sm() as session:
        yield session


@pytest.mark.asyncio
async def test_valid_jwt_returns_sub(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_verify(token: str, settings: object) -> str | None:
        del settings
        return "user_123" if token == "good" else None

    monkeypatch.setattr(auth_mod, "verify_clerk_jwt", fake_verify)

    owner = await get_current_owner(
        authorization="Bearer good",
        settings=_SettingsShim(),  # type: ignore[arg-type]
    )
    assert owner == "user_123"


@pytest.mark.asyncio
async def test_missing_authorization_raises_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_mod, "verify_clerk_jwt", AsyncMock(return_value=None))

    with pytest.raises(HTTPException) as excinfo:
        await get_current_owner(
            authorization=None,
            settings=_SettingsShim(),  # type: ignore[arg-type]
        )
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_invalid_token_raises_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_mod, "verify_clerk_jwt", AsyncMock(return_value=None))

    with pytest.raises(HTTPException) as excinfo:
        await get_current_owner(
            authorization="Bearer bad",
            settings=_SettingsShim(),  # type: ignore[arg-type]
        )
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_clerk_jwt_returns_none_without_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Empty:
        CLERK_JWKS_URL = None
        CLERK_ISSUER = None

    fetch = AsyncMock(side_effect=AssertionError("JWKS must not be fetched"))
    monkeypatch.setattr(auth_mod, "_fetch_jwks_document", fetch)

    sub = await verify_clerk_jwt("any", _Empty())  # type: ignore[arg-type]
    assert sub is None
    fetch.assert_not_called()


@pytest.mark.asyncio
async def test_require_wcs_service_accepts_allowlisted() -> None:
    caller = await require_wcs_service("mch_wiki-cog", _ServiceSettings())  # type: ignore[arg-type]
    assert caller == "mch_wiki-cog"


@pytest.mark.asyncio
async def test_require_wcs_service_rejects_unknown() -> None:
    with pytest.raises(HTTPException) as excinfo:
        await require_wcs_service("user_123", _ServiceSettings())  # type: ignore[arg-type]
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_require_wcs_service_rejects_when_allowlist_empty() -> None:
    empty = type("_EmptyServiceSettings", (), {"WCS_SERVICE_MACHINE_IDS": []})()
    with pytest.raises(HTTPException) as excinfo:
        await require_wcs_service("mch_wiki-cog", empty)  # type: ignore[arg-type]
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_require_wcs_admin_or_service_accepts_machine() -> None:
    caller = await require_wcs_admin_or_service(
        "mch_wiki-cog",
        session=None,  # type: ignore[arg-type]
        settings=_ServiceSettings(),  # type: ignore[arg-type]
    )
    assert caller == "mch_wiki-cog"


@pytest.mark.asyncio
async def test_require_wcs_admin_or_service_accepts_admin_user(
    db_session: AsyncSession,
) -> None:
    db_session.add(
        WcsUserProfile(user_id="admin-user", email="", display_name="", is_admin=True)
    )
    await db_session.commit()

    caller = await require_wcs_admin_or_service(
        "admin-user",
        db_session,
        _ServiceSettings(),  # type: ignore[arg-type]
    )
    assert caller == "admin-user"


@pytest.mark.asyncio
async def test_require_wcs_admin_or_service_rejects_non_admin_non_service(
    db_session: AsyncSession,
) -> None:
    db_session.add(
        WcsUserProfile(user_id="viewer", email="", display_name="", is_admin=False)
    )
    await db_session.commit()

    with pytest.raises(HTTPException) as excinfo:
        await require_wcs_admin_or_service(
            "viewer",
            db_session,
            _ServiceSettings(),  # type: ignore[arg-type]
        )
    assert excinfo.value.status_code == 403


def test_wcs_service_machine_ids_parses_comma_separated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("WCS_SERVICE_MACHINE_IDS", "mch_a, mch_b")
    settings = get_settings()
    assert settings.WCS_SERVICE_MACHINE_IDS == ["mch_a", "mch_b"]
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_flags_list_accessible(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """Integration: flags endpoint still accessible after auth cleanup."""
    monkeypatch.setattr(
        auth_mod, "verify_clerk_jwt", AsyncMock(return_value="dev-owner")
    )
    r = await client.get("/v1/flags", headers={"Authorization": "Bearer test-token"})
    assert r.status_code == 200
