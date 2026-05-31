"""
GUITAR ATLAS — Index Engine: 指標算出コア
=========================================
MFI / VFI_AO / VFI_AC / BPI / GAI-E + 3スプレッドを算出する。

算出フロー:
  1. 各モデルの IndexComponent を 5成分加重和で計算
  2. バスケット内モデルを weight で加重平均 → MFI / VFI / BPI の raw_value
  3. GAI-E = 0.40×MFI + 0.30×VFI_AC + 0.30×BPI
  4. 3スプレッドを比率で計算
  5. キャリブレーター経由で 2026-06-01=100 に正規化

単一スナップショット（データ1日のみ）のとき:
  - デルタ成分はすべて 0
  - RegionalSpreadIndex のみ算出可能
  - シミュレーションモードでは人工的なデルタを注入して全成分をデモ
"""
from __future__ import annotations

import logging
import math
import random
from datetime import date
from typing import Optional

from .models import (
    ComponentBreakdown,
    ModelScore,
    IndexResult,
    SpreadResult,
    DailyIndexReport,
    COMPONENT_WEIGHTS,
    GAI_E_WEIGHTS,
    SPREAD_DEFINITIONS,
    REGIONAL_NORMALIZE_DIVISOR,
    MIN_PRIOR_COUNT_OBS,
)
from .fetcher import (
    BasketTarget,
    ModelWindowData,
    WindowStats,
    fetch_all_basket_windows,
    fetch_mention_momentum,
    load_basket_targets,
    get_latest_snapshot_date,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# 1. IndexComponent 算出（1モデル）
# ─────────────────────────────────────────────────────────────

def _pct_change(current: float, prior: float) -> float:
    """(current - prior) / prior × 100、prior=0 の場合は 0.0"""
    if prior == 0:
        return 0.0
    return (current - prior) / abs(prior) * 100.0


def _count_delta(current: float, prior: float, min_obs: int) -> float:
    """count 系デルタ。prior 観測が min_obs 未満なら 0.0(ゲート)。
    それ以外は通常の %変化。旧 max(prior,0.5) 床を置換。
    根拠: weight_backtest_2026-05-30.md §8.2"""
    if prior < min_obs:
        return 0.0
    return _pct_change(current, prior)


def _regional_spread(current: Optional[WindowStats]) -> float:
    """
    RegionalSpreadIndex: US / JP 出品比率を表現。
      生レンジは -100〜+100（+100 = 全て US、-100 = 全て JP、0 = 同数 or データなし）。
      他のデルタ成分（±数%〜±数十%）と同オーダーに揃えるため、
      REGIONAL_NORMALIZE_DIVISOR (=10) で除し、実効レンジを ±10 にする。
      根拠: weight_backtest_2026-05-30.md §2（地理アーティファクトが headline を支配する問題の是正）。

    この指標は「US市場とJP市場の需給差分」を表す。
    US優勢の場合、USD建て需要が強い → 正の信号。
    """
    if not current:
        return 0.0
    total = current.n_us + current.n_jp
    if total == 0:
        return 0.0
    raw = ((current.n_us - current.n_jp) / total) * 100.0
    return raw / REGIONAL_NORMALIZE_DIVISOR


def compute_model_component(
    target: BasketTarget,
    window_data: ModelWindowData,
    mention_momentum: float = 0.0,
) -> ComponentBreakdown:
    """
    1モデルの IndexComponent（5成分）を算出する。

    Args:
        target:           バスケット内のモデル情報
        window_data:      current / prior 2ウィンドウの統計
        mention_momentum: fetch_mention_momentum() の結果（%変化率）

    Returns:
        ComponentBreakdown（weighted_score() が IndexComponent スコア）
    """
    c = window_data.current
    p = window_data.prior

    # ── 成分1: ΔAvgPrice%(7d) ────────────────────────────────
    has_price = bool(
        c and c.avg_price_usd is not None
        and p and p.avg_price_usd is not None
        and p.avg_price_usd > 0
    )
    if has_price:
        avg_price_delta = _pct_change(c.avg_price_usd, p.avg_price_usd)
    else:
        avg_price_delta = 0.0

    # ── 成分2: ΔSaleVelocity(7d) ────────────────────────────
    has_velocity = bool(c and p)
    if has_velocity:
        sale_velocity_delta = _count_delta(
            float(c.n_sold), float(p.n_sold), MIN_PRIOR_COUNT_OBS
        )
    else:
        sale_velocity_delta = 0.0

    # ── 成分3: ΔListingVolume%(7d) ──────────────────────────
    if has_velocity:
        listing_volume_delta = _count_delta(
            float(c.n_listings), float(p.n_listings), MIN_PRIOR_COUNT_OBS
        )
    else:
        listing_volume_delta = 0.0

    # ── 成分4: RegionalSpreadIndex ──────────────────────────
    regional_spread = _regional_spread(c)

    # ── 成分5: MentionMomentum ──────────────────────────────
    has_mention = mention_momentum != 0.0

    return ComponentBreakdown(
        avg_price_delta_pct      = round(avg_price_delta, 4),
        sale_velocity_delta      = round(sale_velocity_delta, 4),
        listing_volume_delta_pct = round(listing_volume_delta, 4),
        regional_spread_index    = round(regional_spread, 4),
        mention_momentum         = round(mention_momentum, 4),
        has_price_data           = has_price,
        has_velocity_data        = has_velocity,
        has_mention_data         = has_mention,
    )


def compute_model_score(
    target: BasketTarget,
    window_data: ModelWindowData,
    mention_momentum: float = 0.0,
) -> ModelScore:
    """
    BasketTarget と WindowData から ModelScore を生成。
    """
    c = window_data.current
    p = window_data.prior

    breakdown = compute_model_component(target, window_data, mention_momentum)

    return ModelScore(
        basket_id          = target.basket_id,
        brand_name         = target.brand_name,
        model              = target.model,
        basket             = target.basket,
        weight             = target.weight,
        breakdown          = breakdown,
        n_current          = c.n_listings if c else 0,
        n_prior            = p.n_listings if p else 0,
        n_sold_current     = c.n_sold if c else 0,
        avg_price_current  = c.avg_price_usd if c else None,
        avg_price_prior    = p.avg_price_usd if p else None,
        has_data           = bool(c and c.n_listings > 0),
    )


# ─────────────────────────────────────────────────────────────
# 2. バスケット Index 集計
# ─────────────────────────────────────────────────────────────

def compute_basket_index(
    model_scores: list[ModelScore],
    index_name: str,
    snapshot_date: date,
    aggregation: str = "weighted_mean",   # "weighted_mean" | "median"
) -> IndexResult:
    """
    モデルスコアのリストからバスケット Index の raw_value を算出。

    aggregation="weighted_mean" (既定, MFI/BPI):
        raw_value = Σ(weight_i × component_score_i) / Σ(weight_i)
                    （データあるモデルのみ分母に含める）
    aggregation="median" (VFI_AC / VFI_AO):
        raw_value = median(component_score_i)
        超高額個体(Gibson Burst 等)による平均 whipsaw を抑える。
        根拠: weight_backtest_2026-05-30.md §8.3。VFI バスケット重みは均一(全 1.0)のため
        非加重 statistics.median で確定。

    calibrated_value は calibrator.py 側で後から埋める。
    """
    models_with_data = [m for m in model_scores if m.has_data]
    n_total    = len(model_scores)
    n_with_data = len(models_with_data)

    # raw_value 集計（集計法で分岐）
    if models_with_data:
        if aggregation == "median":
            import statistics
            raw_value = statistics.median(
                [m.component_score for m in models_with_data]
            )
        else:
            total_weight = sum(m.weight for m in models_with_data)
            raw_value = (
                sum(m.weighted_contribution for m in models_with_data) / total_weight
                if total_weight > 0 else 0.0
            )
    else:
        raw_value = 0.0

    # バスケット全体の平均価格（参考値）
    prices = [m.avg_price_current for m in models_with_data if m.avg_price_current is not None]
    avg_price_usd = sum(prices) / len(prices) if prices else None

    total_listings = sum(m.n_current for m in model_scores)

    # components サマリー（各成分の加重平均）
    components: dict = {}
    if models_with_data:
        tw = sum(m.weight for m in models_with_data) or 1.0
        for key in COMPONENT_WEIGHTS:
            components[key] = round(
                sum(getattr(m.breakdown, key) * m.weight for m in models_with_data) / tw, 4
            )
        components["weighted_score"]    = round(raw_value, 4)
        components["n_models_with_data"] = n_with_data
        components["n_models_total"]     = n_total
        components["avg_price_usd"]      = round(avg_price_usd, 2) if avg_price_usd else None
        components["total_listings"]     = total_listings

    return IndexResult(
        snapshot_date       = snapshot_date,
        index_name          = index_name,
        raw_value           = round(raw_value, 6),
        calibrated_value    = None,  # calibrator.py が後から設定
        components          = components,
        model_scores        = model_scores,
        n_models_total      = n_total,
        n_models_with_data  = n_with_data,
        avg_price_usd       = avg_price_usd,
        total_listings      = total_listings,
    )


# ─────────────────────────────────────────────────────────────
# 3. GAI-E 合成
# ─────────────────────────────────────────────────────────────

def compute_gai_e(
    mfi: IndexResult,
    vfi_ac: IndexResult,
    bpi: IndexResult,
    snapshot_date: date,
) -> IndexResult:
    """
    GAI-E = 0.40 × MFI + 0.30 × VFI_AC + 0.30 × BPI
    calibrated_value がある場合はそちらで合成。
    """
    def _val(idx: IndexResult) -> float:
        return idx.calibrated_value if idx.calibrated_value is not None else idx.raw_value

    raw_value = (
        GAI_E_WEIGHTS["MFI"] * _val(mfi)
        + GAI_E_WEIGHTS["VFI"] * _val(vfi_ac)
        + GAI_E_WEIGHTS["BPI"] * _val(bpi)
    )

    # キャリブレーション済み値の有無で calibrated も合成
    if all(i.calibrated_value is not None for i in [mfi, vfi_ac, bpi]):
        calibrated_value = raw_value  # 既に calibrated で計算済み
    else:
        calibrated_value = None

    components = {
        "formula":   "0.40×MFI + 0.30×VFI_AC + 0.30×BPI",
        "MFI_input": round(_val(mfi), 4),
        "VFI_input": round(_val(vfi_ac), 4),
        "BPI_input": round(_val(bpi), 4),
        "MFI_weight":  GAI_E_WEIGHTS["MFI"],
        "VFI_weight":  GAI_E_WEIGHTS["VFI"],
        "BPI_weight":  GAI_E_WEIGHTS["BPI"],
    }

    # モデルスコアは 3バスケット合算
    all_model_scores = mfi.model_scores + vfi_ac.model_scores + bpi.model_scores

    return IndexResult(
        snapshot_date       = snapshot_date,
        index_name          = "GAI-E",
        raw_value           = round(raw_value, 6),
        calibrated_value    = round(calibrated_value, 6) if calibrated_value is not None else None,
        components          = components,
        model_scores        = all_model_scores,
        n_models_total      = mfi.n_models_total + vfi_ac.n_models_total + bpi.n_models_total,
        n_models_with_data  = mfi.n_models_with_data + vfi_ac.n_models_with_data + bpi.n_models_with_data,
        avg_price_usd       = None,
        total_listings      = mfi.total_listings + vfi_ac.total_listings + bpi.total_listings,
    )


# ─────────────────────────────────────────────────────────────
# 4. 3スプレッド算出
# ─────────────────────────────────────────────────────────────

def compute_spreads(
    index_map: dict[str, IndexResult],
    snapshot_date: date,
) -> dict[str, SpreadResult]:
    """
    BoutiquePremium / VintagePremium / HeritageSpread を算出。

    Args:
        index_map: {"MFI": ..., "VFI_AC": ..., "BPI": ...}
    """
    results: dict[str, SpreadResult] = {}

    for spread_name, (num_key, den_key) in SPREAD_DEFINITIONS.items():
        num_idx = index_map.get(num_key)
        den_idx = index_map.get(den_key)

        if not (num_idx and den_idx):
            logger.warning("スプレッド %s: %s or %s が未算出", spread_name, num_key, den_key)
            continue

        num_val = num_idx.display_value
        den_val = den_idx.display_value

        if den_val == 0 or math.isnan(den_val):
            logger.warning("スプレッド %s: 分母が0またはNaN", spread_name)
            ratio = 0.0
        else:
            ratio = num_val / den_val

        results[spread_name] = SpreadResult(
            snapshot_date     = snapshot_date,
            spread_name       = spread_name,
            numerator_index   = num_key,
            denominator_index = den_key,
            numerator_value   = round(num_val, 4),
            denominator_value = round(den_val, 4),
            ratio             = round(ratio, 6),
        )

    return results


# ─────────────────────────────────────────────────────────────
# 5. シミュレーションモード（1日分データ補完）
# ─────────────────────────────────────────────────────────────

def inject_simulation_deltas(
    model_scores: list[ModelScore],
    seed: int = 42,
    volatility: float = 5.0,
) -> list[ModelScore]:
    """
    過去データがない場合（シングルスナップショット）に、
    リアルな価格変動範囲の模擬デルタを注入してデモ用スコアを生成。

    CEO向けテストランで指標の動き方・重みの効果を確認するための機能。
    本番では使用しない。

    Args:
        model_scores: compute_model_score() の結果リスト
        seed:         乱数シード（再現性のため固定）
        volatility:   デルタの標準偏差 (%)
    """
    rng = random.Random(seed)
    simulated: list[ModelScore] = []

    for ms in model_scores:
        # 価格変動: バスケット別に異なる volatility
        if ms.basket == "VFI":
            vol = volatility * 1.5   # Vintage は変動大きめ
        elif ms.basket == "BPI":
            vol = volatility * 0.8   # Boutique は安定気味
        else:
            vol = volatility

        # 各成分に独立のガウスノイズ
        sim_breakdown = ComponentBreakdown(
            avg_price_delta_pct      = rng.gauss(0, vol),
            sale_velocity_delta      = rng.gauss(0, vol * 0.5),
            listing_volume_delta_pct = rng.gauss(0, vol * 0.3),
            regional_spread_index    = ms.breakdown.regional_spread_index,  # 実データ維持
            mention_momentum         = rng.gauss(0, vol * 0.4),
            has_price_data           = True,
            has_velocity_data        = True,
            has_mention_data         = False,
        )

        # ModelScore をコピーしてブレークダウンを差し替え
        sim_score = ModelScore(
            basket_id          = ms.basket_id,
            brand_name         = ms.brand_name,
            model              = ms.model,
            basket             = ms.basket,
            weight             = ms.weight,
            breakdown          = sim_breakdown,
            n_current          = ms.n_current,
            n_prior            = ms.n_prior,
            n_sold_current     = ms.n_sold_current,
            avg_price_current  = ms.avg_price_current,
            avg_price_prior    = ms.avg_price_prior,
            has_data           = ms.has_data or True,  # シミュレーションではデータありとみなす
        )
        simulated.append(sim_score)

    return simulated


# ─────────────────────────────────────────────────────────────
# 6. メイン日次算出エントリーポイント
# ─────────────────────────────────────────────────────────────

def run_daily_calculation(
    target_date: date,
    *,
    simulation_mode: bool = False,
    window_days: int = 7,
    calibrator=None,   # calibrator.Calibrator インスタンス
) -> DailyIndexReport:
    """
    指定日の全指標を算出して DailyIndexReport を返す。

    Args:
        target_date:     算出対象日
        simulation_mode: True = シングルスナップショット時の補完デルタを注入
        window_days:     ウィンドウサイズ（デフォルト7日）
        calibrator:      キャリブレーターインスタンス（None = キャリブレーションなし）
    """
    logger.info("=== 算出開始: %s (simulation=%s) ===", target_date, simulation_mode)

    # ── ターゲット取得 ────────────────────────────────────────
    mfi_targets  = load_basket_targets("MFI")
    vfi_targets  = load_basket_targets("VFI")
    bpi_targets  = load_basket_targets("BPI")
    all_targets  = mfi_targets + vfi_targets + bpi_targets

    # ── ウィンドウデータ取得（通常 / all_original_only の2パス）──
    logger.info("listings_daily からデータ取得中...")
    window_map_all = fetch_all_basket_windows(all_targets, target_date, window_days=window_days)
    window_map_ao  = fetch_all_basket_windows(
        vfi_targets, target_date, window_days=window_days, all_original_only=True
    )

    # ── モデルスコア算出 ──────────────────────────────────────
    def _score_targets(targets: list[BasketTarget], window_map: dict) -> list[ModelScore]:
        scores = []
        for t in targets:
            wd = window_map.get(t.basket_id)
            if wd is None:
                # データなし → ゼロスコアで補完
                from .fetcher import WindowStats
                wd = type('WD', (), {
                    'product_id': t.product_id,
                    'basket_id': t.basket_id,
                    'current': None,
                    'prior': None,
                })()
            momentum = fetch_mention_momentum(t.product_id, target_date, window_days)
            scores.append(compute_model_score(t, wd, momentum))
        return scores

    mfi_scores    = _score_targets(mfi_targets,  window_map_all)
    vfi_ac_scores = _score_targets(vfi_targets,  window_map_all)
    vfi_ao_scores = _score_targets(vfi_targets,  window_map_ao)
    bpi_scores    = _score_targets(bpi_targets,  window_map_all)

    # シミュレーションモード: デルタ補完
    if simulation_mode:
        logger.info("シミュレーションモード: 模擬デルタを注入")
        mfi_scores    = inject_simulation_deltas(mfi_scores,    seed=42, volatility=4.0)
        vfi_ac_scores = inject_simulation_deltas(vfi_ac_scores, seed=43, volatility=7.0)
        vfi_ao_scores = inject_simulation_deltas(vfi_ao_scores, seed=44, volatility=8.0)
        bpi_scores    = inject_simulation_deltas(bpi_scores,    seed=45, volatility=3.5)

    # ── バスケット Index 集計 ─────────────────────────────────
    mfi_result    = compute_basket_index(mfi_scores,    "MFI",    target_date)
    vfi_ac_result = compute_basket_index(vfi_ac_scores, "VFI_AC", target_date, aggregation="median")
    vfi_ao_result = compute_basket_index(vfi_ao_scores, "VFI_AO", target_date, aggregation="median")
    bpi_result    = compute_basket_index(bpi_scores,    "BPI",    target_date)

    logger.info("  MFI  raw=%.4f  (%d/%d models w/ data)",
                mfi_result.raw_value, mfi_result.n_models_with_data, mfi_result.n_models_total)
    logger.info("  VFI_AC raw=%.4f  (%d/%d models w/ data)",
                vfi_ac_result.raw_value, vfi_ac_result.n_models_with_data, vfi_ac_result.n_models_total)
    logger.info("  VFI_AO raw=%.4f  (%d/%d models w/ data)",
                vfi_ao_result.raw_value, vfi_ao_result.n_models_with_data, vfi_ao_result.n_models_total)
    logger.info("  BPI  raw=%.4f  (%d/%d models w/ data)",
                bpi_result.raw_value, bpi_result.n_models_with_data, bpi_result.n_models_total)

    # ── キャリブレーション ────────────────────────────────────
    is_calibrated = False
    if calibrator is not None:
        for idx in [mfi_result, vfi_ac_result, vfi_ao_result, bpi_result]:
            calibrated = calibrator.calibrate(idx.index_name, idx.raw_value)
            if calibrated is not None:
                idx.calibrated_value = round(calibrated, 4)
        is_calibrated = calibrator.is_calibrated

    # ── GAI-E 合成 ────────────────────────────────────────────
    gai_e_result = compute_gai_e(mfi_result, vfi_ac_result, bpi_result, target_date)

    # ── 3スプレッド ───────────────────────────────────────────
    index_map = {
        "MFI":    mfi_result,
        "VFI_AO": vfi_ao_result,
        "VFI_AC": vfi_ac_result,
        "BPI":    bpi_result,
        "GAI-E":  gai_e_result,
    }
    spreads = compute_spreads(index_map, target_date)

    logger.info("  GAI-E raw=%.4f", gai_e_result.raw_value)
    for sname, sres in spreads.items():
        logger.info("  %s ratio=%.4f", sname, sres.ratio)

    return DailyIndexReport(
        snapshot_date   = target_date,
        mfi             = mfi_result,
        vfi_ao          = vfi_ao_result,
        vfi_ac          = vfi_ac_result,
        bpi             = bpi_result,
        gai_e           = gai_e_result,
        spreads         = spreads,
        is_calibrated   = is_calibrated,
        simulation_mode = simulation_mode,
    )
