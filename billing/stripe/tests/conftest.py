from __future__ import annotations

import os
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def stripe_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "test_secret_key_dummy")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "test_webhook_secret_dummy")
    monkeypatch.setenv("STRIPE_PRICE_ID_MONTHLY", "price_monthly_dummy")
    monkeypatch.setenv("STRIPE_PRICE_ID_YEARLY", "price_yearly_dummy")
    monkeypatch.setenv("STRIPE_PORTAL_RETURN_URL", "https://example.test/account")
    monkeypatch.setenv("SUPABASE_URL", "https://supabase.example.test")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "supabase_service_dummy")


class FakeResponse:
    def __init__(self, data: list[dict[str, Any]] | None = None):
        self.data = data or []


class FakeQuery:
    def __init__(self, db: "FakeSupabase", table_name: str):
        self.db = db
        self.table_name = table_name
        self.action = "select"
        self.payload: dict[str, Any] | list[dict[str, Any]] | None = None
        self.filters: list[tuple[str, Any]] = []
        self.limit_count: int | None = None
        self.conflict_key: str | None = None

    def select(self, *_args: str) -> "FakeQuery":
        self.action = "select"
        return self

    def insert(self, payload: dict[str, Any]) -> "FakeQuery":
        self.action = "insert"
        self.payload = payload
        return self

    def upsert(self, payload: dict[str, Any], on_conflict: str | None = None) -> "FakeQuery":
        self.action = "upsert"
        self.payload = payload
        self.conflict_key = on_conflict
        return self

    def update(self, payload: dict[str, Any]) -> "FakeQuery":
        self.action = "update"
        self.payload = payload
        return self

    def eq(self, key: str, value: Any) -> "FakeQuery":
        self.filters.append((key, value))
        return self

    def limit(self, count: int) -> "FakeQuery":
        self.limit_count = count
        return self

    def execute(self) -> FakeResponse:
        rows = self.db.tables.setdefault(self.table_name, [])
        if self.action == "select":
            result = self._matching(rows)
            if self.limit_count is not None:
                result = result[: self.limit_count]
            return FakeResponse([dict(row) for row in result])
        if self.action == "insert":
            assert isinstance(self.payload, dict)
            rows.append(dict(self.payload))
            return FakeResponse([dict(self.payload)])
        if self.action == "upsert":
            assert isinstance(self.payload, dict)
            key = self.conflict_key or self._default_conflict_key()
            matched = None
            for row in rows:
                if row.get(key) == self.payload.get(key):
                    matched = row
                    break
            if matched is None:
                rows.append(dict(self.payload))
            else:
                matched.update(self.payload)
            return FakeResponse([dict(self.payload)])
        if self.action == "update":
            assert isinstance(self.payload, dict)
            changed = []
            for row in self._matching(rows):
                row.update(self.payload)
                changed.append(dict(row))
            return FakeResponse(changed)
        raise AssertionError(f"unknown action: {self.action}")

    def _matching(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = rows
        for key, value in self.filters:
            result = [row for row in result if row.get(key) == value]
        return result

    def _default_conflict_key(self) -> str:
        if self.table_name == "premium_customers":
            return "wp_user_id"
        if self.table_name == "premium_subscriptions":
            return "stripe_subscription_id"
        if self.table_name == "stripe_event_log":
            return "event_id"
        return "id"


class FakeSupabase:
    def __init__(self):
        self.tables: dict[str, list[dict[str, Any]]] = {
            "premium_customers": [],
            "premium_subscriptions": [],
            "stripe_event_log": [],
        }

    def table(self, table_name: str) -> FakeQuery:
        return FakeQuery(self, table_name)


@pytest.fixture
def fake_supabase() -> FakeSupabase:
    return FakeSupabase()
