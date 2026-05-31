"""
GUITAR ATLAS — Basket Matcher
================================
スクレイプした listing のタイトル文字列を Supabase の products テーブルに
マッチングし、(product_id, confidence) を返す。

■ マッチング戦略（3段階）
  1. brand + model キーワードの両方が含まれる → high confidence (0.85)
  2. brand のみ + 部分的な model ワード → medium (0.65)
  3. brand のみ → low (0.50)
  しきい値 0.50 未満 → None（unmatched）

■ Phase 1 active 58モデル + passive 45モデルの両方を対象とする
■ 大文字小文字・全角半角の正規化あり
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

logger = logging.getLogger(__name__)

# ── データクラス ──────────────────────────────────────────────────────────────

@dataclass
class MatchResult:
    product_id: str
    brand_name: str
    model: str
    basket_id: str
    is_passive: bool
    confidence: float


# ── テキスト正規化 ─────────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    """
    全角→半角変換 + 小文字化 + 記号除去。
    日本語タイトルと英語モデル名を比較可能にする。
    """
    # Unicode NFKC（全角数字/英字→半角）
    text = unicodedata.normalize("NFKC", text)
    text = text.lower()
    # ハイフン系を統一
    text = re.sub(r"[－―—–]", "-", text)
    # 不要な記号を除去（英数字・スペース・ハイフン・アポストロフィだけ残す）
    text = re.sub(r"[^\w\s\-']", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _keywords(text: str) -> set[str]:
    """正規化後の単語集合（2文字以上）。"""
    return {w for w in _normalize(text).split() if len(w) >= 2}


# ── ProductCatalog ─────────────────────────────────────────────────────────────

class ProductCatalog:
    """
    Supabase products テーブルを一括取得してメモリに保持する。
    頻繁にリクエストしないよう lru_cache を使う。
    """

    def __init__(self, products: list[dict]) -> None:
        self._products = products
        # 高速検索用インデックス: brand_name (normalized) → [product]
        self._by_brand: dict[str, list[dict]] = {}
        for p in products:
            key = _normalize(p.get("brand_name", ""))
            self._by_brand.setdefault(key, []).append(p)

    @classmethod
    def from_supabase(cls) -> "ProductCatalog":
        """Supabase から products を全件取得して ProductCatalog を返す。"""
        from supabase import create_client
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_KEY")
        if not (url and key):
            raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY missing in .env")
        sb = create_client(url, key)
        res = sb.table("products").select(
            "product_id, brand_name, model, basket_id, is_passive, year_range_str"
        ).execute()
        products = res.data or []
        logger.info("ProductCatalog loaded: %d products from Supabase", len(products))
        return cls(products)

    # ── メインマッチング ──────────────────────────────────────────────────────

    def match(self, title: str, hint_brand: Optional[str] = None) -> Optional[MatchResult]:
        """
        listing タイトル文字列を products テーブルにマッチングする。

        Args:
            title: スクレイプした listing タイトル（日英混在可）
            hint_brand: ブランド名のヒント（検索クエリのブランドが分かっている場合）

        Returns:
            MatchResult (confidence >= 0.50) or None
        """
        title_norm = _normalize(title)
        title_kw = _keywords(title)

        best: Optional[MatchResult] = None
        best_score = 0.0

        # ヒントブランドがある場合、そのブランドに絞って検索（高速化）
        brand_keys = list(self._by_brand.keys())
        if hint_brand:
            hint_norm = _normalize(hint_brand)
            # 完全一致優先、部分一致も考慮
            brand_keys = sorted(
                brand_keys,
                key=lambda k: (0 if k == hint_norm else (1 if hint_norm in k or k in hint_norm else 2))
            )

        for brand_key in brand_keys:
            products = self._by_brand[brand_key]

            # brand が title に含まれているか
            brand_in_title = brand_key in title_norm or any(
                w in title_kw for w in brand_key.split() if len(w) >= 3
            )

            for p in products:
                score = self._score(title_norm, title_kw, brand_key, brand_in_title, p)
                if score > best_score:
                    best_score = score
                    best = MatchResult(
                        product_id=p["product_id"],
                        brand_name=p["brand_name"],
                        model=p["model"],
                        basket_id=p.get("basket_id", ""),
                        is_passive=bool(p.get("is_passive", False)),
                        confidence=score,
                    )

        if best and best.confidence >= 0.50:
            logger.debug(
                "Match: '%s' → %s %s (conf=%.2f)",
                title[:60], best.brand_name, best.model, best.confidence,
            )
            return best

        logger.debug("No match (conf<0.50) for: '%s'", title[:60])
        return None

    def _score(
        self,
        title_norm: str,
        title_kw: set[str],
        brand_key: str,
        brand_in_title: bool,
        product: dict,
    ) -> float:
        """
        (title, product) のマッチングスコアを 0.0〜1.0 で返す。

        スコア構成:
          - brand 一致: +0.40
          - model 全ワード一致: +0.45
          - model 部分一致（半分以上）: +0.25
          - year_range がある場合（VFI 等）の年代一致: +0.10
          - 年代ミスマッチ（明らかに別世代）: -0.30
        """
        if not brand_in_title:
            return 0.0

        score = 0.40  # brand 一致ベース

        # モデル名のワードマッチ
        model_norm = _normalize(product.get("model", ""))
        model_kw = _keywords(model_norm)

        if not model_kw:
            return score

        # 全モデルワードが title に含まれる（完全一致）
        matched_kw = model_kw & title_kw
        match_ratio = len(matched_kw) / len(model_kw)

        if match_ratio >= 0.9:
            score += 0.45
        elif match_ratio >= 0.6:
            score += 0.25
        elif match_ratio >= 0.3:
            score += 0.10
        else:
            score += 0.0

        # VFI: 年代範囲チェック
        year_range = product.get("year_range_str", "")
        if year_range:
            score += self._year_bonus(title_norm, year_range)

        return min(score, 1.0)

    @staticmethod
    def _year_bonus(title_norm: str, year_range: str) -> float:
        """
        年代文字列（"1954-1959" など）を title から検出してボーナス/ペナルティを返す。
        """
        # year_range から start/end を抽出
        m = re.search(r"(\d{4})[^\d]+(\d{4})", year_range)
        if not m:
            return 0.0
        yr_start, yr_end = int(m.group(1)), int(m.group(2))

        # title 内の年号を抽出
        years_in_title = [int(y) for y in re.findall(r"\b(1[89]\d{2}|20[0-2]\d)\b", title_norm)]
        if not years_in_title:
            return 0.0

        # title の年号が年代範囲内か
        in_range = any(yr_start <= y <= yr_end for y in years_in_title)
        out_of_range = any(
            (y < yr_start - 5 or y > yr_end + 5) for y in years_in_title
        )

        if in_range and not out_of_range:
            return 0.10
        elif out_of_range:
            return -0.30
        return 0.0


# ── 受動的収集用：ブランドのみマッチング ────────────────────────────────────────

def match_passive_brand(title: str, brand: str, catalog: ProductCatalog) -> Optional[MatchResult]:
    """
    受動的収集（Pedals/Acoustic/Amps）用のブランドマッチング。
    brand が title に含まれているなら最低 confidence 0.50 で返す。
    """
    title_norm = _normalize(title)
    brand_norm = _normalize(brand)

    # ブランド名の主要ワードが title に含まれているか
    brand_words = [w for w in brand_norm.split() if len(w) >= 3]
    if not brand_words:
        return None

    if not any(w in title_norm for w in brand_words):
        return None

    # catalog から最もブランドが一致する product を探す
    return catalog.match(title, hint_brand=brand)
