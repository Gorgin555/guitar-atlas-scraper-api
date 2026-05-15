"""
GUITAR ATLAS - Scraper API (FastAPI)
=====================================
n8n ?????? Python ????????.
Reverb/?????/Yahoo ?????? + Index Engine + Claude ???????? HTTP ????????????.

??: COO ????
??: 2026-05-15
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
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type

# ?? ???????????????????????????????????????????????
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

load_dotenv(os.path.join(os.path.dirname(__file__), "../../.env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("atlas-scraper-api")

# ?? ?????? ????????????????????????????????????????????????????????
ATLAS_API_SECRET = os.environ.get("ATLAS_API_SECRET", "")


def verify_secret(x_atlas_secret: str = Header(...)):
    """n8n ?????????????."""
    if ATLAS_API_SECRET and x_atlas_secret != ATLAS_API_SECRET:
        raise HTTPException(status_code=403, detail="Invalid API secret")
    return True


# ?? Supabase ?????? ????????????????????????????????????????????????
from supabase import create_client, Client as SupabaseClient

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ["SUPABASE_KEY"]
supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# ?? FastAPI App ??????????????????????????????????????????????????????????
app = FastAPI(
    title="GUITAR ATLAS Scraper API",
    description="n8n ??????????? Python ??????",
    version="1.0.0",
)


# ???????????????????????????????????????????????????????????????????
# ???????
# ???????????????????????????????????????????????????????????????????

@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}

# ????????????????????????????????????????????????????????????????????
# Reverb API ????
# ???????????????????????????????????????????????????????????????????

class FetchReverbRequest(BaseModel):
    basket: str = "active"          # "active" | "passive" | "all"
    max_pages_per_model: int = 3    # Reverb ??????????
    dry_run: bool = False


@app.post("/fetch/reverb")
async def fetch_reverb(req: FetchReverbRequest, _auth=Depends(verify_secret)):
    """Reverb API ??????????????."""
    try:
        from ingest.fetch_listings import run_fetch
        result = await run_fetch(
            basket_filter=req.basket,
            max_pages=req.max_pages_per_model,
            dry_run=req.dry_run,
        )
        logger.info(f"Reverb fetch complete: {result}")
        return {"source": "reverb", "success": True, **result}
    except Exception as e:
        logger.error(f"Reverb fetch error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

class FetchDigimartRequest(BaseModel):
    basket: str = "active"
    dry_run: bool = False

@app.post("/fetch/digimart")
async def fetch_digimart(req: FetchDigimartRequest, _auth=Depends(verify_secret)):
    """Digimart scraper."""
    try:
        from scrapers.digimart import DigimartScraper
        from scrapers.matcher import ListingMatcher
        scraper = DigimartScraper()
        matcher = ListingMatcher(supabase)
        products = _get_products(basket=req.basket)
        inserted = 0
        errors = 0
        for product in products:
            try:
                listings = await scraper.fetch(product)
                if req.dry_run:
                    continue
                matched = matcher.match(listings, product)
                if matched:
                    _upsert_listings(matched, source="digimart")
                    inserted += len(matched)
            except Exception as e:
                logger.warning(f"Digimart error for {product.get('model')}: {e}")
                errors += 1
        return {"source": "digimart", "success": True, "inserted": inserted, "errors": errors, "products_scanned": len(products)}
    except Exception as e:
        logger.error(f"Digimart fetch error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

class FetchYahooRequest(BaseModel):
    basket: str = "active"
    mode: str = "active"
    dry_run: bool = False

@app.post("/fetch/yahoo")
async def fetch_yahoo(req: FetchYahooRequest, _auth=Depends(verify_secret)):
    """Yahoo scraper."""
    try:
        from scrapers.yahoo_auctions import YahooAuctionsScraper
        from scrapers.matcher import ListingMatcher
        scraper = YahooAuctionsScraper()
        matcher = ListingMatcher(supabase)
        products = _get_products(basket=req.basket)
        inserted = 0
        errors = 0
        for product in products:
            try:
                listings = await scraper.fetch(product, mode=req.mode)
                if req.dry_run:
                    continue
                matched = matcher.match(listings, product)
                if matched:
                    _upsert_listings(matched, source="yahoo")
                    inserted += len(matched)
            except Exception as e:
                logger.warning(f"Yahoo error for {product.get('model')}: {e}")
                errors += 1
        return {"source": "yahoo", "success": True, "inserted": inserted, "errors": errors, "products_scanned": len(products)}
    except Exception as e:
        logger.error(f"Yahoo fetch error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/fetch/passive")
async def fetch_passive(_auth=Depends(verify_secret)):
    """Passive collection (Pedals/Acoustic/Amps/JBI)."""
    try:
        from scrapers.passive_collector import PassiveCollector
        collector = PassiveCollector(supabase)
        result = await collector.run_all()
        return {"source": "passive", "success": True, **result}
    except Exception as e:
        logger.error(f"Passive collection error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

class IndexRunRequest(BaseModel):
    target_date: Optional[str] = None
    write_to_db: bool = True

@app.post("/index/run")
async def run_index(req: IndexRunRequest, _auth=Depends(verify_secret)):
    """Run GAI-E/MFI/VFI/BPI index."""
    try:
        from index_engine.engine import run_daily_pipeline
        target = date.fromisoformat(req.target_date) if req.target_date else date.today()
        result = await _run_async(run_daily_pipeline, target, write_to_db=req.write_to_db)
        return {"success": True, "date": str(target), **result}
    except Exception as e:
        logger.error(f"Index run error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/index/latest")
async def get_latest_index(_auth=Depends(verify_secret)):
    """Get latest index."""
    try:
        resp = supabase.table("index_daily").select("*").order("snapshot_date", desc=True).limit(1).execute()
        if not resp.data:
            raise HTTPException(status_code=404, detail="No index data found")
        return resp.data[0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class SeedGenerationRequest(BaseModel):
    num_seeds: int = 3
    language: str = "ja"

@app.post("/content/seeds")
async def generate_seeds(req: SeedGenerationRequest, _auth=Depends(verify_secret)):
    """Generate article seeds via Claude."""
    try:
        import anthropic
        idx_resp = supabase.table("index_daily").select("*").order("snapshot_date", desc=True).limit(1).execute()
        idx = idx_resp.data[0] if idx_resp.data else {}
        hot_resp = supabase.table("listings_daily").select("brand, model, price_usd, source, snapshot_date").order("snapshot_date", desc=True).limit(200).execute()
        hot_summary = _summarize_hot_models(hot_resp.data or [])
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        lang_instruction = "Write in Japanese." if req.language == "ja" else "Write in English."
        prompt = f"""You are GUITAR ATLAS CSO. Generate {req.num_seeds} article seeds for {date.today().isoformat()}.
