"""
GUITAR ATLAS — Index Engine: Vintage コンディション分類タグ自動付与
=================================================================
listing の title + description → condition_tags を Claude Sonnet で自動判定。

実装方針（CLO はぐりん監修）:
  - 7タグの多値分類（1リストに複数タグが付く可能性あり）
  - all_original と他の「劣化タグ」は原則排他だが、補足タグ（case_present, paperwork_present）は共存
  - 確信度が低い場合は [] を返す（過分類よりアンダー分類を優先）
  - バッチ処理: 1回の Claude 呼び出しで複数 listing を分類（コスト削減）

環境変数:
  ANTHROPIC_API_KEY: sk-ant-... （.env に設定済み）

使用例:
  from index_engine.vintage_tagger import VintageTagger
  tagger = VintageTagger()
  tags = tagger.classify_listing("1959 Gibson Les Paul Standard ...", "All original PAFs...")
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# 許可される全タグ（core_architecture.md 準拠）
ALLOWED_TAGS: set[str] = {
    "all_original",
    "partial_changed",
    "refin",
    "replaced_neck",
    "parts_caster",
    "case_present",
    "paperwork_present",
}

# タグの排他グループ（いずれか1つのみ）
EXCLUSIVE_GROUPS: list[set[str]] = [
    {"all_original", "partial_changed", "refin", "replaced_neck", "parts_caster"},
]

# タグの説明文（Claude へのプロンプト用）
TAG_DESCRIPTIONS = {
    "all_original":      "All original parts, no replacements, no refin. Every component is factory original.",
    "partial_changed":   "Some parts have been replaced (pickups, tuners, etc.) but not the neck.",
    "refin":             "The guitar has been refinished (refin). Original finish removed or painted over.",
    "replaced_neck":     "The neck has been replaced. This significantly affects vintage value.",
    "parts_caster":      "A parts guitar assembled from various sources, not a single original instrument.",
    "case_present":      "Original vintage case is included/pictured.",
    "paperwork_present": "Original hang tags, case candy, or ownership documentation is present.",
}


# ─────────────────────────────────────────────────────────────
# バッチ分類の入出力構造
# ─────────────────────────────────────────────────────────────

@dataclass
class ListingForTagging:
    """タグ付け対象の1件のリスティング"""
    listing_id:  str
    title:       str
    description: Optional[str]

    def to_text(self, max_desc_chars: int = 800) -> str:
        desc = (self.description or "")[:max_desc_chars]
        return f"Title: {self.title}\nDescription: {desc}"


@dataclass
class TaggingResult:
    """1件のタグ付け結果"""
    listing_id:  str
    tags:        list[str]
    confidence:  float        # 0.0〜1.0（Claudeの推定値）
    reasoning:   str          # 判定根拠（ログ/監査用）
    raw_response: str         # 生レスポンス（デバッグ用）


# ─────────────────────────────────────────────────────────────
# Vintage Tagger
# ─────────────────────────────────────────────────────────────

class VintageTagger:
    """
    Claude Sonnet を使ってヴィンテージギターのコンディションタグを自動付与する。

    Usage:
        tagger = VintageTagger()
        tags = tagger.classify_listing("1959 Les Paul", "All original PAFs...")
        # → ["all_original", "paperwork_present"]

        results = tagger.batch_classify([listing1, listing2, ...])
    """

    MODEL = "claude-sonnet-4-6"

    SYSTEM_PROMPT = """You are a vintage guitar expert and auction specialist.
Your task: analyze guitar listing text and assign condition classification tags.

AVAILABLE TAGS:
{tag_descriptions}

RULES:
1. Assign ONLY tags from the available list above.
2. The primary condition tags are mutually exclusive:
   [all_original, partial_changed, refin, replaced_neck, parts_caster]
   → Assign exactly ONE of these if determinable, or NONE if uncertain.
