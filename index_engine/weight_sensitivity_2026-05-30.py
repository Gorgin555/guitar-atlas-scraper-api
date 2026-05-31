"""
GUITAR ATLAS — 重み感度分析 / 構造点検 (2026-05-30, CSO スラリン, Opus)
=====================================================================
目的: 6/1 の指標重みロック前に、重み v1.0 の構造的健全性を点検する。

データ前提 (重要・正直な明示):
  - 5/13 テストランは simulation_mode=True。
    → avg_price / sale_velocity / listing_volume の3デルタは Gaussian 注入値 (擬似)。
    → RegionalSpreadIndex (+55.0/+0.0/+44.2) と MentionMomentum (0/0/0) のみ実データ。
  - したがって「市場の実シグナル」としての GAI-E +3.94 は読まない。
  - 本スクリプトが厳密に主張できるのは「式の構造」に関する点 (スケール/死重み) のみ。
    これらは 18日分の実時系列が無くても formula レベルで成立する。

stdlib のみ。pip 不要。`python3 weight_sensitivity_2026-05-30.py` で実行。
"""

# ── 実測コンポーネント値 (5/13 テストラン由来) ───────────────────────
# regional / mention は実データ。price/velocity/volume は simulation 注入値 (擬似)。
COMPONENTS = {
    #            price     velocity  volume   regional  mention
    "MFI":    {"price": -0.698, "velocity": 4.978, "volume": 1.976, "regional": 55.003, "mention": 0.0},
    "VFI_AC": {"price":  3.123, "velocity": -7.443, "volume": -6.981, "regional": 0.000, "mention": 0.0},
    "BPI":    {"price":  1.312, "velocity": 3.058, "volume": 1.630, "regional": 44.233, "mention": 0.0},
}
COMP_W = {"price": 0.35, "velocity": 0.25, "volume": 0.15, "regional": 0.10, "mention": 0.15}
GAI_W  = {"MFI": 0.40, "VFI_AC": 0.30, "BPI": 0.30}

KEYS = ["price", "velocity", "volume", "regional", "mention"]
LABEL = {"price": "ΔAvgPrice%", "velocity": "ΔSaleVelocity", "volume": "ΔListingVol%",
         "regional": "RegionalSpread", "mention": "MentionMomentum"}


def index_component(comp, w):
    return sum(comp[k] * w[k] for k in KEYS)


def gai_e(basket_vals, gw):
    return sum(basket_vals[b] * gw[b] for b in gw)


def hr(c="-"):
    print(c * 72)


# ════════════════════════════════════════════════════════════════════
print("=" * 72)
print("  GUITAR ATLAS 重み感度 / 構造点検  —  2026-05-30  CSO スラリン (Opus)")
print("=" * 72)

# ── 1. 各バスケットの寄与分解 (どの成分が headline を作っているか) ──────
print("\n【 1. IndexComponent の成分寄与分解 】")
print("  各セル = 成分値 × 重み = 寄与ポイント。|寄与| 最大の成分が headline を支配する。")
hr()
print(f"  {'成分':<16}{'重み':>6} | " + " | ".join(f"{b:>10}" for b in COMPONENTS))
hr()
basket_ic = {}
contrib = {b: {} for b in COMPONENTS}
for k in KEYS:
    row = f"  {LABEL[k]:<16}{COMP_W[k]:>6.2f} | "
    cells = []
    for b in COMPONENTS:
        c = COMPONENTS[b][k] * COMP_W[k]
        contrib[b][k] = c
        cells.append(f"{c:>+10.3f}")
    print(row + " | ".join(cells))
hr()
ic_row = f"  {'IndexComponent':<16}{'':>6} | "
for b in COMPONENTS:
    basket_ic[b] = index_component(COMPONENTS[b], COMP_W)
print(ic_row + " | ".join(f"{basket_ic[b]:>+10.3f}" for b in COMPONENTS))

# regional の支配率
print("\n  ▸ RegionalSpread が IndexComponent に占める割合 (|寄与| 比):")
for b in COMPONENTS:
    tot_abs = sum(abs(contrib[b][k]) for k in KEYS)
    share = abs(contrib[b]["regional"]) / tot_abs * 100 if tot_abs else 0
    print(f"      {b:<8}: regional寄与 {contrib[b]['regional']:>+7.3f} / |寄与|合計 {tot_abs:6.3f} = {share:5.1f}%")

g0 = gai_e(basket_ic, GAI_W)
print(f"\n  ▸ 現行式の GAI-E = {g0:+.4f}")
print("    (注: price/velocity/volume が擬似のため値自体は読まない。regional 支配の構造に注目)")

# ── 2. 構造的欠陥 #1: RegionalSpread のスケール不整合 ────────────────
print("\n【 2. 構造点検 #1 — RegionalSpreadIndex のスケール不整合 】")
print("  regional は -100〜+100 スケール。他3デルタは概ね ±数%〜±数十%。")
print("  → 重み 0.10 でも +55×0.10=+5.5 となり、price(0.35) を圧倒。")
print("  対策案: regional を /10 にリスケール (実効 ±10 スケールへ) して同じ 0.10 を適用。")
hr()
W_fix1 = dict(COMP_W)
COMP_fix1 = {b: dict(COMPONENTS[b]) for b in COMPONENTS}
for b in COMP_fix1:
    COMP_fix1[b]["regional"] = COMP_fix1[b]["regional"] / 10.0
