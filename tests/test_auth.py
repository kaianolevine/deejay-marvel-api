"""Authentication — Clerk JWT verification tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from kaianolevine_api import auth as auth_mod
from kaianolevine_api.auth import (
    get_current_owner,
    require_wcs_service,
    verify_clerk_jwt,
)


class _SettingsShim:
    """Minimal settings object for unit-testing auth helpers."""

    CLERK_JWKS_URL = "https://example.clerk.accounts.dev/.well-known/jwks.json"
    CLERK_ISSUER = "https://example.clerk.accounts.dev"
    CLERK_SECRET_KEY = "sk_test"


@pytest.mark.asyncio
async def test_valid_jwt_returns_sub(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_verify(token: str, settings: object) -> tuple[str, str] | None:
        del settings
        return ("user_123", "jwt") if token == "good" else None

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

    result = await verify_clerk_jwt("any", _Empty())  # type: ignore[arg-type]
    assert result is None
    fetch.assert_not_called()


@pytest.mark.asyncio
async def test_verify_clerk_jwt_opaque_returns_sub_and_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        auth_mod,
        "_verify_opaque_token",
        AsyncMock(return_value="mch_wiki-cog"),
    )

    result = await verify_clerk_jwt("opaque-token-no-dots", _SettingsShim())  # type: ignore[arg-type]
    assert result == ("mch_wiki-cog", "opaque")


@pytest.mark.asyncio
async def test_verify_clerk_jwt_jwt_returns_sub_and_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_mod, "_fetch_jwks_document", AsyncMock(return_value={}))
    monkeypatch.setattr(
        auth_mod,
        "_decode_clerk_jwt_sync",
        lambda token, settings, jwks_doc: "user_abc",  # noqa: ARG005
    )

    result = await verify_clerk_jwt(
        "header.payload.sig",
        _SettingsShim(),  # type: ignore[arg-type]
    )
    assert result == ("user_abc", "jwt")


@pytest.mark.asyncio
async def test_verify_clerk_jwt_opaque_failure_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_mod, "_verify_opaque_token", AsyncMock(return_value=None))

    result = await verify_clerk_jwt("bad-opaque", _SettingsShim())  # type: ignore[arg-type]
    assert result is None


@pytest.mark.asyncio
async def test_require_wcs_service_accepts_opaque() -> None:
    caller = await require_wcs_service(("mch_wiki-cog", "opaque"))
    assert caller == "mch_wiki-cog"


@pytest.mark.asyncio
async def test_require_wcs_service_rejects_jwt() -> None:
    with pytest.raises(HTTPException) as excinfo:
        await require_wcs_service(("user_123", "jwt"))
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_flags_list_accessible(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """Integration: flags endpoint still accessible after auth cleanup."""
    monkeypatch.setattr(
        auth_mod, "verify_clerk_jwt", AsyncMock(return_value=("dev-owner", "jwt"))
    )
    r = await client.get("/v1/flags", headers={"Authorization": "Bearer test-token"})
    assert r.status_code == 200
