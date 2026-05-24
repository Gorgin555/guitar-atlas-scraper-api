from __future__ import annotations

import importlib


class FakeSubscriptionList:
    @staticmethod
    def list(status, limit):
        assert status == "all"
        assert limit == 100
        return {
            "data": [
                {
                    "id": "sub_remote",
                    "customer": "cus_remote",
                    "status": "active",
                    "current_period_start": 1234567000,
                    "current_period_end": 1234567999,
                    "cancel_at_period_end": False,
                    "metadata": {"plan": "yearly"},
                }
            ]
        }


class FakeStripe:
    Subscription = FakeSubscriptionList


def test_reconcile_daily_fixes_missing_subscription(monkeypatch, fake_supabase):
    reconciler = importlib.import_module("billing.stripe.reconciler")
    monkeypatch.setattr(reconciler, "stripe", FakeStripe)
    monkeypatch.setattr(reconciler, "get_supabase", lambda: fake_supabase)

    result = reconciler.reconcile_daily()

    assert result["checked"] == 1
    assert result["fixed"] == 1
    assert result["discrepancies"][0]["stripe_subscription_id"] == "sub_remote"
    assert fake_supabase.tables["premium_subscriptions"][0]["plan"] == "yearly"


def test_reconcile_daily_noops_when_subscription_matches(monkeypatch, fake_supabase):
    reconciler = importlib.import_module("billing.stripe.reconciler")
    fake_supabase.tables["premium_subscriptions"].append(
        {
            "stripe_subscription_id": "sub_remote",
            "stripe_customer_id": "cus_remote",
            "plan": "yearly",
            "status": "active",
            "current_period_start": "2009-02-13T23:16:40+00:00",
            "current_period_end": "2009-02-13T23:33:19+00:00",
            "cancel_at_period_end": False,
            "canceled_at": None,
        }
    )
    monkeypatch.setattr(reconciler, "stripe", FakeStripe)
    monkeypatch.setattr(reconciler, "get_supabase", lambda: fake_supabase)

    result = reconciler.reconcile_daily()

    assert result["checked"] == 1
    assert result["fixed"] == 0
    assert result["discrepancies"] == []
