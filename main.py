"""
GUITAR ATLAS -- Scraper API (FastAPI) v2.1
==========================================
Standalone FastAPI service for n8n orchestration.
- Uses httpx for Supabase REST API (no supabase-py, supports sb_secret_* keys)
- Uses httpx for Reverb API
- Fully self-contained (no imports from local ingest/scrapers modules)

Author: COO Dream
Updated: 2026-05-17 -- Fix: supabase-py removed, httpx REST client
"""

import json
import logging
import os
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("atlas-scraper-api")

# -- Environment ---------------------------------------------------------------
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
REVERB_TOKEN = os.environ.get("REVERB_PERSONAL_TOKEN", "")
REVERB_API_BASE = os.environ.get("REVERB_API_BASE", "https://api.reverb.com/api")
REVERB_USER_AGENT = os.environ.get(
    "REVERB_USER_AGENT", "GuitarAtlas/0.1 (contact: i49rake@gmail.com)"
)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ATLAS_API_SECRET = os.environ.get("ATLAS_API_SECRET", "")


# -- Security ------------------------------------------------------------------
def verify_secret(x_atlas_secret: str = Header(...)):
    if ATLAS_API_SECRET and x_atlas_secret != ATLAS_API_SECRET:
        raise HTTPException(status_code=403, detail="Invalid API secret")
    return True


# -- Supabase REST helpers (no supabase-py needed) ----------------------------

