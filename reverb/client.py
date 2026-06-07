"""
GUITAR ATLAS - Reverb API client
================================

公式 Reverb API (https://www.reverb-api.com/) を扱う薄いラッパー。

設計方針:
  - Personal Access Token 認証 (Phase 1)
  - 1 req/sec の自主レート制限（Reverb側の明示制限はないが慣習）
  - 429/5xx は指数バックオフ付きで自動リトライ
  - 全レスポンスを `raw_payload` として呼び出し側に渡し DB に丸ごと保存させる
  - 検索は basket_v1.yaml の brand+model クエリを直接通す

主要API:
  GET /api/listings           : 検索（出品中）
  GET /api/listings/all       : 含む過去（売却済み含む）。無いケースは検索パラメタで切替
  GET /api/my/account         : 自プロフィール（疎通確認用）

Usage:
    client = ReverbClient.from_env()
    profile = client.get_profile()
    for listing in client.search_listings(query="Suhr Classic S Antique", state="all"):
        ...
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Iterator, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ReverbAPIError(Exception):
    """Reverb API からのエラーレスポンス全般。"""

    def __init__(self, message: str, *, status_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class RateLimitError(ReverbAPIError):
    """429 専用。クライアント側の sleep 制御に使う。"""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class ReverbConfig:
    token: str
    api_base: str = "https://api.reverb.com/api"
    user_agent: str = "GuitarAtlas/0.1 (contact: i49rake@gmail.com)"
    request_interval_sec: float = 1.0          # 自主レート制限
    timeout_sec: float = 30.0
    max_retries: int = 5

    @classmethod
    def from_env(cls) -> "ReverbConfig":
        token = os.environ.get("REVERB_PERSONAL_TOKEN")
        if not token:
            raise RuntimeError(
                "REVERB_PERSONAL_TOKEN is not set. "
                "See docs/02_reverb_api_setup.md and update .env."
            )
        return cls(
            token=token,
            api_base=os.environ.get("REVERB_API_BASE", cls.api_base),
            user_agent=os.environ.get("REVERB_USER_AGENT", cls.user_agent),
        )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class ReverbClient:
    """Reverb API minimal client with rate limiting + retry."""

    def __init__(self, config: ReverbConfig):
        self.config = config
        self._last_call_at: float = 0.0
        self._session = self._build_session()

    @classmethod
    def from_env(cls) -> "ReverbClient":
        return cls(ReverbConfig.from_env())

    # ---------------- session ----------------

    def _build_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({
            "Authorization": f"Bearer {self.config.token}",
            "Accept": "application/hal+json",
            "Accept-Version": "3.0",
            "Content-Type": "application/hal+json",
            "User-Agent": self.config.user_agent,
        })

        retry = Retry(
            total=self.config.max_retries,
            backoff_factor=1.5,
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=("GET",),
            raise_on_status=False,
        )
        s.mount("https://", HTTPAdapter(max_retries=retry))
        return s

    # ---------------- rate limit ----------------

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call_at
        if elapsed < self.config.request_interval_sec:
            time.sleep(self.config.request_interval_sec - elapsed)
        self._last_call_at = time.monotonic()

    # ---------------- core request ----------------

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        self._throttle()
        url = path if path.startswith("http") else f"{self.config.api_base}{path}"
        logger.debug("Reverb %s %s params=%s", method, url, kwargs.get("params"))
        resp = self._session.request(
            method,
            url,
            timeout=self.config.timeout_sec,
            **kwargs,
        )

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "10"))
            logger.warning("Reverb 429 received, sleeping %s s", retry_after)
            time.sleep(retry_after)
            raise RateLimitError("Rate limited", status_code=429, payload=resp.text)

        if resp.status_code >= 400:
            raise ReverbAPIError(
                f"Reverb API error {resp.status_code}: {resp.text[:500]}",
                status_code=resp.status_code,
                payload=_safe_json(resp),
            )

        return _safe_json(resp) or {}

    # ---------------- public methods ----------------

    def get_profile(self) -> dict[str, Any]:
        """疎通確認用。/api/my/account を叩いて返す。"""
        return self._request("GET", "/my/account")

    def search_listings(
        self,
        query: str,
        *,
        state: str = "live",            # 'live' | 'sold' | 'all' (Reverb 側の挙動に応じて切替)
        per_page: int = 50,
        max_pages: int = 10,
        extra_params: Optional[dict[str, Any]] = None,
    ) -> Iterator[dict[str, Any]]:
        """
        Reverb 出品検索。ページネーションを透過的に処理して listings を逐次 yield する。

        Args:
            query: 自由検索文字列（例: "Suhr Classic S Antique HSS"）
            state: 'live' / 'sold' / 'all'
            per_page: ページサイズ（最大50想定）
            max_pages: 取得上限ページ数（暴走防止）
            extra_params: brand_slug, year_min, year_max, condition 等を追加で渡せる

        Yields:
            listing dict（Reverb のレスポンス1件分そのまま）
        """
        params: dict[str, Any] = {
            "query": query,
            "per_page": per_page,
            "page": 1,
        }
        if state and state != "live":
            params["state"] = state
        if extra_params:
            params.update(extra_params)

        next_url: Optional[str] = None
        page_count = 0

        while page_count < max_pages:
            if next_url:
                payload = self._request("GET", next_url)
            else:
                payload = self._request("GET", "/listings", params=params)

            listings = payload.get("listings") or []
            for item in listings:
                yield item

            page_count += 1

            # HAL ナビゲーション
            links = (payload.get("_links") or {})
            next_link = links.get("next") or {}
            next_url = next_link.get("href")
            if not next_url:
                break

    def get_listing(self, listing_id: str) -> dict[str, Any]:
        return self._request("GET", f"/listings/{listing_id}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Price Guide methods (TH-06e)
# ---------------------------------------------------------------------------

def search_price_guides(
    self: ReverbClient,
    *,
    query: Optional[str] = None,
    make: Optional[str] = None,
    model: Optional[str] = None,
    per_page: int = 24,
    max_pages: int = 1,
) -> Iterator[dict[str, Any]]:
    """GET /priceguide and yield price_guides[] with guarded paging."""
    params: dict[str, Any] = {
        "per_page": per_page,
        "page": 1,
    }
    if query:
        params["query"] = query
    if make:
        params["make"] = make
    if model:
        params["model"] = model

    next_url: Optional[str] = None
    page_count = 0

    while page_count < max_pages:
        if next_url:
            payload = self._request("GET", next_url)
        else:
            payload = self._request("GET", "/priceguide", params=params)

        for item in payload.get("price_guides") or []:
            yield item

        page_count += 1

        links = payload.get("_links") or {}
        next_link = links.get("next") or {}
        next_url = next_link.get("href")
        if next_url:
            continue

        current_page = int(payload.get("current_page") or params.get("page") or page_count)
        total_pages = int(payload.get("total_pages") or current_page)
        if current_page >= total_pages:
            break
        params["page"] = current_page + 1


def get_price_guide_transactions(
    self: ReverbClient,
    guide_id: int | str,
    *,
    per_page: int = 50,
    max_pages: int = 2,
) -> Iterator[dict[str, Any]]:
    """GET /priceguide/{guide_id}/transactions and yield transaction rows."""
    params: dict[str, Any] = {
        "per_page": per_page,
        "page": 1,
    }
    next_url: Optional[str] = None
    page_count = 0

    while page_count < max_pages:
        if next_url:
            payload = self._request("GET", next_url)
        else:
            payload = self._request("GET", f"/priceguide/{guide_id}/transactions", params=params)

        rows = payload.get("transactions") or payload.get("price_guide_transactions") or []
        for item in rows:
            yield item

        page_count += 1

        links = payload.get("_links") or {}
        next_link = links.get("next") or {}
        next_url = next_link.get("href")
        if next_url:
            continue

        current_page = int(payload.get("current_page") or params.get("page") or page_count)
        total_pages = int(payload.get("total_pages") or current_page)
        if current_page >= total_pages:
            break
        params["page"] = current_page + 1


ReverbClient.search_price_guides = search_price_guides
ReverbClient.get_price_guide_transactions = get_price_guide_transactions
