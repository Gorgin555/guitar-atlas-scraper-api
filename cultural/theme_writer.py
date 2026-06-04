"""
GUITAR ATLAS TH-19 cultural theme write layer.

Created: 2026-06-01
Purpose: Validate, draft-upsert, and publish weekly cultural themes.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


DISCLAIMER_VERSION: str = "report_data_notice_v1"
LAYER_SOURCE_PHASE1: str = "market"
HERO_MIN: int = 3
HERO_MAX: int = 10
BANNED_TERMS: tuple[str, ...] = (
    "買い時",
    "売り時",
    "投資妙味",
    "投資価値",
    "値上がり益",
    "利確",
    "損切り",
    "相場",
    "相場観",
    "銘柄",
    "買い推奨",
    "売り推奨",
)

_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_supabase_client = None


@dataclass
class ThemeDraft:
    week_start: str
    slug: str
    headline: str
    summary: str
    body: str
    hero_product_ids: list[str]
    supporting_trend_ids: list[str]
    connected_index_date: str
    layer_source: str = LAYER_SOURCE_PHASE1
    author_model: str = "sonnet"
    disclaimer_version: str = DISCLAIMER_VERSION


class PublishGuardError(Exception):
    """Raised when a cultural theme cannot pass the publish guards."""


def get_supabase():
    """Return a lazily initialized Supabase service client using Legacy JWT env vars."""
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client

    try:
        from supabase import create_client
    except ImportError as exc:  # pragma: no cover - depends on deployment env
        raise RuntimeError("supabase package is required for database access") from exc

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY are required")

    _supabase_client = create_client(url, key)
    return _supabase_client


def validate_draft(draft: ThemeDraft) -> list[str]:
    """Return validation failures for a ThemeDraft; an empty list means valid."""
    reasons: list[str] = []

    if not (HERO_MIN <= len(draft.hero_product_ids) <= HERO_MAX):
        reasons.append(f"V-1 hero_product_ids must contain {HERO_MIN}..{HERO_MAX} ids")

    if len(draft.supporting_trend_ids) < 1:
        reasons.append("V-2 supporting_trend_ids must contain at least 1 id")

    if not draft.connected_index_date:
        reasons.append("V-3 connected_index_date is required")
    elif not _is_iso_date(draft.connected_index_date):
        reasons.append("V-3 connected_index_date must be an ISO date")

    if draft.layer_source != LAYER_SOURCE_PHASE1:
        reasons.append("V-4 layer_source must be 'market' in Phase 1")

    banned_found = _find_banned_terms(draft)
    if banned_found:
        terms = ", ".join(banned_found)
        reasons.append(f"V-5 banned terms found in headline/summary/body: {terms}")

    if not _SLUG_PATTERN.fullmatch(draft.slug):
        reasons.append("V-6 slug must be kebab-case")
    if not _is_iso_date(draft.week_start):
        reasons.append("V-6 week_start must be an ISO date")

    return reasons


def resolve_is_beta(supporting_trend_ids: list[str], *, client) -> bool:
    """Return the OR of trend_snapshots.is_beta, treating missing ids as beta."""
    if not supporting_trend_ids:
        return True

    expected_ids = set(supporting_trend_ids)
    rows = (
        client.table("trend_snapshots")
        .select("snapshot_id,is_beta")
        .in_("snapshot_id", list(expected_ids))
        .execute()
        .data
        or []
    )
    found_ids = {row.get("snapshot_id") for row in rows}
    if found_ids != expected_ids:
        return True
    return any(bool(row.get("is_beta")) for row in rows)


def upsert_theme(draft: ThemeDraft, *, client, dry_run: bool = False) -> dict:
    """Validate and upsert a cultural theme as draft, with is_beta auto-derived."""
    reasons = validate_draft(draft)
    if reasons:
        raise ValueError("; ".join(reasons))

    is_beta = resolve_is_beta(draft.supporting_trend_ids, client=client)
    row = _draft_to_row(draft, is_beta=is_beta)
    if dry_run:
        return {"theme_id": None, "is_beta": is_beta, "status": "draft", "dry_run": True}

    response = (
        client.table("cultural_themes")
        .upsert(row, on_conflict="week_start,slug")
        .execute()
    )
    data = response.data or []
    persisted = data[0] if data else row
    return {
        "theme_id": persisted.get("theme_id"),
        "is_beta": bool(persisted.get("is_beta", is_beta)),
        "status": persisted.get("status", "draft"),
        "dry_run": False,
    }


def publish_theme(theme_id: str, *, client, dry_run: bool = False) -> dict:
    """Publish a draft theme only after all TH-19 publish guards pass."""
    theme = _first_row(
        client.table("cultural_themes")
        .select("*")
        .eq("theme_id", theme_id)
        .limit(1)
        .execute()
    )
    if theme is None:
        raise PublishGuardError("P-1 theme not found")

    draft = _row_to_draft(theme)
    reasons = validate_draft(draft)
    if reasons:
        raise PublishGuardError("; ".join(reasons))

    if theme.get("layer_source") != LAYER_SOURCE_PHASE1:
        raise PublishGuardError("P-1 layer_source must be 'market'")

    if not _index_exists(draft.connected_index_date, client=client):
        raise PublishGuardError("P-2 connected_index_date does not exist in index_daily")

    if not _all_trends_exist(draft.supporting_trend_ids, client=client):
        raise PublishGuardError("P-3 supporting_trend_ids must all exist in trend_snapshots")

    if not (HERO_MIN <= len(draft.hero_product_ids) <= HERO_MAX):
        raise PublishGuardError(f"P-4 hero_product_ids must contain {HERO_MIN}..{HERO_MAX} ids")

    is_beta = resolve_is_beta(draft.supporting_trend_ids, client=client)
    published_at = _utc_now()
    payload = {"status": "published", "published_at": published_at, "is_beta": is_beta}
    if dry_run:
        return {
            "theme_id": theme_id,
            "is_beta": is_beta,
            "status": "published",
            "published_at": None,
            "dry_run": True,
        }

    response = (
        client.table("cultural_themes")
        .update(payload)
        .eq("theme_id", theme_id)
        .execute()
    )
    data = response.data or []
    persisted = data[0] if data else {**theme, **payload}
    return {
        "theme_id": persisted.get("theme_id", theme_id),
        "is_beta": bool(persisted.get("is_beta", is_beta)),
        "status": persisted.get("status", "published"),
        "published_at": persisted.get("published_at", published_at),
        "dry_run": False,
    }


def _draft_to_row(draft: ThemeDraft, *, is_beta: bool) -> dict[str, Any]:
    return {
        "week_start": draft.week_start,
        "slug": draft.slug,
        "headline": draft.headline,
        "summary": draft.summary,
        "body": draft.body,
        "hero_product_ids": list(draft.hero_product_ids),
        "supporting_trend_ids": list(draft.supporting_trend_ids),
        "connected_index_date": draft.connected_index_date,
        "layer_source": draft.layer_source,
        "author_model": draft.author_model,
        "disclaimer_version": draft.disclaimer_version,
        "is_beta": is_beta,
        "status": "draft",
    }


def _row_to_draft(row: dict[str, Any]) -> ThemeDraft:
    return ThemeDraft(
        week_start=str(row.get("week_start") or ""),
        slug=str(row.get("slug") or ""),
        headline=str(row.get("headline") or ""),
        summary=str(row.get("summary") or ""),
        body=str(row.get("body") or ""),
        hero_product_ids=list(row.get("hero_product_ids") or []),
        supporting_trend_ids=list(row.get("supporting_trend_ids") or []),
        connected_index_date=str(row.get("connected_index_date") or ""),
        layer_source=str(row.get("layer_source") or ""),
        author_model=str(row.get("author_model") or "sonnet"),
        disclaimer_version=str(row.get("disclaimer_version") or DISCLAIMER_VERSION),
    )


def _find_banned_terms(draft: ThemeDraft) -> list[str]:
    text = "\n".join([draft.headline, draft.summary, draft.body])
    return [term for term in BANNED_TERMS if term in text]


def _is_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return True


def _first_row(response: Any) -> dict[str, Any] | None:
    rows = getattr(response, "data", None) or []
    return rows[0] if rows else None


def _index_exists(snapshot_date: str, *, client) -> bool:
    return (
        _first_row(
            client.table("index_daily")
            .select("snapshot_date")
            .eq("snapshot_date", snapshot_date)
            .limit(1)
            .execute()
        )
        is not None
    )


def _all_trends_exist(supporting_trend_ids: list[str], *, client) -> bool:
    if not supporting_trend_ids:
        return False
    expected_ids = set(supporting_trend_ids)
    rows = (
        client.table("trend_snapshots")
        .select("snapshot_id")
        .in_("snapshot_id", list(expected_ids))
        .execute()
        .data
        or []
    )
    return {row.get("snapshot_id") for row in rows} == expected_ids


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_draft_yaml(path: Path) -> ThemeDraft:
    if path.name == ".env" or path.match("*/code/.env"):
        raise ValueError("refusing to read .env as a draft file")

    import yaml

    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("draft YAML must contain a mapping")
    return ThemeDraft(**payload)


def _main() -> int:
    parser = argparse.ArgumentParser(description="Write or publish cultural themes.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--draft", type=Path, help="YAML ThemeDraft file to upsert as draft")
    group.add_argument("--publish", help="theme_id to publish")
    parser.add_argument("--dry-run", action="store_true", help="validate without writing")
    args = parser.parse_args()

    client = get_supabase()
    if args.draft is not None:
        result = upsert_theme(_load_draft_yaml(args.draft), client=client, dry_run=args.dry_run)
    else:
        result = publish_theme(args.publish, client=client, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI wrapper
    raise SystemExit(_main())
