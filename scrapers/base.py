"""
GUITAR ATLAS — BaseScraper
===========================
CLO はぐりん監修 法務ガイドライン準拠 (memory/legal/scraping_rules.md)

■ 設計原則
  - robots.txt の厳守（URIごとにキャッシュ、TTL 24h）
  - デフォルト 3秒 + ランダムジッター（±1秒）のレート制限
  - 画像 URL のフィルタリング（raw_payload への混入防止）
  - 自社 User-Agent 明示
  - Tenacity によるリトライ（429/503 を指数バックオフで処理）
"""
from __future__ import annotations

import logging
import random
import re
import time
import urllib.robotparser
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlparse

import requests
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

logger = logging.getLogger(__name__)

# ── 定数 ──────────────────────────────────────────────────────────────────────

USER_AGENT = (
    "GuitarAtlas/1.0 "
    "(Market Intelligence; contact: i49rake@gmail.com; "
    "data-collection-no-image-scraping)"
)

# robots.txt キャッシュ TTL（秒）
ROBOTS_TTL_SECONDS = 60 * 60 * 24  # 24時間

# 画像系 URL パターン（raw_payload からフィルタ）
_IMAGE_URL_RE = re.compile(
    r'https?://[^\s"\']+\.(jpg|jpeg|png|gif|webp|avif|svg|bmp|ico)',
    re.IGNORECASE,
)

# ── リトライ判定 ───────────────────────────────────────────────────────────────

def _should_retry(exc: BaseException) -> bool:
    """429 / 503 / 接続エラー のみリトライ。"""
    if isinstance(exc, requests.HTTPError):
        return exc.response is not None and exc.response.status_code in (429, 503)
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    return False


# ── RobotsCache ───────────────────────────────────────────────────────────────

class RobotsCache:
    """
    ドメインごとに robots.txt をキャッシュし、アクセス可否を判定する。
    TTL を超えると再取得する。
    """

    def __init__(self, ttl_seconds: int = ROBOTS_TTL_SECONDS) -> None:
        self._cache: dict[str, tuple[urllib.robotparser.RobotFileParser, datetime]] = {}
        self._ttl = timedelta(seconds=ttl_seconds)
        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT

    def _load(self, domain: str) -> urllib.robotparser.RobotFileParser:
        robots_url = f"{domain}/robots.txt"
        logger.debug("Fetching robots.txt: %s", robots_url)
        parser = urllib.robotparser.RobotFileParser()
        try:
            resp = self._session.get(robots_url, timeout=10)
            if resp.status_code == 200:
                parser.parse(resp.text.splitlines())
            else:
                # robots.txt が存在しない / 取得失敗 → 保守的にアクセス不可とする
                logger.warning(
                    "robots.txt fetch failed (%s) for %s — treating as disallowed",
                    resp.status_code, domain,
                )
                # 空の parser は全て allow 扱いになるが、ここでは失敗を明示
        except Exception as e:
            logger.warning("robots.txt fetch error for %s: %s — treating as disallowed", domain, e)
        return parser

    def _domain(self, url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def is_allowed(self, url: str) -> bool:
        domain = self._domain(url)
        entry = self._cache.get(domain)
        if entry is None or datetime.utcnow() - entry[1] > self._ttl:
            parser = self._load(domain)
            self._cache[domain] = (parser, datetime.utcnow())
        else:
            parser = entry[0]
        allowed = parser.can_fetch(USER_AGENT, url)
        if not allowed:
            logger.info("robots.txt DISALLOWED: %s", url)
        return allowed


# ── BaseScraper ───────────────────────────────────────────────────────────────

class BaseScraper:
    """
    全スクレイパーの基底クラス。

    Usage:
        class MyScraper(BaseScraper):
            def scrape_page(self, url) -> list[dict]:
                ...
    """

    def __init__(
        self,
        rate_limit_seconds: float = 3.0,
        jitter_seconds: float = 1.0,
        robots_ttl: int = ROBOTS_TTL_SECONDS,
    ) -> None:
        self.rate_limit = rate_limit_seconds
        self.jitter = jitter_seconds
        self.robots = RobotsCache(ttl_seconds=robots_ttl)
        self._last_request_time: float = 0.0

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "ja,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })

    # ── レート制限 ──────────────────────────────────────────────────────────

    def _wait(self) -> None:
        """前回リクエストからの経過時間を見て、必要なら待機する。"""
        elapsed = time.monotonic() - self._last_request_time
        wait_time = self.rate_limit + random.uniform(-self.jitter, self.jitter)
        wait_time = max(wait_time, 0)
        remaining = wait_time - elapsed
        if remaining > 0:
            logger.debug("Rate limit: sleeping %.2fs", remaining)
            time.sleep(remaining)

    # ── HTTP GET（リトライ付き）────────────────────────────────────────────

    @retry(
        retry=retry_if_exception(_should_retry),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2, min=4, max=120),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _get(self, url: str, **kwargs) -> requests.Response:
        """
        robots.txt チェック後に GET する。
        レート制限・リトライ込み。
        """
        if not self.robots.is_allowed(url):
            raise RobotsDisallowedError(f"robots.txt disallowed: {url}")

        self._wait()
        self._last_request_time = time.monotonic()

        resp = self.session.get(url, timeout=20, **kwargs)
        resp.raise_for_status()
        logger.debug("GET %s → %s", url, resp.status_code)
        return resp

    # ── 画像フィルタ ─────────────────────────────────────────────────────────

    @staticmethod
    def strip_images(payload: dict) -> dict:
        """
        raw_payload の JSONB に画像 URL が混入しないようフィルタリング。
        CLO はぐりん監修: 画像転載ゼロを担保。
        """
        import json
        raw = json.dumps(payload)
        # 画像 URL を [IMAGE_REMOVED] に置換
        cleaned = _IMAGE_URL_RE.sub("[IMAGE_REMOVED]", raw)
        return json.loads(cleaned)

    # ── 価格パース ────────────────────────────────────────────────────────────

    @staticmethod
    def parse_jpy(text: str) -> Optional[float]:
        """
        '¥123,456' / '123,456円' / '123456' → 123456.0
        数字以外を除去してfloatに変換。失敗時は None。
        """
        if not text:
            return None
        digits = re.sub(r"[^\d]", "", text)
        try:
            return float(digits) if digits else None
        except ValueError:
            return None


# ── カスタム例外 ──────────────────────────────────────────────────────────────

class RobotsDisallowedError(Exception):
    """robots.txt で Disallow されているパスへのアクセス試行。"""
    pass


class ScraperError(Exception):
    """スクレイパー汎用エラー。"""
    pass
