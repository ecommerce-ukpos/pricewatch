from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qs
import json, os

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def get_sb():
    from supabase import create_client
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

@app.get("/api/dashboard")
def dashboard():
    sb = get_sb()
    all_snap = sb.table("latest_snapshots").select("sku_id,competitor_id,diff_pct_normalised,diff_pct,availability").execute().data or []
    critical = sum(1 for r in all_snap if (r.get("diff_pct_normalised") or r.get("diff_pct") or 0) <= -10)
    warning  = sum(1 for r in all_snap if -10 < (r.get("diff_pct_normalised") or r.get("diff_pct") or 0) <= -5)
    cheapest = sum(1 for r in all_snap if (r.get("diff_pct_normalised") or r.get("diff_pct") or 0) > 0)
    oos      = sum(1 for r in all_snap if r.get("availability") == "out_of_stock")
    review   = sb.table("competitor_matches").select("id", count="exact").eq("match_status","review").execute().count or 0
    last_run = sb.table("sync_runs").select("*").order("started_at", desc=True).limit(1).execute().data
    alerts   = sb.table("alerts").select("*, skus(short_title,product_url,slug), competitors(name,domain)").eq("dismissed", False).order("created_at", desc=True).limit(50).execute().data or []
    worst    = sb.table("worst_differentials").select("*").limit(10).execute().data or []
    return {"metrics": {"critical":critical,"warning":warning,"cheapest":cheapest,"oos":oos,"review":review}, "last_run": last_run[0] if last_run else None, "alerts": alerts, "worst": worst}

@app.get("/api/skus")
def list_skus(q: Optional[str]=None, diff: Optional[str]=None, vat: Optional[str]=None, stock: Optional[str]=None, page: int=1, limit: int=50):
    sb = get_sb()
    limit = min(limit, 200)
    offset = (page-1)*limit
    query = sb.table("latest_snapshots").select("*")
    if q: query = query.or_(f"short_title.ilike.%{q}%,sku_id.ilike.%{q}%,competitor_name.ilike.%{q}%")
    if diff == "crit":  query = query.lte("diff_pct_normalised", -10)
    elif diff == "warn": query = query.gt("diff_pct_normalised", -10).lte("diff_pct_normalised", -5)
    elif diff == "par":  query = query.gte("diff_pct_normalised", -2).lte("diff_pct_normalised", 2)
    elif diff == "cheap": query = query.gt("diff_pct_normalised", 0)
    if vat: query = query.eq("competitor_vat", vat)
    if stock == "oos":    query = query.eq("availability","out_of_stock")
    elif stock == "unavail": query = query.eq("availability","unavailable")
    elif stock == "in":   query = query.eq("availability","in_stock")
    result = query.order("diff_pct_normalised", nullsfirst=False).range(offset, offset+limit-1).execute()
    return {"data": result.data or [], "page": page, "limit": limit}

@app.get("/api/competitors")
def list_competitors():
    sb = get_sb()
    return {"data": sb.table("competitors").select("*").eq("active", True).order("name").execute().data or []}

@app.patch("/api/competitors/{competitor_id}")
def update_competitor(competitor_id: int, body: dict):
    sb = get_sb()
    result = sb.table("competitors").update(body).eq("id", competitor_id).execute()
    return {"ok": True, "data": result.data}

@app.get("/api/review")
def review_queue():
    sb = get_sb()
    rows = sb.table("competitor_matches").select("*, skus(short_title,price_ex_vat,product_url,slug), competitors(name,domain,vat_status)").eq("match_status","review").order("confidence").execute().data or []
    return {"data": rows}

@app.post("/api/review/{match_id}")
def review_match(match_id: int, body: dict):
    sb = get_sb()
    new_status = "matched" if body.get("decision") == "approve" else "rejected"
    sb.table("competitor_matches").update({"match_status": new_status, "human_reviewed": True, "reviewed_at": datetime.now(timezone.utc).isoformat()}).eq("id", match_id).execute()
    return {"ok": True, "status": new_status}

@app.post("/api/alerts/{alert_id}/dismiss")
def dismiss_alert(alert_id: int):
    sb = get_sb()
    sb.table("alerts").update({"dismissed": True, "dismissed_at": datetime.now(timezone.utc).isoformat()}).eq("id", alert_id).execute()
    return {"ok": True}

@app.get("/api/runs")
def list_runs(limit: int=20):
    sb = get_sb()
    return {"data": sb.table("sync_runs").select("*").order("started_at", desc=True).limit(min(limit,100)).execute().data or []}

@app.post("/api/lookup")
def manual_lookup(body: dict):
    sb = get_sb()
    sku = sb.table("skus").select("sku_id").eq("sku_id", body.get("sku_id","")).execute().data
    if not sku:
        return {"error": "SKU not found"}
    return {"ok": True, "message": f"Lookup queued for {body.get('sku_id')}"}