3. Supplementary tags [case_present, paperwork_present] can coexist with any primary tag.
4. When in doubt, assign FEWER tags (under-tagging preferred over over-tagging).
5. Base your judgment ONLY on what is explicitly stated or clearly implied in the listing text.

OUTPUT FORMAT (JSON only, no extra text):
{
  "tags": ["tag1", "tag2"],
  "confidence": 0.85,
  "reasoning": "Brief explanation of why these tags were assigned"
}"""

    BATCH_SYSTEM_PROMPT = """You are a vintage guitar expert.
Analyze MULTIPLE guitar listings and classify each one.

AVAILABLE TAGS:
{tag_descriptions}

RULES:
1. Assign ONLY tags from the available list.
2. Primary condition tags [all_original, partial_changed, refin, replaced_neck, parts_caster] are mutually exclusive.
3. Supplementary tags [case_present, paperwork_present] can coexist with any primary tag.
4. When in doubt, assign FEWER tags.

OUTPUT FORMAT: JSON array with one entry per listing, in the same order as input.
[
  {"listing_id": "...", "tags": [...], "confidence": 0.0-1.0, "reasoning": "..."},
  ...
]
Do NOT include any text outside the JSON array."""

    def __init__(self, api_key: Optional[str] = None):
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY が未設定。.env に ANTHROPIC_API_KEY=sk-ant-... を追加してください。"
            )
        import anthropic
        self._client = anthropic.Anthropic(api_key=key)

    def _format_tag_descriptions(self) -> str:
        lines = []
        for tag, desc in TAG_DESCRIPTIONS.items():
            lines.append(f"  - {tag}: {desc}")
        return "\n".join(lines)

    def _validate_and_clean_tags(self, raw_tags: list) -> list[str]:
        """
        APIレスポンスのタグを検証・クリーニング。
        - 不明なタグを除去
        - 排他グループの整合性チェック（複数ある場合は最初のものを採用）
        """
        # 1. 許可タグのみ残す
        valid = [t for t in raw_tags if isinstance(t, str) and t in ALLOWED_TAGS]

        # 2. 排他グループ内で複数ある場合、最初のものだけ残す
        for group in EXCLUSIVE_GROUPS:
            found = [t for t in valid if t in group]
            if len(found) > 1:
                keep = found[0]
                valid = [t for t in valid if t not in group or t == keep]
                logger.debug("排他グループ競合: %s → %s を採用", found, keep)

        return valid

    def classify_listing(
        self,
        title: str,
        description: Optional[str] = None,
        listing_id: str = "unknown",
    ) -> TaggingResult:
        """
        1件のリスティングをタグ分類する。

        Args:
            title:       リスティングタイトル
            description: リスティング説明文（None可）
            listing_id:  追跡用ID

        Returns:
            TaggingResult
        """
        text = f"Title: {title}\nDescription: {(description or '')[:800]}"

        system = self.SYSTEM_PROMPT.format(
            tag_descriptions=self._format_tag_descriptions()
        )

        try:
            resp = self._client.messages.create(
                model=self.MODEL,
                max_tokens=512,
                system=system,
                messages=[{"role": "user", "content": text}],
            )
            raw = resp.content[0].text.strip()
            data = json.loads(raw)
            tags = self._validate_and_clean_tags(data.get("tags", []))
            confidence = float(data.get("confidence", 0.0))
            reasoning = str(data.get("reasoning", ""))

            return TaggingResult(
                listing_id   = listing_id,
                tags         = tags,
                confidence   = confidence,
                reasoning    = reasoning,
                raw_response = raw,
            )

        except json.JSONDecodeError as e:
            logger.warning("[tagger] JSON parse error for listing %s: %s", listing_id, e)
            return TaggingResult(listing_id=listing_id, tags=[], confidence=0.0,
                                 reasoning="JSON parse error", raw_response="")
        except Exception as e:
            logger.error("[tagger] Claude API error for listing %s: %s", listing_id, e)
            return TaggingResult(listing_id=listing_id, tags=[], confidence=0.0,
                                 reasoning=str(e), raw_response="")

    def batch_classify(
        self,
        listings: list[ListingForTagging],
        batch_size: int = 10,
    ) -> list[TaggingResult]:
        """
        複数リスティングをバッチで分類（1 Claude 呼び出しで batch_size 件）。
        コスト削減のため推奨。

        Args:
            listings:   ListingForTagging のリスト
            batch_size: 1バッチあたりの件数（10件推奨）

        Returns:
            listing 順番通りの TaggingResult リスト
        """
        results: list[TaggingResult] = []
        system = self.BATCH_SYSTEM_PROMPT.format(
            tag_descriptions=self._format_tag_descriptions()
        )

        for i in range(0, len(listings), batch_size):
            batch = listings[i:i + batch_size]
            # バッチ入力テキスト作成
            items_text = []
            for lst in batch:
                items_text.append(
                    f"[LISTING {lst.listing_id}]\n{lst.to_text()}"
                )
            user_content = "\n\n---\n\n".join(items_text)

            try:
                resp = self._client.messages.create(
                    model=self.MODEL,
                    max_tokens=1024 + batch_size * 100,
                    system=system,
                    messages=[{"role": "user", "content": user_content}],
                )
                raw = resp.content[0].text.strip()
                parsed = json.loads(raw)

                # ID → TaggingResult マッピング
                id_to_result: dict[str, TaggingResult] = {}
                for item in parsed:
                    lid = str(item.get("listing_id", ""))
                    tags = self._validate_and_clean_tags(item.get("tags", []))
                    id_to_result[lid] = TaggingResult(
                        listing_id   = lid,
                        tags         = tags,
                        confidence   = float(item.get("confidence", 0.0)),
                        reasoning    = str(item.get("reasoning", "")),
                        raw_response = json.dumps(item),
                    )

                # 入力順に並び替え
                for lst in batch:
                    if lst.listing_id in id_to_result:
                        results.append(id_to_result[lst.listing_id])
                    else:
                        logger.warning("[tagger batch] listing_id %s が応答に含まれていない", lst.listing_id)
                        results.append(TaggingResult(
                            listing_id=lst.listing_id, tags=[], confidence=0.0,
                            reasoning="ID not in batch response", raw_response=raw
                        ))

            except (json.JSONDecodeError, Exception) as e:
                logger.error("[tagger batch] Error on batch %d: %s", i // batch_size, e)
                for lst in batch:
                    results.append(TaggingResult(
                        listing_id=lst.listing_id, tags=[], confidence=0.0,
                        reasoning=str(e), raw_response=""
                    ))

            logger.info("[tagger] Batch %d/%d 完了 (%d listings)",
                        i // batch_size + 1,
                        (len(listings) + batch_size - 1) // batch_size,
                        len(batch))

        return results


# ─────────────────────────────────────────────────────────────
# Supabase へのタグ書き戻し
# ─────────────────────────────────────────────────────────────

def update_condition_tags_in_db(
    results: list[TaggingResult],
    *,
    dry_run: bool = False,
    min_confidence: float = 0.6,
) -> dict[str, int]:
    """
    TaggingResult のタグを listings_daily.condition_tags に書き戻す。

    Args:
        results:        batch_classify() の結果リスト
        dry_run:        True = DBに書かず結果を表示のみ
        min_confidence: この値未満の confidence は書き込み対象外

    Returns:
        {"updated": N, "skipped_low_confidence": M, "errors": K}
    """
    import os
    from supabase import create_client

    sb = create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_KEY"],
    )

    counts = {"updated": 0, "skipped_low_confidence": 0, "errors": 0}

    for r in results:
        if r.confidence < min_confidence:
            counts["skipped_low_confidence"] += 1
            continue

        if not r.tags:
            # タグなし → 空配列で上書き（意図的にクリア）
            pass

        if dry_run:
            logger.info("[dry-run] %s → %s (conf=%.2f)", r.listing_id, r.tags, r.confidence)
            counts["updated"] += 1
            continue

        try:
            sb.table("listings_daily").update(
                {"condition_tags": r.tags}
            ).eq("listing_id", r.listing_id).execute()
            counts["updated"] += 1
        except Exception as e:
            logger.error("[tagger db] Update error for %s: %s", r.listing_id, e)
            counts["errors"] += 1

    return counts


# ─────────────────────────────────────────────────────────────
# VFI リスティングのタグ自動付与バッチ
# ─────────────────────────────────────────────────────────────

def run_vfi_tagging(
    *,
    snapshot_date: Optional[str] = None,
    dry_run: bool = False,
    min_confidence: float = 0.6,
    batch_size: int = 10,
) -> dict[str, int]:
    """
    listings_daily から VFI モデルの未タグ listing を取得してタグ付与。

    Args:
        snapshot_date:   対象日（None = 最新の snapshot_date）
        dry_run:         True = DB書き込みなし
        min_confidence:  タグ書き込みの最低信頼度
        batch_size:      1バッチの件数

    Returns:
        {"fetched": N, "updated": M, "skipped": K, "errors": L}
    """
    import os
    from supabase import create_client
    from dotenv import load_dotenv

    load_dotenv()
    sb = create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_KEY"],
    )

    # VFI product_id を取得
    bm_res = sb.table("basket_membership").select("product_id").eq("basket", "VFI").execute()
    vfi_product_ids = [r["product_id"] for r in (bm_res.data or [])]

    if not vfi_product_ids:
        logger.warning("[vfi_tagging] VFI モデルが basket_membership に存在しない")
        return {"fetched": 0, "updated": 0, "skipped": 0, "errors": 0}

    # 対象 listing を取得（condition_tags が空のもの）
    q = (
        sb.table("listings_daily")
        .select("listing_id, title, description")
        .in_("product_id", vfi_product_ids)
        .eq("condition_tags", "{}")  # 空配列
    )
    if snapshot_date:
        q = q.eq("snapshot_date", snapshot_date)

    res = q.execute()
    rows = res.data or []
    logger.info("[vfi_tagging] 対象 listing: %d 件", len(rows))

    if not rows:
        return {"fetched": 0, "updated": 0, "skipped": 0, "errors": 0}

    # ListingForTagging に変換
    listings = [
        ListingForTagging(
            listing_id  = r["listing_id"],
            title       = r.get("title") or "",
            description = r.get("description"),
        )
        for r in rows
    ]

    # タグ付与
    tagger = VintageTagger()
    results = tagger.batch_classify(listings, batch_size=batch_size)

    # DB書き戻し
    update_counts = update_condition_tags_in_db(results, dry_run=dry_run, min_confidence=min_confidence)

    return {
        "fetched":   len(rows),
        "updated":   update_counts["updated"],
        "skipped":   update_counts["skipped_low_confidence"],
        "errors":    update_counts["errors"],
    }


# ─────────────────────────────────────────────────────────────
# CLI スタンドアロン実行
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # クイックテスト
    tagger = VintageTagger()

    test_listings = [
        ListingForTagging(
            listing_id="test_001",
            title="1959 Gibson Les Paul Standard Sunburst",
            description="All original PAF humbuckers, original finish, comes with original brown hardshell case. All documentation intact.",
        ),
        ListingForTagging(
            listing_id="test_002",
            title="1957 Fender Stratocaster",
            description="Excellent player. Refin'd in the 70s, pickups are original, neck is original. No case.",
        ),
        ListingForTagging(
            listing_id="test_003",
            title="1962 Fender Telecaster Vintage",
            description="Replaced Fender neck from 1964, original body, bridge pickup replaced. Plays great.",
        ),
    ]

    print("=== Vintage Tagger テスト ===\n")
    results = tagger.batch_classify(test_listings)
    for r in results:
        print(f"[{r.listing_id}] tags={r.tags} confidence={r.confidence:.2f}")
        print(f"  理由: {r.reasoning}")
        print()

    sys.exit(0)
