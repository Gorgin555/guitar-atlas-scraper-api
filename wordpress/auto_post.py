"""
GUITAR ATLAS — WordPress REST API 自動投稿スクリプト
CMO ピエール / COO ドレアム 共同実装

役割:
  - Claude API で生成した記事を WordPress に自動投稿
  - n8n ワークフローから呼び出す（朝6:00 分析 → 朝7:00 CEO承認 → 承認後即投稿）
  - カテゴリ自動設定（GAI-E / VFI / BPI / Boutique Premium / Trend）

使用方法:
  python -m wordpress.auto_post --title "..." --content "..." --category gai-e
  python -m wordpress.auto_post --dry-run  # 実際には投稿せず出力確認

環境変数（.env に設定）:
  WP_URL         = https://theguitaratlas.com
  WP_USER        = atlas_admin
  WP_APP_PASSWORD = xxxx xxxx xxxx xxxx xxxx xxxx  # アプリケーションパスワード
  CONVERTKIT_API_SECRET = xxxxxxxx
"""

import os
import sys
import json
import base64
import argparse
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

# .env 読み込み（code/ ディレクトリの .env）
load_dotenv(Path(__file__).parent.parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────

WP_URL = os.getenv("WP_URL", "https://theguitaratlas.com")
WP_USER = os.getenv("WP_USER", "atlas_admin")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "")  # スペース区切りのまま OK

# カテゴリスラッグ → WordPress カテゴリID のマッピング
# 初回セットアップ後に `python -m wordpress.auto_post --sync-categories` で自動取得
CATEGORY_SLUG_MAP: dict[str, int] = {
    "gai-e": 0,           # setup_categories() 実行後に更新される
    "vfi": 0,
    "bpi": 0,
    "boutique-premium": 0,
    "trend": 0,
}

# コンテンツトーン → タグID マッピング（同様に sync 後に更新）
TAG_SLUG_MAP: dict[str, int] = {
    "observed": 0,
    "indexed": 0,
    "spread": 0,
    "forecast": 0,
    "field-note": 0,
}

# ─────────────────────────────────────────────
# 認証ヘルパー
# ─────────────────────────────────────────────

def _auth_header() -> dict:
    """WordPress アプリケーションパスワード認証ヘッダーを生成"""
    credentials = f"{WP_USER}:{WP_APP_PASSWORD}"
    token = base64.b64encode(credentials.encode()).decode("utf-8")
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
    }


def _api(endpoint: str) -> str:
    return f"{WP_URL}/wp-json/wp/v2/{endpoint}"


# ─────────────────────────────────────────────
# カテゴリ・タグ管理
# ─────────────────────────────────────────────

def sync_categories() -> dict[str, int]:
    """WordPress からカテゴリ一覧を取得し、スラッグ→ID マッピングを更新"""
    logger.info("WordPress カテゴリ一覧を同期中...")
    r = requests.get(_api("categories"), params={"per_page": 100}, timeout=10)
    r.raise_for_status()
    slug_map = {cat["slug"]: cat["id"] for cat in r.json()}
    logger.info(f"取得カテゴリ: {slug_map}")
    return slug_map


def sync_tags() -> dict[str, int]:
    """WordPress からタグ一覧を取得"""
    logger.info("WordPress タグ一覧を同期中...")
    r = requests.get(_api("tags"), params={"per_page": 100}, timeout=10)
    r.raise_for_status()
    slug_map = {tag["slug"]: tag["id"] for tag in r.json()}
    logger.info(f"取得タグ: {slug_map}")
    return slug_map


