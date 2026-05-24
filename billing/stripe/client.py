"""
GUITAR ATLAS TH-07a Stripe SDK wrapper.

Created: 2026-05-18
Purpose: Customer, subscription, checkout, and billing portal helpers for Premium.
"""
from __future__ import annotations

import os
from typing import Any, Literal

import stripe
from supabase import create_client
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .plans import resolve_price_id

PlanName = Literal["monthly", "yearly"]

_supabase_client = None
_api_key_configured = False


def _ensure_stripe_api_key() -> None:
    """Configure stripe.api_key on first need without breaking import-time health checks."""
    global _api_key_configured
    if _api_key_configured:
        return
    api_key = os.environ.get("STRIPE_SECRET_KEY")
    if not api_key:
        raise RuntimeError("STRIPE_SECRET_KEY not configured")
    stripe.api_key = api_key
    _api_key_configured = True


def get_supabase():
    """Return a lazily initialized Supabase service client.

    Raises:
        RuntimeError: If Supabase credentials are not configured.
    """
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


_stripe_retry = retry(
    retry=retry_if_exception_type(stripe.error.StripeError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)


@_stripe_retry
def create_customer(email: str, wp_user_id: int, name: str | None = None) -> dict:
    """Create or return the Stripe Customer linked to a WP user.

    Args:
        email: Customer email address.
        wp_user_id: WordPress user ID.
        name: Optional customer display name.

    Returns:
        {"customer_id": "cus_...", "email": ..., "created": <epoch>}
    """
    _ensure_stripe_api_key()
    sb = get_supabase()
    existing = _first_row(
        sb.table("premium_customers")
        .select("*")
        .eq("wp_user_id", wp_user_id)
        .limit(1)
        .execute()
    )
    if existing:
        return {
            "customer_id": existing["stripe_customer_id"],
            "email": existing["email"],
            "created": existing.get("created_at"),
        }

    customer = stripe.Customer.create(
        email=email,
        name=name,
        metadata={"wp_user_id": str(wp_user_id)},
    )
    row = {
        "wp_user_id": wp_user_id,
        "stripe_customer_id": customer["id"],
        "email": email,
    }
    sb.table("premium_customers").insert(row).execute()
    return {
        "customer_id": customer["id"],
        "email": email,
        "created": customer.get("created"),
    }


@_stripe_retry
def create_subscription(customer_id: str, plan: PlanName) -> dict:
    """Create a Stripe subscription for a customer and Premium plan.

    Args:
        customer_id: Stripe Customer ID.
        plan: "monthly" or "yearly".

    Returns:
        {"subscription_id": "sub_...", "status": "...", "current_period_end": <epoch>}
    """
    _ensure_stripe_api_key()
    subscription = stripe.Subscription.create(
        customer=customer_id,
        items=[{"price": resolve_price_id(plan)}],
        metadata={"plan": plan},
    )
    return {
        "subscription_id": subscription["id"],
        "status": subscription["status"],
        "current_period_end": subscription.get("current_period_end"),
    }


@_stripe_retry
def create_checkout_session(
    wp_user_id: int,
    plan: PlanName,
    success_url: str,
    cancel_url: str,
) -> dict:
    """Create a Stripe Checkout Session in subscription mode.

    Args:
        wp_user_id: WordPress user ID.
        plan: "monthly" or "yearly".
        success_url: Redirect URL after payment success.
        cancel_url: Redirect URL after cancellation.

    Returns:
        {"session_id": "cs_...", "url": "https://checkout.stripe.com/..."}
    """
    _ensure_stripe_api_key()
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": resolve_price_id(plan), "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=str(wp_user_id),
        metadata={"wp_user_id": str(wp_user_id), "plan": plan},
        subscription_data={"metadata": {"wp_user_id": str(wp_user_id), "plan": plan}},
    )
    return {"session_id": session["id"], "url": session["url"]}


@_stripe_retry
def get_subscription_status(subscription_id: str) -> dict:
    """Return Stripe subscription status details.

    Args:
        subscription_id: Stripe Subscription ID.

    Returns:
        {"status": "...", "current_period_end": <epoch>, "cancel_at_period_end": bool}
    """
    _ensure_stripe_api_key()
    subscription = stripe.Subscription.retrieve(subscription_id)
    return {
        "status": subscription["status"],
        "current_period_end": subscription.get("current_period_end"),
        "cancel_at_period_end": bool(subscription.get("cancel_at_period_end")),
    }


@_stripe_retry
def cancel_subscription(subscription_id: str, at_period_end: bool = True) -> dict:
    """Cancel a Stripe subscription, defaulting to period-end cancellation.

    Args:
        subscription_id: Stripe Subscription ID.
        at_period_end: If true, keep access until the paid period ends.

    Returns:
        {"subscription_id": "...", "status": "...", "canceled_at": <epoch>}
    """
    _ensure_stripe_api_key()
    if at_period_end:
        subscription = stripe.Subscription.modify(
            subscription_id,
            cancel_at_period_end=True,
        )
    else:
        subscription = stripe.Subscription.cancel(subscription_id)
    return {
        "subscription_id": subscription["id"],
        "status": subscription["status"],
        "canceled_at": subscription.get("canceled_at"),
    }


@_stripe_retry
def create_billing_portal_session(customer_id: str, return_url: str) -> dict:
    """Create a Stripe Customer Portal session for an existing customer."""
    _ensure_stripe_api_key()
    session = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=return_url,
    )
    return {"url": session["url"]}
