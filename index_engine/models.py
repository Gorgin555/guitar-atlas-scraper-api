"""
GUITAR ATLAS — Index Engine: データモデル & 定数
================================================
全指標エンジンで共有する dataclass 群と、
basket_v1.yaml / core_architecture.md から抽出した定数。

確定値（CEO承認済み 2026-05-09）:
  GAI-E = 0.40×MFI + 0.30×VFI + 0.30×BPI
  IndexComponent = 5成分加重和（詳細は COMPONENT_WEIGHTS 参照）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

# ─────────────────────────────────────────────────────────────
# 1. 定数（CEO承認済み）
# ─────────────────────────────────────────────────────────────

# 指標方法バージョン（index_snapshots.method_version に記録）
# v1.1-phase1: 2026-05-30 重みバックテスト審査を受けた成分スケール是正版。
#   ① RegionalSpreadIndex を ±100→±10 に正規化（REGIONAL_NORMALIZE_DIVISOR）
#   ② Phase 1 で恒常ゼロの MentionMomentum 重みをゼロ化し残り4成分を1.0へ再正規化
# 根拠: memory/strategy/weight_backtest_2026-05-30.md
METHOD_VERSION: str = "v1.1-phase1"

# RegionalSpreadIndex は -100〜+100 スケール。他のデルタ成分 (±数%〜±数十%) と
# 同オーダーに揃えるための除数。±100 → ±10 にマップ。詳細: weight_backtest_2026-05-30.md §2
REGIONAL_NORMALIZE_DIVISOR: float = 10.0

# count 系デルタ(成約数/出品数)の最小 prior 観測数ゲート。
# prior 観測が N 件未満のモデルは当該 count デルタを 0 とする(%変化が統計的に無意味なため)。
# 旧 max(prior, 0.5) 床のコールドスタート爆発を是正。根拠: weight_backtest_2026-05-30.md §8.2
MIN_PRIOR_COUNT_OBS: int = 3

# IndexComponent の 5成分重み — v1.0 原設計（CEO承認 2026-05-09）。
# Phase 2 で MentionMomentum が実データで起動する際の再ベース基準として保持する。
_COMPONENT_WEIGHTS_V1_0: dict[str, float] = {
    "avg_price_delta_pct":      0.35,   # ΔAvgPrice%(7d)
    "sale_velocity_delta":      0.25,   # ΔSaleVelocity(7d)
    "listing_volume_delta_pct": 0.15,   # ΔListingVolume%(7d)
    "regional_spread_index":    0.10,   # US/JP RegionalSpread
    "mention_momentum":         0.15,   # Mention 増減
}


def _phase1_weights(base: dict[str, float]) -> dict[str, float]:
    """Phase 1: MentionMomentum をゼロ化し、残り4成分を合計1.0へ再正規化する。
    TH-16 (mention 起動) 時は base をそのまま使い method_version をバンプして再ベースする。"""
    w = dict(base)
    w["mention_momentum"] = 0.0
    s = sum(w.values())
    return {k: (v / s if k != "mention_momentum" else 0.0) for k, v in w.items()}


# v1.1-phase1 の実効重み（mention=0、残り4成分=1.0）
COMPONENT_WEIGHTS: dict[str, float] = _phase1_weights(_COMPONENT_WEIGHTS_V1_0)
assert abs(sum(COMPONENT_WEIGHTS.values()) - 1.0) < 1e-9, "重み合計が1.0でない"
assert COMPONENT_WEIGHTS["mention_momentum"] == 0.0, "Phase 1 では mention 重みは 0"

# GAI-E 合成重み（合計 = 1.00）
GAI_E_WEIGHTS: dict[str, float] = {
    "MFI": 0.40,
    "VFI": 0.30,  # VFI_AC を採用（All Conditions）
    "BPI": 0.30,
}
assert abs(sum(GAI_E_WEIGHTS.values()) - 1.0) < 1e-9, "GAI-E重み合計が1.0でない"

# 3スプレッド定義: spread_name → (numerator, denominator)
SPREAD_DEFINITIONS: dict[str, tuple[str, str]] = {
    "BoutiquePremium": ("BPI",    "MFI"),
    "VintagePremium":  ("VFI_AC", "MFI"),
    "HeritageSpread":  ("VFI_AC", "BPI"),
}

# Vintage コンディション分類タグ（CLO はぐりん監修）
VFI_CONDITION_TAGS: list[str] = [
    "all_original",      # 全オリジナル（100% baseline）
    "partial_changed",   # 一部交換（-10〜-20%）
    "refin",             # リフィニッシュ（-20〜-40%）
    "replaced_neck",     # ネック交換（-50%以上）
    "parts_caster",      # パーツ寄せ集め（個別査定）
    "case_present",      # 純正ケース付属（+5%）
    "paperwork_present", # ハングタグ・書類完備（+10%）
]

# Index 名称の完全リスト
ALL_INDEX_NAMES = ["MFI", "VFI_AO", "VFI_AC", "BPI", "GAI-E"] + list(SPREAD_DEFINITIONS.keys())

# ─────────────────────────────────────────────────────────────
# 2. ComponentBreakdown — IndexComponent の 5成分内訳
# ─────────────────────────────────────────────────────────────

@dataclass
class ComponentBreakdown:
    """
    IndexComponent = Σ(w_i × component_i)

    各成分の値:
      - avg_price_delta_pct:      7d 平均価格変化率 (%)、例: +3.5 = +3.5%
      - sale_velocity_delta:      7d 成約件数変化率 (%)
      - listing_volume_delta_pct: 7d 出品数変化率 (%)
      - regional_spread_index:    US vs JP 出品比率スコア (-100〜+100)
      - mention_momentum:         7d 言及数変化率 (%)
    """
    avg_price_delta_pct:      float = 0.0
    sale_velocity_delta:      float = 0.0
    listing_volume_delta_pct: float = 0.0
    regional_spread_index:    float = 0.0
    mention_momentum:         float = 0.0

    # データ品質フラグ
    has_price_data:    bool = False
    has_velocity_data: bool = False
    has_mention_data:  bool = False

    def weighted_score(self) -> float:
        """IndexComponent スコアを算出（加重和）"""
        return (
            COMPONENT_WEIGHTS["avg_price_delta_pct"]      * self.avg_price_delta_pct
            + COMPONENT_WEIGHTS["sale_velocity_delta"]      * self.sale_velocity_delta
            + COMPONENT_WEIGHTS["listing_volume_delta_pct"] * self.listing_volume_delta_pct
            + COMPONENT_WEIGHTS["regional_spread_index"]    * self.regional_spread_index
            + COMPONENT_WEIGHTS["mention_momentum"]         * self.mention_momentum
        )

    def to_dict(self) -> dict:
        return {
            "avg_price_delta_pct":      round(self.avg_price_delta_pct, 4),
            "sale_velocity_delta":      round(self.sale_velocity_delta, 4),
            "listing_volume_delta_pct": round(self.listing_volume_delta_pct, 4),
            "regional_spread_index":    round(self.regional_spread_index, 4),
            "mention_momentum":         round(self.mention_momentum, 4),
            "weighted_score":           round(self.weighted_score(), 4),
            "has_price_data":           self.has_price_data,
            "has_velocity_data":        self.has_velocity_data,
            "has_mention_data":         self.has_mention_data,
        }


# ─────────────────────────────────────────────────────────────
# 3. ModelScore — 個別モデルのスコア
# ─────────────────────────────────────────────────────────────

@dataclass
class ModelScore:
    """
    バスケット内 1モデルの算出結果。
    basket_membership.weight でバスケット内加重を表す。
    """
    basket_id:    str
    brand_name:   str
    model:        str
    basket:       str   # "MFI" / "VFI" / "BPI"
    weight:       float
    breakdown:    ComponentBreakdown

    # 生データサマリー（デバッグ / CEO向け表示用）
    n_current:         int            = 0     # current 7d 出品数
    n_prior:           int            = 0     # prior 7d 出品数
    n_sold_current:    int            = 0     # current 7d 成約数
    avg_price_current: Optional[float] = None
    avg_price_prior:   Optional[float] = None
    has_data:          bool           = False

    @property
    def component_score(self) -> float:
        return self.breakdown.weighted_score()

    @property
    def weighted_contribution(self) -> float:
        """バスケット Index への寄与 = weight × component_score"""
        return self.weight * self.component_score


# ─────────────────────────────────────────────────────────────
# 4. IndexResult — 1指標の算出結果
# ─────────────────────────────────────────────────────────────

@dataclass
class IndexResult:
    """
    MFI / VFI_AO / VFI_AC / BPI / GAI-E のいずれか 1指標の日次結果。

    raw_value:        バスケット加重平均の生スコア
    calibrated_value: 2026-06-01 = 100 に正規化した値
                      ベース日データ未確定時は None
    """
    snapshot_date:    date
    index_name:       str
    raw_value:        float
    calibrated_value: Optional[float]
    components:       dict     # ComponentBreakdown 集約

    model_scores:       list[ModelScore] = field(default_factory=list)
    n_models_total:     int              = 0
    n_models_with_data: int              = 0
    avg_price_usd:      Optional[float]  = None   # バスケット平均価格 USD（参考）
    total_listings:     int              = 0

    @property
    def display_value(self) -> float:
        """表示用の値：キャリブレーション済みがあればそちら、なければ生値"""
        return self.calibrated_value if self.calibrated_value is not None else self.raw_value

    def to_snapshot_row(self, method_version: str = "v1.0") -> dict:
        """index_snapshots テーブルへの INSERT 用 dict"""
        return {
            "snapshot_date": self.snapshot_date.isoformat(),
            "index_name":    self.index_name,
            "value":         round(self.display_value, 4),
            "components":    self.components,
            "method_version": method_version,
        }


# ─────────────────────────────────────────────────────────────
# 5. SpreadResult — 3スプレッド算出結果
# ─────────────────────────────────────────────────────────────

@dataclass
class SpreadResult:
    """
    スプレッド = numerator_index / denominator_index の比率。

    BoutiquePremium = BPI / MFI
    VintagePremium  = VFI_AC / MFI
    HeritageSpread  = VFI_AC / BPI
    """
    snapshot_date:    date
    spread_name:      str
    numerator_index:  str
    denominator_index: str
    numerator_value:   float
    denominator_value: float
    ratio:             float   # numerator / denominator

    def to_snapshot_row(self, method_version: str = "v1.0") -> dict:
        """index_snapshots テーブルへの INSERT 用 dict"""
        return {
            "snapshot_date": self.snapshot_date.isoformat(),
            "index_name":    self.spread_name,
            "value":         round(self.ratio, 6),
            "components": {
                "numerator":        self.numerator_index,
                "denominator":      self.denominator_index,
                "numerator_value":  round(self.numerator_value, 4),
                "denominator_value": round(self.denominator_value, 4),
            },
            "method_version": method_version,
        }


# ─────────────────────────────────────────────────────────────
# 6. DailyIndexReport — 1日分の全指標まとめ
# ─────────────────────────────────────────────────────────────

@dataclass
class DailyIndexReport:
    """日次バッチで生成される全指標の統合レポート。"""
    snapshot_date:   date
    mfi:             IndexResult
    vfi_ao:          IndexResult   # VFI All Original Only
    vfi_ac:          IndexResult   # VFI All Conditions
    bpi:             IndexResult
    gai_e:           IndexResult
    spreads:         dict[str, SpreadResult]   # spread_name → SpreadResult
    is_calibrated:   bool = False
    simulation_mode: bool = False  # True = シミュレーション（実データ不足時）
    notes:           str  = ""

    def all_index_results(self) -> dict[str, IndexResult]:
        return {
            "MFI":    self.mfi,
            "VFI_AO": self.vfi_ao,
            "VFI_AC": self.vfi_ac,
            "BPI":    self.bpi,
            "GAI-E":  self.gai_e,
        }

    def all_snapshot_rows(self, method_version: str = "v1.0") -> list[dict]:
        rows = [r.to_snapshot_row(method_version) for r in self.all_index_results().values()]
        rows += [s.to_snapshot_row(method_version) for s in self.spreads.values()]
        return rows
