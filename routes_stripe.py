"""
GUITAR ATLAS TH-07a Stripe FastAPI routes.

Created: 2026-05-18
Purpose: Checkout, webhook, and billing portal endpoints for Premium.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

try:  # uvicorn main:app
    from main import get_supabase, verify_secret
except ImportError:  # package import in tests
    from .main import get_supabase, verify_secret

router = APIRouter()


def _first_row(response: Any) -> dict[str, Any] | None:
    rows = getattr(response, "data", None) or []
    return rows[0] if rows else None


@router.post("/stripe/checkout")
async def create_checkout(
    wp_user_id: int,
    plan: Literal["monthly", "yearly"],
    _auth=Depends(verify_secret),
) -> dict:
    """Create a Stripe Checkout Session for a Premium plan."""
    try:
        from billing.stripe.client import create_checkout_session

        # Phase 1 default URLs can be overridden by env for later LP variants.
        result = await asyncio.to_thread(
            create_checkout_session,
            wp_user_id,
            plan,
            os.environ.get(
                "STRIPE_CHECKOUT_SUCCESS_URL",
                "https://theguitaratlas.com/account?checkout=success",
            ),
            os.environ.get(
                "STRIPE_CHECKOUT_CANCEL_URL",
                "https://theguitaratlas.com/premium?checkout=cancel",
            ),
        )
        return {"success": True, **result}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/stripe/webhook")
async def webhook(request: Request) -> JSONResponse:
    """Receive Stripe webhooks. Authentication is Stripe signature only."""
    from billing.stripe.webhook_handler import handle_event

    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")
    status_code, body = await asyncio.to_thread(handle_event, payload, signature)
    return JSONResponse(status_code=status_code, content=body)


@router.get("/stripe/portal")
async def billing_portal(wp_user_id: int, _auth=Depends(verify_secret)) -> dict:
    """Create a Stripe Customer Portal URL for a WP user."""
    try:
        sb = get_supabase()
        customer = _first_row(
            sb.table("premium_customers")
            .select("stripe_customer_id")
            .eq("wp_user_id", wp_user_id)
            .limit(1)
            .execute()
        )
        if not customer:
            raise HTTPException(status_code=404, detail="Premium customer not found")

        from billing.stripe.client import create_billing_portal_session

        result = await asyncio.to_thread(
            create_billing_portal_session,
            customer["stripe_customer_id"],
            os.environ["STRIPE_PORTAL_RETURN_URL"],
        )
        return {"success": True, **result}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
