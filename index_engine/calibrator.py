"""
GUITAR ATLAS — Index Engine: ベース日キャリブレーション
======================================================
2026-06-01 = Index 100 の正規化ロジック。

設計:
  - ベース値は calibration.json（index_engine/ 直下）に永続保存
  - ベース日以前は raw_value をそのまま返す（calibrated_value = None）
  - ベース日当日: raw_value を記録し = 100 として格納
  - ベース日以降: calibrated = raw / base_raw × 100

キャリブレーション方式 ("chained" vs "level"):
  - "level": calibrated_t = raw_t / base_raw × 100
             → 絶対価格水準を反映（最もシンプル）
  - "chained": calibrated_t = calibrated_{t-1} × (1 + raw_t / 100)
              → 日次モメンタムを連鎖（金融インデックス標準）
  Phase 1 では "level" 方式を採用（シングルスナップショット時も機能するため）
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ベース日（CEO承認済み）
# 2026-05-31 変更: 2026-06-01 → 2026-06-25。
#   理由: 収集パイプライン停止(5/23〜)とデプロイ不備でクリーンデータが 6/1 に揃わないため、
#   Board #07 D-52(ローンチ 7/1 リスケ)の余裕を使い set-base を 6/25 へ移送(CEO 承認 2026-05-31)。
#   set_base はこの日付でのみ実行可。6/1 での誤ロックを構造的に防止する関所。
#   根拠: memory/strategy/weight_backtest_2026-05-30.md §8 (fix-before-lock) + 6/25 決定ノート。
BASE_DATE = date(2026, 6, 25)

# ベース Index 値（固定）
BASE_VALUE = 100.0

# キャリブレーション定数の保存パス
_DEFAULT_CALIB_PATH = Path(__file__).parent / "calibration.json"


class Calibrator:
    """
    2026-06-01 = 100 のキャリブレーションを管理するクラス。

    calibration.json の構造:
    {
      "base_date": "2026-06-01",
      "base_value": 100.0,
      "base_raws": {
        "MFI":    <float>,
        "VFI_AO": <float>,
        "VFI_AC": <float>,
        "BPI":    <float>,
        "GAI-E":  <float>
      },
      "method": "level",
      "set_at":  "<ISO datetime>"
    }
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = path or _DEFAULT_CALIB_PATH
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                logger.info("[calibrator] Loaded calibration from %s", self.path)
            except (json.JSONDecodeError, IOError) as e:
                logger.warning("[calibrator] Failed to load %s: %s — starting fresh", self.path, e)
                self._data = {}
        else:
            self._data = {}
            logger.info("[calibrator] No calibration file found at %s — not yet calibrated", self.path)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        logger.info("[calibrator] Saved calibration to %s", self.path)

    @property
    def is_calibrated(self) -> bool:
        """ベース日の raw 値が記録済みかどうか"""
        return bool(self._data.get("base_raws"))

    @property
    def base_raws(self) -> dict[str, float]:
        return self._data.get("base_raws", {})

    def set_base(
        self,
        index_name: str,
        raw_value: float,
        target_date: date,
        force: bool = False,
    ) -> None:
        """
        ベース日の raw_value を記録する。

        Args:
            index_name:  "MFI" / "VFI_AO" / "VFI_AC" / "BPI" / "GAI-E"
            raw_value:   その日の raw スコア
            target_date: 記録対象日（BASE_DATE と一致するか確認）
            force:       True = 既存の記録を上書き
        """
        if target_date != BASE_DATE:
            raise ValueError(
                f"set_base は {BASE_DATE} でのみ呼び出せます。受け取った日付: {target_date}"
            )

        if not self._data.get("base_raws"):
            self._data["base_raws"] = {}
            self._data["base_date"] = BASE_DATE.isoformat()
            self._data["base_value"] = BASE_VALUE
            self._data["method"] = "level"

        if index_name in self._data["base_raws"] and not force:
            logger.info("[calibrator] %s の base_raw は既に設定済み (%.6f)。force=True で上書き可",
                        index_name, self._data["base_raws"][index_name])
            return

        self._data["base_raws"][index_name] = raw_value
        self._data["set_at"] = __import__("datetime").datetime.now().isoformat()
        self._save()
        logger.info("[calibrator] %s base_raw = %.6f (date=%s)", index_name, raw_value, target_date)

    def set_all_bases(
        self,
        index_raws: dict[str, float],
        target_date: date,
        force: bool = False,
    ) -> None:
        """
        全指標のベース値を一括設定。

        Args:
            index_raws: {"MFI": 1.23, "VFI_AC": 0.87, ...}
        """
        for name, raw in index_raws.items():
            self.set_base(name, raw, target_date, force=force)

    def calibrate(self, index_name: str, raw_value: float) -> Optional[float]:
        """
        raw_value を 2026-06-01 = 100 基準に正規化して返す。

        ベース未設定の場合は None を返す（キャリブレーション前扱い）。

        Formula ("level" method):
            calibrated = raw_value / base_raw × 100

        Args:
            index_name: 指標名
            raw_value:  算出された生スコア

        Returns:
            float: 正規化された Index 値（100基準）
            None:  ベース未設定
        """
        base_raws = self._data.get("base_raws", {})
        base_raw = base_raws.get(index_name)

        if base_raw is None:
            return None

        if base_raw == 0:
            logger.warning("[calibrator] %s のベース raw が 0 — キャリブレーション不可", index_name)
            return None

        return (raw_value / base_raw) * BASE_VALUE

    def simulate_calibration(
        self,
        index_raws: dict[str, float],
    ) -> dict[str, float]:
        """
        実ベースデータなしで「もし今日がベース日なら」を仮定した calibration を返す。
        テストラン用: 現在のデータを 100 とみなして各指標を正規化する。

        Args:
            index_raws: {"MFI": raw_mfi, "VFI_AC": raw_vfi_ac, ...}

        Returns:
            {"MFI": 100.0, "VFI_AC": 100.0, ...} （当然全て 100 だが、
            将来の変動をシミュレートするための基準として使う）
        """
        return {name: 100.0 for name in index_raws}

    def print_status(self) -> None:
        """キャリブレーション状態を標準出力に表示"""
        print(f"\n{'='*50}")
        print("キャリブレーション状態")
        print(f"{'='*50}")
        print(f"ベース日:      {BASE_DATE} (= {BASE_VALUE})")
        print(f"ファイル:      {self.path}")
        print(f"設定済み:      {self.is_calibrated}")
        if self.is_calibrated:
            print(f"記録日時:      {self._data.get('set_at', 'N/A')}")
            print(f"算出方式:      {self._data.get('method', 'level')}")
            print("\nベース raw 値:")
            for name, val in self.base_raws.items():
                print(f"  {name:10s} = {val:.6f}")
        else:
            print(f"\n  ⚠️  未キャリブレーション（{BASE_DATE} 以降に自動設定されます）")
        print(f"{'='*50}\n")


