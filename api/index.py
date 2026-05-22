"""
api/index.py — PriceWatch Pro FastAPI backend with auth.

All previous endpoints are preserved. Auth is added inline (no separate
auth.py module needed) so this is a single-file drop-in.

What's new:
  * /api/config              — public, returns Supabase URL + anon key for the SPA
  * /api/auth/me             — returns the caller's profile (any status)
  * /api/auth/request-access — public; creates a pending profile (domain-restricted)
  * /api/admin/users         — super-admin; list/filter by status
  * /api/admin/pending-count — super-admin; badge counter
  * /api/admin/users/{id}/approve — super-admin; sends Supabase invite + marks approved
  * /api/admin/users/{id}/reject  — super-admin; marks rejected / revokes access

Every existing data endpoint now requires a valid bearer token from an
approved user, enforced by Depends(require_user).

Required env vars (set on Vercel):
  SUPABASE_URL              already set
  SUPABASE_SERVICE_KEY      already set
  SUPABASE_ANON_KEY         NEW — Settings -> API -> anon/publishable key
  APP_URL                   NEW — https://pricewatch-iota.vercel.app
  ALLOWED_EMAIL_DOMAINS     NEW — "ukpos.com"
"""

import os
import re
import uuid
from datetime import datetime, timezone
from typing import Optional, Literal

import httpx
from fastapi import FastAPI, Depends, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# ============================================================================
# Configuration
# ============================================================================
SUPABASE_URL         = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
SUPABASE_ANON_KEY    = os.environ.get("SUPABASE_ANON_KEY", "")
APP_URL              = os.environ.get("APP_URL", "").rstrip("/")
ALLOWED_DOMAINS      = [d.strip().lower() for d in
                        os.environ.get("ALLOWED_EMAIL_DOMAINS", "ukpos.com").split(",")
                        if d.strip()]

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def _email_domain_allowed(email: str) -> bool:
    if not _EMAIL_RE.match(email or ""):
        return False
    if not ALLOWED_DOMAINS:
        return True
    return email.rsplit("@", 1)[1].lower() in ALLOWED_DOMAINS

# ============================================================================
# FastAPI app
# ============================================================================
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def get_sb():
    from supabase import create_client
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# ============================================================================
# Auth: low-level helpers
# ============================================================================
def _admin_headers() -> dict:
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }

async def _sb_get_async(path: str, params: Optional[dict] = None) -> httpx.Response:
    async with httpx.AsyncClient(timeout=15.0) as c:
        return await c.get(f"{SUPABASE_URL}{path}", headers=_admin_headers(), params=params)

async def _sb_post_async(path: str, json: dict, prefer: Optional[str] = None) -> httpx.Response:
    headers = _admin_headers()
    if prefer:
        headers["Prefer"] = prefer
    async with httpx.AsyncClient(timeout=15.0) as c:
        return await c.post(f"{SUPABASE_URL}{path}", headers=headers, json=json)

async def _sb_patch_async(path: str, params: dict, json: dict) -> httpx.Response:
    async with httpx.AsyncClient(timeout=15.0) as c:
        return await c.patch(
            f"{SUPABASE_URL}{path}",
            headers={**_admin_headers(), "Prefer": "return=representation"},
            params=params, json=json,
        )

async def _user_from_token(access_token: str) -> Optional[dict]:
    """Validate a Supabase access token; return the auth.users row or None."""
    if not SUPABASE_ANON_KEY:
        # Misconfiguration — fail closed
        return None
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={
                "apikey": SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {access_token}",
            },
        )
    if r.status_code != 200:
        return None
    return r.json()

async def _profile_by_id(user_id: str) -> Optional[dict]:
    r = await _sb_get_async(
        "/rest/v1/profiles",
        params={"id": f"eq.{user_id}", "select": "*", "limit": 1},
    )
    if r.status_code != 200:
        return None
    rows = r.json()
    return rows[0] if rows else None

async def _profile_by_email(email: str) -> Optional[dict]:
    r = await _sb_get_async(
        "/rest/v1/profiles",
        params={"email": f"eq.{email.lower()}", "select": "*", "limit": 1},
    )
    if r.status_code != 200:
        return None
    rows = r.json()
    return rows[0] if rows else None

