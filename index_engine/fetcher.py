"""
GUITAR ATLAS — Index Engine: Supabase データ取得層
===================================================
listings_daily / mentions から指標算出に必要なデータを取得する。

設計方針:
  - supabase-py（PostgREST）を使用
  - 7d ウィンドウ × 2本（current / prior）でデルタ算出
  - MentionMomentum は Phase 1 では 0 返却（mentions テーブル未蓄積）
  - VFI は condition_tags で all_original 系統を分離
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Supabase クライアント
# ─────────────────────────────────────────────────────────────

def _get_supabase():
    from supabase import create_client
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not (url and key):
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY が .env に未設定")
    return create_client(url, key)


# ─────────────────────────────────────────────────────────────
# データ構造
# ─────────────────────────────────────────────────────────────

@dataclass
class BasketTarget:
    """バスケット内の 1モデルのフェッチ対象情報"""
    product_id:    str
    basket_id:     str
    brand_name:    str
    model:         str
    basket:        str            # MFI / VFI / BPI
    weight:        float
    year_range_str: Optional[str] = None


@dataclass
class WindowStats:
    """7日間ウィンドウの集計値"""
    date_from:    date
    date_to:      date
    n_listings:   int
    n_sold:       int
    avg_price_usd: Optional[float]
    n_us:         int   # US出品数
    n_jp:         int   # JP出品数
    is_all_original_filtered: bool = False  # VFI_AO 用フィルタ済みか


@dataclass
class ModelWindowData:
    """1モデルの current + prior 2ウィンドウデータ"""
    product_id: str
    basket_id:  str
    current:    Optional[WindowStats]
    prior:      Optional[WindowStats]


# ─────────────────────────────────────────────────────────────
# ターゲット一覧取得
# ─────────────────────────────────────────────────────────────

def load_basket_targets(
    basket: Optional[str] = None,
    *,
    include_passive: bool = False,
) -> list[BasketTarget]:
    """
    products + basket_membership を JOIN してバスケット構成モデルを取得。

    Args:
        basket: "MFI" / "VFI" / "BPI" / None（全バスケット）
        include_passive: True なら受動収集対象も含む
    """
    sb = _get_supabase()
    q = (
        sb.table("products")
        .select(
            "product_id, basket_id, brand_name, model, year_range_str, "
            "basket_membership!inner(basket, weight)"
        )
    )
    if not include_passive:
        q = q.eq("is_passive", False)
    if basket:
        q = q.eq("basket_membership.basket", basket)

    res = q.execute()
    targets: list[BasketTarget] = []
    for r in res.data or []:
        bm_list = r.get("basket_membership") or []
        for bm in bm_list:
            if basket and bm["basket"] != basket:
                continue
            targets.append(BasketTarget(
                product_id    = r["product_id"],
                basket_id     = r["basket_id"],
                brand_name    = r["brand_name"],
                model         = r["model"],
                basket        = bm["basket"],
                weight        = float(bm.get("weight") or 1.0),
                year_range_str = r.get("year_range_str"),
            ))

    targets.sort(key=lambda t: (t.basket, t.basket_id))
    logger.info("Loaded %d basket targets (basket=%s)", len(targets), basket or "ALL")
    return targets


# ─────────────────────────────────────────────────────────────
# 7日間ウィンドウ集計
# ─────────────────────────────────────────────────────────────

def _compute_window_stats(
    rows: list[dict],
    date_from: date,
    date_to: date,
    all_original_only: bool = False,
) -> WindowStats:
    """
    listings_daily の行リストから 1ウィンドウ分の統計を計算。

    all_original_only=True の場合、condition_tags に 'all_original' を含む
    行のみを価格集計に使用（VFI_AO 系列向け）。
    """
    # スナップショット日付でフィルタ
    in_window = [
        r for r in rows
        if r.get("snapshot_date") and date_from <= date.fromisoformat(r["snapshot_date"]) <= date_to
    ]

    # VFI_AO: price 集計を all_original のみに限定
    price_rows = in_window
    if all_original_only:
        price_rows = [
            r for r in in_window
            if "all_original" in (r.get("condition_tags") or [])
        ]

    prices = [r["price_usd"] for r in price_rows if r.get("price_usd") is not None]
    avg_price = sum(prices) / len(prices) if prices else None

    n_sold = sum(1 for r in in_window if r.get("is_sold"))
    n_us   = sum(1 for r in in_window if (r.get("location_country") or "").upper() == "US")
    n_jp   = sum(1 for r in in_window if (r.get("location_country") or "").upper() in ("JP", "JPN", "JAPAN"))

    return WindowStats(
        date_from    = date_from,
        date_to      = date_to,
        n_listings   = len(in_window),
        n_sold       = n_sold,
        avg_price_usd = avg_price,
        n_us         = n_us,
        n_jp         = n_jp,
        is_all_original_filtered = all_original_only,
    )


def fetch_model_window_data(
    target: BasketTarget,
    target_date: date,
    *,
    window_days: int = 7,
    all_original_only: bool = False,
) -> ModelWindowData:
    """
    1モデルについて current_7d + prior_7d の listings_daily データを取得。

    current_7d: [target_date - 6, target_date]
    prior_7d:   [target_date - 13, target_date - 7]
    """
    sb = _get_supabase()

    # 取得範囲: prior の開始から current の終了まで（14日分）
    fetch_from = target_date - timedelta(days=window_days * 2 - 1)
    fetch_to   = target_date

    q = (
        sb.table("listings_daily")
        .select(
            "listing_id, snapshot_date, price_usd, is_sold, "
            "condition_tags, location_country"
        )
        .eq("product_id", target.product_id)
        .gte("snapshot_date", fetch_from.isoformat())
        .lte("snapshot_date", fetch_to.isoformat())
    )

    res = q.execute()
    rows = res.data or []

    # current / prior 窓を切り分け
    current_from = target_date - timedelta(days=window_days - 1)
    current_to   = target_date
    prior_from   = target_date - timedelta(days=window_days * 2 - 1)
    prior_to     = target_date - timedelta(days=window_days)

    current = _compute_window_stats(rows, current_from, current_to, all_original_only)
    prior   = _compute_window_stats(rows, prior_from,   prior_to,   all_original_only)

    return ModelWindowData(
        product_id = target.product_id,
        basket_id  = target.basket_id,
        current    = current,
        prior      = prior,
    )


def fetch_all_basket_windows(
    targets: list[BasketTarget],
    target_date: date,
    *,
    window_days: int = 7,
    all_original_only: bool = False,
) -> dict[str, ModelWindowData]:
    """
    全モデルのウィンドウデータをまとめて取得。
    Returns: basket_id → ModelWindowData
    """
    result: dict[str, ModelWindowData] = {}
    for t in targets:
        try:
            data = fetch_model_window_data(
                t, target_date,
                window_days=window_days,
                all_original_only=all_original_only,
            )
            result[t.basket_id] = data
            logger.debug("[%s] %s: current=%d, prior=%d listings",
                         t.basket, t.basket_id,
                         data.current.n_listings if data.current else 0,
                         data.prior.n_listings if data.prior else 0)
        except Exception as e:
            logger.warning("[%s] %s fetch error: %s", t.basket, t.basket_id, e)
    return result


# ─────────────────────────────────────────────────────────────
# MentionMomentum 取得（Phase 1 スタブ）
# ─────────────────────────────────────────────────────────────

def fetch_mention_momentum(
    product_id: str,
    target_date: date,
    window_days: int = 7,
) -> float:
    """
    mentions テーブルから MentionMomentum を算出。
    Phase 1 では mentions データがほぼ存在しないため 0.0 を返す。
    Phase 2 以降: engagement の変化率を返すよう拡張予定。

    Returns:
        float: 変化率 (%)、例: +20.0 = +20%
    """
    # TODO Phase 2: mentions テーブルから実データ取得
    # sb = _get_supabase()
    # ...
    return 0.0


# ─────────────────────────────────────────────────────────────
# 利用可能な最新スナップショット日取得
# ─────────────────────────────────────────────────────────────

def get_latest_snapshot_date() -> Optional[date]:
    """
    listings_daily テーブル内の最新 snapshot_date を返す。
    データがない場合は None。
    """
    sb = _get_supabase()
    res = (
        sb.table("listings_daily")
        .select("snapshot_date")
        .order("snapshot_date", desc=True)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    if not rows:
        return None
    return date.fromisoformat(rows[0]["snapshot_date"])


def get_available_snapshot_dates(limit: int = 30) -> list[date]:
    """listings_daily に存在するスナップショット日の一覧（新しい順）"""
    sb = _get_supabase()
    # DISTINCT 相当: snapshot_date でグループ化は PostgREST では困難なので
    # 代替として個別 select → Python 側で dedup
    res = (
        sb.table("listings_daily")
        .select("snapshot_date")
        .order("snapshot_date", desc=True)
        .limit(limit * 50)  # 多め取得してから dedup
        .execute()
    )
    seen: set[str] = set()
    dates: list[date] = []
    for r in (res.data or []):
        d = r.get("snapshot_date")
        if d and d not in seen:
            seen.add(d)
            dates.append(date.fromisoformat(d))
            if len(dates) >= limit:
                break
    return dates


if __name__ == "__main__":
    import sys
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    latest = get_latest_snapshot_date()
    print(f"最新スナップショット日: {latest}")
    dates = get_available_snapshot_dates()
    print(f"利用可能な日付: {dates}")

    targets = load_basket_targets()
    print(f"バスケットターゲット数: {len(targets)}")
    sys.exit(0)
