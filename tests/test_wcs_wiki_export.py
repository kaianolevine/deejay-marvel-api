"""Tests for admin-only full-corpus wiki export."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kaianolevine_api import auth as auth_mod
from kaianolevine_api.main import app
from kaianolevine_api.models import WcsEntity, WcsSource, WcsTranscript
from kaianolevine_api.services import wcs_wiki as wiki_svc
from tests.test_wcs_sources_endpoint import _create_transcript, _source_payload


@pytest.fixture(autouse=True)
async def seed_dev_owner_wcs_admin(reset_db, async_engine) -> None:
    from sqlalchemy import text

    async with async_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO wcs_user_profiles (user_id, email, display_name, is_admin) "
                "VALUES ('dev-owner', '', '', 1) "
                "ON CONFLICT (user_id) DO UPDATE SET is_admin = excluded.is_admin"
            )
        )


@pytest.fixture
async def db_session(async_engine) -> AsyncIterator[AsyncSession]:
    sm = async_sessionmaker(async_engine, expire_on_commit=False, autoflush=False)
    async with sm() as session:
        yield session


async def test_export_wiki_corpus_returns_full_database(
    db_session: AsyncSession,
) -> None:
    t1 = WcsTranscript(
        owner_id="dev-owner",
        raw_text="a",
        source_type="plaud",
        source_filename="a.txt",
        drive_file_id=f"d-{uuid.uuid4().hex[:8]}",
    )
    t2 = WcsTranscript(
        owner_id="dev-owner",
        raw_text="b",
        source_type="plaud",
        source_filename="b.txt",
        drive_file_id=f"d-{uuid.uuid4().hex[:8]}",
    )
    db_session.add_all([t1, t2])
    await db_session.flush()
    private = WcsSource(
        owner_id="dev-owner",
        transcript_id=t1.id,
        is_default_visible=False,
    )
    public = WcsSource(
        owner_id="dev-owner",
        transcript_id=t2.id,
        is_default_visible=True,
    )
    db_session.add_all([private, public])
    await db_session.commit()

    export = await wiki_svc.export_wiki_corpus(db_session)
    source_ids = {s.id for s in export.sources}
    assert private.id in source_ids
    assert public.id in source_ids

    db_source_count = (
        await db_session.execute(select(func.count()).select_from(WcsSource))
    ).scalar_one()
    db_entity_count = (
        await db_session.execute(
            select(func.count())
            .select_from(WcsEntity)
            .where(WcsEntity.merged_into_id.is_(None))
        )
    ).scalar_one()
    assert len(export.sources) == db_source_count
    assert len(export.entities) == db_entity_count


async def test_export_forbidden_for_non_admin(client) -> None:
    original_verify = auth_mod.verify_clerk_jwt
    auth_mod.verify_clerk_jwt = AsyncMock(return_value="stranger-user")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": "Bearer stranger-token"},
    ) as stranger:
        resp = await stranger.get("/v1/wcs/wiki/export")
        assert resp.status_code == 403
    auth_mod.verify_clerk_jwt = original_verify


async def test_export_includes_private_source_for_admin(client) -> None:
    transcript_id = await _create_transcript(client)
    create = await client.post(
        "/v1/wcs/sources",
        json=_source_payload(
            transcript_id,
            is_default_visible=False,
            visibility="private",
            title="Export private lesson",
        ),
    )
    assert create.status_code == 200
    private_id = create.json()["data"]["id"]

    resp = await client.get("/v1/wcs/wiki/export")
    assert resp.status_code == 200
    source_ids = {s["id"] for s in resp.json()["data"]["sources"]}
    assert private_id in source_ids
