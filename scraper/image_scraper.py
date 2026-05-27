"""
UKPOS Product Image Scraper
============================
Downloads product images from ukpos.com (which blocks hotlinking) and
stores them in Supabase Storage so they can be served publicly.

Run quarterly — only re-fetches images older than REFRESH_DAYS or missing entirely.

Environment variables:
    SUPABASE_URL
    SUPABASE_SERVICE_KEY
    IMAGE_WORKERS       (default: 10) — concurrent download threads
    IMAGE_REFRESH_DAYS  (default: 90) — re-download if older than this many days
    IMAGE_SKU_LIMIT     (default: 5000) — max SKUs per run (0 = all)
    LOG_LEVEL           (default: INFO)
"""

import asyncio
import io
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from supabase import create_client, Client

# ── Config ─────────────────────────────────────────────────────────────────────
SUPABASE_URL      = os.environ["SUPABASE_URL"]
SUPABASE_KEY      = os.environ["SUPABASE_SERVICE_KEY"]
WORKERS           = int(os.getenv("IMAGE_WORKERS", "10"))
REFRESH_DAYS      = int(os.getenv("IMAGE_REFRESH_DAYS", "90"))
SKU_LIMIT         = int(os.getenv("IMAGE_SKU_LIMIT", "5000"))
BUCKET            = "sku-images"

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Helpers ────────────────────────────────────────────────────────────────────

# Headers that mimic a real browser visiting ukpos.com — avoids 403
HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer":         "https://www.ukpos.com/",
    "Accept":          "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

def storage_path(sku_id: str, original_url: str) -> str:
    """Derive a stable storage path from SKU ID, preserving the extension."""
    ext = original_url.rsplit(".", 1)[-1].split("?")[0].lower()
    if ext not in ("jpg", "jpeg", "png", "webp", "gif"):
        ext = "jpg"
    return f"{sku_id}.{ext}"

def public_url(sb: Client, path: str) -> str:
    """Return the public CDN URL for a stored image."""
    return sb.storage.from_(BUCKET).get_public_url(path)

def needs_refresh(synced_at: Optional[str]) -> bool:
    """True if image has never been synced, or is older than REFRESH_DAYS."""
    if not synced_at:
        return True
    try:
        last = datetime.fromisoformat(synced_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - last).days >= REFRESH_DAYS
    except Exception:
        return True

# ── Core download + upload ──────────────────────────────────────────────────────

async def fetch_and_store(
    client: httpx.AsyncClient,
    sb: Client,
    sku: dict,
    sem: asyncio.Semaphore,
    stats: dict,
) -> None:
    sku_id      = sku["sku_id"]
    source_url  = sku.get("image_url", "")

    if not source_url:
        log.debug(f"  {sku_id}: no source URL — skipping")
        stats["skipped"] += 1
        return

    async with sem:
        path = storage_path(sku_id, source_url)
        try:
            # ── Download from ukpos.com ─────────────────────────────────────
            resp = await client.get(source_url, headers=HEADERS, timeout=20, follow_redirects=True)
            if resp.status_code == 404:
                log.warning(f"  {sku_id}: 404 — image not found at {source_url}")
                stats["not_found"] += 1
                return
            if resp.status_code != 200:
                log.warning(f"  {sku_id}: HTTP {resp.status_code}")
                stats["failed"] += 1
                return

            image_bytes = resp.content
            content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()

            # ── Upload to Supabase Storage ──────────────────────────────────
            # upsert=True overwrites existing file
            sb.storage.from_(BUCKET).upload(
                path=path,
                file=image_bytes,
                file_options={
                    "content-type":  content_type,
                    "cache-control": "public, max-age=7776000",  # 90 days
                    "upsert":        "true",
                },
            )

            # ── Update skus row ─────────────────────────────────────────────
            cdn_url = public_url(sb, path)
            sb.table("skus").update({
                "image_url":      cdn_url,
                "image_synced_at": datetime.now(timezone.utc).isoformat(),
            }).eq("sku_id", sku_id).execute()

            log.info(f"  ✓ {sku_id:20s}  {len(image_bytes)//1024:>4d}KB  → {path}")
            stats["succeeded"] += 1

        except httpx.TimeoutException:
            log.warning(f"  {sku_id}: timeout downloading image")
            stats["failed"] += 1
        except Exception as e:
            log.error(f"  {sku_id}: {e}")
            stats["failed"] += 1

        # Small polite delay — image CDN is separate from the main site
        await asyncio.sleep(0.2)

# ── Main ────────────────────────────────────────────────────────────────────────

async def run(force: bool = False):
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    log.info(f"Starting image sync | workers={WORKERS} refresh_days={REFRESH_DAYS} force={force}")

    # ── Fetch SKUs that need a refresh ─────────────────────────────────────────
    query = sb.table("skus").select("sku_id,image_url,image_synced_at").eq("active", True)
    if SKU_LIMIT > 0:
        query = query.limit(SKU_LIMIT)
    all_skus = query.execute().data or []

    if force:
        skus_to_process = all_skus
        log.info(f"  Force mode — processing all {len(skus_to_process)} SKUs")
    else:
        skus_to_process = [s for s in all_skus if needs_refresh(s.get("image_synced_at"))]
        already_fresh   = len(all_skus) - len(skus_to_process)
        log.info(f"  {len(all_skus)} total SKUs | {already_fresh} already fresh | {len(skus_to_process)} to process")

    if not skus_to_process:
        log.info("Nothing to do — all images are current.")
        return

    # ── Run downloads concurrently ─────────────────────────────────────────────
    stats = {"succeeded": 0, "failed": 0, "skipped": 0, "not_found": 0}
    sem   = asyncio.Semaphore(WORKERS)
    start = time.time()

    async with httpx.AsyncClient() as client:
        await asyncio.gather(*[
            fetch_and_store(client, sb, sku, sem, stats)
            for sku in skus_to_process
        ])

    elapsed = time.time() - start
    log.info(
        f"\nDone in {elapsed:.0f}s — "
        f"✓ {stats['succeeded']} succeeded  "
        f"✗ {stats['failed']} failed  "
        f"⊘ {stats['skipped']} skipped  "
        f"404 {stats['not_found']} not found"
    )


if __name__ == "__main__":
    force = "--force" in sys.argv
    asyncio.run(run(force=force))
