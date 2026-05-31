"""
TH-03 Index Engine v1.1 — 成分スケール是正の単体テスト
=====================================================
根拠 SPEC: code/specs/TH-03_index_v1_1_fix.md §6
根拠審査: memory/strategy/weight_backtest_2026-05-30.md

実行: cd code && python3 -m pytest index_engine/tests/test_weights_v1_1.py -q
"""
from __future__ import annotations

from datetime import date

from index_engine.models import (
    COMPONENT_WEIGHTS,
    REGIONAL_NORMALIZE_DIVISOR,
    MIN_PRIOR_COUNT_OBS,
    METHOD_VERSION,
    ComponentBreakdown,
)


# ── 1. 重み合計 = 1.0 かつ mention = 0 ──────────────────────────────
def test_weights_sum_to_one_and_mention_zero():
    assert abs(sum(COMPONENT_WEIGHTS.values()) - 1.0) < 1e-9
    assert COMPONENT_WEIGHTS["mention_momentum"] == 0.0


# ── 2. 再正規化値が 0.35/0.85 等と厳密一致 ──────────────────────────
def test_renormalized_values():
    s = 0.85  # 0.35 + 0.25 + 0.15 + 0.10
    assert abs(COMPONENT_WEIGHTS["avg_price_delta_pct"]      - 0.35 / s) < 1e-12
    assert abs(COMPONENT_WEIGHTS["sale_velocity_delta"]      - 0.25 / s) < 1e-12
    assert abs(COMPONENT_WEIGHTS["listing_volume_delta_pct"] - 0.15 / s) < 1e-12
    assert abs(COMPONENT_WEIGHTS["regional_spread_index"]    - 0.10 / s) < 1e-12
    # 参考: price ≈ 0.411764..., regional ≈ 0.117647...
    assert abs(COMPONENT_WEIGHTS["avg_price_delta_pct"] - 0.4117647) < 1e-6
    assert abs(COMPONENT_WEIGHTS["regional_spread_index"] - 0.1176470) < 1e-6


# ── 3. _regional_spread が正規化を適用する ─────────────────────────
def test_regional_spread_normalized():
    from index_engine.calculator import _regional_spread

    class W:  # WindowStats スタブ（n_us / n_jp のみ使用）
        def __init__(self, n_us, n_jp):
            self.n_us = n_us
            self.n_jp = n_jp

    # (80-20)/100 * 100 / 10 = 6.0
    assert abs(_regional_spread(W(80, 20)) - 6.0) < 1e-9
    # 全 US: 100/100 *100 /10 = 10.0（実効レンジ上限）
    assert abs(_regional_spread(W(50, 0)) - 10.0) < 1e-9
    # データなし
    assert _regional_spread(None) == 0.0
    assert _regional_spread(W(0, 0)) == 0.0


def test_regional_divisor_constant():
    assert REGIONAL_NORMALIZE_DIVISOR == 10.0


# ── 4. 5/13 実測成分（regional は正規化後）での IndexComponent 期待値 ──
def test_weighted_score_matches_backtest_anchor():
    # weight_backtest_2026-05-30.md §4 / SPEC §5 のアンカー（MFI）。
    # regional は正規化後の値（55.003/10 = 5.5003）を入力する。
    bd = ComponentBreakdown(
        avg_price_delta_pct      = -0.698,
        sale_velocity_delta      = 4.978,
        listing_volume_delta_pct = 1.976,
        regional_spread_index    = 5.5003,
        mention_momentum         = 0.0,
    )
    assert abs(bd.weighted_score() - 2.172) < 0.01

    # BPI アンカー（regional 44.233/10 = 4.4233）
    bd_bpi = ComponentBreakdown(
        avg_price_delta_pct      = 1.312,
        sale_velocity_delta      = 3.058,
        listing_volume_delta_pct = 1.630,
        regional_spread_index    = 4.4233,
        mention_momentum         = 0.0,
    )
    assert abs(bd_bpi.weighted_score() - 2.248) < 0.01

    # VFI_AC アンカー（regional 0）
    bd_vfi = ComponentBreakdown(
        avg_price_delta_pct      = 3.123,
        sale_velocity_delta      = -7.443,
        listing_volume_delta_pct = -6.981,
        regional_spread_index    = 0.0,
        mention_momentum         = 0.0,
    )
    assert abs(bd_vfi.weighted_score() - (-2.135)) < 0.01


