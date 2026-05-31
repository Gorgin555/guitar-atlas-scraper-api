"""
GUITAR ATLAS — デジマートスクレイパー
======================================
https://www.digimart.net/ からギター出品情報（数値のみ）を収集し、
Supabase listings_daily に upsert する。

■ 収集データ（数値・テキスト限定）
  - タイトル、価格（JPY）、コンディション、出品日、店舗名、都道府県
  - 画像転載なし（CLO はぐりん監修 memory/legal/scraping_rules.md）

■ URL 構造
  Search: https://www.digimart.net/search
          ?category_id=2          # ギター
          &keyword={keyword}
          &sort=published_at_desc  # 新着順
          &per_page=30

■ HTML セレクター（2026-05 時点）
  デジマートは server-side rendering で本文を返す。
  主要セレクターはバージョン変更への耐性のため複数フォールバックあり。

Usage:
    from scrapers.digimart import DigimartScraper
    scraper = DigimartScraper()
    listings = scraper.fetch("Fender Stratocaster", max_pages=3)
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterator, Optional
from urllib.parse import quote, urlencode

from bs4 import BeautifulSoup

from .base import BaseScraper, ScraperError

logger = logging.getLogger(__name__)

DIGIMART_BASE = "https://www.digimart.net"

# ── コンディション正規化 ─────────────────────────────────────────────────────────

_CONDITION_MAP = {
    "新品": "brand_new",
    "新古品": "mint",
    "美品": "excellent",
    "極上品": "excellent",
    "良品": "very_good",
    "中古良品": "good",
    "中古": "good",
    "訳あり": "fair",
    "ジャンク": "poor",
    # 英語表記（まれに出現）
    "new": "brand_new",
    "mint": "mint",
    "excellent": "excellent",
    "very good": "very_good",
    "good": "good",
}


def _normalize_condition(raw: str) -> str:
    raw_lower = raw.strip().lower()
    for k, v in _CONDITION_MAP.items():
        if k in raw_lower:
            return v
    return "used"  # default


# ── DigimartScraper ────────────────────────────────────────────────────────────

class DigimartScraper(BaseScraper):
    """
    デジマート向けスクレイパー。

    BaseScraper の robots.txt チェック・レート制限・画像フィルタを継承。
    """

    # ── 検索 URL 構築 ──────────────────────────────────────────────────────

    def _search_url(self, keyword: str, page: int = 1, per_page: int = 30) -> str:
        params = {
            "category_id": "2",   # エレキギター
            "keyword": keyword,
            "sort": "published_at_desc",
            "per_page": str(per_page),
        }
        if page > 1:
            params["page"] = str(page)
        return f"{DIGIMART_BASE}/search?{urlencode(params, quote_via=quote)}"

    # ── ページ取得 ─────────────────────────────────────────────────────────

    def _fetch_page(self, keyword: str, page: int) -> Optional[BeautifulSoup]:
        url = self._search_url(keyword, page=page)
        try:
            resp = self._get(url)
            return BeautifulSoup(resp.text, "lxml")
        except Exception as e:
            logger.warning("Digimart page fetch error (keyword=%s, page=%d): %s", keyword, page, e)
            return None

    # ── HTML から出品カードを抽出 ─────────────────────────────────────────────

    def _parse_listing_cards(self, soup: BeautifulSoup) -> list[BeautifulSoup]:
        """
        出品カード要素のリストを返す。
        デジマートの HTML 構造変化に対応するため複数セレクターを試す。
        """
        # 優先順位: 実際の HTML 構造に合わせてチューニングすること
        selectors = [
            "div.ds-card",              # デジマート標準カード
            "li.list-item",             # リスト表示
            "article.product-item",     # 記事形式
            "div[data-instrument-id]",  # data 属性指定
            "div.instrument-card",      # 旧型カード
        ]
        for sel in selectors:
            cards = soup.select(sel)
            if cards:
                logger.debug("Digimart: found %d cards with selector '%s'", len(cards), sel)
                return cards

        # フォールバック: 価格要素の親を遡る
        price_els = soup.select("span.price, div.price, p.price")
        if price_els:
            logger.debug("Digimart: fallback — found %d price elements", len(price_els))
            return [p.parent for p in price_els if p.parent]

        logger.warning("Digimart: no listing cards found on page")
        return []

    def _extract_listing(self, card: BeautifulSoup) -> Optional[dict[str, Any]]:
        """
        出品カード HTML から数値・テキストデータを抽出する。
        画像URL は抽出しない（CLO 法務ガイドライン）。
        """
        try:
            # ── タイトル ─────────────────────────────────────────────
            title = None
            for sel in ["h3.title", "h2.title", ".item-title", ".product-title",
                        "a.item-name", ".name", "h3", "h2"]:
                el = card.select_one(sel)
                if el:
                    title = el.get_text(strip=True)
                    break

            if not title:
                return None

            # ── URL / 出品 ID ────────────────────────────────────────
            src_url = None
            src_id = None
            for sel in ["a.item-link", "a.title-link", "a[href*='/cat']", "a[href*='/DS']", "a"]:
                link = card.select_one(sel)
                if link and link.get("href"):
                    href = link["href"]
                    src_url = href if href.startswith("http") else DIGIMART_BASE + href
                    # URL から商品 ID を抽出（例: /cat01/DS12345678/ → DS12345678）
                    m = re.search(r"/(DS\d+|[A-Z]{2}\d+)/", href)
                    if m:
                        src_id = m.group(1)
                    break

            if not src_id:
                # URL が取れなかった場合はタイトルのハッシュを ID 代わりに使う
                src_id = f"dm_{abs(hash(title)) % 10**10}"

            # ── 価格 ──────────────────────────────────────────────────
            price_local = None
            for sel in [".price", ".selling-price", ".item-price", "span.price",
                        ".instrument-price", "[data-price]"]:
                el = card.select_one(sel)
                if el:
                    price_text = el.get("data-price") or el.get_text(strip=True)
                    price_local = self.parse_jpy(price_text)
                    if price_local:
                        break

            # ── コンディション ────────────────────────────────────────
            condition_raw = None
            for sel in [".condition", ".item-condition", ".status", ".state",
                        "[data-condition]", ".badge"]:
                el = card.select_one(sel)
                if el:
                    condition_raw = el.get_text(strip=True)
                    break

            condition = _normalize_condition(condition_raw) if condition_raw else None

            # ── 出品日 ────────────────────────────────────────────────
            listed_at = None
            for sel in ["time", ".date", ".listed-date", ".created-at",
                        "[datetime]", ".publish-date"]:
                el = card.select_one(sel)
                if el:
                    dt_str = el.get("datetime") or el.get_text(strip=True)
                    listed_at = _parse_date(dt_str)
                    if listed_at:
                        break

            # ── 店舗名 ────────────────────────────────────────────────
            seller_name = None
            for sel in [".shop-name", ".store-name", ".seller", ".shop",
                        "a[href*='/shop/']", ".dealer"]:
                el = card.select_one(sel)
                if el:
                    seller_name = el.get_text(strip=True)
                    break

            # ── 出品地（都道府県）───────────────────────────────────
            location_region = None
            for sel in [".prefecture", ".location", ".area", ".region"]:
                el = card.select_one(sel)
                if el:
                    location_region = el.get_text(strip=True)
                    break

            # ── raw_payload（画像 URL 除去済み）─────────────────────
            raw = {
                "title": title,
                "price_text": str(price_local) if price_local else None,
                "condition_raw": condition_raw,
                "seller_name": seller_name,
                "location": location_region,
                "src_url": src_url,
            }
            clean_raw = self.strip_images(raw)

            return {
                "source": "digimart",
                "source_listing_id": src_id,
                "source_url": src_url,
                "title": title,
                "price_local": price_local,
                "currency": "JPY",
                "price_usd": None,   # FX 変換は index_engine 担当
                "condition": condition,
                "condition_raw": condition_raw,
                "condition_tags": [],
                "listed_at": listed_at,
                "sold_at": None,
                "is_sold": False,
                "location_country": "JP",
                "location_region": location_region,
                "seller_type": "dealer",  # デジマートは基本ディーラーのみ
                "seller_name": seller_name,
                "description": None,  # 詳細ページは取得しない（レート制限保護）
                "raw_payload": clean_raw,
            }

        except Exception as e:
            logger.debug("Digimart card parse error: %s", e)
            return None

    # ── ページネーション確認 ──────────────────────────────────────────────

    def _has_next_page(self, soup: BeautifulSoup, current_page: int) -> bool:
        """次ページが存在するかどうか確認。"""
        # 「次へ」リンクの存在確認
        for sel in ["a.next", "a[rel='next']", ".pagination .next", "li.next > a"]:
            if soup.select_one(sel):
                return True
        # 現在ページのページネーション番号が続いているか
        current_links = soup.select(f"a[href*='page={current_page + 1}']")
        return bool(current_links)

    # ── 公開インターフェース ──────────────────────────────────────────────

    def fetch(
        self,
        keyword: str,
        max_pages: int = 3,
        per_page: int = 30,
    ) -> Iterator[dict[str, Any]]:
        """
        キーワード検索で出品情報を収集し、1件ずつ yield する。

        Args:
            keyword: 検索キーワード（例: "Fender American Vintage II 1957 Stratocaster"）
            max_pages: 最大取得ページ数（デフォルト3）
            per_page: 1ページあたりの件数（デフォルト30）

        Yields:
            dict: 出品情報（listings_daily スキーマに準拠）
        """
        logger.info("Digimart fetch: keyword='%s' max_pages=%d", keyword, max_pages)
        total = 0

        for page in range(1, max_pages + 1):
            soup = self._fetch_page(keyword, page)
            if soup is None:
                break

            cards = self._parse_listing_cards(soup)
            if not cards:
                logger.info("Digimart: no cards on page %d, stopping", page)
                break

            for card in cards:
                listing = self._extract_listing(card)
                if listing:
                    total += 1
                    yield listing

            if not self._has_next_page(soup, page):
                break

        logger.info("Digimart: total %d listings fetched for '%s'", total, keyword)


# ── 日付パース ─────────────────────────────────────────────────────────────────

def _parse_date(text: str) -> Optional[str]:
    """
    デジマートの日付文字列を ISO 8601 に変換する。
    例: "2026年5月14日" → "2026-05-14T00:00:00+00:00"
        "2026-05-14" → "2026-05-14T00:00:00+00:00"
    """
    if not text:
        return None
    text = text.strip()

    # ISO 形式
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            pass

    # 日本語形式: 2026年5月14日
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    if m:
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            pass

    # スラッシュ形式: 2026/5/14
    m = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})", text)
    if m:
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            pass

    return None