# ─────────────────────────────────────────────────────────────
# 便利関数
# ─────────────────────────────────────────────────────────────

def get_default_calibrator() -> Calibrator:
    """デフォルトパスのキャリブレーターを返す"""
    return Calibrator(_DEFAULT_CALIB_PATH)


def reset_calibration(confirm: bool = False) -> None:
    """
    キャリブレーション定数をリセット（開発・テスト用）。
    confirm=True が必要（誤実行防止）。
    """
    if not confirm:
        raise ValueError("reset_calibration を実行するには confirm=True を渡してください")
    if _DEFAULT_CALIB_PATH.exists():
        _DEFAULT_CALIB_PATH.unlink()
        logger.info("[calibrator] calibration.json を削除しました")
    else:
        logger.info("[calibrator] calibration.json は存在しませんでした")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    calib = get_default_calibrator()
    calib.print_status()

    # テスト: ベース日でのセット
    if "--set-test" in sys.argv:
        print("テスト: 仮のベース値をセット")
        test_raws = {"MFI": 1.23, "VFI_AO": 0.45, "VFI_AC": 0.78, "BPI": 0.91, "GAI-E": 0.87}
        calib.set_all_bases(test_raws, BASE_DATE, force=True)
        calib.print_status()

        for name, raw in test_raws.items():
            c = calib.calibrate(name, raw)
            print(f"  {name}: calibrate({raw:.4f}) = {c:.4f}")

    sys.exit(0)
