"""
GUITAR ATLAS — Index Engine: CEO向けテストラン
=============================================
現在保有するデータ（listings_daily）を使って全指標を算出し、
CEO Tatsuya に重み調整判断を仰ぐためのレポートを生成・出力する。

実行方法:
    cd ~/Desktop/ATLAS/code
    source .venv/bin/activate
    python -m index_engine.run_test
    python -m index_engine.run_test --output-md ~/Desktop/test_run_report.md
    python -m index_engine.run_test --simulation   # シミュレーションモードで全成分を表示

担当: CSO スラリン
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date
from pathlib import Path
from textwrap import indent
from typing import Optional

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# レポート整形
# ─────────────────────────────────────────────────────────────

def _bar(value: float, max_val: float, width: int = 20) -> str:
    """ASCII プログレスバー"""
    filled = int(round(width * min(abs(value), max_val) / max(max_val, 1)))
    bar = "█" * filled + "░" * (width - filled)
    sign = "+" if value >= 0 else "-"
    return f"[{sign}{bar}]"


def _price_fmt(usd: Optional[float]) -> str:
    if usd is None:
        return "N/A"
    return f"${usd:,.0f}"


def _val_fmt(v: Optional[float], is_calibrated: bool) -> str:
    if v is None:
        return "N/A"
    calib_mark = "" if is_calibrated else " (raw)"
    return f"{v:.2f}{calib_mark}"


def build_report(report, simulation_mode: bool) -> str:
    """DailyIndexReport からテキストレポートを生成"""
    from .models import COMPONENT_WEIGHTS, GAI_E_WEIGHTS

    lines = []
    sep  = "=" * 70
    sep2 = "-" * 70

    def add(*args):
        lines.append(" ".join(str(a) for a in args))

    # ── ヘッダー ─────────────────────────────────────────────
    add(sep)
    add("  GUITAR ATLAS — Index Engine テストラン レポート")
    add(sep)
    add(f"  算出日:  {report.snapshot_date}")
    add(f"  実行者:  CSO スラリン")
    add(f"  モード:  {'🔶 シミュレーション（模擬デルタ注入）' if simulation_mode else '🟢 実データ（シングルスナップショット）'}")
    if not simulation_mode:
        add( "  ⚠️  現在データは1日分のみのため、デルタ成分はすべて 0 です。")
        add( "      シミュレーションモードは --simulation オプションで起動できます。")
    add(sep)
    add()

    # ── 1. 指標サマリー ───────────────────────────────────────
    add("【 1. 指標サマリー 】")
    add(sep2)
    add(f"  {'指標':<12}  {'値':>8}  {'avg価格 USD':>14}  {'カバレッジ':>12}  出品数")
    add(sep2)

    idx_map = report.all_index_results()
    for name in ["GAI-E", "MFI", "VFI_AC", "VFI_AO", "BPI"]:
        idx = idx_map[name]
        dv    = idx.display_value
        price = _price_fmt(idx.avg_price_usd)
        cov   = f"{idx.n_models_with_data}/{idx.n_models_total} models"
        calib_mark = "" if idx.calibrated_value is not None else " *"
        add(f"  {name:<12}  {dv:>8.2f}{calib_mark}  {price:>14}  {cov:>12}  {idx.total_listings:>6}")

    add()
    add("  * = 未キャリブレーション（raw スコア）。2026-06-01 以降に 100 基準に正規化されます。")
    add()

    # ── 2. 3スプレッド ───────────────────────────────────────
    add("【 2. 3スプレッド 】")
    add(sep2)
    add(f"  {'スプレッド':<22}  {'比率':>8}  {'解釈'}")
    add(sep2)

    spread_interpretations = {
        "BoutiquePremium":  "ブティックが現行フラッグシップの {r:.1f}x の評価",
        "VintagePremium":   "ヴィンテージが現行フラッグシップの {r:.1f}x の評価",
        "HeritageSpread":   "ヴィンテージがブティックの {r:.1f}x の評価",
    }

    for sname, sres in report.spreads.items():
        interp_tmpl = spread_interpretations.get(sname, "{r:.2f}x")
        interp = interp_tmpl.format(r=sres.ratio)
        add(f"  {sname:<22}  {sres.ratio:>8.4f}  {interp}")

    add()

    # ── 3. バスケット別モデル明細 ─────────────────────────────
    add("【 3. バスケット別モデル明細 】")

    for basket_name, idx_name in [("MFI", "MFI"), ("VFI (AC)", "VFI_AC"), ("BPI", "BPI")]:
        idx = idx_map[idx_name]
        add()
        add(f"  ▍ {basket_name}  (合計: {idx.total_listings} listings, avg: {_price_fmt(idx.avg_price_usd)})")
        add(f"  {'ID':<6}  {'Brand':<15}  {'Model':<38}  {'出品数':>6}  {'avg USD':>10}  Score")
        add("  " + "-" * 86)

        scores = sorted(idx.model_scores, key=lambda m: m.basket_id)
        for ms in scores:
            score = ms.component_score
            price = _price_fmt(ms.avg_price_current)
            has_mark = "  " if ms.has_data else "✗ "
            add(f"  {has_mark}{ms.basket_id:<5}  {ms.brand_name:<15}  {ms.model:<38}  "
                f"{ms.n_current:>6}  {price:>10}  {score:>+.3f}")

    add()

    # ── 4. IndexComponent 内訳（成分別） ─────────────────────
    add("【 4. IndexComponent 成分別スコア 】")
    add()
    add("  成分名                   重み    MFI     VFI_AC  BPI")
    add(sep2)

    components_data = {
        "avg_price_delta_pct":      "ΔAvgPrice%(7d)",
        "sale_velocity_delta":      "ΔSaleVelocity(7d)",
        "listing_volume_delta_pct": "ΔListingVolume%(7d)",
        "regional_spread_index":    "RegionalSpreadIndex",
        "mention_momentum":         "MentionMomentum",
    }

    for key, label in components_data.items():
        w = COMPONENT_WEIGHTS[key]
        mfi_v    = idx_map["MFI"].components.get(key, 0)
        vfi_v    = idx_map["VFI_AC"].components.get(key, 0)
        bpi_v    = idx_map["BPI"].components.get(key, 0)
        add(f"  {label:<26} {w:.2f}  {mfi_v:>+7.3f}  {vfi_v:>+7.3f}  {bpi_v:>+7.3f}")

    add(sep2)
    add(f"  {'IndexComponent (加重和)':<26}       "
        f"{idx_map['MFI'].raw_value:>+7.3f}  "
        f"{idx_map['VFI_AC'].raw_value:>+7.3f}  "
        f"{idx_map['BPI'].raw_value:>+7.3f}")
    add()

    # ── 5. GAI-E 合成内訳 ─────────────────────────────────────
    add("【 5. GAI-E 合成内訳 】")
    add()
    add(f"  GAI-E = {GAI_E_WEIGHTS['MFI']:.2f}×MFI + {GAI_E_WEIGHTS['VFI']:.2f}×VFI_AC + {GAI_E_WEIGHTS['BPI']:.2f}×BPI")
    mfi_contrib = GAI_E_WEIGHTS["MFI"] * idx_map["MFI"].display_value
    vfi_contrib = GAI_E_WEIGHTS["VFI"] * idx_map["VFI_AC"].display_value
    bpi_contrib = GAI_E_WEIGHTS["BPI"] * idx_map["BPI"].display_value
    add(f"        = {mfi_contrib:.4f} + {vfi_contrib:.4f} + {bpi_contrib:.4f}")
    add(f"        = {report.gai_e.display_value:.4f}")
    add()

    # ── 6. CEO向け重み調整ガイド ──────────────────────────────
    add("【 6. CSO スラリンからの重み調整ガイド 】")
    add(sep2)
    add("""
  現在の重みセット（v1.0）と CEO に検討いただきたい代替案を提示します。

  ── IndexComponent 成分重み ──────────────────────────────────────
  現行 v1.0（core_architecture.md 承認済み）:
    ΔAvgPrice%(7d)       : 0.35  ← 価格変動を最重要視
    ΔSaleVelocity(7d)    : 0.25  ← 成約速度で市場の熱量を捉える
    ΔListingVolume%(7d)  : 0.15  ← 供給サイドの変化
    RegionalSpreadIndex  : 0.10  ← US/JP 需給差（地域分散の補正）
    MentionMomentum      : 0.15  ← SNS言及（Phase 2 以降本格化）

  代替案 A（価格純化型）: 価格への依存を高め、他成分を圧縮
    ΔAvgPrice%(7d)       : 0.50
    ΔSaleVelocity(7d)    : 0.25
    ΔListingVolume%(7d)  : 0.10
    RegionalSpreadIndex  : 0.05
    MentionMomentum      : 0.10
    → より「価格指数」寄り。Bloomberg 比較では説明しやすい。

  代替案 B（流動性重視型）: 成約速度 + 出品量をより重視
    ΔAvgPrice%(7d)       : 0.30
    ΔSaleVelocity(7d)    : 0.35  ← 上げる
    ΔListingVolume%(7d)  : 0.20  ← 上げる
    RegionalSpreadIndex  : 0.05
    MentionMomentum      : 0.10
    → 価格は硬直しがちなので「市場の活発さ」を前面に出す案。

  ── GAI-E 合成重み ────────────────────────────────────────────────
  現行 v1.0:
    MFI : 0.40  VFI : 0.30  BPI : 0.30
    → バランス型。現行品の動向を最も重く扱う。

  代替案 C（Vintage 重視）:
    MFI : 0.30  VFI : 0.40  BPI : 0.30
    → ヴィンテージ市場の動向をヘッドラインとして打ち出したい場合。

  代替案 D（Boutique 特化）:
    MFI : 0.30  VFI : 0.25  BPI : 0.45
    → B2B向け（Suhr / Anderson 等）の感度を上げる場合。

  ── CEO へのお願い ────────────────────────────────────────────────
  このレポートを見ていただいて、以下をご指示ください：
  1. 現行 v1.0 の重みを承認するか、代替案から選択するか
  2. VFI のスプレッド計算に VFI_AO / VFI_AC どちらを使うか（現行: VFI_AC）
  3. その他調整希望があればご指示ください

  → 決定後、スラリンが core_architecture.md と basket_v1.yaml を更新します。
