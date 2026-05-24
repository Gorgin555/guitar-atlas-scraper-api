"""
GUITAR ATLAS TH-07a Stripe webhook handler.

Created: 2026-05-18
Purpose: Verify Stripe webhook signatures and sync Premium subscription state.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import stripe
from supabase import create_client

from .client import _ensure_stripe_api_key
from wordpress.sync_membership import demote_from_premium, promote_to_premium

logger = logging.getLogger(__name__)

SUPPORTED_EVENTS = [
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.paid",
    "invoice.payment_failed",
]

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


def _ts(epoch: int | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _payload_for_log(event: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(event, default=str))


def _event_exists(sb: Any, event_id: str) -> bool:
    existing = _first_row(
        sb.table("stripe_event_log")
        .select("event_id")
        .eq("event_id", event_id)
        .limit(1)
        .execute()
    )
    return existing is not None


def _log_event(
    sb: Any,
    event: dict[str, Any],
    processed: bool,
    error: str | None = None,
) -> None:
    sb.table("stripe_event_log").insert(
        {
            "event_id": event["id"],
            "event_type": event["type"],
            "payload": _payload_for_log(event),
            "processed": processed,
            "error": error,
        }
    ).execute()


def _plan_from_subscription(subscription: dict[str, Any]) -> str:
    metadata_plan = (subscription.get("metadata") or {}).get("plan")
    if metadata_plan in ("monthly", "yearly"):
        return metadata_plan
    items = ((subscription.get("items") or {}).get("data") or [])
    interval = None
    if items:
        price = items[0].get("price") or {}
        interval = (price.get("recurring") or {}).get("interval")
    return "yearly" if interval == "year" else "monthly"


def _upsert_customer_from_session(sb: Any, session: dict[str, Any]) -> None:
    wp_user_id = session.get("client_reference_id") or (session.get("metadata") or {}).get("wp_user_id")
    customer_id = session.get("customer")
    customer_details = session.get("customer_details") or {}
    email = customer_details.get("email") or session.get("customer_email")
    if not (wp_user_id and customer_id and email):
        return
    row = {
        "wp_user_id": int(wp_user_id),
        "stripe_customer_id": customer_id,
        "email": email,
    }
    sb.table("premium_customers").upsert(row, on_conflict="wp_user_id").execute()


def _subscription_row(subscription: dict[str, Any]) -> dict[str, Any]:
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


def _upsert_subscription(sb: Any, subscription: dict[str, Any]) -> None:
    sb.table("premium_subscriptions").upsert(
        _subscription_row(subscription),
        on_conflict="stripe_subscription_id",
    ).execute()


def _wp_user_id_from_subscription(sb: Any, subscription: dict[str, Any]) -> int | None:
    customer_id = subscription.get("customer")
    if not customer_id:
        return None
    row = _first_row(
        sb.table("premium_customers")
        .select("wp_user_id")
        .eq("stripe_customer_id", customer_id)
        .limit(1)
        .execute()
    )
    return int(row["wp_user_id"]) if row else None


def _mark_wp_sync_failed(sb: Any, subscription_id: str, error_message: str) -> None:
    logger.error("WP membership sync failed for %s: %s", subscription_id, error_message)
    sb.table("premium_subscriptions").update(
        {"wp_sync_failed": True}
    ).eq("stripe_subscription_id", subscription_id).execute()
    # TODO Phase 2: notify Slack #alerts for manual recovery.


def _mark_wp_sync_success(sb: Any, subscription_id: str) -> None:
    sb.table("premium_subscriptions").update(
        {
            "wp_sync_failed": False,
            "wp_sync_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("stripe_subscription_id", subscription_id).execute()


def _handle_checkout_session_completed(sb: Any, session: dict[str, Any]) -> None:
    _upsert_customer_from_session(sb, session)
    subscription_id = session.get("subscription")
    if subscription_id:
        subscription = stripe.Subscription.retrieve(subscription_id)
        _upsert_subscription(sb, subscription)
        wp_user_id = _wp_user_id_from_subscription(sb, subscription)
        if wp_user_id is not None:
            try:
                promote_to_premium(wp_user_id, subscription["customer"])
                _mark_wp_sync_success(sb, subscription["id"])
            except Exception as exc:
                _mark_wp_sync_failed(sb, subscription["id"], str(exc))


def _handle_subscription_event(sb: Any, subscription: dict[str, Any]) -> None:
    _upsert_subscription(sb, subscription)
    if subscription.get("status") == "canceled":
        wp_user_id = _wp_user_id_from_subscription(sb, subscription)
        if wp_user_id is not None:
            try:
                demote_from_premium(wp_user_id, reason="canceled")
                _mark_wp_sync_success(sb, subscription["id"])
            except Exception as exc:
                _mark_wp_sync_failed(sb, subscription["id"], str(exc))


def _handle_invoice_paid(sb: Any, invoice: dict[str, Any]) -> None:
    subscription_id = invoice.get("subscription")
    if not subscription_id:
        return
    row = {
        "status": "active",
        "payment_failure_count": 0,
        "latest_invoice_url": invoice.get("hosted_invoice_url"),
    }
    sb.table("premium_subscriptions").update(row).eq(
        "stripe_subscription_id",
        subscription_id,
    ).execute()


def _handle_invoice_payment_failed(sb: Any, invoice: dict[str, Any]) -> None:
    subscription_id = invoice.get("subscription")
    if not subscription_id:
        return
    current = _first_row(
        sb.table("premium_subscriptions")
        .select("payment_failure_count")
        .eq("stripe_subscription_id", subscription_id)
        .limit(1)
        .execute()
    )
    failure_count = int((current or {}).get("payment_failure_count") or 0) + 1
    sb.table("premium_subscriptions").update(
        {"status": "past_due", "payment_failure_count": failure_count}
    ).eq("stripe_subscription_id", subscription_id).execute()


def _dispatch(sb: Any, event: dict[str, Any]) -> None:
    event_type = event["type"]
    obj = event["data"]["object"]
    if event_type == "checkout.session.completed":
        _handle_checkout_session_completed(sb, obj)
    elif event_type in (
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        _handle_subscription_event(sb, obj)
    elif event_type == "invoice.paid":
        _handle_invoice_paid(sb, obj)
    elif event_type == "invoice.payment_failed":
        _handle_invoice_payment_failed(sb, obj)


def handle_event(event_payload: dict | bytes, signature: str) -> tuple[int, dict]:
    """Verify, dispatch, and log a Stripe webhook event.

    Args:
        event_payload: Raw request bytes from FastAPI, or a dict for tests.
        signature: Stripe-Signature header value.

    Returns:
        (status_code, response_body)
    """
    _ensure_stripe_api_key()
    try:
        event = stripe.Webhook.construct_event(
            event_payload,
            signature,
            os.environ["STRIPE_WEBHOOK_SECRET"],
        )
    except Exception:
        return 400, {"error": "invalid signature"}

    event = dict(event)
    sb = get_supabase()
    if _event_exists(sb, event["id"]):
        return 200, {"deduped": True}

    if event["type"] not in SUPPORTED_EVENTS:
        _log_event(sb, event, processed=True)
        return 200, {"ignored": True}

    try:
        _dispatch(sb, event)
        _log_event(sb, event, processed=True)
    except Exception as exc:
        return 500, {"error": str(exc)}
    return 200, {"received": True}