Index:
{json.dumps(idx, ensure_ascii=False, indent=2)}
Hot models:
{hot_summary}
Fields: title, hook, key_data_points, category(gai-e|vfi|bpi|boutique-premium|trend), tags(observed|indexed|spread|forecast|field-note), priority(high|medium|low), reason.
{lang_instruction}
Return JSON array only."""
        message = client.messages.create(model="claude-sonnet-4-6", max_tokens=2048, messages=[{"role": "user", "content": prompt}])
        raw = message.content[0].text.strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"): raw = raw[4:]
        seeds = json.loads(raw.strip())
        today = date.today().isoformat()
        rows = [{"generated_at": today, "title": s.get("title"), "hook": s.get("hook"), "key_data_points": s.get("key_data_points", []), "category": s.get("category"), "tags": s.get("tags", []), "priority": s.get("priority", "medium"), "reason": s.get("reason"), "language": req.language, "status": "pending"} for s in seeds]
        supabase.table("article_seeds").upsert(rows).execute()
        return {"success": True, "count": len(seeds), "date": today, "seeds": seeds}
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Claude JSON parse error: {e}")
    except Exception as e:
        logger.error(f"Seed generation error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

def _get_products(basket: str = "active") -> list[dict]:
    q = supabase.table("products").select("*")
    if basket == "active": q = q.eq("is_active", True)
    elif basket == "passive": q = q.eq("is_active", False)
    return q.execute().data or []

def _upsert_listings(listings: list[dict], source: str):
    if not listings: return
    for row in listings: row["source"] = source
    supabase.table("listings_daily").upsert(listings, on_conflict="listing_id,snapshot_date").execute()

def _summarize_hot_models(listings: list[dict]) -> str:
    if not listings: return "No data"
    from collections import defaultdict
    by_model: dict[str, list[float]] = defaultdict(list)
    for row in listings:
        key = f"{row.get('brand','?')} {row.get('model','?')}"
        if row.get("price_usd"): by_model[key].append(float(row["price_usd"]))
    lines = []
    for model, prices in sorted(by_model.items(), key=lambda x: -len(x[1]))[:10]:
        avg = sum(prices) / len(prices)
        lines.append(f"- {model}: avg ${avg:,.0f} ({len(prices)} listings)")
    return "\n".join(lines) if lines else "No data"

async def _run_async(func, *args, **kwargs):
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
