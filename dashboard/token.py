"""
GUITAR ATLAS TH-07d dashboard HMAC token helpers.

Created: 2026-05-18
Purpose: Issue and verify short-lived Premium dashboard access tokens.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from typing import Any

_hmac_secret_configured = False
_hmac_secret_bytes: bytes | None = None


class TokenExpired(Exception):
    """Raised when token exp claim is in the past."""


class TokenInvalid(Exception):
    """Raised when HMAC verification fails or payload is malformed."""


def _ensure_hmac_secret() -> bytes:
    """Load DASHBOARD_HMAC_SECRET on first need without import-time failures."""
    global _hmac_secret_configured, _hmac_secret_bytes
    if _hmac_secret_configured:
        assert _hmac_secret_bytes is not None
        return _hmac_secret_bytes
    secret = os.environ.get("DASHBOARD_HMAC_SECRET")
    if not secret:
        raise RuntimeError("DASHBOARD_HMAC_SECRET not configured")
    _hmac_secret_bytes = secret.encode("utf-8")
    _hmac_secret_configured = True
    return _hmac_secret_bytes


def _b64url_encode(data: bytes) -> str:
    return urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return urlsafe_b64decode((text + padding).encode("ascii"))


def _sign(payload_b64: str, secret: bytes) -> str:
    digest = hmac.new(secret, payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64url_encode(digest)


def issue_token(wp_user_id: int, roles: list[str], ttl_seconds: int = 1800) -> str:
    """Issue a short-lived HMAC-SHA256 token.

    Returns:
        "<payload_b64>.<sig_b64>" where payload is {"u": int, "r": list[str], "exp": int}.
    """
    secret = _ensure_hmac_secret()
    payload: dict[str, Any] = {
        "u": int(wp_user_id),
        "r": [str(role) for role in roles],
        "exp": int(time.time()) + int(ttl_seconds),
    }
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = _b64url_encode(payload_json)
    return f"{payload_b64}.{_sign(payload_b64, secret)}"


def verify_token(token: str) -> dict:
    """Verify a token and return its payload."""
    secret = _ensure_hmac_secret()
    try:
        payload_b64, sig_b64 = token.split(".", 1)
    except ValueError as exc:
        raise TokenInvalid("token must contain payload and signature") from exc

    if not payload_b64 or not sig_b64:
        raise TokenInvalid("token payload or signature is empty")

    expected_sig = _sign(payload_b64, secret)
    if not hmac.compare_digest(expected_sig, sig_b64):
        raise TokenInvalid("token signature mismatch")

    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception as exc:
        raise TokenInvalid("token payload is malformed") from exc

    if not isinstance(payload, dict):
        raise TokenInvalid("token payload must be an object")
    if not isinstance(payload.get("u"), int) or not isinstance(payload.get("r"), list):
        raise TokenInvalid("token payload missing required claims")
    exp = payload.get("exp")
    if not isinstance(exp, int):
        raise TokenInvalid("token exp claim is missing")
    if exp < int(time.time()):
        raise TokenExpired("token expired")
    return payload
