from __future__ import annotations

import importlib


class FakeCheckoutSession:
    calls: list[dict] = []

    @classmethod
    def create(cls, **kwargs):
        cls.calls.append(kwargs)
        return {"id": "cs_dummy", "url": "https://checkout.stripe.test/session"}


class FakeCustomer:
    @staticmethod
    def create(**kwargs):
        return {"id": "cus_dummy", "email": kwargs["email"], "created": 1234567890}


class FakeSubscription:
    @staticmethod
    def create(**kwargs):
        return {
            "id": "sub_dummy",
            "status": "active",
            "current_period_end": 1234567999,
        }

    @staticmethod
    def retrieve(subscription_id):
        return {
            "id": subscription_id,
            "status": "active",
            "current_period_end": 1234567999,
            "cancel_at_period_end": False,
        }

    @staticmethod
    def modify(subscription_id, **kwargs):
        return {
            "id": subscription_id,
            "status": "active",
            "canceled_at": None,
            **kwargs,
        }


class FakeStripe:
    Customer = FakeCustomer
    Subscription = FakeSubscription

    class checkout:
        Session = FakeCheckoutSession


def test_scenario_1_monthly_checkout_session(monkeypatch):
    client = importlib.import_module("billing.stripe.client")
    monkeypatch.setattr(client, "stripe", FakeStripe)

    result = client.create_checkout_session(
        wp_user_id=42,
        plan="monthly",
        success_url="https://example.test/success",
        cancel_url="https://example.test/cancel",
    )

    assert result == {
        "session_id": "cs_dummy",
        "url": "https://checkout.stripe.test/session",
    }
    assert FakeCheckoutSession.calls[-1]["line_items"][0]["price"] == "price_monthly_dummy"
    assert FakeCheckoutSession.calls[-1]["metadata"]["wp_user_id"] == "42"


def test_create_customer_is_idempotent(monkeypatch, fake_supabase):
    client = importlib.import_module("billing.stripe.client")
    fake_supabase.tables["premium_customers"].append(
        {
            "wp_user_id": 42,
            "stripe_customer_id": "cus_existing",
            "email": "member@example.test",
            "created_at": "2026-05-18T00:00:00+00:00",
        }
    )
    monkeypatch.setattr(client, "get_supabase", lambda: fake_supabase)
    monkeypatch.setattr(client, "stripe", FakeStripe)

    result = client.create_customer("member@example.test", 42)

    assert result["customer_id"] == "cus_existing"
    assert len(fake_supabase.tables["premium_customers"]) == 1


def test_create_subscription_uses_yearly_price(monkeypatch):
    client = importlib.import_module("billing.stripe.client")
    monkeypatch.setattr(client, "stripe", FakeStripe)

    result = client.create_subscription("cus_dummy", "yearly")

    assert result == {
        "subscription_id": "sub_dummy",
        "status": "active",
        "current_period_end": 1234567999,
    }
