"""
GUITAR ATLAS - 58モデルマスター投入スクリプト
=============================================

`memory/data/basket_v1.yaml` を読み込み、以下を投入する:

  1. brands         : ブランドマスター（重複時はスキップ）
  2. products       : 58モデル + 受動収集対象
  3. basket_membership : 各モデルがどの Index (MFI/VFI/BPI) に属するか

冪等。再実行しても重複しない（ON CONFLICT DO NOTHING / UPDATE）。

Usage:
    cd ~/Desktop/ATLAS/code
    source .venv/bin/activate
    python -m ingest.seed_products
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .db import pg_conn

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]   # ~/Desktop/ATLAS
BASKET_PATH = REPO_ROOT / "memory" / "data" / "basket_v1.yaml"


# ---------------------------------------------------------------------------
# YAML → 構造化レコード
# ---------------------------------------------------------------------------

def _parse_year_range(year_range_str: str | None) -> tuple[int | None, int | None]:
    if not year_range_str:
        return None, None
    m = re.match(r"^\s*(\d{4})\s*-\s*(\d{4})\s*$", year_range_str)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.match(r"^\s*(\d{4})\s*$", year_range_str)
    if m:
        y = int(m.group(1))
        return y, y
    return None, None


def _flatten_basket(yaml_data: dict) -> tuple[list[dict], list[dict]]:
    """
    basket_v1.yaml → (products, memberships)
    """
    products: list[dict] = []
    memberships: list[dict] = []

    for section_key, basket_name in (("mfi", "MFI"), ("vfi", "VFI"), ("bpi", "BPI")):
        section = yaml_data.get(section_key) or {}
        for _brand_group_key, items in section.items():
            for item in items:
                year_min, year_max = _parse_year_range(item.get("year_range"))
                products.append({
                    "basket_id": item["id"],
                    "brand_name": item["brand"],
                    "model": item["model"],
                    "category": item.get("category"),
                    "year_min": year_min,
                    "year_max": year_max,
                    "year_range_str": item.get("year_range"),
                    "notes": item.get("note"),
                    "is_passive": False,
                    "passive_category": None,
                })
                memberships.append({
                    "basket_id": item["id"],
                    "basket": basket_name,
                    "weight": 1.0,
                })

    # passive collection
    passive = yaml_data.get("passive_collection") or {}
    passive_map = {
        "pedals": "pedal",
        "acoustic": "acoustic",
        "amps": "amp",
        "jbi_japan_boutique": "jbi",
    }
    for key, cat in passive_map.items():
        block = passive.get(key) or {}
        brands = block.get("brands_to_track") or []
        for idx, brand_label in enumerate(brands, start=1):
            # ブランド名とモデル名をシンプル分解（"Klon (Centaur, KTR)" → brand="Klon", model="Centaur, KTR"）
            m = re.match(r"^\s*([^()]+?)\s*\(([^)]*)\)\s*$", brand_label)
            if m:
                brand = m.group(1).strip()
                model = m.group(2).strip()
            else:
                brand = brand_label.strip()
                model = "(brand-level passive tracking)"
            basket_id = f"P-{cat.upper()}-{idx:02d}"
            products.append({
                "basket_id": basket_id,
                "brand_name": brand,
                "model": model,
                "category": cat,
                "year_min": None,
                "year_max": None,
                "year_range_str": None,
                "notes": "Phase 1 passive collection",
                "is_passive": True,
                "passive_category": cat,
            })
            memberships.append({
                "basket_id": basket_id,
                "basket": "PASSIVE",
                "weight": 0.0,
            })

    return products, memberships


# ---------------------------------------------------------------------------
# DB 投入
# ---------------------------------------------------------------------------

UPSERT_BRAND_SQL = """
INSERT INTO brands (name, country, tier)
VALUES (%(name)s, %(country)s, %(tier)s)
ON CONFLICT (name) DO UPDATE SET
    country = COALESCE(EXCLUDED.country, brands.country),
    tier    = COALESCE(EXCLUDED.tier,    brands.tier)
