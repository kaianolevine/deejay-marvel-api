from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_current_owner, get_settings
from ..database import get_db_session
from ..models import Set as DbSet
from ..models import Track as DbTrack
from ..models import TrackCatalog
from ..schemas import (
    Envelope,
    LivePlayRecord,
    LivePlaysIngest,
    LivePlaysResponseData,
    success_envelope,
)
from ..services.flags import is_enabled
from ..services.normalization import normalize_for_matching

router = APIRouter()


@router.post(
    "/live-plays",
    response_model=Envelope[LivePlaysResponseData],
    summary="Ingest live play history",
    description="Ingest live play history, merge same-day CSV plays, and flag true duplicates.",
)
async def ingest_live_plays(
    payload: LivePlaysIngest = Body(..., embed=False),
    owner_id: str = Depends(get_current_owner),
    session: AsyncSession = Depends(get_db_session),
) -> Envelope[LivePlaysResponseData]:
    settings = get_settings()
    if not await is_enabled("flags.deejay_api.live_plays_enabled", session):
        raise HTTPException(
            status_code=503,
            detail={"code": "feature_disabled", "message": "Live plays are currently disabled"},
        )

    inserted = 0
    merged = 0
    flagged_for_review = 0

    for play in payload.plays:
        played_at = play.played_at
        play_date = played_at.date()
        norm_title, norm_artist = normalize_for_matching(play.title, play.artist)

        catalog_result = await session.execute(
            select(TrackCatalog).where(
                TrackCatalog.owner_id == owner_id,
                TrackCatalog.title_normalized == norm_title,
                TrackCatalog.artist_normalized == norm_artist,
            )
        )
        catalog = catalog_result.scalar_one_or_none()

        if catalog is None:
            catalog = TrackCatalog(
                owner_id=owner_id,
                title=play.title,
                artist=play.artist,
                title_normalized=norm_title,
                artist_normalized=norm_artist,
                source="live_history",
                confidence="low",
                genre=getattr(play, "genre", None),
                bpm=getattr(play, "bpm", None),
                release_year=getattr(play, "release_year", None),
                play_count=1,
                first_played=play_date,
                last_played=play_date,
            )
            session.add(catalog)
            await session.flush()
        else:
            if catalog.genre is None and getattr(play, "genre", None) is not None:
                catalog.genre = getattr(play, "genre")
            if catalog.bpm is None and getattr(play, "bpm", None) is not None:
                catalog.bpm = getattr(play, "bpm")
            if catalog.release_year is None and getattr(play, "release_year", None) is not None:
                catalog.release_year = getattr(play, "release_year")
            catalog.play_count += 1
            catalog.last_played = play_date

        csv_row_result = await session.execute(
            select(DbTrack)
            .join(DbSet, DbTrack.set_id == DbSet.id)
            .where(
                DbTrack.owner_id == owner_id,
                DbTrack.catalog_id == catalog.id,
                DbTrack.source == "csv",
                DbSet.set_date == play_date,
            )
        )
        csv_row = csv_row_result.scalar_one_or_none()
        if csv_row is not None:
            csv_row.played_at = played_at
            csv_row.source = "merged"
            csv_row.needs_review = False
            merged += 1
            continue

        existing_live_result = await session.execute(
            select(DbTrack).where(
                DbTrack.owner_id == owner_id,
                DbTrack.catalog_id == catalog.id,
                DbTrack.source.in_(["live", "merged"]),
                func.date(DbTrack.played_at) == play_date,
            )
        )
        existing_live = existing_live_result.scalar_one_or_none()
        if existing_live is None:
            session.add(
                DbTrack(
                    owner_id=owner_id,
                    set_id=None,
                    catalog_id=catalog.id,
                    play_order=0,
                    play_time=None,
                    played_at=played_at,
                    source="live",
                    needs_review=False,
                    title=play.title,
                    artist=play.artist,
                    data_quality="minimal",
                )
            )
            inserted += 1
            continue

        delta_seconds = abs((existing_live.played_at - played_at).total_seconds())
        if delta_seconds <= 60:
            continue

        existing_live.needs_review = True
        session.add(
            DbTrack(
                owner_id=owner_id,
                set_id=None,
                catalog_id=catalog.id,
                play_order=0,
                play_time=None,
                played_at=played_at,
                source="live",
                needs_review=True,
                title=play.title,
                artist=play.artist,
                data_quality="minimal",
            )
        )
        flagged_for_review += 2

    await session.commit()
    data = LivePlaysResponseData(
        inserted=inserted,
        merged=merged,
        flagged_for_review=flagged_for_review,
    )
    return success_envelope(data, count=1, version=settings.API_VERSION)


@router.get(
    "/live-plays/recent",
    response_model=Envelope[list[LivePlayRecord]],
    summary="Recent live plays",
    description="List recent plays from live history ordered by played_at descending.",
)
async def list_recent_live_plays(
    limit: int = Query(default=50, ge=1, le=200),
    owner_id: str = Depends(get_current_owner),
    session: AsyncSession = Depends(get_db_session),
) -> Envelope[list[LivePlayRecord]]:
    settings = get_settings()
    if not await is_enabled("flags.deejay_api.live_plays_enabled", session):
        raise HTTPException(
            status_code=503,
            detail={"code": "feature_disabled", "message": "Live plays are currently disabled"},
        )

    stmt = (
        select(DbTrack, TrackCatalog)
        .join(TrackCatalog, DbTrack.catalog_id == TrackCatalog.id, isouter=True)
        .where(
            DbTrack.owner_id == owner_id,
            DbTrack.played_at.is_not(None),
        )
        .order_by(DbTrack.played_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()

    data = [
        LivePlayRecord(
            played_at=track.played_at or dt.datetime.now(dt.UTC),
            title=track.title,
            artist=track.artist,
            set_id=track.set_id,
            genre=track.genre if track.genre is not None else (catalog.genre if catalog else None),
            bpm=track.bpm if track.bpm is not None else (catalog.bpm if catalog else None),
            release_year=(
                track.release_year
                if track.release_year is not None
                else (catalog.release_year if catalog else None)
            ),
            label=track.label if track.label is not None else (catalog.label if catalog else None),
            remix=track.remix if track.remix is not None else (catalog.remix if catalog else None),
            needs_review=track.needs_review,
            source=track.source,
        )
        for track, catalog in rows
    ]
    return success_envelope(data, count=len(data), version=settings.API_VERSION)