def setup_categories(dry_run: bool = False) -> dict[str, int]:
    """
    初期5カテゴリ + コンテンツトーンタグをWordPressに作成する
    初回セットアップ時のみ実行
    """
    categories = [
        {
            "name": "GAI-E — 総合指標",
            "slug": "gai-e",
            "description": "Guitar Atlas Index - Electric（GAI-E）の週次・月次分析レポート。全58モデルの加重合成指数を発表。"
        },
        {
            "name": "VFI — ヴィンテージ",
            "slug": "vfi",
            "description": "Vintage Flagship Index（VFI）。Pre-CBS Fender・Gibson Burst等、18ヴィンテージモデルの市場動態。All Original / All Conditions の2系列で発表。"
        },
        {
            "name": "BPI — ブティック",
            "slug": "bpi",
            "description": "Boutique Premium Index（BPI）。Suhr・Tom Anderson・Knaggs等、22ブティックモデルの価格動向を追跡。"
        },
        {
            "name": "Boutique Premium",
            "slug": "boutique-premium",
            "description": "BPI/MFI スプレッド分析。ブティックギターとメインストリームの価格差が示す市場インサイト。"
        },
        {
            "name": "Trend — 市場シグナル",
            "slug": "trend",
            "description": "急騰・急落モデルの検出、SNS言及急増、Cultural Layer シグナル。市場の先行指標を速報。"
        },
    ]

    tags = [
        {"name": "Observed", "slug": "observed", "description": "観測事実の報告"},
        {"name": "Indexed", "slug": "indexed", "description": "指標化・分析"},
        {"name": "Spread", "slug": "spread", "description": "スプレッド分析"},
        {"name": "Forecast", "slug": "forecast", "description": "予測・シグナル"},
        {"name": "Field Note", "slug": "field-note", "description": "現場観測メモ"},
    ]

    created_cats: dict[str, int] = {}
    created_tags: dict[str, int] = {}

    if dry_run:
        logger.info("[DRY RUN] 以下のカテゴリを作成予定:")
        for cat in categories:
            logger.info(f"  Category: {cat['name']} (slug: {cat['slug']})")
        for tag in tags:
            logger.info(f"  Tag: {tag['name']} (slug: {tag['slug']})")
        return {}

    # カテゴリ作成
    for cat in categories:
        try:
            r = requests.post(
                _api("categories"),
                headers=_auth_header(),
                json=cat,
                timeout=10
            )
            if r.status_code == 201:
                data = r.json()
                created_cats[cat["slug"]] = data["id"]
                logger.info(f"✅ カテゴリ作成: {cat['name']} (ID: {data['id']})")
            elif r.status_code == 400 and "term_exists" in r.text:
                # 既存カテゴリの場合はIDを取得
                existing = sync_categories()
                if cat["slug"] in existing:
                    created_cats[cat["slug"]] = existing[cat["slug"]]
                    logger.info(f"⚠️ 既存カテゴリ: {cat['name']} (ID: {existing[cat['slug']]})")
            else:
                logger.error(f"❌ カテゴリ作成失敗: {cat['name']} — {r.status_code} {r.text}")
        except Exception as e:
            logger.error(f"❌ 例外: {cat['name']} — {e}")

    # タグ作成
    for tag in tags:
        try:
            r = requests.post(
                _api("tags"),
                headers=_auth_header(),
                json=tag,
                timeout=10
            )
            if r.status_code == 201:
                data = r.json()
                created_tags[tag["slug"]] = data["id"]
                logger.info(f"✅ タグ作成: {tag['name']} (ID: {data['id']})")
            elif r.status_code == 400 and "term_exists" in r.text:
                existing = sync_tags()
                if tag["slug"] in existing:
                    created_tags[tag["slug"]] = existing[tag["slug"]]
                    logger.info(f"⚠️ 既存タグ: {tag['name']} (ID: {existing[tag['slug']]})")
            else:
                logger.error(f"❌ タグ作成失敗: {tag['name']} — {r.status_code} {r.text}")
        except Exception as e:
            logger.error(f"❌ 例外: {tag['name']} — {e}")

    logger.info(f"カテゴリマッピング: {created_cats}")
    logger.info(f"タグマッピング: {created_tags}")

    # .env に追記できるよう出力
    print("\n--- credentials.md に追記するカテゴリID ---")
    for slug, id_ in created_cats.items():
        print(f"  WP_CATEGORY_{slug.upper().replace('-', '_')}: {id_}")
    for slug, id_ in created_tags.items():
        print(f"  WP_TAG_{slug.upper().replace('-', '_')}: {id_}")

    return {**created_cats, **created_tags}


