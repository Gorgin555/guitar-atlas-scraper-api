"""
GUITAR ATLAS - Reverb listings → listings_daily (PostgREST 経由)
================================================================

products テーブルに登録された Phase 1 対象モデル（MFI/VFI/BPI = 58モデル）について、
Reverb API から live listings を取得し、`listings_daily` に upsert する。

設計（v0.2 - 2026-05-10 ブラウザ駆動の頓挫を受けた書き直し）:
  - supabase-py の PostgREST クライアント経由で読み書き（**DB password 不要**）
  - `.env` に必要な変数: SUPABASE_URL / SUPABASE_SERVICE_KEY / REVERB_PERSONAL_TOKEN
  - 1 req/sec の自主レート制限
  - source + source_listing_id + snapshot_date の UNIQUE で同日再実行は重複しない

Usage:
    cd ~/Desktop/ATLAS/code
    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    python -m ingest.fetch_listings --basket BPI --limit-models 5     # 5 件試走（約 30秒）
    python -m ingest.fetch_listings                                    # 58モデル全件（約 1〜2 分）
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from dotenv import load_dotenv

from reverb.client import ReverbAPIError, ReverbClient

logger = logging.getLogger(__name__)


@dataclass
class Target:
    product_id: str
    basket_id: str
    brand_name: str
    model: str
    year_range_str: Optional[str]
    basket: Optional[str]


# ---------------------------------------------------------------------------
# Supabase client (PostgREST)
# ---------------------------------------------------------------------------

def get_supabase():
    from supabase import create_client
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not (url and key):
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY must be set in .env")
    return create_client(url, key)


def load_targets(basket: Optional[str] = None, limit: Optional[int] = None) -> list[Target]:
    """
    products + basket_membership を JOIN して非 passive モデルを取得。
    PostgREST の埋込クエリで basket_membership を取り込む。
    """
    sb = get_supabase()
    q = (sb.table("products")
            .select("product_id, basket_id, brand_name, model, year_range_str, "
                    "basket_membership!inner(basket)")
            .eq("is_passive", False))
    if basket:
        q = q.eq("basket_membership.basket", basket)
    res = q.execute()
    rows = res.data or []
    targets = []
    for r in rows:
        bm = r.get("basket_membership") or []
        # basket_membership!inner は配列で返る
        b = (bm[0]["basket"] if bm else None)
        targets.append(Target(
            product_id=r["product_id"],
            basket_id=r["basket_id"],
            brand_name=r["brand_name"],
            model=r["model"],
            year_range_str=r.get("year_range_str"),
            basket=b,
        ))
    targets.sort(key=lambda t: (t.basket or "", t.basket_id))
    if limit:
        targets = targets[:limit]
    return targets


# ---------------------------------------------------------------------------
# Reverb listing → row dict
# ---------------------------------------------------------------------------

def _parse_price(price: dict[str, Any] | None) -> tuple[Optional[float], Optional[str]]:
    if not price:
        return None, None
    amount = price.get("amount") or price.get("amount_cents")
    if amount is None:
        return None, price.get("currency")
    try:
        return float(amount), price.get("currency")
    except (TypeError, ValueError):
        return None, price.get("currency")


_REVERB_CONDITION_MAP = {
    "Mint": "mint", "Excellent": "excellent", "Very Good": "very_good",
    "Good": "good", "Fair": "fair", "Poor": "poor",
    "Non Functioning": "non_functioning", "B-Stock": "b_stock", "Brand New": "brand_new",
}


def _to_row(target: Target, listing: dict[str, Any], snapshot_date: str) -> dict[str, Any]:
    src_id = str(listing.get("id") or listing.get("listing_id") or "")
    price_amount, currency = _parse_price(listing.get("price"))
    price_usd_amount = price_amount if currency == "USD" else None
    cond_raw = ((listing.get("condition") or {}).get("display_name")
                or listing.get("condition_slug") or None)
    cond_norm = _REVERB_CONDITION_MAP.get(cond_raw or "")
    location_country = ((listing.get("shipping") or {}).get("local_pickup_country_code")
                        or (listing.get("location") or {}).get("country_code"))
    location_region = (listing.get("location") or {}).get("region")
    seller = listing.get("shop") or {}
    src_url = ((listing.get("_links") or {}).get("web") or {}).get("href")

    return {
        "source": "reverb",
        "source_listing_id": src_id,
        "source_url": src_url,
        "product_id": target.product_id,
        "matched_confidence": 0.80,
        "listed_at": listing.get("created_at"),
        "sold_at": listing.get("sold_at"),
        "is_sold": bool(listing.get("sold_at")),
        "price_local": price_amount,
        "currency": currency,
        "price_usd": price_usd_amount,
        "condition": cond_norm,
        "condition_raw": cond_raw,
        "condition_tags": [],
        "location_country": location_country,
        "location_region": location_region,
        "seller_type": "dealer" if seller else "unknown",
        "seller_name": seller.get("name"),
        "title": listing.get("title"),
        "description": listing.get("description"),
        "raw_payload": listing,                       # PostgREST が JSONB に変換
        "snapshot_date": snapshot_date,
    }


def _build_query(target: Target) -> str:
    base = f"{target.brand_name} {target.model}"
    if target.year_range_str:
        base = f"{base} {target.year_range_str}"
    return base


# ---------------------------------------------------------------------------
# Upsert via PostgREST
# ---------------------------------------------------------------------------

def fetch_and_upsert(
    targets: Iterable[Target],
    *,
    per_page: int = 50,
    max_pages: int = 2,
    state: str = "live",
    dry_run: bool = False,
) -> dict[str, int]:
    sb = get_supabase()
    client = ReverbClient.from_env()
    snapshot_date = datetime.now(timezone.utc).astimezone().date().isoformat()
    counts = {"targets": 0, "listings": 0, "errors": 0}

    for t in targets:
        counts["targets"] += 1
        query = _build_query(t)
        logger.info("[%s] %-3s %s", t.basket or "—", t.basket_id, query)
        try:
            rows = []
            for listing in client.search_listings(
                query=query, state=state, per_page=per_page, max_pages=max_pages,
            ):
                rows.append(_to_row(t, listing, snapshot_date))
            if dry_run:
                logger.info("  [dry-run] %d listings (not inserted)", len(rows))
                continue

            # ── バッチ内 dedupe（source_listing_id × snapshot_date で一意化）─────
            # 同一 Reverb listing が複数 target に紐づく場合に upsert が ON CONFLICT
            # 内で同一キーを2度更新しようとして 409 を返すのを防ぐ。
            if rows:
                seen: set[tuple[str, str]] = set()
                deduped: list[dict[str, Any]] = []
                dropped = 0
                for row in rows:
                    key = (row.get("source_listing_id") or "", row.get("snapshot_date") or "")
                    if key in seen:
                        dropped += 1
                        continue
                    seen.add(key)
                    deduped.append(row)
                rows = deduped
                if dropped:
                    logger.debug("  dedupe: dropped %d duplicate rows", dropped)

            if rows:
                # PostgREST upsert with on_conflict for our composite UNIQUE
                sb.table("listings_daily").upsert(
                    rows,
                    on_conflict="source,source_listing_id,snapshot_date",
                ).execute()
                counts["listings"] += len(rows)
            logger.info("  → upserted %d listings", len(rows))
        except ReverbAPIError as e:
            counts["errors"] += 1
            logger.warning("  ✗ Reverb error for %s: %s", t.basket_id, e)
        except Exception as e:
            counts["errors"] += 1
            logger.exception("  ✗ Unexpected error for %s: %r", t.basket_id, e)

    return counts


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Reverb listings and upsert into listings_daily.")
    parser.add_argument("--basket", choices=["MFI", "VFI", "BPI"], default=None)
    parser.add_argument("--limit-models", type=int, default=None)
    parser.add_argument("--per-page", type=int, default=50)
    parser.add_argument("--max-pages", type=int, default=2)
    parser.add_argument("--state", default="live")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    targets = load_targets(basket=args.basket, limit=args.limit_models)
    if not targets:
        print("✗ No targets loaded. products テーブルに 58 モデルが入っているか確認してください。")
        return 1

    logger.info("Loaded %d targets (basket=%s)", len(targets), args.basket or "ALL")
    counts = fetch_and_upsert(
        targets,
        per_page=args.per_page,
        max_pages=args.max_pages,
        state=args.state,
        dry_run=args.dry_run,
    )
    logger.info("Done. %s", counts)
    return 0 if counts["errors"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
