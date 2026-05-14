"""
api/main.py
───────────
FastAPI backend for PriceWatch Pro.
Deployed as a Vercel Serverless Function (api/main.py → /api/*).

Environment variables:
    SUPABASE_URL
    SUPABASE_SERVICE_KEY
    SCRAPER_WEBHOOK_SECRET   (optional — for triggering manual scrapes)
"""

import asyncio
import hmac
import hashlib
import os
import subprocess
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client

app = FastAPI(title="PriceWatch Pro API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_sb():
    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_KEY"],
    )


# ─── Dashboard summary ────────────────────────────────────────────────────────

@app.get("/api/dashboard")
async def dashboard():
    sb = get_sb()

    # Metric counts from latest_snapshots view
    all_snap = sb.table("latest_snapshots").select(
        "sku_id,competitor_id,diff_pct_normalised,diff_pct,availability"
    ).execute().data

    critical = sum(1 for r in all_snap if (r.get("diff_pct_normalised") or r.get("diff_pct") or 0) <= -10)
    warning  = sum(1 for r in all_snap if -10 < (r.get("diff_pct_normalised") or r.get("diff_pct") or 0) <= -5)
    cheapest = sum(1 for r in all_snap if (r.get("diff_pct_normalised") or r.get("diff_pct") or 0) > 0)
    oos      = sum(1 for r in all_snap if r.get("availability") in ("out_of_stock",))
    review   = sb.table("competitor_matches").select("id", count="exact").eq("match_status","review").execute().count or 0

    # Last sync run
    last_run = sb.table("sync_runs").select("*").order("started_at", desc=True).limit(1).execute().data
    last_run = last_run[0] if last_run else None

    # Undismissed alerts
    alerts = sb.table("alerts").select(
        "*, skus(short_title,product_url,slug), competitors(name,domain)"
    ).eq("dismissed", False).order("created_at", desc=True).limit(50).execute().data

    # Worst differentials (top 10)
    worst = sb.table("worst_differentials").select("*").limit(10).execute().data

    return {
        "metrics": {
            "critical": critical,
            "warning":  warning,
            "cheapest": cheapest,
            "oos":      oos,
            "review":   review,
        },
        "last_run": last_run,
        "alerts":   alerts,
        "worst":    worst,
    }


# ─── SKU comparisons ─────────────────────────────────────────────────────────

@app.get("/api/skus")
async def list_skus(
    q:      Optional[str]  = Query(None),
    diff:   Optional[str]  = Query(None),   # crit|warn|par|cheap
    vat:    Optional[str]  = Query(None),   # ex|inc|unknown
    stock:  Optional[str]  = Query(None),   # in|oos|unavail
    page:   int            = Query(1, ge=1),
    limit:  int            = Query(50, le=200),
):
    sb = get_sb()
    query = sb.table("latest_snapshots").select("*")

    if q:
        query = query.or_(
            f"short_title.ilike.%{q}%,sku_id.ilike.%{q}%,competitor_name.ilike.%{q}%"
        )

    if diff == "crit":
        query = query.lte("diff_pct_normalised", -10)
    elif diff == "warn":
        query = query.gt("diff_pct_normalised", -10).lte("diff_pct_normalised", -5)
    elif diff == "par":
        query = query.gte("diff_pct_normalised", -2).lte("diff_pct_normalised", 2)
    elif diff == "cheap":
        query = query.gt("diff_pct_normalised", 0)

    if vat:
        query = query.eq("competitor_vat", vat)

    if stock == "oos":
        query = query.eq("availability", "out_of_stock")
    elif stock == "unavail":
        query = query.eq("availability", "unavailable")
    elif stock == "in":
        query = query.eq("availability", "in_stock")

    offset = (page - 1) * limit
    result = query.order("diff_pct_normalised", nullsfirst=False).range(offset, offset + limit - 1).execute()
    return {"data": result.data, "page": page, "limit": limit}


@app.get("/api/skus/{sku_id}")
async def get_sku(sku_id: str):
    sb = get_sb()
    sku = sb.table("skus").select("*").eq("sku_id", sku_id).single().execute().data
    if not sku:
        raise HTTPException(404, "SKU not found")

    snapshots = sb.table("price_snapshots").select(
        "*, competitors(name,domain)"
    ).eq("sku_id", sku_id).order("scraped_at", desc=True).limit(200).execute().data

    history = sb.table("price_snapshots").select(
        "scraped_at,competitor_id,competitor_price,competitor_vat,diff_pct_normalised,availability,competitors(name)"
    ).eq("sku_id", sku_id).order("scraped_at", desc=True).limit(500).execute().data

    return {"sku": sku, "snapshots": snapshots, "history": history}