# ── 5. method_version ───────────────────────────────────────────────
def test_method_version():
    assert METHOD_VERSION == "v1.1-phase1"


# ── 6. count デルタの min-prior-observations ゲート（§10.1 / §10.3-6）──
def test_count_delta_min_prior_gate():
    from index_engine.calculator import _count_delta, _pct_change

    # prior=2 (< 3) → ゲートで 0.0
    assert _count_delta(10, 2, 3) == 0.0
    # prior=3 (== 3) → 通常の %変化 = (6-3)/3*100 = 100.0
    assert _count_delta(6, 3, 3) == _pct_change(6, 3) == 100.0
    # prior=0 → 0.0
    assert _count_delta(50, 0, 3) == 0.0
    # 定数
    assert MIN_PRIOR_COUNT_OBS == 3


# ── 7. 旧 max(prior,0.5) 床の非発火回帰（§10.3-7）──────────────────
def test_old_floor_does_not_fire():
    from index_engine.calculator import _count_delta

    # 旧式なら (50 - 0.5) / 0.5 * 100 ≈ 9900 のコールドスタート爆発が出ていた。
    old_floor_value = (50 - 0.5) / 0.5 * 100.0
    assert abs(old_floor_value - 9900.0) < 1.0  # 旧挙動の確認
    # 新ゲートでは prior=0 < 3 → 0.0（爆発が出ない）
    assert _count_delta(50, 0, MIN_PRIOR_COUNT_OBS) == 0.0


# ── ModelScore スタブ（compute_basket_index が参照する属性のみ実装）──
class _StubScore:
    def __init__(self, score: float, weight: float = 1.0):
        self._score = score
        self.weight = weight
        self.has_data = True
        self.n_current = 1
        self.avg_price_current = None
        self.breakdown = ComponentBreakdown()  # components サマリー getattr 用（全ゼロ）

    @property
    def component_score(self) -> float:
        return self._score

    @property
    def weighted_contribution(self) -> float:
        return self.weight * self._score


# ── 8. VFI median 集計が外れ値に頑健（§10.3-8）────────────────────
def test_vfi_median_robust_to_outlier():
    from index_engine.calculator import compute_basket_index

    # component_score = [-2.0, -1.0, +1.0, +10.0(外れ値), 0.0]
    scores = [_StubScore(s) for s in (-2.0, -1.0, 1.0, 10.0, 0.0)]

    # median: ソート [-2,-1,0,1,10] → 中央値 0.0
    res_median = compute_basket_index(scores, "VFI_AC", date(2026, 5, 31), aggregation="median")
    assert abs(res_median.raw_value - 0.0) < 1e-9

    # weighted_mean: 外れ値で正方向へ引っ張られる ((-2-1+1+10+0)/5 = 1.6)
    res_mean = compute_basket_index(scores, "VFI_AC", date(2026, 5, 31), aggregation="weighted_mean")
    assert abs(res_mean.raw_value - 1.6) < 1e-9
    assert res_mean.raw_value > res_median.raw_value  # 外れ値で正に引っ張られる対比


# ── 9. 集計法デフォルト = 加重平均（MFI/BPI 回帰、§10.3-9）─────────
def test_aggregation_default_is_weighted_mean():
    from index_engine.calculator import compute_basket_index

    scores = [_StubScore(s) for s in (-2.0, -1.0, 1.0, 10.0, 0.0)]
    # aggregation 引数なし（既定）= 従来の加重平均
    res_default = compute_basket_index(scores, "MFI", date(2026, 5, 31))
    assert abs(res_default.raw_value - 1.6) < 1e-9
