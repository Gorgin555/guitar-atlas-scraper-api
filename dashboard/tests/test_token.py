from __future__ import annotations

import pytest

from dashboard import token


def test_issue_verify_round_trip() -> None:
    issued = token.issue_token(42, ["subscriber", "premium_member"])
    payload = token.verify_token(issued)
    assert payload["u"] == 42
    assert payload["r"] == ["subscriber", "premium_member"]
    assert isinstance(payload["exp"], int)


def test_expired_token_raises() -> None:
    issued = token.issue_token(42, ["premium_member"], ttl_seconds=-1)
    with pytest.raises(token.TokenExpired):
        token.verify_token(issued)


def test_tampered_signature_raises() -> None:
    issued = token.issue_token(42, ["premium_member"])
    payload_b64, _sig_b64 = issued.split(".", 1)
    with pytest.raises(token.TokenInvalid):
        token.verify_token(f"{payload_b64}.tampered")


def test_invalid_format_raises() -> None:
    with pytest.raises(token.TokenInvalid):
        token.verify_token("not-a-dashboard-token")
