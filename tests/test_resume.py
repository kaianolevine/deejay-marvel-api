from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from kaianolevine_api.config import get_settings


@pytest.fixture(autouse=True)
def clear_resume_token_cache() -> Iterator[None]:
    from kaianolevine_api.routers import resume as resume_mod

    resume_mod._token_cache["token"] = None
    resume_mod._token_cache["expires_at"] = 0.0
    yield
    resume_mod._token_cache["token"] = None
    resume_mod._token_cache["expires_at"] = 0.0


def _meta_response_ok() -> MagicMock:
    r = MagicMock()
    r.status_code = 200
    r.json = MagicMock(
        return_value={
            "id": "fid",
            "name": 'Re"sume\r\n.pdf',
            "mimeType": "application/pdf",
            "size": "10",
            "webViewLink": "https://example.com",
        }
    )
    return r


def _stream_response_ok() -> MagicMock:
    stream = MagicMock()
    stream.status_code = 200

    async def aiter_bytes():
        yield b"%PDF-1.4"

    stream.aiter_bytes = aiter_bytes
    stream.aclose = AsyncMock()
    return stream


def _httpx_client_factory_ok():
    call_state = {"n": 0}

    def factory(*args: object, **kwargs: object) -> MagicMock:
        call_state["n"] += 1
        if call_state["n"] == 1:
            inst = MagicMock()
            inst.__aenter__ = AsyncMock(return_value=inst)
            inst.__aexit__ = AsyncMock(return_value=None)
            inst.get = AsyncMock(return_value=_meta_response_ok())
            return inst
        inst = MagicMock()
        inst.build_request = MagicMock(return_value=MagicMock())
        inst.send = AsyncMock(return_value=_stream_response_ok())
        inst.aclose = AsyncMock()
        return inst

    return factory


def _httpx_client_factory_meta_fail():
    call_state = {"n": 0}

    def factory(*args: object, **kwargs: object) -> MagicMock:
        call_state["n"] += 1
        if call_state["n"] == 1:
            inst = MagicMock()
            inst.__aenter__ = AsyncMock(return_value=inst)
            inst.__aexit__ = AsyncMock(return_value=None)
            meta = MagicMock()
            meta.status_code = 404
            meta.json = MagicMock(return_value={})
            inst.get = AsyncMock(return_value=meta)
            return inst
        raise AssertionError("unexpected second AsyncClient when metadata fails")

    return factory


def _httpx_client_factory_download_fail():
    call_state = {"n": 0}

    def factory(*args: object, **kwargs: object) -> MagicMock:
        call_state["n"] += 1
        if call_state["n"] == 1:
            inst = MagicMock()
            inst.__aenter__ = AsyncMock(return_value=inst)
            inst.__aexit__ = AsyncMock(return_value=None)
            inst.get = AsyncMock(return_value=_meta_response_ok())
            return inst
        inst = MagicMock()
        inst.build_request = MagicMock(return_value=MagicMock())
        stream = MagicMock()
        stream.status_code = 403
        stream.aclose = AsyncMock()
        inst.send = AsyncMock(return_value=stream)
        inst.aclose = AsyncMock()
        return inst

    return factory


@pytest.mark.asyncio
async def test_resume_501_when_resume_file_id_missing(
    monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    monkeypatch.delenv("RESUME_FILE_ID", raising=False)
    get_settings.cache_clear()
    resp = await client.get("/v1/resume")
    assert resp.status_code == 501
    assert resp.json()["error"]["code"] == "not_configured"
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_resume_200_headers_and_streaming_body(
    monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    monkeypatch.setenv("RESUME_FILE_ID", "file-abc")
    monkeypatch.setenv("GOOGLE_CLIENT_EMAIL", "svc@proj.iam.gserviceaccount.com")
    monkeypatch.setenv("GOOGLE_PRIVATE_KEY", "dummy")
    get_settings.cache_clear()

    factory = _httpx_client_factory_ok()
    with (
        patch(
            "kaianolevine_api.routers.resume.get_access_token",
            new_callable=AsyncMock,
            return_value="test-token",
        ),
        patch("kaianolevine_api.routers.resume.httpx.AsyncClient", side_effect=factory),
    ):
        resp = await client.get("/v1/resume")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/pdf")
    assert resp.headers["cache-control"] == "public, max-age=3600"
    assert (
        resp.headers["content-security-policy"]
        == "frame-ancestors https://software.kaianolevine.com"
    )
    assert resp.headers["content-disposition"] == 'inline; filename="Resume.pdf"'
    lowered = {k.lower() for k in resp.headers.keys()}
    assert "x-frame-options" not in lowered
    assert resp.content == b"%PDF-1.4"
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_resume_502_when_drive_metadata_fails(
    monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    monkeypatch.setenv("RESUME_FILE_ID", "file-abc")
    monkeypatch.setenv("GOOGLE_CLIENT_EMAIL", "svc@proj.iam.gserviceaccount.com")
    monkeypatch.setenv("GOOGLE_PRIVATE_KEY", "dummy")
    get_settings.cache_clear()

    factory = _httpx_client_factory_meta_fail()
    with (
        patch(
            "kaianolevine_api.routers.resume.get_access_token",
            new_callable=AsyncMock,
            return_value="test-token",
        ),
        patch("kaianolevine_api.routers.resume.httpx.AsyncClient", side_effect=factory),
    ):
        resp = await client.get("/v1/resume")

    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "upstream_error"
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_resume_502_when_drive_download_fails(
    monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    monkeypatch.setenv("RESUME_FILE_ID", "file-abc")
    monkeypatch.setenv("GOOGLE_CLIENT_EMAIL", "svc@proj.iam.gserviceaccount.com")
    monkeypatch.setenv("GOOGLE_PRIVATE_KEY", "dummy")
    get_settings.cache_clear()

    factory = _httpx_client_factory_download_fail()
    with (
        patch(
            "kaianolevine_api.routers.resume.get_access_token",
            new_callable=AsyncMock,
            return_value="test-token",
        ),
        patch("kaianolevine_api.routers.resume.httpx.AsyncClient", side_effect=factory),
    ):
        resp = await client.get("/v1/resume")

    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "upstream_error"
    get_settings.cache_clear()
