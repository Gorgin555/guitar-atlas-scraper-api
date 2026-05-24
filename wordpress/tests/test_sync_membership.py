from __future__ import annotations

import pytest

import wordpress.sync_membership as sync_membership
from wordpress.tests.conftest import FakeWPResponse


def test_scenario_1_promote_to_premium(fake_wp):
    fake_wp.responses = [
        FakeWPResponse(
            200,
            {
                "success": True,
                "new_roles": ["subscriber", "premium_member"],
                "wp_user_id": 42,
            },
        )
    ]

    result = sync_membership.promote_to_premium(42, "cus_dummy")

    assert result["success"] is True
    assert "premium_member" in result["new_roles"]
    assert fake_wp.calls[0]["json"] == {
        "wp_user_id": 42,
        "action": "promote",
        "stripe_customer_id": "cus_dummy",
    }
    assert "X-GA-Internal-Token" in fake_wp.calls[0]["headers"]


def test_scenario_2_demote_from_premium_canceled(fake_wp):
    fake_wp.responses = [
        FakeWPResponse(
            200,
            {
                "success": True,
                "new_roles": ["subscriber"],
                "wp_user_id": 42,
            },
        )
    ]

    result = sync_membership.demote_from_premium(42, "canceled")

    assert result["success"] is True
    assert result["new_roles"] == ["subscriber"]
    assert fake_wp.calls[0]["json"] == {
        "wp_user_id": 42,
        "action": "demote",
        "stripe_customer_id": "",
        "reason": "canceled",
    }


@pytest.mark.skip(reason="# TEST_SKIP: PHP-only logic, manual test in WP admin")
def test_scenario_3_content_gate_php_only():
    assert False


def test_scenario_4_wp_rest_auth_error_does_not_retry(fake_wp):
    fake_wp.responses = [FakeWPResponse(401, {"success": False, "error": "unauthorized"})]

    with pytest.raises(sync_membership.WordPressAuthError):
        sync_membership.promote_to_premium(42, "cus_dummy")

    assert len(fake_wp.calls) == 1


def test_scenario_5_promote_is_idempotent_when_already_premium(fake_wp):
    fake_wp.responses = [
        FakeWPResponse(
            200,
            {
                "success": True,
                "already": True,
                "new_roles": ["subscriber", "premium_member"],
                "wp_user_id": 42,
            },
        )
    ]

    result = sync_membership.promote_to_premium(42, "cus_dummy")

    assert result["success"] is True
    assert result["already"] is True
    assert "premium_member" in result["new_roles"]


def test_reconcile_wp_with_stripe_retries_failed_active_sync(fake_supabase, monkeypatch):
    fake_supabase.tables["premium_customers"].append(
        {"wp_user_id": 42, "stripe_customer_id": "cus_dummy"}
    )
    fake_supabase.tables["premium_subscriptions"].append(
        {
            "stripe_subscription_id": "sub_dummy",
            "stripe_customer_id": "cus_dummy",
            "status": "active",
            "wp_sync_failed": True,
        }
    )
    monkeypatch.setattr(sync_membership, "get_supabase", lambda: fake_supabase)
    monkeypatch.setattr(sync_membership, "promote_to_premium", lambda wp_user_id, customer_id: {"success": True})

    result = sync_membership.reconcile_wp_with_stripe()

    row = fake_supabase.tables["premium_subscriptions"][0]
    assert result == {"checked": 1, "fixed": 1, "still_failed": 0}
    assert row["wp_sync_failed"] is False
    assert row["wp_sync_at"]
