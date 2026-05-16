"""
GUITAR ATLAS - Scraper API (FastAPI)
n8n wrapper service for Reverb/Digimart/Yahoo scrapers + Index Engine + Claude seeds.
Author: COO Dream
Date: 2026-05-15
"""

import os
import sys
import json
import logging
from datetime import date, datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# Path resolution - add parent dirs to sys.path for shared modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("atlas-scraper-api")

# Security
ATLAS_API_SECRET = os.environ.get("ATLAS_API_SECRET", "")


def verify_secret(x_atlas_secret: str = Header(default="")):
    """Shared secret auth with n8n."""
    if ATLAS_API_SECRET and x_atlas_secret != ATLAS_API_SECRET:
        raise HTTPException(status_code=403, detail="Invalid API secret")
    return True


# Supabase client (lazy init to avoid KeyError on startup)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY", "")

_supabase_client = None


def get_supabase():
    global _supabase_client
    if _supabase_client is None:
        if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
            raise HTTPException(status_code=503, detail="Supabase not configured (missing env vars)")
        from supabase import create_client
        _supabase_client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _supabase_client


# Other env vars
REVERB_TOKEN = os.environ.get("REVERB_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
WP_URL = os.environ.get("WP_URL", "")
WP_USER = os.environ.get("WP_USER", "")
WP_APP_PASSWORD = os.environ.get("WP_APP_PASSWORD", "")

# FastAPI App
app = FastAPI(
    title="GUITAR ATLAS Scraper API",
    description="Python backend for n8n orchestration",
    version="1.0.0",
)


# Health check - always returns 200 regardless of env vars
@app.get("/health")
def health():
    env_status = {
        "supabase_url": bool(SUPABASE_URL),
        "supabase_key": bool(SUPABASE_SERVICE_KEY),
        "reverb_token": bool(REVERB_TOKEN),
        "anthropic_key": bool(ANTHROPIC_API_KEY),
        "wp_url": bool(WP_URL),
    }
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "env": env_status,
        "ready": all(env_status.values()),
    }


# Debug endpoint - list env var names (not values) for troubleshooting
@app.get("/debug/env")
def debug_env():
    keys = sorted(os.environ.keys())
    return {"env_var_count": len(keys), "keys": keys}


# Reverb API fetch
class FetchReverbRequest(BaseModel):
    basket: str = "active"
    max_pages_per_model: int = 3
    dry_run: bool = False


