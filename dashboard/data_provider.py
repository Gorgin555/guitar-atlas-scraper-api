"""
GUITAR ATLAS TH-07d dashboard data provider.

Created: 2026-05-18
Purpose: Aggregate Premium dashboard data from Supabase.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from supabase import create_client

_supabase_client = None


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


def _rows(response: Any) -> list[dict[str, Any]]:
    return list(getattr(response, "data", None) or [])


def _float(row: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = row.get(key)
    return default if value is None else float(value)


def _latest_index_row(sb: Any) -> dict[str, Any]:
    return _first_row(
        sb.table("index_daily")
        .select("*")
        .order("snapshot_date", desc=True)
        .limit(1)
        .execute()
    ) or {}


def _product_models(sb: Any, product_ids: set[str]) -> dict[str, str]:
    if not product_ids:
        return {}
    rows = _rows(sb.table("products").select("product_id,model,name").execute())
    models: dict[str, str] = {}
    for row in rows:
        product_id = str(row.get("product_id") or "")
        if product_id in product_ids:
            models[product_id] = str(row.get("model") or row.get("name") or product_id)
    return models


def _alert_rows(sb: Any) -> list[dict[str, Any]]:
    return _rows(
        sb.table("dashboard_alerts")
        .select("*")
        .order("triggered_at", desc=True)
        .execute()
    )


def _read_alert_ids(sb: Any, wp_user_id: int) -> set[int]:
    rows = _rows(
        sb.table("alert_read_log")
        .select("alert_id")
        .eq("wp_user_id", wp_user_id)
        .execute()
    )
    return {int(row["alert_id"]) for row in rows if row.get("alert_id") is not None}


def _mover_payload(alert: dict[str, Any], models: dict[str, str]) -> dict[str, Any]:
    product_id = str(alert.get("product_id"))
    return {
        "product_id": product_id,
        "model": str(alert.get("model") or models.get(product_id) or product_id),
        "delta_pct": _float(alert, "delta_pct"),
        "price_usd": _float(alert, "current_price"),
    }


def _parse_since(since: datetime | str | None) -> datetime | None:
    if since is None:
        return None
    if isinstance(since, datetime):
        return since
    value = since.replace("Z", "+00:00")
    return datetime.fromisoformat(value)


def _alert_time(row: dict[str, Any]) -> datetime:
    value = str(row.get("triggered_at") or datetime.now(timezone.utc).isoformat())
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def get_dashboard_payload(wp_user_id: int) -> dict:
    """Return the first-view Premium dashboard payload."""
    sb = get_supabase()
    customer = _first_row(
        sb.table("premium_customers")
        .select("*")
        .eq("wp_user_id", wp_user_id)
        .limit(1)
        .execute()
    )
    subscription = None
    if customer:
        subscription = _first_row(
            sb.table("premium_subscriptions")
            .select("*")
            .eq("stripe_customer_id", customer["stripe_customer_id"])
            .limit(1)
            .execute()
        )

    index_row = _latest_index_row(sb)
    as_of = index_row.get("snapshot_date") or index_row.get("created_at")
    alert_rows = _alert_rows(sb)
    product_ids = {str(row.get("product_id")) for row in alert_rows if row.get("product_id")}
    models = _product_models(sb, product_ids)
    read_ids = _read_alert_ids(sb, wp_user_id)

    gainers = sorted(
        (row for row in alert_rows if _float(row, "delta_pct") >= 0),
        key=lambda row: _float(row, "delta_pct"),
        reverse=True,
    )[:10]
    losers = sorted(
        (row for row in alert_rows if _float(row, "delta_pct") < 0),
        key=lambda row: _float(row, "delta_pct"),
    )[:10]

    return {
        "user": {
            "wp_user_id": int(wp_user_id),
            "plan": (subscription or {}).get("plan"),
            "current_period_end": (subscription or {}).get("current_period_end"),
            "status": (subscription or {}).get("status", "inactive"),
        },
        "indices": {
            "GAI_E": {"value": _float(index_row, "gai_e"), "delta_7d": _float(index_row, "gai_e_delta_7d"), "as_of": as_of},
            "MFI": {"value": _float(index_row, "mfi"), "delta_7d": _float(index_row, "mfi_delta_7d"), "as_of": as_of},
            "VFI_AO": {"value": _float(index_row, "vfi_ao"), "delta_7d": _float(index_row, "vfi_ao_delta_7d"), "as_of": as_of},
            "VFI_AC": {"value": _float(index_row, "vfi_ac"), "delta_7d": _float(index_row, "vfi_ac_delta_7d"), "as_of": as_of},
            "BPI": {"value": _float(index_row, "bpi"), "delta_7d": _float(index_row, "bpi_delta_7d"), "as_of": as_of},
        },
        "spreads": {
            "boutique_premium": {"value": _float(index_row, "boutique_premium"), "ma7": _float(index_row, "boutique_premium_ma7")},
            "vintage_premium": {"value": _float(index_row, "vintage_premium"), "ma7": _float(index_row, "vintage_premium_ma7")},
            "heritage_spread": {"value": _float(index_row, "heritage_spread"), "ma7": _float(index_row, "heritage_spread_ma7")},
        },
        "movers": {
            "top_gainers": [_mover_payload(row, models) for row in gainers],
            "top_losers": [_mover_payload(row, models) for row in losers],
        },
        "deep_report": {"latest_url": None, "latest_published": None},
        "alerts_unread": len([row for row in alert_rows if int(row.get("alert_id", 0)) not in read_ids]),
    }


def get_user_alerts(wp_user_id: int, since: datetime | str | None = None) -> list[dict]:
    """Return dashboard alerts for a WP user, annotated with read state."""
    sb = get_supabase()
    cutoff = _parse_since(since)
    alert_rows = _alert_rows(sb)
    if cutoff is not None:
        alert_rows = [row for row in alert_rows if _alert_time(row) >= cutoff]
    product_ids = {str(row.get("product_id")) for row in alert_rows if row.get("product_id")}
    models = _product_models(sb, product_ids)
    read_ids = _read_alert_ids(sb, wp_user_id)
    result: list[dict[str, Any]] = []
    for row in alert_rows:
        alert_id = int(row["alert_id"])
        product_id = str(row.get("product_id"))
        result.append(
            {
                "alert_id": alert_id,
                "model": str(row.get("model") or models.get(product_id) or product_id),
                "delta_pct": _float(row, "delta_pct"),
                "triggered_at": row.get("triggered_at"),
                "read": alert_id in read_ids,
            }
        )
    return result


def mark_alert_read(wp_user_id: int, alert_id: int) -> dict:
    """Mark one dashboard alert as read for a WP user."""
    sb = get_supabase()
    sb.table("alert_read_log").upsert(
        {"wp_user_id": int(wp_user_id), "alert_id": int(alert_id)},
        on_conflict="wp_user_id,alert_id",
    ).execute()
    return {"success": True, "wp_user_id": int(wp_user_id), "alert_id": int(alert_id)}
