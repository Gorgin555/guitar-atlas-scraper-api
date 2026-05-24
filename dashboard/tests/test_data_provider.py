from __future__ import annotations

import dashboard.data_provider as data_provider


def _seed_payload(fake_supabase) -> None:
    fake_supabase.tables["premium_customers"] = [
        {"wp_user_id": 42, "stripe_customer_id": "cus_123", "email": "ceo@example.test"}
    ]
    fake_supabase.tables["premium_subscriptions"] = [
        {
            "stripe_customer_id": "cus_123",
            "plan": "monthly",
            "status": "active",
            "current_period_end": "2026-06-18T00:00:00+00:00",
        }
    ]
    fake_supabase.tables["index_daily"] = [
        {
            "snapshot_date": "2026-05-18",
            "gai_e": 103.94,
            "gai_e_delta_7d": 3.94,
            "mfi": 106.8,
            "mfi_delta_7d": 6.8,
            "vfi_ao": 97.42,
            "vfi_ao_delta_7d": -2.58,
            "vfi_ac": 98.19,
            "vfi_ac_delta_7d": -1.81,
            "bpi": 105.89,
            "bpi_delta_7d": 5.89,
            "boutique_premium": 0.77,
            "boutique_premium_ma7": 0.79,
            "vintage_premium": 6.3,
            "vintage_premium_ma7": 6.15,
            "heritage_spread": 8.2,
            "heritage_spread_ma7": 7.95,
        }
    ]
    fake_supabase.tables["products"] = [
        {"product_id": "11111111-1111-1111-1111-111111111111", "model": "V1 Pre-CBS Strat"},
        {"product_id": "22222222-2222-2222-2222-222222222222", "model": "V12 SG Std '61"},
        {"product_id": "33333333-3333-3333-3333-333333333333", "model": "Suhr Modern Plus"},
    ]
    fake_supabase.tables["dashboard_alerts"] = [
        {
            "alert_id": 1,
            "product_id": "11111111-1111-1111-1111-111111111111",
            "triggered_at": "2026-05-18T10:00:00+00:00",
            "delta_pct": 14.2,
            "current_price": 5106,
            "ma7_price": 4471,
            "window_days": 7,
            "category": "VFI",
        },
        {
            "alert_id": 2,
            "product_id": "22222222-2222-2222-2222-222222222222",
            "triggered_at": "2026-05-18T09:00:00+00:00",
            "delta_pct": -8.1,
            "current_price": 4200,
            "ma7_price": 4570,
            "window_days": 7,
            "category": "VFI",
        },
        {
            "alert_id": 3,
            "product_id": "33333333-3333-3333-3333-333333333333",
            "triggered_at": "2026-05-17T08:00:00+00:00",
            "delta_pct": 11.8,
            "current_price": 3100,
            "ma7_price": 2773,
            "window_days": 7,
            "category": "BPI",
        },
    ]


def test_dashboard_payload_shape(fake_supabase, monkeypatch) -> None:
    _seed_payload(fake_supabase)
    monkeypatch.setattr(data_provider, "get_supabase", lambda: fake_supabase)
    payload = data_provider.get_dashboard_payload(42)

    assert payload["user"]["plan"] == "monthly"
    assert payload["indices"]["GAI_E"]["value"] == 103.94
    assert payload["spreads"]["boutique_premium"]["ma7"] == 0.79
    assert payload["movers"]["top_gainers"][0]["model"] == "V1 Pre-CBS Strat"
    assert payload["movers"]["top_losers"][0]["delta_pct"] == -8.1
    assert payload["deep_report"] == {"latest_url": None, "latest_published": None}


def test_dashboard_payload_past_due_status(fake_supabase, monkeypatch) -> None:
    _seed_payload(fake_supabase)
    fake_supabase.tables["premium_subscriptions"][0]["status"] = "past_due"
    monkeypatch.setattr(data_provider, "get_supabase", lambda: fake_supabase)

    assert data_provider.get_dashboard_payload(42)["user"]["status"] == "past_due"


def test_dashboard_payload_unread_alerts(fake_supabase, monkeypatch) -> None:
    _seed_payload(fake_supabase)
    fake_supabase.tables["alert_read_log"] = [{"wp_user_id": 42, "alert_id": 1}]
    monkeypatch.setattr(data_provider, "get_supabase", lambda: fake_supabase)

    assert data_provider.get_dashboard_payload(42)["alerts_unread"] == 2
    data_provider.mark_alert_read(42, 2)
    assert len(fake_supabase.tables["alert_read_log"]) == 2