""")
    add(sep2)
    add()

    # ── 7. 次のアクション ─────────────────────────────────────
    add("【 7. 次のアクションリスト 】")
    add()
    add("  [ ] CEO: 上記の重みを確認 → 承認 or 代替案を選択")
    add("  [ ] COO ドレアム: fetch_listings.py に dedupe 修正（5分）")
    add("  [ ] COO ドレアム: n8n の朝6:00 JST 日次バッチを設定")
    add("  [ ] CSO スラリン: 承認された重みを core_architecture.md に反映")
    add("  [ ] CSO スラリン: 2026-06-01 = Index 100 のキャリブレーション本番実行")
    add("  [ ] CLO はぐりん: デジマート/Yahoo Auctions スクレイパー規約レビュー")
    add()
    add(sep)
    add("  GUITAR ATLAS — Index Engine v1.0  |  CSO スラリン  |  2026-05-14")
    add(sep)

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# メイン実行
# ─────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="GUITAR ATLAS — CEO向けテストラン"
    )
    parser.add_argument("--simulation", action="store_true",
                        help="シミュレーションモード（模擬デルタを注入して全成分を表示）")
    parser.add_argument("--date", type=str, default=None,
                        help="算出対象日 YYYY-MM-DD（省略時=最新スナップショット日）")
    parser.add_argument("--output-md", type=str, default=None,
                        help="Markdown レポートの出力パス（省略時=標準出力）")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="詳細ログを表示")
    args = parser.parse_args()

    load_dotenv()
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(message)s")

    # 接続確認
    for key in ["SUPABASE_URL", "SUPABASE_SERVICE_KEY"]:
        if not os.environ.get(key):
            print(f"✗ {key} が .env に設定されていません")
            return 1

    from .fetcher import get_latest_snapshot_date
    from .calculator import run_daily_calculation
    from .calibrator import get_default_calibrator

    # 対象日
    target_date = date.fromisoformat(args.date) if args.date else get_latest_snapshot_date()
    if target_date is None:
        print("✗ listings_daily にデータがありません。先に fetch_listings.py を実行してください。")
        return 1

    print(f"\n▶ 指標算出開始: {target_date}  (simulation={args.simulation})\n")

    calib = get_default_calibrator()

    # 算出実行
    report = run_daily_calculation(
        target_date,
        simulation_mode = args.simulation,
        calibrator      = calib,
    )

    # レポート生成
    report_text = build_report(report, simulation_mode=args.simulation)

    if args.output_md:
        out_path = Path(args.output_md)
        # Markdown 形式で出力
        md_lines = ["```\n", report_text, "\n```\n"]
        out_path.write_text("".join(md_lines), encoding="utf-8")
        print(f"\n✓ レポートを {out_path} に保存しました。")
    else:
        print(report_text)

    # index_snapshots へのサンプル行表示
    print("\n─── index_snapshots に書き込まれる行（--dry-run で確認） ───")
    for row in report.all_snapshot_rows():
        print(f"  {row['snapshot_date']}  {row['index_name']:<14}  value={row['value']:>10.4f}")

    print(f"\n✓ テストラン完了。'python -m index_engine.engine --dry-run' で DB書き込みをドライランできます。\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
