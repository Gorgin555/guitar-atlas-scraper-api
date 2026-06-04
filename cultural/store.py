"""
GUITAR ATLAS TH-cultural-store read layer.

Created: 2026-05-31
Purpose: Read published cultural themes and weekly trend snapshots.
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from supabase import create_client

_supabase_client = None
INDEX_BASE_DATE = "2026-06-25"


def get_supabase():
    """Return a lazily initialized Supabase service client."""
    global _supabase_client
    if _supabase_client is None:
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY")
        if not (url and key):
            raise RuntimeError("Supabase credentials not configured")
        _supabase_client = create_client(url, key)
    return _supabase_client


def _first_row(response: Any) -> dict[str, Any] | None:
    rows = getattr(response, "data", None) or []
    return rows[0] if rows else None


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ratio(numerator: Any, denominator: Any) -> float | None:
    top = _to_float(numerator)
    bottom = _to_float(denominator)
    if top is None or bottom in (None, 0):
        return None
    return top / bottom


def _date_str(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _calibration(as_of: str | None, base_date: str = INDEX_BASE_DATE) -> str:
    if not as_of:
        return "pre_calibration"
    try:
        return "pre_calibration" if date.fromisoformat(as_of) < date.fromisoformat(base_date) else "live"
    except ValueError:
        return "pre_calibration"


def _prior_index_row(client: Any, as_of: str | None) -> dict[str, Any] | None:
    """Return the latest index_daily row at least seven days before as_of."""
    if not as_of:
        return None
    try:
        cutoff = (date.fromisoformat(as_of) - timedelta(days=7)).isoformat()
    except ValueError:
        return None
    return _first_row(
        client.table("index_daily")
        .select("*")
        .lte("snapshot_date", cutoff)
        .order("snapshot_date", desc=True)
        .limit(1)
        .execute()
    )


def _wow_delta_pct(current: Any, prior: Any) -> float | None:
    current_value = _to_float(current)
    prior_value = _to_float(prior)
    if current_value is None or prior_value in (None, 0):
        return None
    return ((current_value - prior_value) / prior_value) * 100


def _index_payload(row: dict[str, Any] | None, prior_row: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if row is None:
        return None

    def _index_item(value_key: str) -> dict[str, float | None]:
        return {
            "value": _to_float(row.get(value_key)),
            "wow_delta_pct": _wow_delta_pct(row.get(value_key), (prior_row or {}).get(value_key)),
        }

    return {
        "GAI-E": _index_item("gai_e"),
        "MFI": _index_item("mfi"),
        "VFI": _index_item("vfi_ac"),
        "BPI": _index_item("bpi"),
    }


def _public_product(row: dict[str, Any]) -> dict[str, Any]:
    year = row.get("year_range_str") or row.get("year")
    if year is None and row.get("year_min") is not None and row.get("year_max") is not None:
        year = row["year_min"] if row["year_min"] == row["year_max"] else f"{row['year_min']}-{row['year_max']}"
    return {
        "product_id": row.get("product_id"),
        "brand": row.get("brand") or row.get("brand_name"),
        "model": row.get("model"),
        "variant": row.get("variant"),
        "year": year,
    }


def get_latest_themes(*, limit: int = 4, include_beta: bool = True) -> list[dict]:
    """Return latest published market cultural themes from v_published_themes."""
    query = (
        get_supabase()
        .table("v_published_themes")
        .select("*")
        .order("week_start", desc=True)
        .order("published_at", desc=True)
    )
    if not include_beta:
        query = query.eq("is_beta", False)
    return query.limit(limit).execute().data or []


def get_trends(
    *,
    axis: str,
    week_start: str | None = None,
    include_beta: bool = True,
) -> list[dict]:
    """Return trend_snapshots for an axis, optionally narrowed to one week."""
    query = (
        get_supabase()
        .table("trend_snapshots")
        .select("*")
        .eq("axis", axis)
        .order("week_start", desc=True)
    )
    if week_start is not None:
        query = query.eq("week_start", week_start)
    if not include_beta:
        query = query.eq("is_beta", False)
    return query.execute().data or []


def get_index_band() -> dict:
    """Return the latest public index headline band from index_daily."""
    sb = get_supabase()
    row = _first_row(
        sb
        .table("index_daily")
        .select("*")
        .order("snapshot_date", desc=True)
        .limit(1)
        .execute()
    )
    as_of = _date_str((row or {}).get("snapshot_date"))
    if row is None:
        return {
            "as_of": None,
            "calibration": "pre_calibration",
            "base_date": INDEX_BASE_DATE,
            "indices": {},
            "spreads": {},
        }
    prior_row = _prior_index_row(sb, as_of)

    spreads = {
        "boutique_premium": _ratio(row.get("bpi"), row.get("mfi")),
        "vintage_premium": _ratio(row.get("vfi_ac"), row.get("mfi")),
        "heritage_spread": _ratio(row.get("vfi_ac"), row.get("bpi")),
    }
    for key in list(spreads):
        if spreads[key] is None:
            spreads[key] = _to_float(row.get(key))

    return {
        "as_of": as_of,
        "calibration": _calibration(as_of),
        "base_date": INDEX_BASE_DATE,
        "indices": _index_payload(row, prior_row),
        "spreads": spreads,
    }


def get_theme_with_backing(theme_id: str) -> dict:
    """Return one published theme with its backing trends and connected index row."""
    sb = get_supabase()
    theme = _first_row(
        sb.table("v_published_themes")
        .select("*")
        .eq("theme_id", theme_id)
        .limit(1)
        .execute()
    )
    if theme is None:
        return {"theme": None, "trends": [], "index_daily": None}

    trend_ids = theme.get("supporting_trend_ids") or []
    trends = []
    if trend_ids:
        trends = (
            sb.table("trend_snapshots")
            .select("*")
            .in_("snapshot_id", trend_ids)
            .execute()
            .data
            or []
        )

    index_daily = None
    connected_index_date = theme.get("connected_index_date")
    if connected_index_date:
        index_daily = _first_row(
            sb.table("index_daily")
            .select("*")
            .eq("snapshot_date", connected_index_date)
            .limit(1)
            .execute()
        )

    return {"theme": theme, "trends": trends, "index_daily": index_daily}


def get_published_theme_detail(theme_id: str) -> dict | None:
    """Return a published market theme with backing data and public hero products."""
    sb = get_supabase()
    payload = get_theme_with_backing(theme_id)
    theme = payload.get("theme")
    if theme is None:
        return None

    hero_ids = theme.get("hero_product_ids") or []
    hero_products = []
    if hero_ids:
        rows = (
            sb
            .table("products")
            .select("product_id,brand_name,model,variant,year_min,year_max,year_range_str")
            .in_("product_id", hero_ids)
            .execute()
            .data
            or []
        )
        by_id = {str(row.get("product_id")): _public_product(row) for row in rows}
        hero_products = [by_id[str(product_id)] for product_id in hero_ids if str(product_id) in by_id]

    index_as_of = _date_str((payload.get("index_daily") or {}).get("snapshot_date"))
    prior_index = _prior_index_row(sb, index_as_of)

    return {
        **payload,
        "hero_products": hero_products,
        "index_band": {
            "as_of": index_as_of,
            "base_date": INDEX_BASE_DATE,
            "indices": _index_payload(payload.get("index_daily"), prior_index),
        },
    }
