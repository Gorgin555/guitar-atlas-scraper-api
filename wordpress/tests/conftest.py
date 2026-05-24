from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture(autouse=True)
def wordpress_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WP_URL", "https://wp.example.test")
    monkeypatch.setenv("WP_APP_USERNAME", "wp_user_dummy")
    monkeypatch.setenv("WP_APP_PASSWORD", "wp_password_dummy")
    monkeypatch.setenv("GA_MEMBERSHIP_INTERNAL_TOKEN", "membership_token_dummy")
    monkeypatch.setenv("SUPABASE_URL", "https://supabase.example.test")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "supabase_service_dummy")


class FakeWPResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeWPClient:
    calls: list[dict[str, Any]] = []
    responses: list[FakeWPResponse] = []

    def __init__(self, timeout: int):
        self.timeout = timeout

    def __enter__(self) -> "FakeWPClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def post(self, url: str, headers: dict[str, str], json: dict[str, Any]) -> FakeWPResponse:
        self.calls.append({"url": url, "headers": headers, "json": json})
        if self.responses:
            return self.responses.pop(0)
        return FakeWPResponse(200, {"success": True, "new_roles": ["subscriber"], "wp_user_id": json["wp_user_id"]})


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

    def select(self, *_args: str) -> "FakeQuery":
        self.action = "select"
        return self

    def update(self, payload: dict[str, Any]) -> "FakeQuery":
        self.action = "update"
        self.payload = payload
        return self

    def eq(self, key: str, value: Any) -> "FakeQuery":
        self.filters.append((key, value))
        return self

    def limit(self, _count: int) -> "FakeQuery":
        return self

    def execute(self) -> FakeResponse:
        rows = self.db.tables.setdefault(self.table_name, [])
        matches = self._matching(rows)
        if self.action == "select":
            return FakeResponse([dict(row) for row in matches])
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
        }

    def table(self, table_name: str) -> FakeQuery:
        return FakeQuery(self, table_name)


@pytest.fixture
def fake_supabase() -> FakeSupabase:
    return FakeSupabase()


@pytest.fixture
def fake_wp(monkeypatch: pytest.MonkeyPatch) -> type[FakeWPClient]:
    FakeWPClient.calls = []
    FakeWPClient.responses = []
    import wordpress.sync_membership as sync_membership

    monkeypatch.setattr(sync_membership.httpx, "Client", FakeWPClient)
    return FakeWPClient