RETURNING brand_id;
"""

UPSERT_PRODUCT_SQL = """
INSERT INTO products (
    basket_id, brand_id, brand_name, model, variant, category,
    year_min, year_max, year_range_str, is_passive, passive_category, notes
) VALUES (
    %(basket_id)s, %(brand_id)s, %(brand_name)s, %(model)s, %(variant)s, %(category)s,
    %(year_min)s, %(year_max)s, %(year_range_str)s, %(is_passive)s, %(passive_category)s, %(notes)s
)
ON CONFLICT (basket_id) DO UPDATE SET
    brand_id         = EXCLUDED.brand_id,
    brand_name       = EXCLUDED.brand_name,
    model            = EXCLUDED.model,
    variant          = EXCLUDED.variant,
    category         = EXCLUDED.category,
    year_min         = EXCLUDED.year_min,
    year_max         = EXCLUDED.year_max,
    year_range_str   = EXCLUDED.year_range_str,
    is_passive       = EXCLUDED.is_passive,
    passive_category = EXCLUDED.passive_category,
    notes            = EXCLUDED.notes,
    updated_at       = NOW()
RETURNING product_id;
"""

UPSERT_MEMBERSHIP_SQL = """
INSERT INTO basket_membership (product_id, basket, weight)
VALUES (%(product_id)s, %(basket)s, %(weight)s)
ON CONFLICT (product_id, basket) DO UPDATE SET
    weight = EXCLUDED.weight;
"""


def _infer_brand_meta(brand_name: str, is_passive: bool, passive_category: str | None) -> dict:
    """ブランドの country / tier を雑に推定する。手動で後追い修正可能。"""
    country_map = {
        "Fender": "USA", "Gibson": "USA", "PRS": "USA",
        "Suhr": "USA", "Tom Anderson": "USA", "Knaggs": "USA",
        "James Tyler": "USA", "Don Grosh": "USA", "Asher": "USA",
        "Tausch": "Germany",
        "T's Guitars": "Japan", "Saito Guitars": "Japan",
        "Momose": "Japan", "Bacchus Handmade": "Japan", "Crews": "Japan",
    }
    if is_passive:
        tier = "passive"
    elif brand_name in {"Fender", "Gibson", "PRS"}:
        tier = "mainstream"
    else:
        tier = "boutique"
    return {
        "name": brand_name,
        "country": country_map.get(brand_name),
        "tier": tier,
    }


def seed(yaml_path: Path = BASKET_PATH) -> dict:
    if not yaml_path.exists():
        raise FileNotFoundError(f"basket file not found: {yaml_path}")

    with yaml_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    products, memberships = _flatten_basket(data)

    counts = {"brands": 0, "products": 0, "memberships": 0}
    brand_id_cache: dict[str, str] = {}
    product_id_cache: dict[str, str] = {}

    with pg_conn() as conn:
        with conn.cursor() as cur:
            # 1) brands
            unique_brands = {}
            for p in products:
                meta = _infer_brand_meta(p["brand_name"], p["is_passive"], p["passive_category"])
                unique_brands.setdefault(meta["name"], meta)

            for meta in unique_brands.values():
                cur.execute(UPSERT_BRAND_SQL, meta)
                row = cur.fetchone()
                brand_id_cache[meta["name"]] = str(row["brand_id"])
                counts["brands"] += 1

            # 2) products
            for p in products:
                row_in = {
                    **p,
                    "brand_id": brand_id_cache[p["brand_name"]],
                    "variant": None,
                }
                cur.execute(UPSERT_PRODUCT_SQL, row_in)
                row = cur.fetchone()
                product_id_cache[p["basket_id"]] = str(row["product_id"])
                counts["products"] += 1

            # 3) memberships
            for m in memberships:
                cur.execute(UPSERT_MEMBERSHIP_SQL, {
                    "product_id": product_id_cache[m["basket_id"]],
                    "basket": m["basket"],
                    "weight": m["weight"],
                })
                counts["memberships"] += 1

        conn.commit()

    return counts


def main() -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    logger.info("Seeding from %s ...", BASKET_PATH)
    counts = seed()
    logger.info("Done. brands=%(brands)s, products=%(products)s, memberships=%(memberships)s", counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
