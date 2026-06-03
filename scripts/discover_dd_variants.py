"""
scripts/discover_dd_variants.py
────────────────────────────────
One-shot script that:

  1. Reads all competitor_matches rows where competitor = Discount Displays
     and match_status = 'matched', pulling the stored competitor_url (which
     may be a parent/configurable page without super_attribute params, or an
     old accessory URL that returned the wrong price).

  2. For each such URL, fetches the raw HTML, parses the
     initConfigurableOptions JSON blob, and extracts every child variant.

  3. Matches each UKPOS SKU against the best child variant using
     discount_displays.match_variant().

  4. Updates competitor_matches.competitor_url to the canonical variant URL
     (with super_attribute params) and sets match_status='amended' so
     scrape.py picks it up on the next scheduled run.

  5. Also writes a local CSV report of all variants found on every parent
     page, so you can audit what's available without re-fetching.

Run this once after deploying discount_displays.py:

    cd pricewatch
    export SUPABASE_URL=...
    export SUPABASE_SERVICE_KEY=...
    python scripts/discover_dd_variants.py

Optional flags:
    --dry-run       Print what would change, don't write to Supabase
    --force         Re-process even if competitor_url already has super_attribute params
    --sku SKU_ID    Process only this SKU ID (repeatable)
    --csv PATH      Write variant report to this CSV (default: dd_variants.csv)
    --concurrency N HTTP concurrency (default: 3, be polite to their server)
"""

import argparse
import asyncio
import csv
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx

# Allow running from repo root or scripts/ directory
sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))

from discount_displays import (
    parse_configurable_blob,
    match_variant,
    variant_url,
    price_from_blob,
    all_variant_matches,
    DISCOUNT_DISPLAYS_DOMAIN,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s — %(message)s",
)
log = logging.getLogger("dd_discover")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

# Polite headers — we're scraping their site, don't be rude
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def sb_client():
    from supabase import create_client
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def load_dd_matches(sb, specific_skus: list[str]) -> list[dict]:
    """Load all Discount Displays competitor_matches rows."""
    # Get Discount Displays competitor ID
    comps = (
        sb.table("competitors")
        .select("id,domain")
        .ilike("domain", f"%{DISCOUNT_DISPLAYS_DOMAIN}%")
        .execute()
        .data
    )
    if not comps:
        log.error(f"No competitor found with domain matching '{DISCOUNT_DISPLAYS_DOMAIN}'")
        return []

    comp_ids = [c["id"] for c in comps]
    log.info(f"Discount Displays competitor IDs: {comp_ids}")

    query = (
        sb.table("competitor_matches")
        .select("id,sku_id,competitor_id,competitor_url,match_status,confidence")
        .in_("competitor_id", comp_ids)
        .not_.is_("competitor_url", "null")
    )
    if specific_skus:
        query = query.in_("sku_id", specific_skus)

    rows = query.execute().data or []
    log.info(f"Loaded {len(rows)} Discount Displays matches")
    return rows


def load_skus(sb, sku_ids: list[str]) -> dict[str, dict]:
    """Return {sku_id: sku_row} for the given IDs."""
    skus = {}
    for i in range(0, len(sku_ids), 200):
        batch = sb.table("skus").select("*").in_("sku_id", sku_ids[i:i+200]).execute().data
        for s in (batch or []):
            skus[s["sku_id"]] = s
    return skus


def already_has_variant_url(url: str) -> bool:
    """True if the URL already encodes a specific variant (has super_attribute params)."""
    return "super_attribute" in (url or "")


def is_dd_url(url: str) -> bool:
    return DISCOUNT_DISPLAYS_DOMAIN in (url or "").lower()


async def fetch_html(client: httpx.AsyncClient, url: str, sem: asyncio.Semaphore) -> Optional[str]:
    """Fetch raw HTML for a Discount Displays page."""
    # Strip variant params — we want the parent page with all variants in the blob
    base_url = url.split("?")[0]
    async with sem:
        try:
            resp = await client.get(base_url, headers=HEADERS, timeout=30, follow_redirects=True)
            if resp.status_code == 200:
                return resp.text
            else:
                log.warning(f"  HTTP {resp.status_code} for {base_url}")
                return None
        except Exception as e:
            log.warning(f"  Fetch error for {base_url}: {e}")
            return None


