"""
GUITAR ATLAS — Yahoo Auctions スクレイパー
==========================================
https://auctions.yahoo.co.jp/ から公開オークション情報（数値のみ）を収集し、
Supabase listings_daily に upsert する。

■ 収集データ（数値・テキスト限定）
  - タイトル、現在価格/即決価格（JPY）、コンディション
  - 終了日時（出品中）/ 落札日時（終了）
  - 出品者種別（個人/法人）、出品地
  - 画像転載なし（CLO はぐりん監修 memory/legal/scraping_rules.md）

■ URL 構造
  出品中: https://auctions.yahoo.co.jp/search/search
          ?p={keyword}
          &va={keyword}
          &exflg=1            # exact phrase
          &b=1                # offset

  終了済: Phase 1 defer (robots /closedsearch/ strict)

■ HTML セレクター（2026-05 時点）
  Yahoo Auctions は server-side rendering。
  複数フォールバックで HTML 変更への耐性を持つ。

■ 法務メモ（CLO はぐりん）
  Yahoo Japan 利用規約はスクレイピングを原則禁止。
  対応: robots.txt で Disallow でないパスのみ / 公開データのみ / 商業転載なし。
  リスクレベル: 中程度 → Yahoo から停止要請があれば即停止。

Usage:
    from scrapers.yahoo_auctions import YahooAuctionsScraper
    scraper = YahooAuctionsScraper()
    listings = scraper.fetch("Fender Stratocaster", mode="active")
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

YAHOO_AUCTIONS_BASE = "https://auctions.yahoo.co.jp"

# ── コンディション正規化 ─────────────────────────────────────────────────────────

_CONDITION_MAP = {
    "新品": "brand_new",
    "未使用": "mint",
    "未使用に近い": "excellent",
    "目立つ傷や汚れなし": "very_good",
    "やや傷や汚れあり": "good",
    "傷や汚れあり": "fair",
    "全体的に状態が悪い": "poor",
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
    return "used"


# ── 出品者種別の推定 ────────────────────────────────────────────────────────────

def _infer_seller_type(seller_name: Optional[str], rating_text: Optional[str]) -> str:
    """
    ヤフオクの出品者が法人（ディーラー）か個人か推定する。
    確実な判断は難しいため、ヒューリスティックを使う。
    """
    if not seller_name:
        return "unknown"
    # 法人っぽいキーワード
    dealer_kw = ["楽器", "ギター", "music", "guitar", "store", "shop", "ltd",
                 "株式", "有限", "合同", "corp", "inc", "co.", "guitars"]
    name_lower = seller_name.lower()
    if any(kw in name_lower for kw in dealer_kw):
        return "dealer"
    return "individual"


# ── YahooAuctionsScraper ──────────────────────────────────────────────────────

class YahooAuctionsScraper(BaseScraper):
    """
    Yahoo Auctions 向けスクレイパー。

    BaseScraper の robots.txt チェック・レート制限・画像フィルタを継承。
    mode="active": 出品中のオークション
    mode="sold": 終了済み（落札）オークション
    """

    # ── 検索 URL 構築 ──────────────────────────────────────────────────────

    def _search_url(
        self, keyword: str, mode: str = "active", page: int = 1, per_page: int = 50
    ) -> str:
        # b= は offset (1, 51, 101, ...)。per_page は互換用に受けるが、
        # robots Disallow 対象の n= は URL に付与しない。
        offset = (page - 1) * per_page + 1
        params = {
            "p": keyword,
            "va": keyword,
            "exflg": "1",
            "b": str(offset),
        }
        if mode == "sold":
            raise ScraperError(
                "Yahoo sold collection is deferred in Phase 1 (robots /closedsearch/ strict). "
                "See memory/legal/yahoo_robots_clo_ruling_2026-06-05.md."
            )

        return f"{YAHOO_AUCTIONS_BASE}/search/search?{urlencode(params, quote_via=quote)}"

    # ── ページ取得 ─────────────────────────────────────────────────────────

    def _fetch_page(self, keyword: str, mode: str, page: int) -> Optional[BeautifulSoup]:
        url = self._search_url(keyword, mode=mode, page=page)
        try:
            resp = self._get(url)
            return BeautifulSoup(resp.text, "lxml")
        except Exception as e:
            logger.warning("Yahoo Auctions page fetch error (keyword=%s, page=%d): %s", keyword, page, e)
            return None

    # ── HTML から出品リストを抽出 ─────────────────────────────────────────────

    def _parse_listing_items(self, soup: BeautifulSoup) -> list[BeautifulSoup]:
        """
        出品アイテム要素のリストを返す。
        Yahoo Auctions の HTML 構造変化に対応するため複数セレクターを試す。
        """
        selectors = [
            "li.Product",           # Products list
            "li.b-list-item",       # variant
            "div.Product",          # div 版
            ".auction-item",        # 旧型
            "li[data-auction-id]",  # data 属性
            "li[data-auction]",     # data 属性 variant
            ".SearchResult li",     # 汎用
        ]
        for sel in selectors:
            items = soup.select(sel)
            if items:
                logger.debug("Yahoo: found %d items with selector '%s'", len(items), sel)
                return items

        # フォールバック: 入札件数や価格要素の親
        bid_els = soup.select(".bids, .bid-count, .auction-price")
        if bid_els:
            return [el.parent for el in bid_els if el.parent]

        logger.warning("Yahoo Auctions: no listing items found on page")
        return []

    def _extract_listing(
        self, item: BeautifulSoup, mode: str
    ) -> Optional[dict[str, Any]]:
        """
        出品アイテム HTML から数値・テキストデータを抽出する。
        画像 URL は取得しない（CLO 法務ガイドライン）。
        """
        try:
            # ── タイトル ─────────────────────────────────────────────
            title = None
            for sel in ["h3.Product__title", ".Product__title", "h3",
                        ".item-title", ".title", "a.auction-title"]:
                el = item.select_one(sel)
                if el:
                    title = el.get_text(strip=True)
                    break

            if not title:
                return None

            # ── オークション ID / URL ─────────────────────────────────
            src_id = None
            src_url = None

            # data 属性から ID を取得
            src_id = (item.get("data-auction-id") or item.get("data-auction")
                      or item.get("data-item-id"))

            # リンクから URL と ID を取得
            link = item.select_one("a[href*='yahoo.co.jp']") or item.select_one("a[href]")
            if link:
                href = link.get("href", "")
                src_url = href if href.startswith("http") else YAHOO_AUCTIONS_BASE + href
                # URL から auction ID を抽出
                # 例: /item/a123456789/ → a123456789
                m = re.search(r"/item/([a-z0-9]+)/?", href)
                if m and not src_id:
                    src_id = m.group(1)

            if not src_id:
                src_id = f"yau_{abs(hash(title)) % 10**10}"

            # ── 価格 ──────────────────────────────────────────────────
            current_price = None
            buynow_price = None

            # 現在の価格（入札価格 or 開始価格）
            for sel in [".Product__price", ".b-price", ".current-price",
                        "[data-value]", ".price", "span.aucPrice"]:
                el = item.select_one(sel)
                if el:
                    price_text = el.get("data-value") or el.get_text(strip=True)
                    current_price = self.parse_jpy(price_text)
                    if current_price:
                        break

            # 即決価格
            for sel in [".Product__fixedPrice", ".buyPrice", ".fixed-price",
                        ".buynow-price", "span.aucBuyPrice"]:
                el = item.select_one(sel)
                if el:
                    buynow_price = self.parse_jpy(el.get_text(strip=True))
                    break

            # 価格が取れなかった場合はスキップ
            price_local = buynow_price or current_price

            # ── コンディション ────────────────────────────────────────
            condition_raw = None
            for sel in [".Product__condition", ".condition", ".item-state",
                        "[data-condition]", ".aucCondition"]:
                el = item.select_one(sel)
                if el:
                    condition_raw = el.get_text(strip=True)
                    break

            condition = _normalize_condition(condition_raw) if condition_raw else None

            # ── 終了日時 / 落札日時 ─────────────────────────────────
            ended_at = None
            is_sold = (mode == "sold")

            for sel in ["time", ".Product__endTime", ".end-time",
                        "[data-remaining-time]", ".aucEndtime"]:
                el = item.select_one(sel)
                if el:
                    dt_text = el.get("datetime") or el.get("data-end") or el.get_text(strip=True)
                    ended_at = _parse_yahoo_date(dt_text)
                    if ended_at:
                        break

            listed_at = None if is_sold else ended_at  # 出品中は終了時刻を listed_at 扱い
            sold_at = ended_at if is_sold else None

            # ── 出品者 ────────────────────────────────────────────────
            seller_name = None
            for sel in [".Product__seller", ".seller", ".aucBidder",
                        "a[href*='/seller/']", ".seller-id"]:
                el = item.select_one(sel)
                if el:
                    seller_name = el.get_text(strip=True)
                    break

            seller_type = _infer_seller_type(seller_name, None)

            # ── 出品地（都道府県）───────────────────────────────────
            location_region = None
            for sel in [".Product__location", ".location", ".area",
                        "[data-location]", ".aucLocation"]:
                el = item.select_one(sel)
                if el:
                    location_region = el.get_text(strip=True)
                    break

            # ── raw_payload（画像 URL 除去済み）─────────────────────
            raw = {
                "title": title,
                "current_price": str(current_price) if current_price else None,
                "buynow_price": str(buynow_price) if buynow_price else None,
                "condition_raw": condition_raw,
                "ended_at": ended_at,
                "seller_name": seller_name,
                "location": location_region,
                "src_url": src_url,
            }
            clean_raw = self.strip_images(raw)

            return {
                "source": "yahoo_auctions",
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
                "sold_at": sold_at,
                "is_sold": is_sold,
                "location_country": "JP",
                "location_region": location_region,
                "seller_type": seller_type,
                "seller_name": seller_name,
                "description": None,  # 詳細ページは取得しない（レート制限保護）
                "raw_payload": clean_raw,
            }

        except Exception as e:
            logger.debug("Yahoo item parse error: %s", e)
            return None

    # ── ページネーション確認 ──────────────────────────────────────────────

    def _has_next_page(self, soup: BeautifulSoup) -> bool:
        for sel in ["a.next", "a[rel='next']", ".pagination a.next",
                    "a[href*='b='][aria-label*='次']", ".Pager__next"]:
            if soup.select_one(sel):
                return True
        return False

    # ── 公開インターフェース ──────────────────────────────────────────────

    def fetch(
        self,
        keyword: str,
        mode: str = "active",
        max_pages: int = 3,
        per_page: int = 50,
    ) -> Iterator[dict[str, Any]]:
        """
        キーワード検索でオークション情報を収集し、1件ずつ yield する。

        Args:
            keyword: 検索キーワード
            mode: "active"（出品中）or "sold"（落札済み）
            max_pages: 最大取得ページ数（デフォルト3）
            per_page: 1ページあたりの件数（デフォルト50）

        Yields:
            dict: 出品情報（listings_daily スキーマに準拠）
        """
        logger.info(
            "Yahoo Auctions fetch: keyword='%s' mode=%s max_pages=%d",
            keyword, mode, max_pages,
        )
        total = 0

        for page in range(1, max_pages + 1):
            soup = self._fetch_page(keyword, mode, page)
            if soup is None:
                break

            items = self._parse_listing_items(soup)
            if not items:
                logger.info("Yahoo: no items on page %d, stopping", page)
                break

            for item in items:
                listing = self._extract_listing(item, mode)
                if listing:
                    total += 1
                    yield listing

            if not self._has_next_page(soup):
                break

        logger.info(
            "Yahoo Auctions: total %d listings fetched for '%s' (mode=%s)",
            total, keyword, mode,
        )


# ── 日付パース ─────────────────────────────────────────────────────────────────

def _parse_yahoo_date(text: str) -> Optional[str]:
    """
    Yahoo Auctions の日付文字列を ISO 8601 に変換する。

    例:
      "5月14日 22:30" → 現在年を補完して ISO 変換
      "2026.05.14 22:30" → ISO 変換
      "2026-05-14T22:30:00" → そのまま返す
    """
    if not text:
        return None
    text = text.strip()

    # ISO 形式（そのまま）
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})", text)
    if m:
        return text[:19] + "+00:00"

    # YYYY.MM.DD HH:MM
    m = re.search(r"(\d{4})\.(\d{2})\.(\d{2})\s+(\d{2}):(\d{2})", text)
    if m:
        try:
            dt = datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4)), int(m.group(5)), tzinfo=timezone.utc,
            )
            return dt.isoformat()
        except ValueError:
            pass

    # MM月DD日 HH:MM（年なし → 現在年）
    m = re.search(r"(\d{1,2})月(\d{1,2})日\s*(\d{2}):(\d{2})", text)
    if m:
        try:
            now = datetime.now(timezone.utc)
            dt = datetime(
                now.year, int(m.group(1)), int(m.group(2)),
                int(m.group(3)), int(m.group(4)), tzinfo=timezone.utc,
            )
            return dt.isoformat()
        except ValueError:
            pass

    # 残り時間表示（"X日Y時間" など）は絶対日時ではないのでスキップ
    return None
