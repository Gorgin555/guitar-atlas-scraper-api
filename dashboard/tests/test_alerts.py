from __future__ import annotations

import dashboard.alerts as alerts


def test_detect_movers_includes_threshold_exceeded(fake_supabase, monkeypatch) -> None:
    fake_supabase.tables["dashboard_alerts"] = [
        {
            "product_id": "11111111-1111-1111-1111-111111111111",
            "delta_pct": 12.0,
            "current_price": 5106,
            "ma7_price": 4560,
            "window_days": 7,
        }
    ]
    monkeypatch.setattr(alerts, "get_supabase", lambda: fake_supabase)

    result = alerts.detect_movers(window_days=7, threshold_pct=10)

    assert result == [
        {
            "product_id": "11111111-1111-1111-1111-111111111111",
            "delta_pct": 12.0,
            "current_price": 5106.0,
            "ma7": 4560.0,
        }
    ]


def test_detect_movers_excludes_below_threshold(fake_supabase, monkeypatch) -> None:
    fake_supabase.tables["dashboard_alerts"] = [
        {
            "product_id": "22222222-2222-2222-2222-222222222222",
            "delta_pct": 8.0,
            "current_price": 4200,
            "ma7_price": 3888,
            "window_days": 7,
        }
    ]
    monkeypatch.setattr(alerts, "get_supabase", lambda: fake_supabase)

    assert alerts.detect_movers(window_days=7, threshold_pct=10) == []
