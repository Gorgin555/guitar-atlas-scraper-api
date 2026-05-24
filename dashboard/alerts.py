"""
GUITAR ATLAS TH-07d dashboard alert detection.

Created: 2026-05-18
Purpose: Detect movers for Premium dashboard alerting.
"""
from __future__ import annotations

import os
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


def _rows(response: Any) -> list[dict[str, Any]]:
    return list(getattr(response, "data", None) or [])


def detect_movers(window_days: int = 7, threshold_pct: float = 10.0) -> list[dict]:
    """Return products whose move exceeds +/- threshold_pct."""
    sb = get_supabase()
    rows = _rows(
        sb.table("dashboard_alerts")
        .select("*")
        .eq("window_days", window_days)
        .execute()
    )
    movers = [
        {
            "product_id": row.get("product_id"),
            "delta_pct": float(row.get("delta_pct") or 0.0),
            "current_price": None if row.get("current_price") is None else float(row["current_price"]),
            "ma7": None if row.get("ma7_price") is None else float(row["ma7_price"]),
        }
        for row in rows
        if abs(float(row.get("delta_pct") or 0.0)) >= float(threshold_pct)
    ]
    # TODO Phase 2: integrate with n8n daily flow
    return movers