# ─────────────────────────────────────────────
# 記事投稿
# ─────────────────────────────────────────────

def post_article(
    title: str,
    content: str,
    category_slug: str,
    tag_slugs: list[str] = None,
    excerpt: str = "",
    status: str = "draft",  # 'draft' | 'publish' | 'pending'
    meta_description: str = "",
    featured_media: int = 0,
    dry_run: bool = False,
) -> Optional[dict]:
    """
    WordPress に記事を投稿する

    Args:
        title: 記事タイトル
        content: 記事本文（HTML可）
        category_slug: カテゴリスラッグ（gai-e / vfi / bpi / boutique-premium / trend）
        tag_slugs: タグスラッグのリスト（observed / indexed / spread / forecast / field-note）
        excerpt: 抜粋（SEO用）
        status: 投稿ステータス（draft=下書き / publish=即公開 / pending=承認待ち）
        meta_description: Yoast SEO メタ説明
        featured_media: アイキャッチ画像ID（0=なし）
        dry_run: Trueの場合は実際に投稿せず内容を表示

    Returns:
        投稿成功時はレスポンスdict、失敗時はNone
    """
    # カテゴリID解決
    cat_map = sync_categories()
    category_id = cat_map.get(category_slug)
    if not category_id:
        logger.error(f"カテゴリが見つかりません: {category_slug}")
        return None

    # タグID解決
    tag_map = sync_tags()
    tag_ids = []
    if tag_slugs:
        for slug in tag_slugs:
            if slug in tag_map:
                tag_ids.append(tag_map[slug])
            else:
                logger.warning(f"タグが見つかりません: {slug}")

    # 現在日時（日本時間）
    now_jst = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    payload = {
        "title": title,
        "content": content,
        "excerpt": excerpt,
        "status": status,
        "categories": [category_id],
        "tags": tag_ids,
        "date": now_jst,
        # Yoast SEO メタ（Yoast REST API 経由）
        "meta": {
            "_yoast_wpseo_metadesc": meta_description,
        }
    }

    if featured_media:
        payload["featured_media"] = featured_media

    if dry_run:
        logger.info("[DRY RUN] 以下の内容で投稿予定:")
        print(json.dumps({
            "title": title,
            "category": category_slug,
            "tags": tag_slugs,
            "status": status,
            "excerpt": excerpt[:100] + "...",
            "content_length": len(content),
        }, ensure_ascii=False, indent=2))
        return None

    logger.info(f"WordPress へ投稿中: '{title}' (category: {category_slug}, status: {status})")

    r = requests.post(
        _api("posts"),
        headers=_auth_header(),
        json=payload,
        timeout=30
    )

    if r.status_code in (200, 201):
        data = r.json()
        logger.info(f"✅ 投稿成功: ID={data['id']}, URL={data['link']}")
        return data
    else:
        logger.error(f"❌ 投稿失敗: {r.status_code} — {r.text[:500]}")
        return None


def update_article(
    post_id: int,
    status: str = "publish",
    dry_run: bool = False,
) -> Optional[dict]:
    """
    下書き記事を公開する（CEO 承認後に呼び出す）

    Args:
        post_id: 公開する記事のWordPress ID
        status: 'publish' | 'draft'
    """
    if dry_run:
        logger.info(f"[DRY RUN] 記事ID {post_id} を {status} にステータス変更予定")
        return None

    r = requests.post(
        _api(f"posts/{post_id}"),
        headers=_auth_header(),
        json={"status": status},
        timeout=30
    )

    if r.status_code in (200, 201):
        data = r.json()
        logger.info(f"✅ ステータス更新: ID={data['id']}, status={status}, URL={data['link']}")
        return data
    else:
        logger.error(f"❌ 更新失敗: {r.status_code} — {r.text[:500]}")
        return None