@app.post("/fetch/reverb")
async def fetch_reverb(req: FetchReverbRequest, _auth=Depends(verify_secret)):
    """Fetch listings from Reverb API and upsert to Supabase."""
    supabase = get_supabase()
    try:
        from ingest.fetch_listings import run_fetch
        result = await run_fetch(
            basket_filter=req.basket,
            max_pages=req.max_pages_per_model,
            dry_run=req.dry_run,
        )
        logger.info("Reverb fetch complete: %s", result)
        return {"source": "reverb", "success": True, **result}
    except Exception as e:
        logger.error("Reverb fetch error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# Digimart scraper
class FetchDigimartRequest(BaseModel):
    basket: str = "active"
    dry_run: bool = False


@app.post("/fetch/digimart")
async def fetch_digimart(req: FetchDigimartRequest, _auth=Depends(verify_secret)):
    """Fetch listings from Digimart."""
    supabase = get_supabase()
    try:
        from scrapers.digimart import DigimartScraper
        from scrapers.matcher import ListingMatcher
        scraper = DigimartScraper()
        matcher = ListingMatcher(supabase)
        products = _get_products(supabase, basket=req.basket)
        inserted = 0
        errors = 0
        for product in products:
            try:
                listings = await scraper.fetch(product)
                if req.dry_run:
                    continue
                matched = matcher.match(listings, product)
                if matched:
                    _upsert_listings(supabase, matched, source="digimart")
                    inserted += len(matched)
            except Exception as e:
                logger.warning("Digimart error for %s: %s", product.get("model"), e)
                errors += 1
        return {"source": "digimart", "success": True, "inserted": inserted, "errors": errors, "products_scanned": len(products)}
    except Exception as e:
        logger.error("Digimart fetch error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# Yahoo Auctions scraper
class FetchYahooRequest(BaseModel):
    basket: str = "active"
    mode: str = "active"
    dry_run: bool = False


@app.post("/fetch/yahoo")
async def fetch_yahoo(req: FetchYahooRequest, _auth=Depends(verify_secret)):
    """Fetch listings from Yahoo Auctions."""
    supabase = get_supabase()
    try:
        from scrapers.yahoo_auctions import YahooAuctionsScraper
        from scrapers.matcher import ListingMatcher
        scraper = YahooAuctionsScraper()
        matcher = ListingMatcher(supabase)
        products = _get_products(supabase, basket=req.basket)
        inserted = 0
        errors = 0
        for product in products:
            try:
                listings = await scraper.fetch(product, mode=req.mode)
                if req.dry_run:
                    continue
                matched = matcher.match(listings, product)
                if matched:
                    _upsert_listings(supabase, matched, source="yahoo")
                    inserted += len(matched)
            except Exception as e:
                logger.warning("Yahoo error for %s: %s", product.get("model"), e)
                errors += 1
        return {"source": "yahoo", "success": True, "inserted": inserted, "errors": errors, "products_scanned": len(products)}
    except Exception as e:
        logger.error("Yahoo fetch error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# Passive collection
@app.post("/fetch/passive")
async def fetch_passive(_auth=Depends(verify_secret)):
    """Passive data collection for Phase 2-4 categories."""
    supabase = get_supabase()
    try:
        from scrapers.passive_collector import PassiveCollector
        collector = PassiveCollector(supabase)
        result = await collector.run_all()
        return {"source": "passive", "success": True, **result}
    except Exception as e:
        logger.error("Passive collection error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# Index Engine
class IndexRunRequest(BaseModel):
    target_date: Optional[str] = None
    write_to_db: bool = True


@app.post("/index/run")
async def run_index(req: IndexRunRequest, _auth=Depends(verify_secret)):
    """Run GAI-E / MFI / VFI / BPI calculation."""
    supabase = get_supabase()
    try:
        from index_engine.engine import run_daily_pipeline
        target = date.fromisoformat(req.target_date) if req.target_date else date.today()
        result = await run_daily_pipeline(target_date=target, write_to_db=req.write_to_db)
        return {"success": True, "date": str(target), **result}
    except Exception as e:
        logger.error("Index run error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/index/latest")
async def get_latest_index(_auth=Depends(verify_secret)):
    """Get latest index values from Supabase."""
    supabase = get_supabase()
    try:
        res = supabase.table("index_daily").select("*").order("snapshot_date", desc=True).limit(1).execute()
        if res.data:
            return {"success": True, "data": res.data[0]}
        return {"success": True, "data": None, "message": "No index data yet"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Content seeds
class SeedsRequest(BaseModel):
    count: int = 3
    language: str = "ja"
    dry_run: bool = False


@app.post("/content/seeds")
async def generate_seeds(req: SeedsRequest, _auth=Depends(verify_secret)):
    """Generate article seeds using Claude."""
    supabase = get_supabase()
    try:
        if not ANTHROPIC_API_KEY:
            raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
        import anthropic
        idx_res = supabase.table("index_daily").select("*").order("snapshot_date", desc=True).limit(1).execute()
        index_data = idx_res.data[0] if idx_res.data else {}
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt = _build_seed_prompt(index_data, count=req.count, language=req.language)
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = message.content[0].text
        seeds = _parse_seeds(raw, index_data, language=req.language)
        if not req.dry_run and seeds:
            for seed in seeds:
                supabase.table("article_seeds").insert(seed).execute()
        return {"success": True, "count": len(seeds), "seeds": seeds}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Seeds generation error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# Helper functions
def _get_products(supabase, basket: str = "active"):
    try:
        q = supabase.table("products").select("id, brand, model, category, basket_type, reverb_make, reverb_model")
        if basket == "active":
            q = q.eq("basket_type", "active")
        elif basket == "passive":
            q = q.eq("basket_type", "passive")
        res = q.execute()
        return res.data or []
    except Exception as e:
        logger.error("_get_products error: %s", e)
        return []


def _upsert_listings(supabase, listings: list, source: str):
    if not listings:
        return
    for listing in listings:
        listing["source"] = source
        listing.setdefault("listing_date", date.today().isoformat())
    supabase.table("listings_daily").upsert(listings, on_conflict="listing_id,source").execute()


def _build_seed_prompt(index_data: dict, count: int = 3, language: str = "ja") -> str:
    lang = "Japanese" if language == "ja" else "English"
    gai_e = index_data.get("gai_e", "N/A")
    mfi = index_data.get("mfi", "N/A")
    vfi_ac = index_data.get("vfi_ac", "N/A")
    bpi = index_data.get("bpi", "N/A")
    boutique_premium = index_data.get("boutique_premium", "N/A")
    vintage_premium = index_data.get("vintage_premium", "N/A")
    snapshot_date = index_data.get("snapshot_date", "today")
    return f"""You are an expert guitar market analyst. Based on the following Guitar Atlas Index data, generate {count} article seed ideas in {lang}.

INDEX DATA ({snapshot_date}):
- GAI-E: {gai_e}
- MFI: {mfi}
- VFI-AC: {vfi_ac}
- BPI: {bpi}
- Boutique Premium Spread: {boutique_premium}x
- Vintage Premium Spread: {vintage_premium}x

Generate exactly {count} article seeds as a JSON array with fields: title, hook, key_data_points (list), category, priority.
Respond with ONLY the JSON array."""


def _parse_seeds(raw: str, index_data: dict, language: str = "ja") -> list:
    try:
        import re
        json_match = re.search(r'\[.*\]', raw, re.DOTALL)
        if not json_match:
            return []
        seeds_raw = json.loads(json_match.group())
        today = date.today().isoformat()
        return [{
            "generated_at": today,
            "title": s.get("title", ""),
            "hook": s.get("hook", ""),
            "key_data_points": s.get("key_data_points", []),
            "category": s.get("category", "trend"),
            "tags": [],
            "priority": s.get("priority", "medium"),
            "reason": s.get("reason", ""),
            "language": language,
            "status": "pending",
        } for s in seeds_raw]
    except Exception as e:
        logger.error("_parse_seeds error: %s", e)
        return []
