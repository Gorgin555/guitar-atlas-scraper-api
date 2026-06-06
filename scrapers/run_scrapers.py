"""
GUITAR ATLAS — Scrapers CLI ランナー
======================================
デジマート / Yahoo Auctions スクレイパーを実行し、
basket_v1.yaml の 58 モデル（active）+ 受動的収集対象に対して
listings_daily に upsert する。

Usage:
    cd ~/Desktop/ATLAS/code
    source .venv/bin/activate

    # 全ソース・全バスケット（フルラン）
    python -m scrapers.run_scrapers

    # デジマートのみ、MFI バスケット、dry-run
    python -m scrapers.run_scrapers --source digimart --basket MFI --dry-run

    # Yahoo Auctions のみ、受動的収集のみ
    python -m scrapers.run_scrapers --source yahoo --passive-only

    # Pedals カテゴリのみ受動的収集
    python -m scrapers.run_scrapers --passive-only --categories pedals

    # 1モデルだけ動作確認
    python -m scrapers.run_scrapers --limit-models 2 --dry-run

CLI オプション:
    --source        digimart | yahoo | all (default: all)
    --basket        MFI | VFI | BPI (default: all active)
    --limit-models  active モデルの上限数（デバッグ用）
    --passive-only  受動的収集のみ実行
    --no-passive    受動的収集をスキップ
    --categories    pedals,acoustic,amps,jbi_japan_boutique のカンマ区切り
    --max-pages     各キーワードの最大ページ数 (default: 3)
    --dry-run       DB 書き込みをスキップして件数だけ表示
    --log-level     DEBUG | INFO | WARNING (default: INFO)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv

# .env 読み込み（scrapers/ の 1つ上の code/ に .env がある）
_CODE_DIR = Path(__file__).parents[1]
load_dotenv(_CODE_DIR / ".env")


def _get_supabase():
    from supabase import create_client
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not (url and key):
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY missing in .env")
    return create_client(url, key)


def _load_active_targets(basket: str | None, limit: int | None) -> list[dict]:
    """
    products テーブルから active モデル（is_passive=False）を取得する。
    fetch_listings.py の load_targets と同じクエリ。
    """
    sb = _get_supabase()
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
        b = bm[0]["basket"] if bm else None
        targets.append({
            "product_id": r["product_id"],
            "basket_id": r["basket_id"],
            "brand_name": r["brand_name"],
            "model": r["model"],
            "year_range_str": r.get("year_range_str"),
            "basket": b,
        })
    targets.sort(key=lambda t: (t["basket"] or "", t["basket_id"]))
    if limit:
        targets = targets[:limit]
    return targets


def _build_keyword(target: dict) -> str:
    """モデルの検索キーワードを組み立てる。"""
    base = f"{target['brand_name']} {target['model']}"
    if target.get("year_range_str"):
        base = f"{base} {target['year_range_str']}"
    return base


def _upsert_rows(rows: list[dict], dry_run: bool) -> int:
    if not rows:
        return 0
    if dry_run:
        logging.getLogger(__name__).info("[dry-run] Would upsert %d rows", len(rows))
        return len(rows)
    sb = _get_supabase()
    sb.table("listings_daily").upsert(
        rows,
        on_conflict="source,source_listing_id,snapshot_date",
    ).execute()
    return len(rows)


# ── メイン収集ループ（active モデル） ─────────────────────────────────────────

def run_active(
    sources: list[str],
    targets: list[dict],
    max_pages: int,
    dry_run: bool,
    yahoo_mode: str = "both",
) -> dict[str, int]:
    """
    58 active モデルに対して各ソースでスクレイピングし listings_daily に upsert。
    """
    from .digimart import DigimartScraper
    from .yahoo_auctions import YahooAuctionsScraper
    from .matcher import ProductCatalog

    logger = logging.getLogger(__name__)

    scrapers: dict[str, object] = {}
    if "digimart" in sources:
        scrapers["digimart"] = DigimartScraper()
    if "yahoo" in sources:
        scrapers["yahoo"] = YahooAuctionsScraper()

    # product catalog（matcher）を一度だけ読み込む
    try:
        catalog = ProductCatalog.from_supabase()
    except Exception as e:
        logger.warning("Could not load ProductCatalog: %s — confidence will be 0.80", e)
        catalog = None

    snapshot_date = datetime.now(timezone(timedelta(hours=9))).date().isoformat()  # JST (Asia/Tokyo) 明示, tzdata/TZ env 非依存
    counts = {"targets": 0, "listings": 0, "errors": 0}

    for target in targets:
        counts["targets"] += 1
        keyword = _build_keyword(target)
        logger.info(
            "[%s] %-3s %s → '%s'",
            target.get("basket", "—"), target["basket_id"], target["brand_name"], keyword,
        )

        for source_name, scraper in scrapers.items():
            rows_to_upsert = []
            try:
                if source_name == "digimart":
                    gen = scraper.fetch(keyword, max_pages=max_pages)
                else:  # yahoo
                    if yahoo_mode == "active":
                        gen = scraper.fetch(keyword, mode="active", max_pages=max_pages)
                    elif yahoo_mode == "sold":
                        gen = scraper.fetch(keyword, mode="sold", max_pages=max_pages)
                    else:
                        gen_active = scraper.fetch(keyword, mode="active", max_pages=max_pages)
                        gen_sold = scraper.fetch(keyword, mode="sold", max_pages=1)
                        import itertools
                        gen = itertools.chain(gen_active, gen_sold)

                for listing in gen:
                    listing["snapshot_date"] = snapshot_date

                    # product_id のマッチング
                    if catalog:
                        m = catalog.match(listing["title"], hint_brand=target["brand_name"])
                        if m and m.confidence >= 0.50:
                            listing["product_id"] = m.product_id
                            listing["matched_confidence"] = m.confidence
                        else:
                            # 弱いマッチ → target の product_id を直接使う（confidence 低め）
                            listing["product_id"] = target["product_id"]
                            listing["matched_confidence"] = 0.55
                    else:
                        listing["product_id"] = target["product_id"]
                        listing["matched_confidence"] = 0.80

                    rows_to_upsert.append(listing)

                upserted = _upsert_rows(rows_to_upsert, dry_run)
                counts["listings"] += upserted
                logger.info(
                    "  [%s] → %d listings upserted", source_name, upserted
                )

            except Exception as e:
                counts["errors"] += 1
                logger.warning("  [%s] error for %s: %s", source_name, target["basket_id"], e)

    return counts


def _live_brand_keyword(target: dict) -> str:
    return " ".join((target.get("brand_name") or "").split())


def _load_live_brand_targets(limit: int | None = None) -> list[dict]:
    """
    products テーブルから brand-level live tracker を取得する。
    """
    sb = _get_supabase()
    res = (
        sb.table("products")
        .select("product_id, basket_id, brand_name, model, year_range_str, ingestion_priority, basket_membership(basket)")
        .eq("is_live_observed", True)
        .order("ingestion_priority", desc=False)
        .execute()
    )
    rows = res.data or []
    targets = []
    for row in rows:
        memberships = row.get("basket_membership") or []
        if memberships:
            continue
        targets.append({
            "product_id": row["product_id"],
            "basket_id": row.get("basket_id") or "LW",
            "brand_name": row["brand_name"],
            "model": row["model"],
            "year_range_str": row.get("year_range_str"),
            "basket": None,
        })
    if limit:
        targets = targets[:limit]
    return targets


def run_active_live_brands(
    sources: list[str],
    targets: list[dict],
    max_pages: int,
    dry_run: bool,
) -> dict[str, int]:
    """
    Digimart collection for brand-level live tracker products.
    """
    from .digimart import DigimartScraper

    logger = logging.getLogger(__name__)
    active_sources = [source for source in sources if source == "digimart"]
    scrapers: dict[str, object] = {}
    if "digimart" in active_sources:
        scrapers["digimart"] = DigimartScraper()

    snapshot_date = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
    counts = {"targets": 0, "listings": 0, "errors": 0}
    seen: set[tuple[str, str, str]] = set()

    for target in targets:
        counts["targets"] += 1
        keyword = _live_brand_keyword(target)
        logger.info(
            "[Live Brand] %-3s %s -> '%s'",
            target["basket_id"], target["brand_name"], keyword,
        )

        for source_name, scraper in scrapers.items():
            rows_to_upsert = []
            try:
                gen = scraper.fetch(keyword, max_pages=max_pages)
                for listing in gen:
                    listing["snapshot_date"] = snapshot_date
                    listing["product_id"] = target["product_id"]
                    listing["matched_confidence"] = 0.55

                    key = (
                        source_name,
                        str(listing.get("source_listing_id") or ""),
                        snapshot_date,
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    rows_to_upsert.append(listing)

                upserted = _upsert_rows(rows_to_upsert, dry_run)
                counts["listings"] += upserted
                logger.info("  [%s] -> %d listings upserted", source_name, upserted)

            except Exception as e:
                counts["errors"] += 1
                logger.warning("  [%s] error for %s: %s", source_name, target["basket_id"], e)

    return counts


# ── CLI エントリポイント ───────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="GUITAR ATLAS — デジマート / Yahoo Auctions スクレイパー"
    )
    parser.add_argument(
        "--source", choices=["digimart", "yahoo", "all"], default="all",
        help="スクレイピングソース (default: all)",
    )
    parser.add_argument(
        "--basket", choices=["MFI", "VFI", "BPI"], default=None,
        help="Active バスケット絞り込み (default: 全バスケット)",
    )
    parser.add_argument(
        "--limit-models", type=int, default=None,
        help="Active モデルの上限数（動作確認用）",
    )
    parser.add_argument(
        "--passive-only", action="store_true",
        help="受動的収集のみ実行",
    )
    parser.add_argument(
        "--no-passive", action="store_true",
        help="受動的収集をスキップ",
    )
    parser.add_argument(
        "--categories", type=str, default=None,
        help="受動的収集カテゴリをカンマ区切りで指定 (例: pedals,acoustic)",
    )
    parser.add_argument(
        "--max-pages", type=int, default=3,
        help="各キーワードの最大ページ数 (default: 3)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="DB 書き込みをスキップ（動作確認用）",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger(__name__)

    # ソースリスト
    sources = ["digimart", "yahoo"] if args.source == "all" else [args.source]

    # カテゴリリスト（受動的収集）
    categories = args.categories.split(",") if args.categories else None

    logger.info(
        "=== GUITAR ATLAS Scrapers START ===\n"
        "  sources=%s  basket=%s  max_pages=%d  dry_run=%s",
        sources, args.basket or "ALL", args.max_pages, args.dry_run,
    )

    total_counts: dict[str, int] = {"listings": 0, "errors": 0}

    # ── Active モデルの収集 ──────────────────────────────────────────────────
    if not args.passive_only:
        logger.info("--- Active models collection ---")
        targets = _load_active_targets(args.basket, args.limit_models)
        if not targets:
            logger.error("No active targets found. products テーブルを確認してください。")
            return 1
        logger.info("Active targets: %d models", len(targets))

        active_counts = run_active(sources, targets, args.max_pages, args.dry_run)
        total_counts["listings"] += active_counts["listings"]
        total_counts["errors"] += active_counts["errors"]
        logger.info("Active collection done: %s", active_counts)

    # ── 受動的収集（Pedals / Acoustic / Amps / JBI）───────────────────────
    if not args.no_passive:
        logger.info("--- Passive collection (Pedals/Acoustic/Amps/JBI) ---")
        from .passive_collector import PassiveCollector
        collector = PassiveCollector(dry_run=args.dry_run)
        passive_counts = collector.run(
            sources=sources,
            max_pages_per_brand=min(args.max_pages, 2),  # passive は控えめに
            categories=categories,
        )
        total_counts["listings"] += passive_counts["total_listings"]
        total_counts["errors"] += passive_counts["errors"]
        logger.info("Passive collection done: %s", passive_counts)

    # ── サマリー ────────────────────────────────────────────────────────────
    logger.info(
        "=== GUITAR ATLAS Scrapers DONE ===\n"
        "  total_listings=%d  errors=%d",
        total_counts["listings"], total_counts["errors"],
    )

    return 0 if total_counts["errors"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