ic_fix1 = {b: index_component(COMP_fix1[b], W_fix1) for b in COMP_fix1}
for b in COMPONENTS:
    print(f"  {b:<8}: 現行 IC {basket_ic[b]:>+8.3f}  →  regional/10 後 {ic_fix1[b]:>+8.3f}")
g1 = gai_e(ic_fix1, GAI_W)
print(f"  GAI-E: {g0:+.4f}  →  {g1:+.4f}  (regional 人工押し上げ分 {g0 - g1:+.4f} が剥落)")

# ── 3. 構造的欠陥 #2: MentionMomentum 死重み ───────────────────────
print("\n【 3. 構造点検 #2 — MentionMomentum (0.15) が Phase 1 で恒常ゼロ 】")
print("  Phase 1 は TREND データ無し → mention=0。重み 0.15 は inert (15%が死重み)。")
print("  影響A: IndexComponent の振幅が一律 ×0.85 に圧縮 (4成分の実効和が0.85)。")
print("  影響B: TH-16 (7/15) で mention が起動した瞬間に式の性格が変わる")
print("         → 6/1 ベースで守るはずの時系列連続性が途中で折れる (構造的断絶)。")
hr()
# mention を 0 にして残り4成分を 1.0 に再正規化
W_fix2 = {k: COMP_W[k] for k in KEYS if k != "mention"}
s = sum(W_fix2.values())
W_fix2 = {k: v / s for k, v in W_fix2.items()}
W_fix2["mention"] = 0.0
print("  再正規化後の実効重み (Phase 1):")
print("    " + "  ".join(f"{LABEL[k]}={W_fix2[k]:.3f}" for k in KEYS if k != "mention"))
ic_fix2 = {b: index_component(COMPONENTS[b], W_fix2) for b in COMPONENTS}
for b in COMPONENTS:
    print(f"  {b:<8}: 現行 IC {basket_ic[b]:>+8.3f}  →  mention除去+再正規化 {ic_fix2[b]:>+8.3f}  (×{ic_fix2[b]/basket_ic[b] if basket_ic[b] else 0:.3f})")
g2 = gai_e(ic_fix2, GAI_W)
print(f"  GAI-E: {g0:+.4f}  →  {g2:+.4f}")

# ── 4. 両対策を同時適用 ─────────────────────────────────────────────
print("\n【 4. 両対策同時 (regional/10 + mention除去再正規化) 】")
W_both = {k: COMP_W[k] for k in KEYS if k != "mention"}
s = sum(W_both.values()); W_both = {k: v/s for k, v in W_both.items()}; W_both["mention"] = 0.0
ic_both = {b: index_component(COMP_fix1[b], W_both) for b in COMP_fix1}
for b in COMPONENTS:
    sh = abs(COMP_fix1[b]["regional"]*W_both["regional"]) / sum(abs(COMP_fix1[b][k]*W_both[k]) for k in KEYS) * 100 if any(COMP_fix1[b][k] for k in KEYS) else 0
    print(f"  {b:<8}: IC {ic_both[b]:>+8.3f}   (regional 支配率 {sh:4.1f}%)")
g3 = gai_e(ic_both, GAI_W)
print(f"  GAI-E (修正式) = {g3:+.4f}")

# ── 5. GAI-E 合成重みの感度 (0.40/0.30/0.30 を ±0.05 振る) ───────────
print("\n【 5. GAI-E 合成重み感度 (修正式の IC を使用、MFI 重みを振る) 】")
print("  VFI/BPI は残余を均等配分。headline の重み依存度を見る。")
hr()
print(f"  {'MFI_w':>6}{'VFI_w':>7}{'BPI_w':>7} | {'GAI-E':>9}")
hr()
for mfi_w in [0.30, 0.35, 0.40, 0.45, 0.50]:
    rest = (1 - mfi_w) / 2
    gw = {"MFI": mfi_w, "VFI_AC": rest, "BPI": rest}
    print(f"  {mfi_w:>6.2f}{rest:>7.3f}{rest:>7.3f} | {gai_e(ic_both, gw):>+9.4f}")
print("  → 修正式では各バスケット IC が同オーダー(数%)に揃うため、")
print("    合成重みを ±0.05〜0.10 振っても GAI-E の符号・大きさは安定 (頑健)。")

# ── 6. VFI データ薄さの警告 (実データ) ──────────────────────────────
print("\n【 6. データ品質警告 — VFI バスケットの統計的脆弱性 】")
print("  5/13 実測: VFI listings = 31件 / 18モデル = 平均 1.7 obs/model。")
print("  avg価格 $40,630 は Gibson Burst ($280k級) が牽引 → 中央値乖離大。")
print("  VFI は GAI-E の 30% を占めるが、最もノイズが大きい。")
print("  → 7日窓では VFI が空 or 1-2件のモデルが頻発し、週次で大きく振れるリスク。")

print("\n" + "=" * 72)
print("  まとめ: 値ではなく『式の構造』に 2つの是正点。詳細は memo 参照。")
print("=" * 72)
