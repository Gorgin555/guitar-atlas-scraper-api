"""
GUITAR ATLAS — live 時系列バックテスト (COO 環境で実行)
======================================================
目的: 実 listings_daily 上で 5/14〜5/30 の日次指標を再計算し、
      重み v1.0 (または v1.1 修正式) のボラティリティ/カバレッジを確認する。
      weight_backtest_2026-05-30.md §6 の「失敗条件」を実データで判定する材料。

実行環境: COO ローカル (.venv + code/.env が必要、Supabase 到達必須)
  cd code && python3 -m index_engine.live_backtest
  # 期間変更: --start 2026-05-14 --end 2026-05-30
"""
from __future__ import annotations

import argparse
import statistics
from datetime import date, timedelta

from dotenv import load_dotenv

from .calculator import run_daily_calculation


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def main():
    # fetcher._get_supabase() は os.environ を直接参照するが、import 経由では
    # .env が読み込まれない（fetcher の load_dotenv は __main__ 限定）。
    # 本ハーネスの先頭で明示的に code/.env を読み込む。
    load_dotenv()

    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-05-14")
    ap.add_argument("--end",   default="2026-05-30")
    ap.add_argument("--window", type=int, default=7)
    args = ap.parse_args()

    start = date.fromisoformat(args.start)
    end   = date.fromisoformat(args.end)

    print("=" * 78)
    print(f"  GUITAR ATLAS live バックテスト  {start} 〜 {end}  (window={args.window}d)")
    print("  ※ simulation_mode=False = 実データのみ。デルタは実 listings_daily 由来。")
    print("=" * 78)
    header = f"  {'date':<12}{'GAI-E':>9}{'MFI':>9}{'VFI_AC':>9}{'BPI':>9} | {'VFIcov':>7}{'listings':>9}"
    print(header)
    print("-" * 78)

    series = {"GAI-E": [], "MFI": [], "VFI_AC": [], "BPI": []}
    vfi_cov = []

    for d in daterange(start, end):
        try:
            rep = run_daily_calculation(d, simulation_mode=False, window_days=args.window)
        except Exception as e:  # noqa: BLE001
            print(f"  {d.isoformat():<12}  ERROR: {e}")
            continue
        g = rep.gai_e.raw_value
        m = rep.mfi.raw_value
        v = rep.vfi_ac.raw_value
        b = rep.bpi.raw_value
        cov = rep.vfi_ac.n_models_with_data
        tot = rep.gai_e.total_listings
        series["GAI-E"].append(g); series["MFI"].append(m)
        series["VFI_AC"].append(v); series["BPI"].append(b)
        vfi_cov.append(cov)
        print(f"  {d.isoformat():<12}{g:>+9.3f}{m:>+9.3f}{v:>+9.3f}{b:>+9.3f} | {cov:>4}/18{tot:>9}")

    print("-" * 78)
    print("  【 ボラティリティ (標準偏差) / レンジ 】")
    for k, vals in series.items():
        if len(vals) >= 2:
            sd = statistics.pstdev(vals)
            print(f"    {k:<8}: σ={sd:7.3f}  min={min(vals):+8.3f}  max={max(vals):+8.3f}  range={max(vals)-min(vals):7.3f}")
    if vfi_cov:
        thin = sum(1 for c in vfi_cov if c < 5)
        print(f"    VFI カバレッジ: 平均 {statistics.mean(vfi_cov):.1f}/18 モデル、"
              f"5モデル未満の日 {thin}/{len(vfi_cov)}")

    print("\n  【 失敗条件チェック (weight_backtest_2026-05-30.md §6) 】")
    if series["GAI-E"] and len(series["GAI-E"]) >= 2:
        sd_g = statistics.pstdev(series["GAI-E"])
        flag = "⚠️ 要再審査 (地理スイング残存の疑い)" if sd_g > 3.0 else "OK"
        print(f"    GAI-E σ = {sd_g:.3f}  → {flag}  (参考閾値 σ>3.0)")
    if vfi_cov:
        avg_cov = statistics.mean(vfi_cov)
        flag = "⚠️ VFI ダウンウェイト検討" if avg_cov < 5 else "OK"
        print(f"    VFI 平均カバレッジ {avg_cov:.1f}/18  → {flag}")
    print("=" * 78)


if __name__ == "__main__":
    main()