def list_drafts() -> list[dict]:
    """下書き記事一覧を取得（CEO 朝のブリーフィング用）"""
    r = requests.get(
        _api("posts"),
        headers=_auth_header(),
        params={"status": "draft", "per_page": 10},
        timeout=10
    )
    r.raise_for_status()
    drafts = r.json()
    for d in drafts:
        print(f"  ID: {d['id']} | {d['title']['rendered']} | {d['date']}")
    return drafts


# ─────────────────────────────────────────────
# n8n / Claude API 連携用エントリポイント
# ─────────────────────────────────────────────

def publish_from_claude_output(claude_json: dict, dry_run: bool = False) -> Optional[dict]:
    """
    Claude API が生成した記事JSONを受け取り、WordPressに投稿する

    Claude API 出力の期待フォーマット:
    {
        "title": "BPI が 1.8% 上昇、Suhr Classic S 在庫日数の短縮が寄与",
        "content": "<p>...</p>",
        "excerpt": "5月14日時点...",
        "category": "bpi",
        "tags": ["observed"],
        "meta_description": "...",
        "status": "draft"
    }
    """
    return post_article(
        title=claude_json["title"],
        content=claude_json["content"],
        category_slug=claude_json.get("category", "trend"),
        tag_slugs=claude_json.get("tags", []),
        excerpt=claude_json.get("excerpt", ""),
        status=claude_json.get("status", "draft"),
        meta_description=claude_json.get("meta_description", ""),
        dry_run=dry_run,
    )


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="GUITAR ATLAS WordPress 自動投稿")
    subparsers = parser.add_subparsers(dest="command")

    # setup-categories コマンド
    setup_p = subparsers.add_parser("setup-categories", help="初期カテゴリ・タグを WordPress に作成")
    setup_p.add_argument("--dry-run", action="store_true")

    # post コマンド
    post_p = subparsers.add_parser("post", help="記事を投稿")
    post_p.add_argument("--title", required=True)
    post_p.add_argument("--content-file", help="本文ファイルパス（HTML）")
    post_p.add_argument("--content", help="本文テキスト（直接入力）")
    post_p.add_argument("--category", default="trend",
                        choices=["gai-e", "vfi", "bpi", "boutique-premium", "trend"])
    post_p.add_argument("--tags", nargs="*",
                        choices=["observed", "indexed", "spread", "forecast", "field-note"])
    post_p.add_argument("--excerpt", default="")
    post_p.add_argument("--status", default="draft", choices=["draft", "publish", "pending"])
    post_p.add_argument("--dry-run", action="store_true")

    # publish コマンド（CEO承認後）
    pub_p = subparsers.add_parser("publish", help="下書き記事を公開")
    pub_p.add_argument("--id", type=int, required=True, help="WordPress 記事ID")
    pub_p.add_argument("--dry-run", action="store_true")

    # list-drafts コマンド
    subparsers.add_parser("list-drafts", help="下書き一覧表示")

    # sync コマンド
    subparsers.add_parser("sync", help="カテゴリ・タグのIDを同期して表示")

    args = parser.parse_args()

    if args.command == "setup-categories":
        setup_categories(dry_run=args.dry_run)

    elif args.command == "post":
        content = ""
        if args.content_file:
            content = Path(args.content_file).read_text(encoding="utf-8")
        elif args.content:
            content = args.content
        else:
            print("エラー: --content か --content-file が必要です")
            sys.exit(1)

        post_article(
            title=args.title,
            content=content,
            category_slug=args.category,
            tag_slugs=args.tags or [],
            excerpt=args.excerpt,
            status=args.status,
            dry_run=args.dry_run,
        )

    elif args.command == "publish":
        update_article(post_id=args.id, status="publish", dry_run=args.dry_run)

    elif args.command == "list-drafts":
        list_drafts()

    elif args.command == "sync":
        cats = sync_categories()
        tags = sync_tags()
        print("カテゴリ:", json.dumps(cats, ensure_ascii=False, indent=2))
        print("タグ:", json.dumps(tags, ensure_ascii=False, indent=2))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
