"""
GUITAR ATLAS TH-07d Premium dashboard routes.

Created: 2026-05-18
Purpose: Serve dashboard payloads using short-lived HMAC tokens.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from dashboard.data_provider import get_dashboard_payload, get_user_alerts, mark_alert_read
from dashboard.token import TokenExpired, TokenInvalid, verify_token

router = APIRouter()


def _payload_or_error(token: str) -> tuple[dict | None, JSONResponse | None]:
    try:
        return verify_token(token), None
    except TokenExpired:
        return None, JSONResponse(status_code=401, content={"error": "token_expired"})
    except (RuntimeError, TokenInvalid):
        return None, JSONResponse(status_code=401, content={"error": "token_invalid"})


@router.get("/dashboard/data")
async def dashboard_data(t: str = Query(...)) -> Any:
    """Return the Premium dashboard first-view payload."""
    payload, error = _payload_or_error(t)
    if error is not None:
        return error
    assert payload is not None
    return get_dashboard_payload(int(payload["u"]))


@router.get("/dashboard/alerts")
async def dashboard_alerts(t: str = Query(...), since: Optional[str] = None) -> Any:
    """Return dashboard alerts for the token owner."""
    payload, error = _payload_or_error(t)
    if error is not None:
        return error
    assert payload is not None
    return get_user_alerts(int(payload["u"]), since=since)


@router.post("/dashboard/alerts/read")
async def dashboard_alert_read(t: str = Query(...), alert_id: int = Query(...)) -> Any:
    """Mark a dashboard alert as read for the token owner."""
    payload, error = _payload_or_error(t)
    if error is not None:
        return error
    assert payload is not None
    return mark_alert_read(int(payload["u"]), alert_id)
