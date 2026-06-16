"""
GUITAR ATLAS — Scraper API (FastAPI)
=====================================
n8n から呼び出す Python オーケストレーションサービス。
Reverb/デジマート/Yahoo スクレイパー + Index Engine + Claude 記事シード生成を
HTTP エンドポイントとして提供する。

担当: COO ドレアム
作成: 2026-05-15
v1.1 (2026-05-17): 根幹安定化リファクタ
  - Supabase / Reverb / scrapers の初期化を遅延化（health は env なしでも 200）
  - 既存 CLI 関数（run_active / PassiveCollector.run / fetch_and_upsert / run_engine）に委譲
  - on_conflict / 列名 (brand_name / is_passive) / async 呼び出しを実装と一致
  - /index/run が index_snapshots に加えて index_daily へピボット書込
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── パス解決（同一プロジェクトのコードを参照）──────────────────────────
_HERE = os.path.dirname(__file__)
_CODE_DIR = os.path.abspath(os.path.join(_HERE, ".."))         # → code/n8n
_ATLAS_CODE = os.path.abspath(os.path.join(_HERE, "..", ".."))   # → code/
sys.path.insert(0, _ATLAS_CODE)

# .env は Railway では存在しない。ローカル開発時のみ読み込めればよい。
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ATLAS_CODE, ".env"))
except Exception:  # pragma: no cover
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("atlas-scraper-api")

# ── セキュリティ ────────────────────────────────────────────────────────
ATLAS_API_SECRET = os.environ.get("ATLAS_API_SECRET", "")


def verify_secret(x_atlas_secret: Optional[str] = Header(default=None)):
    """n8n との共有シークレットで認証。未設定時はオープン（CEO の手動テスト用）。"""
    if not ATLAS_API_SECRET:
        return True
    if x_atlas_secret != ATLAS_API_SECRET:
        raise HTTPException(status_code=403, detail="Invalid API secret")
    return True


# ── Supabase クライアント（遅延初期化）────────────────────────────────
_supabase_client = None


def get_supabase():
    """
    Supabase クライアントを遅延生成して返す。
    起動時に env が無くてもプロセスは落ちないよう、最初の呼び出し時に作る。
    """
    global _supabase_client
    if _supabase_client is None:
        from supabase import create_client

        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY")
        if not (url and key):
            raise HTTPException(
                status_code=503,
                detail="Supabase credentials not configured (SUPABASE_URL / SUPABASE_SERVICE_KEY)",
            )
        _supabase_client = create_client(url, key)
    return _supabase_client


# ── FastAPI App ──────────────────────────────────────────────────────────
app = FastAPI(
    title="GUITAR ATLAS Scraper API",
    description="n8n オーケストレーション用 Python バックエンド",
    version="1.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://theguitaratlas.com"],
    allow_methods=["GET", "POST"],
    allow_headers=["accept", "Content-Type"],
)



# ═══════════════════════════════════════════════════════════════════
# ヘルスチェック (常に 200 / ASCII only)
# ═══════════════════════════════════════════════════════════════════

@app.get("/health")
def health():
    """常に 200 を返す。Railway healthcheck はここを見ている。"""
    return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/")
def root():
    return {"service": "atlas-scraper-api", "version": "1.1.0"}


# ═══════════════════════════════════════════════════════════════════
# Reverb API フェッチ
# ═══════════════════════════════════════════════════════════════════

class FetchReverbRequest(BaseModel):
    basket: str = "active"          # "active" | "MFI" | "VFI" | "BPI"
    max_pages_per_model: int = 3
    dry_run: bool = False
    limit_models: Optional[int] = None


def _reverb_basket_arg(req_basket: str) -> Optional[str]:
    """API の basket 入力を fetch_listings.load_targets の basket 引数に変換。"""
    b = (req_basket or "").upper()
    if b in ("MFI", "VFI", "BPI"):
        return b
    # "active" / "all" → None で全件
    return None


def _do_fetch_reverb(req: FetchReverbRequest) -> dict[str, Any]:
    from ingest.fetch_listings import fetch_and_upsert, load_targets

    targets = load_targets(
        basket=_reverb_basket_arg(req.basket),
        limit=req.limit_models,
    )
    if not targets:
        return {"targets": 0, "inserted": 0, "errors": 0, "note": "no targets"}

    counts = fetch_and_upsert(
        targets,
        per_page=50,
        max_pages=req.max_pages_per_model,
        state="live",
        dry_run=req.dry_run,
    )
    # 既存 CLI の counts キー（targets / listings / errors）→ 共通フォーマットに正規化
    return {
        "targets": counts.get("targets", 0),
        "inserted": counts.get("listings", 0),
        "errors": counts.get("errors", 0),
    }


def _do_fetch_reverb_live(req: FetchReverbRequest) -> dict[str, Any]:
    from ingest.fetch_listings import fetch_and_upsert_live_brands, load_live_brand_targets

    targets = load_live_brand_targets(limit=req.limit_models)
    if not targets:
        return {"targets": 0, "inserted": 0, "errors": 0, "note": "no targets"}

    fields_set = getattr(req, "model_fields_set", None)
    if fields_set is None:
        fields_set = getattr(req, "__fields_set__", set())
    max_pages = req.max_pages_per_model if "max_pages_per_model" in fields_set else 2
    counts = fetch_and_upsert_live_brands(
        targets,
        per_page=50,
        max_pages=max_pages,
        state="live",
        dry_run=req.dry_run,
    )
    return {
        "targets": counts.get("targets", len(targets)),
        "inserted": counts.get("listings", 0),
        "errors": counts.get("errors", 0),
    }


@app.post("/fetch/reverb")
async def fetch_reverb(req: FetchReverbRequest, _auth=Depends(verify_secret)):
    """Reverb API → listings_daily upsert。既存 fetch_and_upsert に委譲。"""
    try:
        result = await asyncio.to_thread(_do_fetch_reverb, req)
        logger.info("Reverb fetch complete: %s", result)
        return {"source": "reverb", "success": True, **result}
    except Exception as e:
        logger.error("Reverb fetch error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/fetch/reverb/live_brands")
async def fetch_reverb_live_brands(req: FetchReverbRequest, _auth=Depends(verify_secret)):
    """Reverb API -> brand-level live tracker products."""
    try:
        result = await asyncio.to_thread(_do_fetch_reverb_live, req)
        logger.info("Reverb live brand fetch complete: %s", result)
        return {"source": "reverb", "scope": "live_brands", "success": True, **result}
    except Exception as e:
        logger.error("Reverb live brand fetch error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


class FetchPriceGuideRequest(BaseModel):
    basket: Optional[str] = None
    since: Optional[str] = None
    backfill_months: int = 12
    max_guides_per_model: int = 8
    dry_run: bool = False
    limit_models: Optional[int] = None


def _priceguide_subtract_months(base: date, months: int) -> date:
    import calendar

    month_index = base.year * 12 + (base.month - 1) - months
    year = month_index // 12
    month = month_index % 12 + 1
    day = min(base.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _do_fetch_priceguide(req: FetchPriceGuideRequest) -> dict[str, Any]:
    from ingest.fetch_listings import load_targets
    from ingest.fetch_priceguide import fetch_and_upsert_priceguide

    targets = load_targets(
        basket=_reverb_basket_arg(req.basket or ""),
        limit=req.limit_models,
    )
    if not targets:
        return {"targets": 0, "inserted": 0, "errors": 0, "note": "no targets"}

    since = date.fromisoformat(req.since) if req.since else _priceguide_subtract_months(
        date.today(), req.backfill_months
    )
    counts = fetch_and_upsert_priceguide(
        targets,
        since=since,
        max_guides_per_model=req.max_guides_per_model,
        dry_run=req.dry_run,
    )
    return {
        "targets": counts.get("targets", len(targets)),
        "guides": counts.get("guides", 0),
        "inserted": counts.get("transactions", 0),
        "errors": counts.get("errors", 0),
    }


@app.post("/fetch/priceguide")
async def fetch_priceguide(req: FetchPriceGuideRequest, _auth=Depends(verify_secret)):
    """Reverb Price Guide transactions -> priceguide_transactions upsert."""
    try:
        result = await asyncio.to_thread(_do_fetch_priceguide, req)
        logger.info("Price Guide fetch complete: %s", result)
        return {"source": "priceguide", "success": True, **result}
    except Exception as e:
        logger.error("Price Guide fetch error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════
# Digimart / Yahoo スクレイパー（run_active に委譲）
# ═══════════════════════════════════════════════════════════════════

class FetchActiveRequest(BaseModel):
    basket: Optional[str] = None       # "MFI" | "VFI" | "BPI" | None（全件）
    max_pages: int = 1  # TD-007: 600s タイムアウト回避のため日次既定を1ページに (全件は呼出側で明示指定可)
    dry_run: bool = False
    limit_models: Optional[int] = None
    yahoo_mode: str = "active"          # active / sold / both（後方互換）


def _scrapers_basket_arg(b: Optional[str]) -> Optional[str]:
    if not b:
        return None
    bb = b.upper()
    return bb if bb in ("MFI", "VFI", "BPI") else None


def _do_run_active(sources: list[str], req: FetchActiveRequest) -> dict[str, Any]:
    """既存 run_scrapers.run_active を呼び出す薄いラッパー。"""
    from scrapers.run_scrapers import _load_active_targets, run_active

    targets = _load_active_targets(
        basket=_scrapers_basket_arg(req.basket),
        limit=req.limit_models,
    )
    if not targets:
        return {"targets": 0, "inserted": 0, "errors": 0, "note": "no targets"}

    counts = run_active(
        sources=sources,
        targets=targets,
        max_pages=req.max_pages,
        dry_run=req.dry_run,
        yahoo_mode=req.yahoo_mode,
    )
    return {
        "targets": counts.get("targets", len(targets)),
        "inserted": counts.get("listings", 0),
        "errors": counts.get("errors", 0),
    }


def _do_run_active_live(sources: list[str], req: FetchActiveRequest) -> dict[str, Any]:
    from scrapers.run_scrapers import _load_live_brand_targets, run_active_live_brands

    targets = _load_live_brand_targets(limit=req.limit_models)
    if not targets:
        return {"targets": 0, "inserted": 0, "errors": 0, "note": "no targets"}

    counts = run_active_live_brands(
        sources=sources,
        targets=targets,
        max_pages=req.max_pages,
        dry_run=req.dry_run,
    )
    return {
        "targets": counts.get("targets", len(targets)),
        "inserted": counts.get("listings", 0),
        "errors": counts.get("errors", 0),
    }


@app.post("/fetch/digimart")
async def fetch_digimart(req: FetchActiveRequest, _auth=Depends(verify_secret)):
    """デジマート → listings_daily upsert。"""
    try:
        result = await asyncio.to_thread(_do_run_active, ["digimart"], req)
        logger.info("Digimart fetch complete: %s", result)
        return {"source": "digimart", "success": True, **result}
    except Exception as e:
        logger.error("Digimart fetch error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/fetch/digimart/live_brands")
async def fetch_digimart_live_brands(req: FetchActiveRequest, _auth=Depends(verify_secret)):
    """Digimart -> brand-level live tracker products."""
    try:
        result = await asyncio.to_thread(_do_run_active_live, ["digimart"], req)
        logger.info("Digimart live brand fetch complete: %s", result)
        return {"source": "digimart", "scope": "live_brands", "success": True, **result}
    except Exception as e:
        logger.error("Digimart live brand fetch error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/fetch/yahoo")
async def fetch_yahoo(req: FetchActiveRequest, _auth=Depends(verify_secret)):
    """Yahoo オークション → listings_daily upsert。"""
    try:
        result = await asyncio.to_thread(_do_run_active, ["yahoo"], req)
        logger.info("Yahoo fetch complete: %s", result)
        return {"source": "yahoo", "success": True, **result}
    except Exception as e:
        logger.error("Yahoo fetch error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════
# 受動的収集（Pedals / Acoustic / Amps / JBI）
# ═══════════════════════════════════════════════════════════════════

class FetchPassiveRequest(BaseModel):
    sources: Optional[list[str]] = None      # None → ["digimart", "yahoo"]
    max_pages_per_brand: int = 2
    categories: Optional[list[str]] = None
    dry_run: bool = False


def _do_run_passive(req: FetchPassiveRequest) -> dict[str, Any]:
    from scrapers.passive_collector import PassiveCollector

    collector = PassiveCollector(dry_run=req.dry_run)
    counts = collector.run(
        sources=req.sources,
        max_pages_per_brand=req.max_pages_per_brand,
        categories=req.categories,
    )
    return {
        "inserted": counts.get("total_listings", 0),
        "errors": counts.get("errors", 0),
    }


@app.post("/fetch/passive")
async def fetch_passive(
    req: Optional[FetchPassiveRequest] = None,
    _auth=Depends(verify_secret),
):
    """Phase 2-4 の受動的データ収集。body は省略可。"""
    if req is None:
        req = FetchPassiveRequest()
    try:
        result = await asyncio.to_thread(_do_run_passive, req)
        logger.info("Passive collection complete: %s", result)
        return {"source": "passive", "success": True, **result}
    except Exception as e:
        logger.error("Passive collection error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════
# Index Engine 実行
# ═══════════════════════════════════════════════════════════════════

class IndexRunRequest(BaseModel):
    target_date: Optional[str] = None    # "YYYY-MM-DD" / None=最新
    write_to_db: bool = True
    simulation_mode: bool = False
    method_version: str = "v1.0"


def _pivot_report_to_index_daily(report) -> dict[str, Any]:
    """
    DailyIndexReport を index_daily (single-row-per-date) のフォーマットへピボット。
    フィールドが無い場合は None。
    """
    indices = report.all_index_results()
    spreads = getattr(report, "spreads", {}) or {}

    def _val(name: str) -> Optional[float]:
        idx = indices.get(name)
        if idx is None:
            return None
        v = getattr(idx, "display_value", None) or getattr(idx, "calibrated_value", None) or getattr(idx, "raw_value", None)
        return float(v) if v is not None else None

    def _spread(name: str) -> Optional[float]:
        sp = spreads.get(name)
        if sp is None:
            return None
        r = getattr(sp, "ratio", None)
        return float(r) if r is not None else None

    total_listings = 0
    models_with_data = 0
    for idx in indices.values():
        total_listings += int(getattr(idx, "total_listings", 0) or 0)
        models_with_data = max(models_with_data, int(getattr(idx, "n_models_with_data", 0) or 0))

    return {
        "snapshot_date": report.snapshot_date.isoformat(),
        "gai_e": _val("GAI-E") if "GAI-E" in indices else _val("gai_e"),
        "mfi": _val("MFI") if "MFI" in indices else _val("mfi"),
        "vfi_ac": _val("VFI_AC") if "VFI_AC" in indices else _val("vfi_ac"),
        "vfi_ao": _val("VFI_AO") if "VFI_AO" in indices else _val("vfi_ao"),
        "bpi": _val("BPI") if "BPI" in indices else _val("bpi"),
        "boutique_premium": _spread("BoutiquePremium") or _spread("boutique_premium"),
        "vintage_premium":  _spread("VintagePremium")  or _spread("vintage_premium"),
        "heritage_spread":  _spread("HeritageSpread")  or _spread("heritage_spread"),
        "listings_count": total_listings or None,
        "models_with_data": models_with_data or None,
        "calibrated": bool(getattr(report, "is_calibrated", False)),
    }


def _do_run_index(req: IndexRunRequest) -> dict[str, Any]:
    from index_engine.engine import run_engine

    target = date.fromisoformat(req.target_date) if req.target_date else None
    report = run_engine(
        target_date=target,
        dry_run=not req.write_to_db,
        simulation_mode=req.simulation_mode,
        method_version=req.method_version,
        output_json=None,
    )

    # index_daily へのピボット書き込み（n8n / Slack が参照する単一行テーブル）
    row = _pivot_report_to_index_daily(report)
    if req.write_to_db:
        try:
            get_supabase().table("index_daily").upsert(
                row, on_conflict="snapshot_date"
            ).execute()
        except Exception as e:
            logger.warning("index_daily upsert failed (non-fatal): %s", e)

    return {
        "date": row["snapshot_date"],
        "is_calibrated": bool(getattr(report, "is_calibrated", False)),
        "indices": {
            "gai_e": row.get("gai_e"),
            "mfi": row.get("mfi"),
            "vfi_ac": row.get("vfi_ac"),
            "bpi": row.get("bpi"),
        },
        "spreads": {
            "boutique_premium": row.get("boutique_premium"),
            "vintage_premium": row.get("vintage_premium"),
            "heritage_spread": row.get("heritage_spread"),
        },
    }


@app.post("/index/run")
async def run_index(req: IndexRunRequest, _auth=Depends(verify_secret)):
    """GAI-E / MFI / VFI / BPI を計算し index_snapshots + index_daily に書き込む。"""
    try:
        result = await asyncio.to_thread(_do_run_index, req)
        logger.info("Index run complete: %s", result)
        return {"success": True, **result}
    except Exception as e:
        logger.error("Index run error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/index/latest")
def get_latest_index(_auth=Depends(verify_secret)):
    """最新のインデックス値を index_daily から返す。"""
    try:
        resp = (
            get_supabase().table("index_daily")
            .select("*")
            .order("snapshot_date", desc=True)
            .limit(1)
            .execute()
        )
        if not resp.data:
            raise HTTPException(status_code=404, detail="No index_daily rows yet")
        return resp.data[0]
    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_latest_index error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════
# Claude 記事シード生成
# ═══════════════════════════════════════════════════════════════════

class SeedGenerationRequest(BaseModel):
    num_seeds: int = 3
    language: str = "ja"    # "ja" | "en"


def _summarize_hot_models(listings: list[dict]) -> str:
    if not listings:
        return "データなし"
    from collections import defaultdict
    by_model: dict[str, list[float]] = defaultdict(list)
    for row in listings:
        # products 埋め込み (to-one) は dict、念のため list 形も許容。
        prod = row.get("products") or {}
        if isinstance(prod, list):
            prod = prod[0] if prod else {}
        brand = prod.get("brand_name") if isinstance(prod, dict) else None
        model = prod.get("model") if isinstance(prod, dict) else None
        # 後方互換: 旧構造でトップレベルに brand_name/model がある場合も拾う。
        brand = brand or row.get("brand_name") or row.get("brand")
        model = model or row.get("model")
        if not brand or not model:
            # product 未マッチの listing はホットモデル集計から除外。
            continue
        key = f"{brand} {model}"
        if row.get("price_usd"):
            try:
                by_model[key].append(float(row["price_usd"]))
            except (TypeError, ValueError):
                continue
    lines = []
    for model, prices in sorted(by_model.items(), key=lambda x: -len(x[1]))[:10]:
        avg = sum(prices) / len(prices)
        lines.append(f"- {model}: 平均 ${avg:,.0f}（{len(prices)}件）")
    return "\n".join(lines) if lines else "データなし"


def _strip_code_fence(text: str) -> str:
    t = text.strip()
    if "```" not in t:
        return t
    parts = t.split("```")
    # 最も長いコードブロックを採用
    candidates = [p for p in parts if p.strip()]
    body = max(candidates, key=len) if candidates else t
    # 先頭言語タグ除去
    for tag in ("json\n", "json\r\n"):
        if body.lstrip().lower().startswith(tag):
            body = body.lstrip()[len(tag):]
            break
    return body.strip()


def _do_generate_seeds(req: SeedGenerationRequest) -> dict[str, Any]:
    import anthropic

    sb = get_supabase()
    idx_resp = (
        sb.table("index_daily")
        .select("*")
        .order("snapshot_date", desc=True)
        .limit(1)
        .execute()
    )
    idx = idx_resp.data[0] if idx_resp.data else {}

    hot_resp = (
        sb.table("listings_daily")
        # brand_name / model は listings_daily に存在しない (products 側にある)。
        # product_id FK 経由で products.brand_name / products.model を埋め込む。
        .select("price_usd, source, snapshot_date, products(brand_name, model)")
        .order("snapshot_date", desc=True)
        .limit(200)
        .execute()
    )
    hot_summary = _summarize_hot_models(hot_resp.data or [])

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")

    client = anthropic.Anthropic(api_key=api_key)
    lang_instruction = (
        "日本語で記述してください。" if req.language == "ja" else "Write in English."
    )

    prompt = f"""あなたは GUITAR ATLAS の編集者です。GUITAR ATLAS は「ギターカルチャーの観測所」であり、中古ギターの相場解説でも投資情報でもありません。下記の観測データを“裏付け”として、記事シードを{req.num_seeds}本生成してください。

