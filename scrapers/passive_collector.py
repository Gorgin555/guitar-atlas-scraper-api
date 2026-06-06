"""
GUITAR ATLAS — 受動的収集モジュール
=====================================
Phase 1 中に Pedals / Acoustic / Amps / JBI のデータを先行収集し、
listings_daily に記録する。

■ 戦略（core_architecture.md より）
  - Phase 1 中は「記録のみ」。指標算出は Phase 2 以降。
  - Phase 2 開始時点で 3-6ヶ月分の時系列データが既に存在する状態を作る。
  - 各カテゴリの代表ブランドを basket_v1.yaml から読み込み、
    デジマート + Yahoo Auctions で収集する。

■ 収集ブランド（basket_v1.yaml passive_collection より）
  Pedals:   20ブランド（Klon, Strymon, Chase Bliss 等）
  Acoustic: 10ブランド（Collings, Bourgeois, Lowden 等）
  Amps:     10ブランド（Two-Rock, Dr.Z, Matchless 等）
  JBI:      5ブランド（T's Guitars, Saito, Momose 等）

Usage:
    from scrapers.passive_collector import PassiveCollector
    collector = PassiveCollector()
    results = collector.run(sources=["digimart", "yahoo"])
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

# basket_v1.yaml のパス解決
# ローカル開発 (ATLAS/code/scrapers/) と Railway デプロイ (repo root に scrapers/) の
# 両レイアウトに対応するため、複数候補を順に探索する。
# 明示上書きが必要な場合は env BASKET_YAML_PATH を設定する。
def _resolve_basket_yaml() -> Path:
    here = Path(__file__).resolve()
    candidates: list[Path] = []
    env_path = os.environ.get("BASKET_YAML_PATH")
    if env_path:
        candidates.append(Path(env_path))
    candidates += [
        here.parents[2] / "memory" / "data" / "basket_v1.yaml",  # ローカル: ATLAS/memory/data
        here.parents[1] / "memory" / "data" / "basket_v1.yaml",
        here.parents[1] / "data" / "basket_v1.yaml",
        here.parent / "basket_v1.yaml",
    ]
    cwd = Path.cwd()
    candidates += [
        cwd / "memory" / "data" / "basket_v1.yaml",
        cwd / "data" / "basket_v1.yaml",
        cwd / "basket_v1.yaml",
    ]
    for c in candidates:
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    raise FileNotFoundError(
        "basket_v1.yaml が見つかりません。探索候補: "
        + ", ".join(str(c) for c in candidates)
        + "。env BASKET_YAML_PATH で明示指定するか、デプロイ repo に data/basket_v1.yaml を配置してください。"
    )


# 後方互換の定数 (解決失敗時は None、実際の読み込みは _load_basket で都度解決)。
try:
    BASKET_YAML: Optional[Path] = _resolve_basket_yaml()
except FileNotFoundError:
    BASKET_YAML = None

# カテゴリごとの検索キーワード補完
# （ブランド名だけでは広すぎる場合に絞り込むキーワードを付与）
_CATEGORY_SUFFIX: dict[str, str] = {
    "pedals": "エフェクター effect pedal",
    "acoustic": "アコースティックギター acoustic guitar",
    "amps": "ギターアンプ guitar amplifier amp",
    "jbi_japan_boutique": "エレキギター electric guitar",
}


# ── PassiveCollector ──────────────────────────────────────────────────────────

class PassiveCollector:
    """
    受動的収集のオーケストレーター。

    basket_v1.yaml の passive_collection ブランドについて、
    DigimartScraper + YahooAuctionsScraper でデータ収集し
    listings_daily に upsert する。
    """

    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self._basket = self._load_basket()

    # ── basket_v1.yaml 読み込み ───────────────────────────────────────────

    def _load_basket(self) -> dict:
        path = _resolve_basket_yaml()  # 都度解決 (デプロイ後の配置/ env 反映を確実に拾う)
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    def get_passive_targets(self) -> list[dict[str, str]]:
        """
        passive_collection から収集対象ブランドの一覧を返す。

        Returns:
            list of {"category": str, "brand": str, "search_keyword": str}
        """
        targets = []
        pc = self._basket.get("passive_collection", {})

        for category, info in pc.items():
            brands = info.get("brands_to_track", [])
            suffix = _CATEGORY_SUFFIX.get(category, "guitar")

            for brand_entry in brands:
                # "Klon (Centaur, KTR)" のような括弧書きを分解
                brand_name = brand_entry.split("(")[0].strip()
                # 括弧内の代表モデルも検索に活用
                paren_match = __import__("re").search(r"\(([^)]+)\)", brand_entry)
                models_hint = paren_match.group(1) if paren_match else ""

                # メインの検索キーワード
                keyword = brand_name
                targets.append({
                    "category": category,
                    "brand": brand_name,
                    "brand_full": brand_entry,
                    "search_keyword": keyword,
                    "models_hint": models_hint,
                })

        logger.info(
            "PassiveCollector: %d brands loaded from basket_v1.yaml",
            len(targets),
        )
        return targets

    # ── Supabase への upsert ──────────────────────────────────────────────

    def _get_supabase(self):
        from supabase import create_client
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_KEY")
        if not (url and key):
            raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY missing in .env")
        return create_client(url, key)

    def _find_passive_product(self, brand: str, category: str) -> Optional[str]:
        """
        products テーブルから passive ブランドに対応する product_id を取得する。
        完全一致 → 部分一致の順で探す。
        """
        sb = self._get_supabase()
        # まず完全一致
        res = sb.table("products").select("product_id, brand_name").eq(
            "brand_name", brand
        ).eq("is_passive", True).limit(1).execute()
        if res.data:
            return res.data[0]["product_id"]

        # 部分一致（brand_name に brand が含まれる）
        res = sb.table("products").select("product_id, brand_name").ilike(
            "brand_name", f"%{brand}%"
        ).eq("is_passive", True).limit(1).execute()
        if res.data:
            return res.data[0]["product_id"]

        return None

    def _upsert_listings(self, rows: list[dict]) -> int:
        """listings_daily に upsert して upsert 件数を返す。"""
        if not rows:
            return 0
        if self.dry_run:
            logger.info("[dry-run] Would upsert %d passive listings", len(rows))
            return len(rows)
        sb = self._get_supabase()
        sb.table("listings_daily").upsert(
            rows,
            on_conflict="source,source_listing_id,snapshot_date",
        ).execute()
        return len(rows)

    # ── メイン収集ループ ──────────────────────────────────────────────────

    def run(
        self,
        sources: list[str] | None = None,
        max_pages_per_brand: int = 2,
        categories: list[str] | None = None,
    ) -> dict[str, int]:
        """
        全カテゴリのブランドについて受動的収集を実行する。

        Args:
            sources: ["digimart", "yahoo"] のサブセット（None で両方）
            max_pages_per_brand: ブランドごとの最大ページ数
            categories: 特定カテゴリのみ収集 (None で全カテゴリ)

        Returns:
            dict: {"total_listings": int, "errors": int}
        """
        if sources is None:
            sources = ["digimart", "yahoo"]

        from .digimart import DigimartScraper
        from .yahoo_auctions import YahooAuctionsScraper
        from .matcher import ProductCatalog

        scrapers = {}
        if "digimart" in sources:
            scrapers["digimart"] = DigimartScraper()
        if "yahoo" in sources:
            scrapers["yahoo"] = YahooAuctionsScraper()

        # products テーブルをキャッシュ
        try:
            catalog = ProductCatalog.from_supabase()
        except Exception as e:
            logger.warning("Could not load ProductCatalog: %s — will store without product_id", e)
            catalog = None

        targets = self.get_passive_targets()
        if categories:
            targets = [t for t in targets if t["category"] in categories]

        snapshot_date = datetime.now(timezone(timedelta(hours=9))).date().isoformat()  # JST (Asia/Tokyo) 明示, tzdata/TZ env 非依存
        counts = {"total_listings": 0, "errors": 0}

        for target in targets:
            brand = target["brand"]
            category = target["category"]
            keyword = target["search_keyword"]

            logger.info(
                "PassiveCollector: [%s] %s → '%s'",
                category, brand, keyword,
            )

            # product_id の解決（失敗しても収集は継続）
            product_id = None
            if catalog:
                match = catalog.match(brand, hint_brand=brand)
                if match:
                    product_id = match.product_id

            # 各ソースで収集
            for source_name, scraper in scrapers.items():
                rows_to_upsert = []
                try:
                    if source_name == "digimart":
                        gen = scraper.fetch(keyword, max_pages=max_pages_per_brand)
                    else:  # yahoo
                        gen = scraper.fetch(
                            keyword, mode="active", max_pages=max_pages_per_brand
                        )

                    for listing in gen:
                        listing["snapshot_date"] = snapshot_date
                        # product_id を付与（NULLの場合はカタログマッチを試みる）
                        if product_id:
                            listing["product_id"] = product_id
                            listing["matched_confidence"] = 0.60
                        elif catalog:
                            m = catalog.match(listing["title"], hint_brand=brand)
                            if m:
                                listing["product_id"] = m.product_id
                                listing["matched_confidence"] = m.confidence
                            else:
                                # product_id なしでは DB の FK 制約に違反する可能性
                                # → passive 専用の "umbrella" product が必要
                                #   今は skip してログに記録
                                logger.debug(
                                    "PassiveCollector: no product match for '%s', skipping",
                                    listing["title"][:60],
                                )
                                continue
                        else:
                            continue

                        # 受動的収集フラグ（raw_payload に記録）
                        listing["raw_payload"]["passive_category"] = category
                        listing["raw_payload"]["passive_brand"] = brand

                        rows_to_upsert.append(listing)

                    upserted = self._upsert_listings(rows_to_upsert)
                    counts["total_listings"] += upserted
                    logger.info(
                        "  [%s] %s: upserted %d listings",
                        source_name, brand, upserted,
                    )

                except Exception as e:
                    counts["errors"] += 1
                    logger.warning(
                        "PassiveCollector error [%s/%s]: %s", source_name, brand, e
                    )

        logger.info("PassiveCollector done: %s", counts)
        return counts
