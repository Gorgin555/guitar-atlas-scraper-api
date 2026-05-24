"""
GUITAR ATLAS TH-07a Stripe-Supabase reconciler.

Created: 2026-05-18
Purpose: Detect and repair missed webhook updates for Premium subscriptions.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import stripe
from supabase import create_client

from .client import _ensure_stripe_api_key
from .webhook_handler import _plan_from_subscription

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


def _ts(epoch: int | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _row_from_subscription(subscription: dict[str, Any]) -> dict[str, Any]:
    return {
        "stripe_subscription_id": subscription["id"],
        "stripe_customer_id": subscription["customer"],
        "plan": _plan_from_subscription(subscription),
        "status": subscription["status"],
        "current_period_start": _ts(subscription.get("current_period_start")),
        "current_period_end": _ts(subscription.get("current_period_end")),
        "cancel_at_period_end": bool(subscription.get("cancel_at_period_end")),
        "canceled_at": _ts(subscription.get("canceled_at")),
    }


def _needs_update(local: dict[str, Any] | None, desired: dict[str, Any]) -> bool:
    if local is None:
        return True
    for key, value in desired.items():
        if str(local.get(key)) != str(value):
            return True
    return False


def reconcile_daily() -> dict:
    """Reconcile Stripe subscriptions into Supabase.

    Returns:
        {"checked": int, "fixed": int, "discrepancies": [...]}
    """
    _ensure_stripe_api_key()
    sb = get_supabase()
    # TODO Phase 2: pagination for >100 subs
    stripe_subscriptions = stripe.Subscription.list(status="all", limit=100)
    remote_items = stripe_subscriptions.get("data", [])
    local_rows = (
        sb.table("premium_subscriptions")
        .select("*")
        .execute()
        .data
        or []
    )
    local_by_id = {row["stripe_subscription_id"]: row for row in local_rows}

    checked = 0
    fixed = 0
    discrepancies = []
    for subscription in remote_items:
        checked += 1
        desired = _row_from_subscription(subscription)
        local = local_by_id.get(subscription["id"])
        if _needs_update(local, desired):
            sb.table("premium_subscriptions").upsert(
                desired,
                on_conflict="stripe_subscription_id",
            ).execute()
            fixed += 1
            discrepancies.append(
                {
                    "stripe_subscription_id": subscription["id"],
                    "local_status": (local or {}).get("status"),
                    "stripe_status": subscription["status"],
                }
            )

    return {"checked": checked, "fixed": fixed, "discrepancies": discrepancies}