# 黄金律（最優先・違反したら作り直し）
データを文化っぽく説明するのではなく、文化の変化をデータで裏付ける。主役は「今のギターシーンの空気・モデル・プレイヤーの感覚」。数字や指数はタイトルや文の主語にせず、文化の読みの“根拠”として後ろに置く。レビューやランキングではなく観測、断定ではなく解釈。

# 文体（ネイティブな自然さが必須）
- 静か / 知的 / 文化的。ですます調を基本。AI 的な抽象表現・説明資料調・翻訳調を避け、音読して自然な日本語にする。
- 派手なコピー・煽り・擬人化過多・YouTube 的タイトル・金融/相場レポート調にしない。
- タイトルは「クリックさせる」ためではなく「読むとギターシーンが少し違って見える」入口にする。

# タイトル / フックの作り方（CFNO）
- まず今週のギターシーンの“文化的な読み（空気）”があり、それを象徴するモデル（固有名）とプレイヤー文脈に接続する。
- 数値（価格・出品数・流動性・話題量）は結論ではなく根拠として hook の後半で軽く触れる。タイトルの冒頭に数字や指数名を置かない。
- 断定しない（「〜と読める」「〜という動きが観測されている」「関心が向いている」）。

# 表示名（外向き名のみ使用）
- 指数は「アトラス指数 / Mainstream Signal / Vintage Signal / Boutique Signal」で書く。
- 内部記号「GAI-E / MFI / VFI / VFI-AC / VFI-AO / BPI」、スプレッド名「Boutique Premium / Vintage Premium / Heritage」は **title・hook に出さない**（スプレッドは公開面で前面化しない）。

