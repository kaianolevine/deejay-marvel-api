"""WCS source visibility rules (default-visible catalog and per-user grants)."""

from __future__ import annotations

import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import WcsSource, WcsSourceGrant


async def user_can_see_source(
    session: AsyncSession,
    user_id: str,
    source: WcsSource,
) -> bool:
    """Return True if the user may see this source."""
    if source.is_default_visible:
        return True

    grant = await session.execute(
        select(WcsSourceGrant).where(
            WcsSourceGrant.user_id == user_id,
            WcsSourceGrant.source_id == source.id,
        )
    )
    return grant.scalars().first() is not None


async def visible_source_ids_for_user(
    session: AsyncSession,
    user_id: str,
) -> list[uuid.UUID]:
    """Return source IDs visible to the user (for filtering canonical rows)."""
    grant_subq = select(WcsSourceGrant.source_id).where(
        WcsSourceGrant.user_id == user_id
    )
    stmt = select(WcsSource.id).where(
        or_(
            WcsSource.is_default_visible.is_(True),
            WcsSource.id.in_(grant_subq),
        )
    )
    rows = await session.execute(stmt)
    return list(rows.scalars().all())