async def process_matches(
    matches: list[dict],
    skus: dict[str, dict],
    dry_run: bool,
    force: bool,
    csv_path: str,
    concurrency: int,
    sb,
) -> dict:
    """Main async processing loop."""
    stats = {
        "total": len(matches),
        "skipped_non_dd": 0,
        "skipped_already_variant": 0,
        "fetched": 0,
        "no_blob": 0,
        "matched": 0,
        "no_match": 0,
        "updated": 0,
        "errors": 0,
    }

    # Deduplicate: multiple SKUs may share the same parent page URL
    # Process each unique parent URL once, then apply to all matching SKUs
    url_to_skus: dict[str, list[dict]] = {}
    for row in matches:
        url = (row.get("competitor_url") or "").split("?")[0]  # strip existing params
        if not url:
            continue
        if not is_dd_url(url):
            stats["skipped_non_dd"] += 1
            continue
        if not force and already_has_variant_url(row.get("competitor_url", "")):
            stats["skipped_already_variant"] += 1
            log.debug(f"  Skipping {row['sku_id']} — already has variant URL")
            continue
        if url not in url_to_skus:
            url_to_skus[url] = []
        url_to_skus[url].append(row)

    log.info(
        f"Processing {len(url_to_skus)} unique parent URLs "
        f"covering {sum(len(v) for v in url_to_skus.values())} SKU matches"
    )

    sem = asyncio.Semaphore(concurrency)
    csv_rows = []

    async with httpx.AsyncClient() as client:
        # Fetch all unique parent pages concurrently (within semaphore limit)
        fetch_tasks = {
            url: asyncio.create_task(fetch_html(client, url, sem))
            for url in url_to_skus
        }
        # Small delay between fetches to be polite
        await asyncio.sleep(0.5)

        for base_url, task in fetch_tasks.items():
            html = await task
            match_rows = url_to_skus[base_url]

            if html is None:
                stats["errors"] += len(match_rows)
                continue

            stats["fetched"] += 1

            # Parse the blob once per parent page
            cp = parse_configurable_blob(html, base_url=base_url)
            if cp is None:
                log.info(f"  No configurable blob at {base_url} — may be a simple product")
                stats["no_blob"] += len(match_rows)
                continue

            # Collect all variants for the CSV report
            all_variants = all_variant_matches(cp)
            log.info(
                f"  {base_url.split('/')[-1]}: "
                f"{len(all_variants)} variants, "
                f"{len(match_rows)} UKPOS SKUs to match"
            )
            for v in all_variants:
                csv_rows.append({
                    "parent_url": base_url,
                    "child_id": v["child_id"],
                    "labels": str(v["labels"]),
                    "variant_url": v["url"],
                    "price_ex_vat": v["price_ex_vat"],
                    "price_inc_vat": v["price_inc_vat"],
                    "in_stock": v["in_stock"],
                    "lead_time": v["lead_time"],
                })

            # Match each UKPOS SKU to the best child variant
            for row in match_rows:
                sku = skus.get(row["sku_id"])
                if not sku:
                    log.warning(f"  SKU {row['sku_id']} not found in skus table")
                    stats["errors"] += 1
                    continue

                vm = match_variant(cp, sku)
                if vm is None:
                    log.warning(f"  No variant match for {sku['sku_id']} on {base_url}")
                    stats["no_match"] += 1
                    continue

                stats["matched"] += 1
                log.info(
                    f"  ✓ {sku['sku_id']:20s} → child {vm.child_id:8s} "
                    f"score={vm.score:3d} labels={vm.labels} "
                    f"£{vm.price_ex_vat} {'✓' if vm.in_stock else 'OOS'}"
                )

                if dry_run:
                    log.info(f"    [DRY RUN] would update competitor_matches id={row['id']} "
                             f"url={vm.url}")
                    continue

                # Update competitor_matches: set specific variant URL + amended status
                # so scrape.py picks it up on the next run
                update_payload = {
                    "competitor_url": vm.url,
                    "match_status":   "amended",      # triggers rescrape
                    "awaiting_scrape": True,
                    "updated_at":     datetime.now(timezone.utc).isoformat(),
                }
                # Only promote if the new URL is actually different
                current_url = row.get("competitor_url", "")
                if vm.url == current_url:
                    log.debug(f"  {sku['sku_id']}: URL unchanged, skipping write")
                    continue

                try:
                    sb.table("competitor_matches").update(update_payload).eq("id", row["id"]).execute()
                    stats["updated"] += 1
                    log.info(f"    → Updated match id={row['id']}")
                except Exception as e:
                    log.error(f"    DB update failed for {sku['sku_id']}: {e}")
                    stats["errors"] += 1

            # Polite delay between parent pages
            await asyncio.sleep(1.5)

    # Write CSV report
    if csv_rows:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
            writer.writeheader()
            writer.writerows(csv_rows)
        log.info(f"Variant report written to {csv_path} ({len(csv_rows)} rows)")

    return stats


def main():
    parser = argparse.ArgumentParser(description="Discover Discount Displays variant URLs")
    parser.add_argument("--dry-run",     action="store_true", help="Don't write to DB")
    parser.add_argument("--force",       action="store_true", help="Re-process already-matched URLs")
    parser.add_argument("--sku",         action="append", default=[], metavar="SKU_ID", help="Limit to specific SKUs")
    parser.add_argument("--csv",         default="dd_variants.csv", help="CSV output path")
    parser.add_argument("--concurrency", type=int, default=3, help="Parallel HTTP requests")
    args = parser.parse_args()

    sb = sb_client()

    matches = load_dd_matches(sb, specific_skus=args.sku)
    if not matches:
        log.info("No Discount Displays matches found — nothing to process.")
        return

    sku_ids = list({r["sku_id"] for r in matches})
    skus    = load_skus(sb, sku_ids)
    log.info(f"Loaded {len(skus)} SKU records")

    if args.dry_run:
        log.info("DRY RUN — no changes will be written to Supabase")

    start = time.time()
    stats = asyncio.run(
        process_matches(
            matches=matches,
            skus=skus,
            dry_run=args.dry_run,
            force=args.force,
            csv_path=args.csv,
            concurrency=args.concurrency,
            sb=sb,
        )
    )
    elapsed = time.time() - start

    print(f"\n{'='*55}")
    print(f"Discount Displays variant discovery complete ({elapsed:.0f}s)")
    print(f"{'='*55}")
    for k, v in stats.items():
        print(f"  {k:30s} {v}")
    if args.dry_run:
        print("\n  [DRY RUN] No changes written.")
    else:
        print(f"\n  {stats['updated']} competitor_matches rows updated.")
        print("  Run scrape.py (scheduled mode) to pick up the new URLs.")


if __name__ == "__main__":
    main()