# ============================================================================
# Auth: dependencies
# ============================================================================
async def require_user(authorization: Optional[str] = Header(default=None)) -> dict:
    """
    Validates the Bearer token; returns the profile dict for an APPROVED user.
    Raises 401 (no/bad token) or 403 (no profile / not approved).
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    token = authorization.split(" ", 1)[1].strip()
    user = await _user_from_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    profile = await _profile_by_id(user["id"])
    if not profile:
        raise HTTPException(status_code=403, detail="Account not provisioned")
    if profile["status"] != "approved":
        raise HTTPException(status_code=403, detail=f"Account {profile['status']}")
    return profile

async def require_admin(profile: dict = Depends(require_user)) -> dict:
    if profile.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="Super admin only")
    return profile

# ============================================================================
# Public auth endpoints
# ============================================================================
@app.get("/api/config")
async def get_config():
    """Public bootstrap config consumed by the SPA on first paint."""
    return {
        "supabase_url": SUPABASE_URL,
        "supabase_anon_key": SUPABASE_ANON_KEY,
        "allowed_domains": ALLOWED_DOMAINS,
    }

@app.get("/api/auth/me")
async def auth_me(authorization: Optional[str] = Header(default=None)):
    """
    Returns the caller's profile regardless of approval status.
    The frontend uses this to decide which view to show
    (pending / rejected / dashboard).

    Profiles are created automatically by the handle_new_auth_user DB
    trigger when an auth.users row is inserted, so by the time anyone has
    a valid session they should have a profile. If not, return 403 and the
    frontend will treat them as pending.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    token = authorization.split(" ", 1)[1].strip()
    user = await _user_from_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    profile = await _profile_by_id(user["id"])
    if not profile:
        raise HTTPException(status_code=403, detail="Account not provisioned")
    return profile

class RequestAccessBody(BaseModel):
    email: str
    full_name: Optional[str] = Field(default=None, max_length=120)

@app.post("/api/auth/request-access")
async def request_access(body: RequestAccessBody):
    """
    Records a pending access request. The request lives in `access_requests`
    until the admin approves — at which point Supabase sends an invite, an
    auth.users row is created, and the DB trigger creates a `profiles` row
    (auto-approved, since the request existed when the auth user appeared).
    """
    email = str(body.email).strip().lower()
    if not _email_domain_allowed(email):
        allowed = ", ".join(ALLOWED_DOMAINS) or "(none configured)"
        raise HTTPException(
            status_code=400,
            detail=f"Only addresses on the following domain(s) are permitted: {allowed}",
        )

    # If a request or profile already exists, just acknowledge (don't leak status)
    existing_profile = await _profile_by_email(email)
    if existing_profile:
        return {"ok": True}
    existing_request = await _sb_get_async(
        "/rest/v1/access_requests",
        params={"email": f"eq.{email}", "select": "email", "limit": 1},
    )
    if existing_request.status_code == 200 and existing_request.json():
        return {"ok": True}

    payload = {
        "email": email,
        "full_name": body.full_name,
    }
    r = await _sb_post_async("/rest/v1/access_requests", json=payload,
                             prefer="return=minimal")
    if r.status_code not in (200, 201, 204):
        if r.status_code == 409:
            return {"ok": True}
        raise HTTPException(status_code=500, detail=f"Failed to record request: {r.text}")
    return {"ok": True}

# ============================================================================
# Admin endpoints
# ============================================================================
StatusFilter = Literal["pending", "approved", "rejected", "all"]

@app.get("/api/admin/users")
async def admin_list_users(
    status: StatusFilter = Query("pending"),
    _admin: dict = Depends(require_admin),
):
    """
    Returns a unified list across access_requests (pending only) and profiles
    (approved / rejected). Each row has these fields:
      kind:        'request' | 'profile'
      id:          email (for requests) or uuid (for profiles)
      email, full_name, status, role, requested_at
    The 'kind' tells the frontend which approve/reject endpoint to call.
    """
    results = []

    if status in ("pending", "all"):
        r = await _sb_get_async(
            "/rest/v1/access_requests",
            params={
                "select": "email,full_name,requested_at",
                "order": "requested_at.desc",
            },
        )
        if r.status_code != 200:
            raise HTTPException(status_code=500, detail="Failed to list requests")
        for row in r.json():
            results.append({
                "kind": "request",
                "id": row["email"],         # email is the PK on access_requests
                "email": row["email"],
                "full_name": row.get("full_name"),
                "status": "pending",
                "role": "user",
                "requested_at": row["requested_at"],
                "approved_at": None,
                "rejected_at": None,
                "rejection_reason": None,
                "last_sign_in_at": None,
            })

    if status in ("approved", "rejected", "all"):
        params = {
            "select": "id,email,full_name,status,role,requested_at,approved_at,rejected_at,rejection_reason,last_sign_in_at",
            "order": "requested_at.desc",
        }
        if status != "all":
            params["status"] = f"eq.{status}"
        else:
            # only approved/rejected from profiles when status='all';
            # pending profiles shouldn't exist post-trigger but be safe
            params["status"] = "in.(approved,rejected)"
        r = await _sb_get_async("/rest/v1/profiles", params=params)
        if r.status_code != 200:
            raise HTTPException(status_code=500, detail="Failed to list users")
        for row in r.json():
            row["kind"] = "profile"
            results.append(row)

    # Sort merged list by requested_at desc
    results.sort(key=lambda x: x.get("requested_at") or "", reverse=True)
    return results

