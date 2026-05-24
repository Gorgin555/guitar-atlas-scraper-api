from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture(autouse=True)
def dashboard_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHBOARD_HMAC_SECRET", "dashboard_secret_dummy")
    monkeypatch.setenv("SUPABASE_URL", "https://supabase.example.test")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "supabase_service_dummy")
    monkeypatch.setenv("WP_URL", "https://wp.example.test")


class FakeResponse:
    def __init__(self, data: list[dict[str, Any]] | None = None):
        self.data = data or []


class FakeQuery:
    def __init__(self, db: "FakeSupabase", table_name: str):
        self.db = db
        self.table_name = table_name
        self.action = "select"
        self.payload: dict[str, Any] | None = None
        self.filters: list[tuple[str, Any]] = []
        self.order_key: str | None = None
        self.order_desc = False
        self.limit_count: int | None = None

    def select(self, *_args: str) -> "FakeQuery":
        self.action = "select"
        return self

    def insert(self, payload: dict[str, Any]) -> "FakeQuery":
        self.action = "insert"
        self.payload = payload
        return self

    def upsert(self, payload: dict[str, Any], **_kwargs: Any) -> "FakeQuery":
        self.action = "upsert"
        self.payload = payload
        return self

    def update(self, payload: dict[str, Any]) -> "FakeQuery":
        self.action = "update"
        self.payload = payload
        return self

    def eq(self, key: str, value: Any) -> "FakeQuery":
        self.filters.append((key, value))
        return self

    def order(self, key: str, desc: bool = False) -> "FakeQuery":
        self.order_key = key
        self.order_desc = desc
        return self

    def limit(self, count: int) -> "FakeQuery":
        self.limit_count = count
        return self

    def execute(self) -> FakeResponse:
        rows = self.db.tables.setdefault(self.table_name, [])
        matches = self._matching(rows)
        if self.action == "select":
            selected = [dict(row) for row in matches]
            if self.order_key:
                selected.sort(key=lambda row: row.get(self.order_key) or "", reverse=self.order_desc)
            if self.limit_count is not None:
                selected = selected[: self.limit_count]
            return FakeResponse(selected)
        if self.action == "insert":
            assert self.payload is not None
            rows.append(dict(self.payload))
            return FakeResponse([dict(self.payload)])
        if self.action == "upsert":
            assert self.payload is not None
            for row in rows:
                if all(row.get(key) == value for key, value in self.payload.items() if key in ("wp_user_id", "alert_id", "stripe_customer_id")):
                    row.update(self.payload)
                    return FakeResponse([dict(row)])
            rows.append(dict(self.payload))
            return FakeResponse([dict(self.payload)])
        if self.action == "update":
            assert self.payload is not None
            for row in matches:
                row.update(self.payload)
            return FakeResponse([dict(row) for row in matches])
        raise AssertionError(f"unknown action: {self.action}")

    def _matching(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = rows
        for key, value in self.filters:
            result = [row for row in result if row.get(key) == value]
        return result


class FakeSupabase:
    def __init__(self):
        self.tables: dict[str, list[dict[str, Any]]] = {
            "premium_customers": [],
            "premium_subscriptions": [],
            "index_daily": [],
            "dashboard_alerts": [],
            "alert_read_log": [],
            "products": [],
        }

    def table(self, table_name: str) -> FakeQuery:
        return FakeQuery(self, table_name)


@pytest.fixture
def fake_supabase() -> FakeSupabase:
    return FakeSupabase()