# 厳禁（1語でも含めたら不可）
- 投資判断語: 買い時 / 売り時 / 相場 / 相場観 / 銘柄 / 投資 / 投資妙味 / 投資価値 / 値上がり益 / 利確 / 損切り / 割安 / 割高 / お買い得 / 狙い目 / 買い推奨 / 売り推奨 / （売買文脈の）強気・弱気 / Value / Return / Undervalued / Overvalued / buy(sell) signal
- 相場レポート・煽り語: 急騰 / 急落 / 暴落 / 転落 / 崩壊 / 冷却 / 異常値 /「〜の実態」/「〜の時代」/「〜が映す」式の煽り構文
- 評価・ランキング語: 完璧 / 万能 / 最高 / 究極 / 完成形 / 理想形 / 非の打ちどころがない / おすすめランキング / ブランドの優劣。比較は優劣でなく「文脈の違い・選ばれ方の違い」で書く。

# トーン基準（例）
- 悪い例（採用不可）:「VFI急落が示すヴィンテージ市場の冷却——両指数が-3.5以下に沈む異常値の意味」（相場・煽り・内部名・数字が主役）
- 良い例（この方向）:「“鳴り”より“弾きやすさ”へ——王道の現行フラッグシップに関心が戻りつつある週」（文化の読みが主役、固有モデルとプレイヤー文脈に接続、数値は本文で控えめに裏付け）

