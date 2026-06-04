"""
GUITAR ATLAS TH-cultural-store FastAPI routes.

Created: 2026-05-31
Purpose: Public read endpoints for published cultural themes and trends.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

from cultural.store import (
    get_index_band,
    get_latest_themes,
    get_published_theme_detail,
    get_trends,
)

router = APIRouter()


@router.get("/cultural/themes")
async def cultural_themes(
    limit: int = Query(default=4, ge=1, le=20),
    include_beta: bool = True,
) -> list[dict[str, Any]]:
    """Return latest published market cultural themes."""
    return get_latest_themes(limit=limit, include_beta=include_beta)


@router.get("/cultural/trends")
async def cultural_trends(
    axis: str = Query(default="regional", pattern="^(regional|sound|spec)$"),
    week: Optional[str] = None,
    include_beta: bool = True,
) -> list[dict[str, Any]]:
    """Return trend snapshots for one cultural trend axis."""
    return get_trends(axis=axis, week_start=week, include_beta=include_beta)


@router.get("/cultural/index-band")
async def cultural_index_band() -> dict[str, Any]:
    """Return the public sticky index headline band."""
    return get_index_band()


@router.get("/cultural/theme/{theme_id}")
async def cultural_theme_detail(theme_id: str) -> dict[str, Any]:
    """Return one published market theme with backing data."""
    payload = get_published_theme_detail(theme_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="theme_not_found")
    return payload