@app.get("/api/admin/pending-count")
async def admin_pending_count(_admin: dict = Depends(require_admin)):
    r = await _sb_get_async(
        "/rest/v1/access_requests",
        params={"select": "email"},
    )
    if r.status_code != 200:
        raise HTTPException(status_code=500, detail="Failed")
    return {"count": len(r.json())}

@app.post("/api/admin/requests/{email}/approve")
async def admin_approve_request(email: str, admin: dict = Depends(require_admin)):
    """
    Approve a pending access request:
      1. Send Supabase invite (creates auth.users row)
      2. DB trigger creates an approved profile (since the access_requests
         row still exists at insert time — trigger checks for it)
      3. Trigger deletes the access_requests row
    """
    email = email.strip().lower()
    if not _email_domain_allowed(email):
        raise HTTPException(status_code=400, detail=f"Email domain not permitted: {email}")

    # Confirm the request exists
    rq = await _sb_get_async(
        "/rest/v1/access_requests",
        params={"email": f"eq.{email}", "select": "*", "limit": 1},
    )
    if rq.status_code != 200 or not rq.json():
        raise HTTPException(status_code=404, detail="Request not found")

    invite_body = {"email": email}
    if APP_URL:
        invite_body["redirect_to"] = APP_URL + "/"
    ir = await _sb_post_async("/auth/v1/admin/invite", json=invite_body)
    if ir.status_code not in (200, 201) and "already" not in ir.text.lower():
        raise HTTPException(status_code=500, detail=f"Invite failed: {ir.text}")

    # Trigger should have already deleted the access_requests row after
    # creating the profile. Belt-and-braces: delete it explicitly in case
    # the trigger didn't (e.g. the auth.users row pre-existed and the
    # trigger didn't fire on a fresh insert).
    async with httpx.AsyncClient(timeout=10.0) as c:
        await c.delete(
            f"{SUPABASE_URL}/rest/v1/access_requests",
            headers=_admin_headers(),
            params={"email": f"eq.{email}"},
        )

    return {"ok": True}

@app.post("/api/admin/requests/{email}/reject")
async def admin_reject_request(email: str, _admin: dict = Depends(require_admin)):
    """
    Reject a pending access request. We simply delete the request row.
    (We don't keep a rejected-requests audit trail to keep the schema simple;
    if the same email re-requests, they go back into the pending queue.)
    """
    email = email.strip().lower()
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.delete(
            f"{SUPABASE_URL}/rest/v1/access_requests",
            headers=_admin_headers(),
            params={"email": f"eq.{email}"},
        )
    if r.status_code not in (200, 204):
        raise HTTPException(status_code=500, detail=f"Could not reject: {r.text}")
    return {"ok": True}

class RejectBody(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=200)

@app.post("/api/admin/users/{profile_id}/revoke")
async def admin_revoke_user(
    profile_id: str,
    body: Optional[RejectBody] = None,
    admin: dict = Depends(require_admin),
):
    """
    Revoke an existing approved user. Marks profile rejected. They keep
    their auth.users row (Supabase Admin -> Users can delete it manually if
    you want to fully remove them), but require_user blocks them since
    their profile status is no longer 'approved'.
    """
    if profile_id == admin["id"]:
        raise HTTPException(status_code=400, detail="You cannot revoke your own account")
    ur = await _sb_patch_async(
        "/rest/v1/profiles",
        params={"id": f"eq.{profile_id}"},
        json={
            "status": "rejected",
            "rejected_at": datetime.now(timezone.utc).isoformat(),
            "rejection_reason": body.reason if body else None,
        },
    )
    if ur.status_code not in (200, 204):
        raise HTTPException(status_code=500, detail=f"Could not revoke: {ur.text}")
    return {"ok": True}

