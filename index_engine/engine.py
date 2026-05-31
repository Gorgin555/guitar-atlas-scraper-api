"""
GUITAR ATLAS — Index Engine: 日次パイプライン
=============================================
毎朝 6:00 JST に走る日次指標算出パイプライン（n8n から呼び出し予定）。

実行フロー:
  1. 最新 snapshot_date を確認
  2. MFI / VFI_AO / VFI_AC / BPI / GAI-E を算出
  3. キャリブレーション適用（2026-06-01 以降）
  4. index_snapshots テーブルに書き込み
  5. Slack ブリーフィング用 JSON を出力

使用方法:
  cd ~/Desktop/ATLAS/code
  source .venv/bin/activate
  python -m index_engine.engine                    # 最新日で実行
  python -m index_engine.engine --date 2026-06-25  # 指定日で実行
  python -m index_engine.engine --dry-run          # DB書き込みなし
  python -m index_engine.engine --set-base         # 2026-06-25 のベース値を設定 (BASE_DATE)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime
from typing import Optional

from dotenv import load_dotenv

from .calculator import run_daily_calculation
from .calibrator import Calibrator, BASE_DATE, get_default_calibrator
from .fetcher import get_latest_snapshot_date, get_available_snapshot_dates
from .models import DailyIndexReport, METHOD_VERSION

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Supabase への書き込み
# ─────────────────────────────────────────────────────────────

def save_report_to_db(
    report: DailyIndexReport,
    *,
    dry_run: bool = False,
    method_version: str = METHOD_VERSION,
) -> dict[str, int]:
    """
    DailyIndexReport を index_snapshots テーブルに upsert。

    Returns:
        {"upserted": N, "errors": M}
    """
    rows = report.all_snapshot_rows(method_version=method_version)

    if dry_run:
        logger.info("[dry-run] %d rows would be upserted to index_snapshots", len(rows))
        for r in rows:
            logger.info("  %s %s = %.4f", r["snapshot_date"], r["index_name"], r["value"])
        return {"upserted": 0, "errors": 0}

    from supabase import create_client
    sb = create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_KEY"],
    )

    counts = {"upserted": 0, "errors": 0}
    for row in rows:
        try:
            sb.table("index_snapshots").upsert(
                row,
                on_conflict="snapshot_date,index_name,method_version",
            ).execute()
            counts["upserted"] += 1
            logger.debug("  ✓ %s %s = %.4f", row["snapshot_date"], row["index_name"], row["value"])
        except Exception as e:
            counts["errors"] += 1
            logger.error("  ✗ %s %s: %s", row["snapshot_date"], row["index_name"], e)

    return counts


# ─────────────────────────────────────────────────────────────
# ブリーフィング JSON 生成（Slack 向け）
# ─────────────────────────────────────────────────────────────

def generate_briefing_json(report: DailyIndexReport) -> dict:
    """
    CEO 朝のブリーフィング用 JSON 出力。
    n8n / Slack webhook からそのまま使えるフォーマット。
    """
    def _fmt(v: Optional[float]) -> str:
        if v is None:
            return "N/A"
        return f"{v:.2f}"

    def _chg_icon(v: Optional[float]) -> str:
        if v is None:
            return "—"
        if v > 0:
            return "📈"
        if v < 0:
            return "📉"
        return "→"

    indices = report.all_index_results()

    index_summary = {}
    for name, idx in indices.items():
        dv = idx.display_value
        price = idx.avg_price_usd
        index_summary[name] = {
            "value":           round(dv, 2) if dv else None,
            "calibrated":      idx.calibrated_value is not None,
            "avg_price_usd":   round(price, 0) if price else None,
            "n_models":        f"{idx.n_models_with_data}/{idx.n_models_total}",
            "total_listings":  idx.total_listings,
        }

    spreads_summary = {}
    for name, sp in report.spreads.items():
        spreads_summary[name] = {
            "ratio":         round(sp.ratio, 4),
            "ratio_pct":     f"{sp.ratio * 100:.1f}%",
            "numerator":     round(sp.numerator_value, 2),
            "denominator":   round(sp.denominator_value, 2),
        }

    return {
        "date":            report.snapshot_date.isoformat(),
        "is_calibrated":   report.is_calibrated,
        "simulation_mode": report.simulation_mode,
        "indices":         index_summary,
        "spreads":         spreads_summary,
        "generated_at":    datetime.now().isoformat(),
        "notes":           report.notes,
    }


# ─────────────────────────────────────────────────────────────
# ベース日セット処理
# ─────────────────────────────────────────────────────────────

def set_base_date_values(force: bool = False) -> int:
    """
    2026-06-01 の指標を算出してキャリブレーションベースとして記録。
    Returns 0 on success, 1 on error.
    """
    logger.info("=== ベース日（%s）の値を設定 ===", BASE_DATE)

    latest = get_latest_snapshot_date()
    if latest is None:
        logger.error("listings_daily にデータがありません")
        return 1

    if latest < BASE_DATE:
        logger.warning(
            "最新スナップショット日 %s はベース日 %s より前です。"
            "ベース日のデータが取得できるまで待機してください。",
            latest, BASE_DATE,
        )
        return 1

    # ベース日で計算
    report = run_daily_calculation(BASE_DATE, simulation_mode=False)
    raws = {name: idx.raw_value for name, idx in report.all_index_results().items()}

    calib = get_default_calibrator()
    calib.set_all_bases(raws, BASE_DATE, force=force)
    calib.print_status()
    return 0


# ─────────────────────────────────────────────────────────────
# メイン日次実行
# ─────────────────────────────────────────────────────────────

def run_engine(
    target_date: Optional[date] = None,
    *,
    dry_run: bool = False,
    simulation_mode: bool = False,
    method_version: str = METHOD_VERSION,
    output_json: Optional[str] = None,
) -> DailyIndexReport:
    """
    日次エンジンの本体。

    Args:
        target_date:    算出対象日（None = listings_daily の最新日）
        dry_run:        True = DB 書き込みなし
        simulation_mode: True = シングルスナップショット時の模擬デルタ
        method_version: index_snapshots に記録するバージョン文字列
        output_json:    ブリーフィング JSON の書き出しパス

    Returns:
        DailyIndexReport
    """
    # 対象日の確定
    if target_date is None:
        target_date = get_latest_snapshot_date()
        if target_date is None:
            raise RuntimeError("listings_daily にデータがありません。まず fetch_listings.py を実行してください。")
        logger.info("対象日を自動検出: %s", target_date)

    # キャリブレーター
    calib = get_default_calibrator()
    calib.print_status()

    # 算出
    report = run_daily_calculation(
        target_date,
        simulation_mode=simulation_mode,
        calibrator=calib,
    )

    # ベース日当日だったら自動でベース値を記録
    if target_date == BASE_DATE and not calib.is_calibrated:
        logger.info("ベース日当日です。raw 値をキャリブレーション定数として記録します。")
        raws = {name: idx.raw_value for name, idx in report.all_index_results().items()}
        calib.set_all_bases(raws, BASE_DATE)
        # 再キャリブレーション
        for idx in report.all_index_results().values():
            cv = calib.calibrate(idx.index_name, idx.raw_value)
            if cv is not None:
                idx.calibrated_value = round(cv, 4)
        report.is_calibrated = True

    # DB 書き込み
    counts = save_report_to_db(report, dry_run=dry_run, method_version=method_version)
    logger.info("DB upsert: %s", counts)

    # ブリーフィング JSON 生成
    briefing = generate_briefing_json(report)
    if output_json:
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump(briefing, f, indent=2, ensure_ascii=False)
        logger.info("ブリーフィング JSON を %s に保存", output_json)
    else:
        print(json.dumps(briefing, indent=2, ensure_ascii=False))

    return report


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="GUITAR ATLAS Index Engine — 日次指標算出パイプライン"
    )
    parser.add_argument("--date", type=str, default=None,
                        help="算出対象日 YYYY-MM-DD（省略時=最新スナップショット日）")
    parser.add_argument("--dry-run", action="store_true",
                        help="DB への書き込みを行わない")
    parser.add_argument("--simulation", action="store_true",
                        help="過去データなし時の模擬デルタを注入")
    parser.add_argument("--set-base", action="store_true",
                        help="BASE_DATE (2026-06-25) のベース値をキャリブレーション定数として記録")
    parser.add_argument("--force-base", action="store_true",
                        help="--set-base と組み合わせて既存の定数を上書き")
    parser.add_argument("--output-json", type=str, default=None,
                        help="ブリーフィング JSON の書き出しパス")
    parser.add_argument("--method-version", type=str, default=METHOD_VERSION,
                        help="index_snapshots の method_version 文字列")

    args = parser.parse_args()

    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.set_base:
        return set_base_date_values(force=args.force_base)

    target_date = date.fromisoformat(args.date) if args.date else None

    try:
        run_engine(
            target_date     = target_date,
            dry_run         = args.dry_run,
            simulation_mode = args.simulation,
            method_version  = args.method_version,
            output_json     = args.output_json,
        )
    except Exception as e:
        logger.exception("Engine エラー: %s", e)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
