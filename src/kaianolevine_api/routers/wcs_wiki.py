"""WCS wiki read router — canonical entity substrate views."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from mini_app_polis import logger as logger_mod
from mini_app_polis.logger import LOG_START, LOG_SUCCESS
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_current_owner, require_wcs_admin, require_wcs_service
from ..config import get_settings
from ..database import get_db_session
from ..schemas import (
    Envelope,
    WcsEntityItem,
    WcsEntityViewItem,
    WcsInstructorItem,
    WcsInstructorViewItem,
    WcsSourceItem,
    WcsSourceViewItem,
    WcsWikiExportItem,
    api_error,
    success_envelope,
)
from ..services import wcs_wiki as wiki_svc

router = APIRouter()
log = logger_mod.get_logger()


def _entity_get(kind: str, path: str):
    @router.get(
        path,
        response_model=Envelope[WcsEntityViewItem],
        summary=f"Get one {kind} by slug",
        description=f"Returns the full wiki view for a {kind} entity.",
    )
    async def handler(
        slug: str,
        owner_id: str = Depends(get_current_owner),
        session: AsyncSession = Depends(get_db_session),
    ) -> Envelope[WcsEntityViewItem]:
        """Return the full wiki view for one ``kind`` entity by slug.

        Resolves the slug under the caller's ownership scope and returns
        the canonical entity view (with related instructors, sources,
        and prerequisite/related entity links). Returns HTTP 404 with
        ``entity_not_found`` when the slug is unknown to the caller.

        Closure binding: ``kind`` is captured from the outer
        :func:`_entity_get` factory so the same handler body serves all
        four WCS wiki entity kinds (concept, technique, pattern, drill).
        """
        settings = get_settings()
        view = await wiki_svc.get_entity_view(session, owner_id, slug=slug, kind=kind)
        if view is None:
            raise api_error(404, "entity_not_found", f"{kind.title()} not found")
        return success_envelope(view, count=1, total=1, version=settings.API_VERSION)

    return handler


def _entity_list(kind: str, path: str):
    @router.get(
        path,
        response_model=Envelope[list[WcsEntityItem]],
        summary=f"List {kind} entities",
        description=f"Paginated list of {kind} entities.",
    )
    async def handler(
        status: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        _owner_id: str = Depends(get_current_owner),
        session: AsyncSession = Depends(get_db_session),
    ) -> Envelope[list[WcsEntityItem]]:
        """Return a paginated list of ``kind`` entities with a total count.

        Accepts an optional ``status`` filter (e.g. ``"published"``,
        ``"draft"``) plus standard ``limit`` / ``offset`` pagination. The
        envelope's ``total`` reflects the full filtered count so callers
        can paginate without re-querying. ``_owner_id`` is required for
        auth even though listings are not currently scoped per-owner.

        Closure binding: ``kind`` is captured from the outer
        :func:`_entity_list` factory so the same handler body serves all
        four WCS wiki entity kinds (concept, technique, pattern, drill).
        """
        settings = get_settings()
        items, total = await wiki_svc.list_entities(
            session, kind=kind, status=status, limit=limit, offset=offset
        )
        return success_envelope(
            items, count=len(items), total=total, version=settings.API_VERSION
        )

    return handler


_entity_get("concept", "/wcs/wiki/concepts/{slug}")
_entity_get("technique", "/wcs/wiki/techniques/{slug}")
_entity_get("pattern", "/wcs/wiki/patterns/{slug}")
_entity_get("drill", "/wcs/wiki/drills/{slug}")

_entity_list("concept", "/wcs/wiki/concepts")
_entity_list("technique", "/wcs/wiki/techniques")
_entity_list("pattern", "/wcs/wiki/patterns")
_entity_list("drill", "/wcs/wiki/drills")


@router.get(
    "/wcs/wiki/instructors/{slug}",
    response_model=Envelope[WcsInstructorViewItem],
    summary="Get one instructor by slug",
    description="Returns the full wiki view for an instructor.",
)
async def get_instructor(
    slug: str,
    owner_id: str = Depends(get_current_owner),
    session: AsyncSession = Depends(get_db_session),
) -> Envelope[WcsInstructorViewItem]:
    """Return the full wiki view for one instructor; 404 if the slug is unknown."""
    settings = get_settings()
    view = await wiki_svc.get_instructor_view(session, owner_id, slug=slug)
    if view is None:
        raise api_error(404, "instructor_not_found", "Instructor not found")
    return success_envelope(view, count=1, total=1, version=settings.API_VERSION)


@router.get(
    "/wcs/wiki/instructors",
    response_model=Envelope[list[WcsInstructorItem]],
    summary="List instructors",
    description="Paginated list of instructors.",
)
async def list_instructors(
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    _owner_id: str = Depends(get_current_owner),
    session: AsyncSession = Depends(get_db_session),
) -> Envelope[list[WcsInstructorItem]]:
    """Return a paginated list of instructors with the total instructor count."""
    settings = get_settings()
    items, total = await wiki_svc.list_instructors(session, limit=limit, offset=offset)
    return success_envelope(
        items, count=len(items), total=total, version=settings.API_VERSION
    )


@router.get(
    "/wcs/wiki/sources/{source_id}",
    response_model=Envelope[WcsSourceViewItem],
    summary="Get one source by id",
    description="Returns the full wiki view for a lesson source.",
)
async def get_source(
    source_id: uuid.UUID,
    owner_id: str = Depends(get_current_owner),
    session: AsyncSession = Depends(get_db_session),
) -> Envelope[WcsSourceViewItem]:
    """Return the full wiki view for one source; 404 if the source is not visible to the caller."""
    settings = get_settings()
    view = await wiki_svc.get_source_view(session, owner_id, source_id=source_id)
    if view is None:
        raise api_error(404, "source_not_found", "Source not found")
    return success_envelope(view, count=1, total=1, version=settings.API_VERSION)


@router.get(
    "/wcs/wiki/sources",
    response_model=Envelope[list[WcsSourceItem]],
    summary="List sources",
    description="Paginated list of sources visible to the caller.",
)
async def list_sources(
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    owner_id: str = Depends(get_current_owner),
    session: AsyncSession = Depends(get_db_session),
) -> Envelope[list[WcsSourceItem]]:
    """Return a paginated list of sources visible to the caller with the total source count."""
    settings = get_settings()
    items, total = await wiki_svc.list_sources(
        session, owner_id, limit=limit, offset=offset
    )
    return success_envelope(
        items, count=len(items), total=total, version=settings.API_VERSION
    )


@router.get(
    "/wcs/wiki/admin/sources",
    response_model=Envelope[list[WcsSourceItem]],
    summary="List all sources (admin)",
    description=(
        "Returns all sources regardless of visibility. Admin-only; substrate "
        "equivalent of legacy GET /v1/wcs/notes/all."
    ),
)
async def list_all_sources_admin(
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    _admin_id: str = Depends(require_wcs_admin),
    session: AsyncSession = Depends(get_db_session),
) -> Envelope[list[WcsSourceItem]]:
    """Return every source regardless of visibility (admin catalog)."""
    settings = get_settings()
    items, total = await wiki_svc.list_all_sources(session, limit=limit, offset=offset)
    return success_envelope(
        items, count=len(items), total=total, version=settings.API_VERSION
    )


@router.get(
    "/wcs/wiki/admin/sources/{source_id}",
    response_model=Envelope[WcsSourceViewItem],
    summary="Get one source by id (admin)",
    description=(
        "Returns the full wiki view for any source regardless of visibility. "
        "Admin-only."
    ),
)
async def get_source_admin(
    source_id: uuid.UUID,
    _admin_id: str = Depends(require_wcs_admin),
    session: AsyncSession = Depends(get_db_session),
) -> Envelope[WcsSourceViewItem]:
    """Return the full wiki view for one source, bypassing visibility checks."""
    settings = get_settings()
    view = await wiki_svc.get_source_view(
        session,
        _admin_id,
        source_id=source_id,
        bypass_visibility=True,
    )
    if view is None:
        raise api_error(404, "source_not_found", "Source not found")
    return success_envelope(view, count=1, total=1, version=settings.API_VERSION)


@router.get(
    "/wcs/wiki/export",
    response_model=Envelope[WcsWikiExportItem],
    summary="Bulk export full corpus (cog-only)",
    description=(
        "Returns the entire WCS corpus in one response, regardless of "
        "per-caller visibility. Cog-only: requires a Clerk M2M credential "
        "(the machine secret distributed via Doppler). The endpoint's "
        "protection is the machine-caller gate; per-user filtering is not "
        "applied. Used by wiki-curator-cog to render the wiki as a single "
        "bundled artifact. NOTE: this gate distinguishes machines from "
        "humans, not one machine from another — every cog still shares one "
        "machine secret, so any cog can call this. Once the principal store "
        "is seeded per cog, this should move to a scope check, which is the "
        "first gate here that would actually be per-caller."
    ),
)
async def export_wiki(
    caller_id: str = Depends(require_wcs_service),
    session: AsyncSession = Depends(get_db_session),
) -> Envelope[WcsWikiExportItem]:
    """Return the full WCS corpus (cog-only, not visibility-filtered)."""
    log.info("%s wiki export caller=%s", LOG_START, caller_id)
    settings = get_settings()
    data = await wiki_svc.export_wiki_corpus(session)
    log.info(
        "%s wiki export entities=%d sources=%d instructors=%d",
        LOG_SUCCESS,
        len(data.entities),
        len(data.sources),
        len(data.instructors),
    )
    return success_envelope(data, count=1, total=1, version=settings.API_VERSION)
