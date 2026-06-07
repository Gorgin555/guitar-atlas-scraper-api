"""
GUITAR ATLAS - Reverb Price Guide transactions -> priceguide_transactions
========================================================================

Reverb Price Guide の成約 transaction を listings_daily とは独立した
priceguide_transactions に取り込む。指標計算は TH-15 / CSO スコープ。
"""
from __future__ import annotations

import argparse
import calendar
import logging
import os
import sys
from datetime import date
from typing import Any, Iterable, Optional

from dotenv import load_dotenv

from ingest.fetch_listings import (
    _REVERB_CONDITION_MAP,
    Target,
    get_supabase,
    load_targets,
)
from reverb.client import ReverbAPIError, ReverbClient

logger = logging.getLogger(__name__)


def _norm_condition(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    return _REVERB_CONDITION_MAP.get(raw, raw.lower().replace(" ", "_").replace("-", "_"))


def _parse_amount(price: dict[str, Any] | None) -> tuple[Optional[float], Optional[str]]:
    if not price:
        return None, None
    amount = price.get("amount")
    if amount is None:
        return None, price.get("currency")
    try:
        return float(amount), price.get("currency")
    except (TypeError, ValueError):
        return None, price.get("currency")


def _parse_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _tx_to_row(target: Target, guide: dict, tx: dict) -> dict[str, Any]:
    price_final_amount, price_final_currency = _parse_amount(tx.get("price_final"))
    price_ask_amount, price_ask_currency = _parse_amount(tx.get("price_ask"))
    condition_raw = tx.get("condition")
    price_guide_id = guide.get("id") or guide.get("price_guide_id")
    order_id = tx.get("order_id")

    return {
        "price_guide_id": int(price_guide_id) if price_guide_id is not None else None,
        "order_id": int(order_id) if order_id is not None else None,
        "transaction_date": _parse_date(tx["date"]).isoformat(),
        "product_id": target.product_id,
        "basket": target.basket,
        "matched_confidence": 0.70,
        "price_final_amount": price_final_amount,
        "price_final_currency": price_final_currency,
        "price_final_usd": price_final_amount if price_final_currency == "USD" else None,
        "price_ask_amount": price_ask_amount,
        "price_ask_currency": price_ask_currency,
        "condition_raw": condition_raw,
        "condition": _norm_condition(condition_raw),
        "source": tx.get("source"),
        "guide_make": guide.get("make"),
        "guide_model": guide.get("model"),
        "guide_year": guide.get("year"),
        "guide_finish": guide.get("finish"),
        "guide_title": guide.get("title"),
        "raw_payload": tx,
    }


def _tokens(value: str | None) -> list[str]:
    return [token.lower() for token in (value or "").replace("-", " ").split() if len(token) > 2]


def _guide_matches_target(guide: dict, target: Target) -> bool:
    # TODO(Phase 1.5): basket_v1.yaml の year/finish を使った高精度 matcher へ。
    guide_make = str(guide.get("make") or "").strip().lower()
    guide_model = str(guide.get("model") or "").strip().lower()
    target_brand = (target.brand_name or "").strip().lower()

    brand_match = bool(
        guide_make
        and target_brand
        and (guide_make == target_brand or guide_make in target_brand or target_brand in guide_make)
    )
    if guide_make and target_brand and not brand_match:
        return False

    target_tokens = _tokens(target.model)
    model_match = bool(target_tokens and any(token in guide_model for token in target_tokens))
    return brand_match or model_match


def _build_query(target: Target) -> str:
    return " ".join(part for part in [target.brand_name, target.model] if part)


def _dedupe_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    seen: set[tuple[Any, Any]] = set()
    deduped: list[dict[str, Any]] = []
    dropped = 0
    for row in rows:
        key = (row.get("price_guide_id"), row.get("order_id"))
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        deduped.append(row)
    return deduped, dropped


def fetch_and_upsert_priceguide(
    targets: Iterable[Target],
    *,
    since: Optional[date] = None,
    max_guides_per_model: int = 8,
    guide_per_page: int = 24,
    guide_max_pages: int = 1,
    tx_per_page: int = 50,
    tx_max_pages: int = 2,
    dry_run: bool = False,
) -> dict[str, int]:
    sb = get_supabase()
    client = ReverbClient.from_env()
    counts = {"targets": 0, "guides": 0, "transactions": 0, "errors": 0}

    for target in targets:
        counts["targets"] += 1
        query = _build_query(target)
        logger.info("[%s] %-3s %s", target.basket or "-", target.basket_id, query)
        try:
            guides: list[dict[str, Any]] = []
            for guide in client.search_price_guides(
                query=query,
                per_page=guide_per_page,
                max_pages=guide_max_pages,
            ):
                if _guide_matches_target(guide, target):
                    guides.append(guide)
                if len(guides) >= max_guides_per_model:
                    break

            rows: list[dict[str, Any]] = []
            for guide in guides:
                guide_id = guide.get("id") or guide.get("price_guide_id")
                if guide_id is None:
                    continue
                counts["guides"] += 1
                for tx in client.get_price_guide_transactions(
                    guide_id,
                    per_page=tx_per_page,
                    max_pages=tx_max_pages,
                ):
                    tx_date = _parse_date(tx["date"])
                    if since and tx_date < since:
                        continue
                    rows.append(_tx_to_row(target, guide, tx))

            if rows:
                rows, dropped = _dedupe_rows(rows)
                if dropped:
                    logger.debug("  priceguide dedupe: dropped %d duplicate rows", dropped)

            counts["transactions"] += len(rows)
            if dry_run:
                logger.info("  [dry-run] %d transactions (not inserted)", len(rows))
                continue

            if rows:
                sb.table("priceguide_transactions").upsert(
                    rows,
                    on_conflict="price_guide_id,order_id",
                ).execute()
            logger.info("  -> upserted %d transactions", len(rows))
        except ReverbAPIError as e:
            counts["errors"] += 1
            logger.warning("  x Reverb error for %s: %s", target.basket_id, e)
        except Exception as e:
            counts["errors"] += 1
            logger.exception("  x Unexpected error for %s: %r", target.basket_id, e)

    return counts


def _subtract_months(base: date, months: int) -> date:
    month_index = base.year * 12 + (base.month - 1) - months
    year = month_index // 12
    month = month_index % 12 + 1
    day = min(base.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _resolve_since(args: argparse.Namespace) -> date | None:
    if args.since:
        return date.fromisoformat(args.since)
    months = args.backfill_months
    if months is None:
        months = int(os.environ.get("PRICEGUIDE_BACKFILL_MONTHS", "12"))
    return _subtract_months(date.today(), months)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch Reverb Price Guide transactions and upsert into priceguide_transactions."
    )
    parser.add_argument("--basket", choices=["MFI", "VFI", "BPI"], default=None)
    parser.add_argument("--limit-models", type=int, default=None)
    parser.add_argument("--since", default=None)
    parser.add_argument("--backfill-months", type=int, default=None)
    parser.add_argument(
        "--max-guides-per-model",
        type=int,
        default=int(os.environ.get("PRICEGUIDE_MAX_GUIDES_PER_MODEL", "8")),
    )
    parser.add_argument("--guide-per-page", type=int, default=24)
    parser.add_argument("--guide-max-pages", type=int, default=1)
    parser.add_argument("--tx-per-page", type=int, default=50)
    parser.add_argument("--tx-max-pages", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    targets = load_targets(basket=args.basket, limit=args.limit_models)
    if not targets:
        print("x No targets loaded. products テーブルに対象モデルが入っているか確認してください。")
        return 1

    since = _resolve_since(args)
    logger.info("Loaded %d targets (basket=%s, since=%s)", len(targets), args.basket or "ALL", since)
    counts = fetch_and_upsert_priceguide(
        targets,
        since=since,
        max_guides_per_model=args.max_guides_per_model,
        guide_per_page=args.guide_per_page,
        guide_max_pages=args.guide_max_pages,
        tx_per_page=args.tx_per_page,
        tx_max_pages=args.tx_max_pages,
        dry_run=args.dry_run,
    )
    logger.info("Done. %s", counts)
    return 0 if counts["errors"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
