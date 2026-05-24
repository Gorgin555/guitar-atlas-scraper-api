from __future__ import annotations

import importlib


def _event(event_id: str, event_type: str, obj: dict) -> dict:
    return {"id": event_id, "type": event_type, "data": {"object": obj}}


class FakeWebhook:
    next_event: dict | None = None
    should_raise = False

    @classmethod
    def construct_event(cls, _payload, _signature, _secret):
        if cls.should_raise:
            raise ValueError("bad signature")
        return cls.next_event


class FakeSubscription:
    @staticmethod
    def retrieve(_subscription_id):
        return {
            "id": "sub_dummy",
            "customer": "cus_dummy",
            "status": "active",
            "current_period_start": 1234567000,
            "current_period_end": 1234567999,
            "cancel_at_period_end": False,
            "metadata": {"plan": "monthly"},
        }


class FakeStripe:
    Webhook = FakeWebhook
    Subscription = FakeSubscription


def test_scenario_3_invalid_webhook_signature(monkeypatch, fake_supabase):
    handler = importlib.import_module("billing.stripe.webhook_handler")
    FakeWebhook.should_raise = True
    monkeypatch.setattr(handler, "stripe", FakeStripe)
    monkeypatch.setattr(handler, "get_supabase", lambda: fake_supabase)

    status, body = handler.handle_event(b"{}", "invalid")

    assert status == 400
    assert body == {"error": "invalid signature"}
    assert fake_supabase.tables["stripe_event_log"] == []


def test_checkout_completed_upserts_subscription_and_logs(monkeypatch, fake_supabase):
    handler = importlib.import_module("billing.stripe.webhook_handler")
    FakeWebhook.should_raise = False
    FakeWebhook.next_event = _event(
        "evt_checkout",
        "checkout.session.completed",
        {
            "customer": "cus_dummy",
            "subscription": "sub_dummy",
            "client_reference_id": "42",
            "customer_details": {"email": "member@example.test"},
            "metadata": {"plan": "monthly", "wp_user_id": "42"},
        },
    )
    monkeypatch.setattr(handler, "stripe", FakeStripe)
    monkeypatch.setattr(handler, "get_supabase", lambda: fake_supabase)
    monkeypatch.setattr(handler, "promote_to_premium", lambda wp_user_id, customer_id: {"success": True})

    status, body = handler.handle_event(b"{}", "valid")

    assert status == 200
    assert body == {"received": True}
    assert fake_supabase.tables["premium_customers"][0]["wp_user_id"] == 42
    assert fake_supabase.tables["premium_subscriptions"][0]["status"] == "active"
    assert fake_supabase.tables["stripe_event_log"][0]["event_id"] == "evt_checkout"


def test_scenario_2_invoice_paid_stores_hosted_invoice_url(monkeypatch, fake_supabase):
    handler = importlib.import_module("billing.stripe.webhook_handler")
    monkeypatch.setattr(handler, "promote_to_premium", lambda wp_user_id, customer_id: {"success": True})
    monkeypatch.setattr(handler, "demote_from_premium", lambda wp_user_id, reason: {"success": True})
    fake_supabase.tables["premium_subscriptions"].append(
        {
            "stripe_subscription_id": "sub_dummy",
            "status": "past_due",
            "payment_failure_count": 2,
        }
    )
    FakeWebhook.should_raise = False
    FakeWebhook.next_event = _event(
        "evt_invoice_paid",
        "invoice.paid",
        {
            "subscription": "sub_dummy",
            "hosted_invoice_url": "https://stripe.test/invoice",
        },
    )
    monkeypatch.setattr(handler, "stripe", FakeStripe)
    monkeypatch.setattr(handler, "get_supabase", lambda: fake_supabase)

    status, body = handler.handle_event(b"{}", "valid")

    row = fake_supabase.tables["premium_subscriptions"][0]
    assert status == 200
    assert body == {"received": True}
    assert row["status"] == "active"
    assert row["payment_failure_count"] == 0
    assert row["latest_invoice_url"] == "https://stripe.test/invoice"


def test_scenario_4_payment_failed_tracks_past_due_role_unchanged(monkeypatch, fake_supabase):
    handler = importlib.import_module("billing.stripe.webhook_handler")
    demote_calls = []
    fake_supabase.tables["premium_subscriptions"].append(
        {
            "stripe_subscription_id": "sub_dummy",
            "status": "active",
            "payment_failure_count": 0,
        }
    )
    FakeWebhook.should_raise = False
    FakeWebhook.next_event = _event(
        "evt_payment_failed",
        "invoice.payment_failed",
        {"subscription": "sub_dummy"},
    )
    monkeypatch.setattr(handler, "stripe", FakeStripe)
    monkeypatch.setattr(handler, "get_supabase", lambda: fake_supabase)
    monkeypatch.setattr(handler, "demote_from_premium", lambda wp_user_id, reason: demote_calls.append((wp_user_id, reason)))

    status, body = handler.handle_event(b"{}", "valid")

    row = fake_supabase.tables["premium_subscriptions"][0]
    assert status == 200
    assert body == {"received": True}
    assert row["status"] == "past_due"
    assert row["payment_failure_count"] == 1
    assert demote_calls == []


def test_scenario_5_duplicate_event_returns_deduped(monkeypatch, fake_supabase):
    handler = importlib.import_module("billing.stripe.webhook_handler")
    monkeypatch.setattr(handler, "promote_to_premium", lambda wp_user_id, customer_id: {"success": True})
    monkeypatch.setattr(handler, "demote_from_premium", lambda wp_user_id, reason: {"success": True})
    fake_supabase.tables["stripe_event_log"].append(
        {"event_id": "evt_duplicate", "event_type": "invoice.paid"}
    )
    FakeWebhook.should_raise = False
    FakeWebhook.next_event = _event(
        "evt_duplicate",
        "invoice.paid",
        {"subscription": "sub_dummy"},
    )
    monkeypatch.setattr(handler, "stripe", FakeStripe)
    monkeypatch.setattr(handler, "get_supabase", lambda: fake_supabase)

    status, body = handler.handle_event(b"{}", "valid")

    assert status == 200
    assert body == {"deduped": True}
    assert len(fake_supabase.tables["stripe_event_log"]) == 1


def test_canceled_subscription_calls_demote(monkeypatch, fake_supabase):
    handler = importlib.import_module("billing.stripe.webhook_handler")
    demote_calls = []
    fake_supabase.tables["premium_customers"].append(
        {
            "wp_user_id": 42,
            "stripe_customer_id": "cus_dummy",
            "email": "member@example.test",
        }
    )
    FakeWebhook.should_raise = False
    FakeWebhook.next_event = _event(
        "evt_deleted",
        "customer.subscription.deleted",
        {
            "id": "sub_dummy",
            "customer": "cus_dummy",
            "status": "canceled",
            "current_period_start": 1234567000,
            "current_period_end": 1234567999,
            "cancel_at_period_end": False,
            "metadata": {"plan": "monthly"},
        },
    )
    monkeypatch.setattr(handler, "stripe", FakeStripe)
    monkeypatch.setattr(handler, "get_supabase", lambda: fake_supabase)
    monkeypatch.setattr(
        handler,
        "demote_from_premium",
        lambda wp_user_id, reason: demote_calls.append((wp_user_id, reason)) or {"success": True},
    )

    status, body = handler.handle_event(b"{}", "valid")

    assert status == 200
    assert body == {"received": True}
    assert demote_calls == [(42, "canceled")]
