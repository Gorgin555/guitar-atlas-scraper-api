"""
GUITAR ATLAS TH-07b WordPress membership sync.

Created: 2026-05-18
Purpose: Sync Stripe subscription state into WordPress premium_member roles.
"""
from __future__ import annotations

import base64
import os
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from supabase import create_client
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

DemoteReason = Literal["canceled", "past_due", "admin"]

_supabase_client = None


class WordPressAuthError(Exception):
    """Raised when WordPress rejects membership sync authentication."""


class MembershipSyncError(Exception):
    """Raised when membership sync cannot be completed."""


class _RetryableMembershipSyncError(MembershipSyncError):
    """Raised for transient WordPress 5xx responses."""


def get_supabase():
    """Return a lazily initialized Supabase service client."""
    global _supabase_client
    if _supabase_client is None:
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY")
        if not (url and key):
            raise MembershipSyncError("Supabase credentials not configured")
        _supabase_client = create_client(url, key)
    return _supabase_client


def _wp_base_url() -> str:
    wp_url = os.environ.get("WP_URL")
    if not wp_url:
        raise MembershipSyncError("WP_URL not configured")
    return wp_url.rstrip("/")


def _auth_headers() -> dict[str, str]:
    username = os.environ.get("WP_APP_USERNAME") or os.environ.get("WP_USER")
    password = os.environ.get("WP_APP_PASSWORD")
    internal_token = os.environ.get("GA_MEMBERSHIP_INTERNAL_TOKEN")
    if not (username and password and internal_token):
        raise WordPressAuthError("WordPress membership sync credentials not configured")
    basic = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/json",
        "X-GA-Internal-Token": internal_token,
    }


def _membership_sync_url() -> str:
    return f"{_wp_base_url()}/wp-json/guitar-atlas/v1/membership/sync"


@retry(
    retry=retry_if_exception_type(_RetryableMembershipSyncError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
def _post_membership_sync(payload: dict[str, Any]) -> dict:
    with httpx.Client(timeout=10) as client:
        response = client.post(
            _membership_sync_url(),
            headers=_auth_headers(),
            json=payload,
        )
    if response.status_code in (401, 403):
        raise WordPressAuthError("WordPress membership sync authentication failed")
    if response.status_code >= 500:
        raise _RetryableMembershipSyncError(
            f"WordPress membership sync transient error: {response.status_code}"
        )
    if response.status_code >= 400:
        raise MembershipSyncError(
            f"WordPress membership sync failed: {response.status_code} {response.text}"
        )
    return response.json()


def promote_to_premium(wp_user_id: int, stripe_customer_id: str) -> dict:
    """Promote a WordPress user to premium_member via WP REST.

    Returns:
        {"success": bool, "new_roles": [...], "wp_user_id": int}

    Raises:
        WordPressAuthError: On missing or invalid WP credentials.
        MembershipSyncError: On non-auth sync failure.
    """
    return _post_membership_sync(
        {
            "wp_user_id": wp_user_id,
            "action": "promote",
            "stripe_customer_id": stripe_customer_id,
        }
    )


def demote_from_premium(wp_user_id: int, reason: DemoteReason) -> dict:
    """Remove premium_member from a WordPress user via WP REST."""
    return _post_membership_sync(
        {
            "wp_user_id": wp_user_id,
            "action": "demote",
            "stripe_customer_id": "",
            "reason": reason,
        }
    )


def _first_row(response: Any) -> dict[str, Any] | None:
    rows = getattr(response, "data", None) or []
    return rows[0] if rows else None


def _wp_user_id_for_customer(sb: Any, stripe_customer_id: str) -> int | None:
    row = _first_row(
        sb.table("premium_customers")
        .select("wp_user_id")
        .eq("stripe_customer_id", stripe_customer_id)
        .limit(1)
        .execute()
    )
    return int(row["wp_user_id"]) if row else None


def reconcile_wp_with_stripe() -> dict:
    """Retry failed active Premium role syncs from Supabase state.

    Returns:
        {"checked": int, "fixed": int, "still_failed": int}
    """
    sb = get_supabase()
    rows = (
        sb.table("premium_subscriptions")
        .select("*")
        .eq("status", "active")
        .eq("wp_sync_failed", True)
        .execute()
        .data
        or []
    )

    checked = 0
    fixed = 0
    still_failed = 0
    for row in rows:
        checked += 1
        subscription_id = row.get("stripe_subscription_id")
        customer_id = row.get("stripe_customer_id")
        wp_user_id = _wp_user_id_for_customer(sb, customer_id) if customer_id else None
        if wp_user_id is None:
            still_failed += 1
            continue
        try:
            promote_to_premium(wp_user_id, customer_id)
            sb.table("premium_subscriptions").update(
                {
                    "wp_sync_failed": False,
                    "wp_sync_at": datetime.now(timezone.utc).isoformat(),
                }
            ).eq("stripe_subscription_id", subscription_id).execute()
            fixed += 1
        except (WordPressAuthError, MembershipSyncError):
            still_failed += 1

    return {"checked": checked, "fixed": fixed, "still_failed": still_failed}
