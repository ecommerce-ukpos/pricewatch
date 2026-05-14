"""
api/cron/nightly.py
────────────────────
Called by Vercel Cron at 01:00am nightly.
Triggers the scraper as a background process.

Note: For long-running scrapes (500 SKUs × 23 competitors), this endpoint
kicks off a Supabase Edge Function or a background job rather than running
inline (Vercel functions have a 5-min max on Pro, 10s on Hobby).

Recommended production pattern:
  - This endpoint enqueues a job in Supabase (pg_cron or Edge Function)
  - The actual scraper runs on a long-lived process (Railway, Fly.io, VPS)
  - Results are written back to Supabase
"""

import os
import subprocess
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()

CRON_SECRET = os.getenv("CRON_SECRET", "")


@app.get("/api/cron/nightly")
async def nightly_cron(request: Request):
    # Vercel sets this header on cron calls
    auth = request.headers.get("authorization", "")
    if CRON_SECRET and auth != f"Bearer {CRON_SECRET}":
        raise HTTPException(401, "Unauthorized")

    # Trigger scraper — adjust to your deployment method
    # Option A: Supabase Edge Function invoke
    # Option B: Railway webhook
    # Option C: Direct subprocess (only works on always-on deployments)

    import httpx
    edge_fn_url = os.getenv("SCRAPER_EDGE_FN_URL")
    if edge_fn_url:
        async with httpx.AsyncClient() as client:
            await client.post(
                edge_fn_url,
                headers={"Authorization": f"Bearer {os.environ['SUPABASE_SERVICE_KEY']}"},
                json={"trigger": "scheduled"},
                timeout=10,
            )
    
    return {"ok": True, "message": "Nightly scrape triggered"}