def _sb_headers(prefer: str = "") -> dict:
    h = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def sb_select(
    table: str,
    select: str = "*",
    filters: Optional[dict] = None,
    order: Optional[str] = None,
    limit: Optional[int] = None,
) -> list:
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    params: dict = {"select": select}
    if filters:
        params.update(filters)
    if order:
        params["order"] = order
    if limit is not None:
        params["limit"] = str(limit)
    r = httpx.get(url, headers=_sb_headers(), params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def sb_upsert(table: str, rows: list, on_conflict: str = "") -> None:
    if not rows:
        return
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    params: dict = {}
    if on_conflict:
        params["on_conflict"] = on_conflict
    r = httpx.post(
        url,
        json=rows,
        headers=_sb_headers("resolution=merge-duplicates,return=minimal"),
        params=params,
        timeout=60,
    )
    r.raise_for_status()


# -- Reverb API client ---------------------------------------------------------

def reverb_search(
    query: str,
    state: str = "live",
    per_page: int = 50,
    max_pages: int = 3,
) -> list:
    headers = {
        "Authorization": f"Bearer {REVERB_TOKEN}",
        "Accept": "application/hal+json",
        "Accept-Version": "3.0",
        "Content-Type": "application/hal+json",
        "User-Agent": REVERB_USER_AGENT,
    }
    params = {"query": query, "per_page": per_page, "page": 1}
    if state and state != "live":
        params["state"] = state

    all_listings = []
    next_url: Optional[str] = None

    for _ in range(max_pages):
        time.sleep(1.0)
        if next_url:
            r = httpx.get(next_url, headers=headers, timeout=30)
        else:
            r = httpx.get(
                f"{REVERB_API_BASE}/listings", headers=headers, params=params, timeout=30
            )

        if r.status_code == 429:
            retry_after = int(r.headers.get("Retry-After", "10"))
            logger.warning("Reverb 429 -- sleeping %ds", retry_after)
            time.sleep(retry_after)
            continue
        r.raise_for_status()

        data = r.json()
        all_listings.extend(data.get("listings") or [])
        links = data.get("_links") or {}
        next_url = (links.get("next") or {}).get("href")
        if not next_url:
            break

    return all_listings


# -- FastAPI App ---------------------------------------------------------------
app = FastAPI(title="GUITAR ATLAS Scraper API", version="2.1.0")


@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


# -- Reverb fetch --------------------------------------------------------------

class FetchReverbRequest(BaseModel):
    basket: str = "active"
    max_pages_per_model: int = 3
    dry_run: bool = False


_REVERB_COND_MAP = {
    "Mint": "mint", "Excellent": "excellent", "Very Good": "very_good",
    "Good": "good", "Fair": "fair", "Poor": "poor",
    "Non Functioning": "non_functioning", "B-Stock": "b_stock", "Brand New": "brand_new",
}


def _parse_listing(listing: dict, product_id: str, snapshot_date: str) -> dict:
    price = listing.get("price") or {}
    price_str = price.get("amount")
    currency = price.get("currency")
    try:
        price_float = float(price_str) if price_str else None
    except (TypeError, ValueError):
        price_float = None
    price_usd = price_float if currency == "USD" else None
    cond_raw = (listing.get("condition") or {}).get("display_name") or listing.get(
        "condition_slug"
    )
    cond_norm = _REVERB_COND_MAP.get(cond_raw or "")
    loc = listing.get("location") or {}
    shipping = listing.get("shipping") or {}
    seller = listing.get("shop") or {}
    src_url = ((listing.get("_links") or {}).get("web") or {}).get("href")
    return {
        "source": "reverb",
        "source_listing_id": str(listing.get("id") or ""),
        "source_url": src_url,
        "product_id": product_id,
        "matched_confidence": 0.80,
        "listed_at": listing.get("created_at"),
        "sold_at": listing.get("sold_at"),
        "is_sold": bool(listing.get("sold_at")),
        "price_local": price_float,
        "currency": currency,
        "price_usd": price_usd,
        "condition": cond_norm,
        "condition_raw": cond_raw,
        "condition_tags": [],
        "location_country": shipping.get("local_pickup_country_code")
        or loc.get("country_code"),
        "location_region": loc.get("region"),
        "seller_type": "dealer" if seller else "unknown",
        "seller_name": seller.get("name"),
        "title": listing.get("title"),
        "snapshot_date": snapshot_date,
    }


@app.post("/fetch/reverb")
async def fetch_reverb(req: FetchReverbRequest, _auth=Depends(verify_secret)):
    try:
        filters: dict = {}
        if req.basket == "active":
            filters["is_passive"] = "eq.false"
        elif req.basket == "passive":
            filters["is_passive"] = "eq.true"

        products = sb_select(
            "products",
            select="product_id,basket_id,brand_name,model,year_range_str",
            filters=filters if filters else None,
        )
        logger.info(
            "Reverb fetch: %d products (basket=%s)", len(products), req.basket
        )

        snapshot_date = date.today().isoformat()
        total_listings = 0
        errors = 0

        for product in products:
            query = f"{product['brand_name']} {product['model']}"
            if product.get("year_range_str"):
                query += f" {product['year_range_str']}"
            try:
                listings = reverb_search(query, max_pages=req.max_pages_per_model)
                if req.dry_run:
                    logger.info("[DRY RUN] %s -> %d listings", query, len(listings))
                    continue
                rows = [
                    _parse_listing(l, product["product_id"], snapshot_date)
                    for l in listings
                ]
                if rows:
                    sb_upsert(
                        "listings_daily",
                        rows,
                        on_conflict="source,source_listing_id,snapshot_date",
                    )
                    total_listings += len(rows)
                logger.info("%s -> %d upserted", query, len(rows))
            except Exception as e:
                logger.warning("Reverb error for %s: %s", product.get("model"), e)
                errors += 1

        return {
            "source": "reverb",
            "success": True,
            "products_scanned": len(products),
            "listings_upserted": total_listings,
            "errors": errors,
        }
    except Exception as e:
        logger.error("Reverb fetch error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# -- Digimart (stub) -----------------------------------------------------------

class FetchDigimartRequest(BaseModel):
    basket: str = "active"
    dry_run: bool = False


@app.post("/fetch/digimart")
async def fetch_digimart(req: FetchDigimartRequest, _auth=Depends(verify_secret)):
    return {
        "source": "digimart",
        "success": True,
        "message": "Digimart scraper not deployed in cloud environment.",
    }


# -- Yahoo Auctions (stub) -----------------------------------------------------

class FetchYahooRequest(BaseModel):
    basket: str = "active"
    mode: str = "active"
    dry_run: bool = False


@app.post("/fetch/yahoo")
async def fetch_yahoo(req: FetchYahooRequest, _auth=Depends(verify_secret)):
    return {
        "source": "yahoo",
        "success": True,
        "message": "Yahoo Auctions scraper not deployed in cloud environment.",
    }


# -- Index run -----------------------------------------------------------------

class IndexRunRequest(BaseModel):
    target_date: Optional[str] = None
    write_to_db: bool = True


@app.post("/index/run")
async def run_index(req: IndexRunRequest, _auth=Depends(verify_secret)):
    try:
        target = req.target_date or date.today().isoformat()

        rows = sb_select(
            "listings_daily",
            select="product_id,price_usd,source,snapshot_date",
            filters={"price_usd": "not.is.null"},
            order="snapshot_date.desc",
            limit=2000,
        )

        by_product: dict = defaultdict(list)
        for row in rows:
            if row.get("price_usd"):
                by_product[row["product_id"]].append(float(row["price_usd"]))

        models_with_data = len(by_product)

        if req.write_to_db:
            index_row = {
                "snapshot_date": target,
                "gai_e": None,
                "mfi": None,
                "vfi_ac": None,
                "vfi_ao": None,
                "bpi": None,
                "listings_count": len(rows),
                "models_with_data": models_with_data,
                "calibrated": False,
            }
            try:
                sb_upsert("index_daily", [index_row], on_conflict="snapshot_date")
                logger.info(
                    "index_daily upserted for %s (%d listings)", target, len(rows)
                )
            except Exception as e:
                logger.warning("index_daily upsert failed (table may not exist): %s", e)

        return {
            "success": True,
            "date": target,
            "products_computed": models_with_data,
            "listings_used": len(rows),
        }
    except Exception as e:
        logger.error("Index run error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/index/latest")
async def get_latest_index(_auth=Depends(verify_secret)):
    try:
        data = sb_select("index_daily", order="snapshot_date.desc", limit=1)
        if not data:
            raise HTTPException(status_code=404, detail="No index data found")
        return data[0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -- Content seeds -------------------------------------------------------------

class SeedGenerationRequest(BaseModel):
    num_seeds: int = 3
    language: str = "ja"


@app.post("/content/seeds")
async def generate_seeds(req: SeedGenerationRequest, _auth=Depends(verify_secret)):
    try:
        import anthropic

        idx_data = sb_select("index_daily", order="snapshot_date.desc", limit=1)
        idx = idx_data[0] if idx_data else {}

        hot_data = sb_select(
            "listings_daily",
            select="price_usd,source,snapshot_date,products(brand_name,model)",
            filters={"price_usd": "not.is.null"},
            order="snapshot_date.desc",
            limit=200,
        )
        hot_summary = _summarize_hot_models(hot_data)

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        lang_instr = (
            "Respond in Japanese." if req.language == "ja" else "Respond in English."
        )

        prompt = f"""You are the Chief Strategy Officer of GUITAR ATLAS.
Based on today's ({date.today().isoformat()}) market data, generate {req.num_seeds} article seeds.

## Today's Index Values
{json.dumps(idx, ensure_ascii=False, indent=2)}

## Hot Models Summary
{hot_summary}

## Requirements
Return a JSON array with {req.num_seeds} objects, each with:
- title: Compelling article title backed by data
- hook: Lead paragraph (2-3 sentences)
- key_data_points: Array of 3-5 data points
- category: One of "gai-e"|"vfi"|"bpi"|"boutique-premium"|"trend"
- tags: Array from "observed"|"indexed"|"spread"|"forecast"|"field-note"
- priority: "high"|"medium"|"low"
- reason: One sentence why this seed was chosen

{lang_instr}

Return ONLY the JSON array. No markdown, no extra text."""

        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = message.content[0].text.strip()
        if "```" in raw:
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else parts[0]
            if raw.startswith("json"):
                raw = raw[4:]
        seeds = json.loads(raw.strip())

        today = date.today().isoformat()
        rows = [
            {
                "generated_at": today,
                "title": s.get("title", ""),
                "hook": s.get("hook"),
                "key_data_points": s.get("key_data_points", []),
                "category": s.get("category", "trend"),
                "tags": s.get("tags", []),
                "priority": s.get("priority", "medium"),
                "reason": s.get("reason"),
                "language": req.language,
                "status": "pending",
            }
            for s in seeds
        ]
        sb_upsert("article_seeds", rows)
        logger.info("Generated %d article seeds for %s", len(seeds), today)

        return {"success": True, "count": len(seeds), "date": today, "seeds": seeds}

    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Claude JSON parse error: {e}")
    except Exception as e:
        logger.error("Seed generation error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# -- Helpers -------------------------------------------------------------------

def _summarize_hot_models(listings: list) -> str:
    if not listings:
        return "No listing data available."
    by_model: dict = defaultdict(list)
    for row in listings:
        product = row.get("products") or {}
        brand = product.get("brand_name", "Unknown")
        model = product.get("model", "Unknown")
        key = f"{brand} {model}"
        if row.get("price_usd"):
            by_model[key].append(float(row["price_usd"]))
    lines = []
    for model_name, prices in sorted(by_model.items(), key=lambda x: -len(x[1]))[:10]:
        avg = sum(prices) / len(prices)
        lines.append(f"- {model_name}: avg ${avg:,.0f} ({len(prices)} listings)")
    return "\n".join(lines) if lines else "No listing data available."


# -- Entrypoint ----------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