# ─── Price history (for charts) ───────────────────────────────────────────────

@app.get("/api/history/{sku_id}")
async def price_history(sku_id: str, days: int = Query(30, le=365)):
    sb = get_sb()
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    rows = sb.table("price_snapshots").select(
        "scraped_at,competitor_id,competitor_price,diff_pct_normalised,availability,competitors(name,domain)"
    ).eq("sku_id", sku_id).gte("scraped_at", since).order("scraped_at").execute().data

    return {"sku_id": sku_id, "days": days, "data": rows}


# ─── Competitors ──────────────────────────────────────────────────────────────

@app.get("/api/competitors")
async def list_competitors():
    sb = get_sb()
    comps = sb.table("competitors").select("*").eq("active", True).order("name").execute().data
    return {"data": comps}


class UpdateCompetitor(BaseModel):
    vat_status: Optional[str] = None
    active:     Optional[bool] = None
    feed_url:   Optional[str] = None
    notes:      Optional[str] = None

@app.patch("/api/competitors/{competitor_id}")
async def update_competitor(competitor_id: int, body: UpdateCompetitor):
    sb = get_sb()
    updates = {k: v for k, v in body.dict().items() if v is not None}
    if not updates:
        raise HTTPException(400, "No fields to update")
    result = sb.table("competitors").update(updates).eq("id", competitor_id).execute()
    return {"ok": True, "data": result.data}


# ─── Matches / review queue ───────────────────────────────────────────────────

@app.get("/api/review")
async def review_queue():
    sb = get_sb()
    rows = sb.table("competitor_matches").select(
        "*, skus(short_title,price_ex_vat,product_url,slug), competitors(name,domain,vat_status)"
    ).eq("match_status", "review").order("confidence").execute().data
    return {"data": rows}


class ReviewDecision(BaseModel):
    decision:   str   # 'approve' | 'reject'
    reviewed_by: Optional[str] = None

@app.post("/api/review/{match_id}")
async def review_match(match_id: int, body: ReviewDecision):
    sb = get_sb()
    from datetime import datetime, timezone
    new_status = "matched" if body.decision == "approve" else "rejected"
    sb.table("competitor_matches").update({
        "match_status":   new_status,
        "human_reviewed": True,
        "reviewed_by":    body.reviewed_by,
        "reviewed_at":    datetime.now(timezone.utc).isoformat(),
    }).eq("id", match_id).execute()
    return {"ok": True, "status": new_status}


# ─── Alerts ───────────────────────────────────────────────────────────────────

@app.post("/api/alerts/{alert_id}/dismiss")
async def dismiss_alert(alert_id: int):
    sb = get_sb()
    from datetime import datetime, timezone
    sb.table("alerts").update({
        "dismissed":    True,
        "dismissed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", alert_id).execute()
    return {"ok": True}


# ─── Manual lookup (triggers scraper for a single SKU) ────────────────────────

class ManualLookup(BaseModel):
    sku_id: str

@app.post("/api/lookup")
async def manual_lookup(body: ManualLookup):
    """
    Trigger an on-demand scrape for a single SKU.
    Runs the scraper in a background task and returns immediately.
    In production this would enqueue a job (e.g. via Supabase Edge Function or a queue).
    """
    sb = get_sb()
    sku = sb.table("skus").select("*").eq("sku_id", body.sku_id).single().execute().data
    if not sku:
        raise HTTPException(404, "SKU not found")

    # Kick off background task
    asyncio.create_task(_run_single_lookup(body.sku_id))
    return {"ok": True, "message": f"Lookup queued for {body.sku_id}"}

async def _run_single_lookup(sku_id: str):
    """Background task - runs scraper for one SKU against all competitors."""
    try:
        subprocess.Popen(
            ["python", "-m", "scraper.scraper", "manual", "--sku", sku_id],
            env={**os.environ},
        )
    except Exception as e:
        pass  # Log to Supabase in production


# ─── Sync runs ────────────────────────────────────────────────────────────────

@app.get("/api/runs")
async def list_runs(limit: int = Query(20, le=100)):
    sb = get_sb()
    rows = sb.table("sync_runs").select("*").order("started_at", desc=True).limit(limit).execute().data
    return {"data": rows}