@app.post("/api/admin/users/{profile_id}/reinstate")
async def admin_reinstate_user(profile_id: str, admin: dict = Depends(require_admin)):
    """Restore a previously rejected user to approved status."""
    ur = await _sb_patch_async(
        "/rest/v1/profiles",
        params={"id": f"eq.{profile_id}"},
        json={
            "status": "approved",
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "approved_by": admin["id"],
            "rejected_at": None,
            "rejection_reason": None,
        },
    )
    if ur.status_code not in (200, 204):
        raise HTTPException(status_code=500, detail=f"Could not reinstate: {ur.text}")
    return {"ok": True}

# ============================================================================
# EXISTING DATA ENDPOINTS — now protected with Depends(require_user)
# ============================================================================

@app.get("/api/dashboard")
def dashboard(_user: dict = Depends(require_user)):
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
    return {
        "metrics": {"critical":critical,"warning":warning,"cheapest":cheapest,"oos":oos,"review":review},
        "last_run": last_run[0] if last_run else None,
        "alerts": alerts,
        "worst": worst,
    }

@app.get("/api/skus")
def list_skus(
    q: Optional[str] = None,
    diff: Optional[str] = None,
    vat: Optional[str] = None,
    stock: Optional[str] = None,
    page: int = 1,
    limit: int = 50,
    _user: dict = Depends(require_user),
):
    sb = get_sb()
    limit = min(limit, 200)
    offset = (page-1)*limit
    query = sb.table("latest_snapshots").select("*")
    if q: query = query.or_(f"short_title.ilike.%{q}%,sku_id.ilike.%{q}%,competitor_name.ilike.%{q}%")
    if diff == "crit":   query = query.lte("diff_pct_normalised", -10)
    elif diff == "warn": query = query.gt("diff_pct_normalised", -10).lte("diff_pct_normalised", -5)
    elif diff == "par":  query = query.gte("diff_pct_normalised", -2).lte("diff_pct_normalised", 2)
    elif diff == "cheap": query = query.gt("diff_pct_normalised", 0)
    if vat: query = query.eq("competitor_vat", vat)
    if stock == "oos":      query = query.eq("availability","out_of_stock")
    elif stock == "unavail": query = query.eq("availability","unavailable")
    elif stock == "in":      query = query.eq("availability","in_stock")
    result = query.order("diff_pct_normalised", nullsfirst=False).range(offset, offset+limit-1).execute()
    return {"data": result.data or [], "page": page, "limit": limit}

@app.get("/api/competitors")
def list_competitors(_user: dict = Depends(require_user)):
    sb = get_sb()
    return {"data": sb.table("competitors").select("*").eq("active", True).order("name").execute().data or []}

@app.patch("/api/competitors/{competitor_id}")
def update_competitor(competitor_id: int, body: dict, _user: dict = Depends(require_user)):
    sb = get_sb()
    result = sb.table("competitors").update(body).eq("id", competitor_id).execute()
    return {"ok": True, "data": result.data}

@app.get("/api/review")
def review_queue(_user: dict = Depends(require_user)):
    sb = get_sb()
    rows = sb.table("competitor_matches").select("*, skus(short_title,price_ex_vat,product_url,slug), competitors(name,domain,vat_status)").eq("match_status","review").order("confidence").execute().data or []
    return {"data": rows}

@app.post("/api/review/{match_id}")
def review_match(match_id: int, body: dict, _user: dict = Depends(require_user)):
    sb = get_sb()
    new_status = "matched" if body.get("decision") == "approve" else "rejected"
    sb.table("competitor_matches").update({
        "match_status": new_status,
        "human_reviewed": True,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", match_id).execute()
    return {"ok": True, "status": new_status}

@app.post("/api/alerts/{alert_id}/dismiss")
def dismiss_alert(alert_id: int, _user: dict = Depends(require_user)):
    sb = get_sb()
    sb.table("alerts").update({
        "dismissed": True,
        "dismissed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", alert_id).execute()
    return {"ok": True}

@app.get("/api/runs")
def list_runs(limit: int = 20, _user: dict = Depends(require_user)):
    sb = get_sb()
    return {"data": sb.table("sync_runs").select("*").order("started_at", desc=True).limit(min(limit,100)).execute().data or []}

@app.post("/api/lookup")
def manual_lookup(body: dict, _user: dict = Depends(require_user)):
    sb = get_sb()
    sku = sb.table("skus").select("sku_id").eq("sku_id", body.get("sku_id","")).execute().data
    if not sku:
        return {"error": "SKU not found"}
    return {"ok": True, "message": f"Lookup queued for {body.get('sku_id')}"}