# 観測データ（タイトルの主役にせず、読みの裏付けに使う）
## 本日のインデックス値（内部値。本文では外向き名へ翻訳して扱う）
{json.dumps(idx, ensure_ascii=False, indent=2, default=str)}

## 価格・出品の動きが目立つモデル（観測値。結論ではなく根拠）
{hot_summary}

## 出力フォーマット
各シードは以下のフィールドを含む JSON で返してください（category / tags は内部分類コードのためそのまま）:
- title: 記事タイトル（上記トーン基準に従う。数字・指数名を冒頭に置かない）
- hook: リード文（2-3文、ですます調。文化の読み→象徴モデル→プレイヤー文脈の順。数値は後半で根拠として軽く）
- key_data_points: 使用するデータポイント（配列、3-5個）
- category: カテゴリ（"gai-e" | "vfi" | "bpi" | "boutique-premium" | "trend"）
- tags: WordPress タグ（配列、"observed"|"indexed"|"spread"|"forecast"|"field-note"）
- priority: 公開優先度（"high" | "medium" | "low"）
- reason: このシードを選んだ理由（1文、編集視点）

{lang_instruction}

# 出力の厳密ルール（必ず守る）
- 厳密にパース可能な **JSON 配列のみ**を返す。前後に説明文・マークダウン・コードフェンスを付けない。
- 文字列値の中では半角ダブルクオート(")を使わない。引用・強調は日本語引用符「」『』または波ダッシュ——で表現する。
- 末尾カンマを付けない。改行や特殊記号は正しくエスケープする。

JSON の配列として {req.num_seeds} 本分のみ返してください。余計な説明は不要です。"""

    # TD: 新プロンプト (文化観測機トーン) はモデルが値内に生のダブルクオートを入れて
    # JSON が壊れる確率があるため、JSON パース失敗時は最大 3 回まで自動リトライする。
    seeds = None
    last_err = None
    for attempt in range(3):
        message = client.messages.create(
            model="claude-opus-4-6",  # 2026-06-02 Sonnet 全廃 (モデル原則 v2.1, CEO 承認)。旧: claude-sonnet-4-6
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text
        try:
            parsed = json.loads(_strip_code_fence(raw))
        except json.JSONDecodeError as e:
            last_err = f"JSON parse error: {e}"
            logger.warning("seed gen attempt %d/3 failed: %s\nraw=%r", attempt + 1, last_err, raw[:300])
            continue
        if not isinstance(parsed, list):
            last_err = "response is not a JSON array"
            logger.warning("seed gen attempt %d/3: %s", attempt + 1, last_err)
            continue
        seeds = parsed
        break

    if seeds is None:
        raise HTTPException(status_code=502, detail=f"Claude seed generation failed after 3 attempts: {last_err}")

    # TD-018: JST (UTC+9) 明示採番。tzdata 非依存。
    # 旧 date.today() は Railway コンテナの UTC 採番 → 6/8 06:00 JST 実行分が
    # generated_at=2026-06-07 で landing し、flow_03 の JST run_date 検索と 0 件マッチになっていた。
    today = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
    rows = []
    for s in seeds:
        if not isinstance(s, dict):
            continue
        rows.append({
            "generated_at": today,
            "title": s.get("title"),
            "hook": s.get("hook"),
            "key_data_points": s.get("key_data_points", []),
            "category": s.get("category"),
            "tags": s.get("tags", []),
            "priority": s.get("priority", "medium"),
            "reason": s.get("reason"),
            "language": req.language,
            "status": "pending",
        })

    if rows:
        sb.table("article_seeds").insert(rows).execute()

    logger.info("Generated %d article seeds for %s", len(rows), today)
    return {"count": len(rows), "date": today, "seeds": seeds}


@app.post("/content/seeds")
async def generate_seeds(req: SeedGenerationRequest, _auth=Depends(verify_secret)):
    try:
        result = await asyncio.to_thread(_do_generate_seeds, req)
        return {"success": True, **result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Seed generation error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════
# Stripe Premium routes (TH-07a)
# ═══════════════════════════════════════════════════════════════════

from routes_stripe import router as stripe_router

app.include_router(stripe_router)


# ═══════════════════════════════════════════════════════════════════
# Dashboard routes (TH-07d)
# ═══════════════════════════════════════════════════════════════════

from routes_dashboard import router as dashboard_router

app.include_router(dashboard_router)

from routes_cultural import router as cultural_router

app.include_router(cultural_router)


# ═══════════════════════════════════════════════════════════════════
# エントリーポイント（ローカル開発用）
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=True)
