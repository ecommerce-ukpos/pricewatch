"""
scripts/discover_dw_variants.py
────────────────────────────────
One-shot script that:

  1. Reads all competitor_matches rows for DisplayWizard (match_status = 'matched'),
     pulling the stored competitor_url (which may be a bare product page without
     ?variant=ID, causing scrape.py to get the wrong price for multi-variant products).

  2. For each unique DisplayWizard product URL, fetches the raw HTML and attempts
     to extract Shopify variant data (embedded JSON in the Gatsby page).

  3. Falls back to the Shopify /products/<handle>.json endpoint if the HTML parse
     doesn't find variant data.

  4. Matches each UKPOS SKU to the best variant using SKU suffix and title scoring.

  5. Updates competitor_matches.competitor_url to the canonical variant URL
     (with ?variant=ID) and sets match_status='amended' so scrape.py picks it
     up on the next scheduled run.

  6. Writes a local CSV report of all variants found for auditing.

Run once after adding DisplayWizard to the scraper:

    cd /workspaces/pricewatch
    export SUPABASE_URL=...
    export SUPABASE_SERVICE_KEY=...
    python scripts/discover_dw_variants.py

Optional flags:
    --dry-run       Print what would change, don't write to DB
    --force         Re-process even if competitor_url already has ?variant=
    --sku SKU_ID    Process only this SKU (repeatable)
    --csv PATH      Write variant report CSV (default: dw_variants.csv)
    --concurrency N HTTP concurrency (default: 3)
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

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))

from display_wizard import (
    parse_shopify_product_json,
    parse_html_embedded_json,
    match_variant,
    variant_url,
    all_variant_info,
    DISPLAY_WIZARD_DOMAIN,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s — %(message)s",
)
log = logging.getLogger("dw_discover")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

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


def load_dw_matches(sb, specific_skus: list[str]) -> list[dict]:
    comps = (
        sb.table("competitors")
        .select("id,domain")
        .ilike("domain", f"%{DISPLAY_WIZARD_DOMAIN}%")
        .execute()
        .data
    )
    if not comps:
        log.error(f"No competitor found with domain matching '{DISPLAY_WIZARD_DOMAIN}'")
        return []

    comp_ids = [c["id"] for c in comps]
    log.info(f"DisplayWizard competitor IDs: {comp_ids}")

    query = (
        sb.table("competitor_matches")
        .select("id,sku_id,competitor_id,competitor_url,match_status,confidence")
        .in_("competitor_id", comp_ids)
        .not_.is_("competitor_url", "null")
    )
    if specific_skus:
        query = query.in_("sku_id", specific_skus)

    rows = query.execute().data or []
    log.info(f"Loaded {len(rows)} DisplayWizard matches")
    return rows


def load_skus(sb, sku_ids: list[str]) -> dict[str, dict]:
    skus = {}
    for i in range(0, len(sku_ids), 200):
        batch = sb.table("skus").select("*").in_("sku_id", sku_ids[i:i+200]).execute().data
        for s in (batch or []):
            skus[s["sku_id"]] = s
    return skus


def already_has_variant_url(url: str) -> bool:
    return "variant=" in (url or "")


def is_dw_url(url: str) -> bool:
    return DISPLAY_WIZARD_DOMAIN in (url or "").lower()


def base_url(url: str) -> str:
    return url.split("?")[0]


def handle_from_url(url: str) -> str:
    """Extract Shopify product handle from a DisplayWizard URL."""
    path = urlparse(url).path.rstrip("/")
    return path.split("/")[-1]


async def fetch_html(
    client: httpx.AsyncClient,
    url: str,
    sem: asyncio.Semaphore,
) -> Optional[str]:
    clean = base_url(url)
    async with sem:
        try:
            resp = await client.get(clean, headers=HEADERS, timeout=30, follow_redirects=True)
            if resp.status_code == 200:
                return resp.text
            log.warning(f"  HTTP {resp.status_code} for {clean}")
            return None
        except Exception as e:
            log.warning(f"  Fetch error for {clean}: {e}")
            return None


async def fetch_shopify_json(
    client: httpx.AsyncClient,
    url: str,
    sem: asyncio.Semaphore,
) -> Optional[dict]:
    """Try the standard Shopify /products/<handle>.json endpoint."""
    handle = handle_from_url(url)
    if not handle:
        return None
    json_url = f"https://www.displaywizard.co.uk/products/{handle}.json"
    async with sem:
        try:
            resp = await client.get(json_url, headers=HEADERS, timeout=20, follow_redirects=True)
            if resp.status_code == 200:
                return resp.json()
            return None
        except Exception as e:
            log.debug(f"  Shopify JSON fetch error for {handle}: {e}")
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
    stats = {
        "total":                    len(matches),
        "skipped_non_dw":          0,
        "skipped_already_variant": 0,
        "fetched":                  0,
        "no_data":                  0,
        "matched":                  0,
        "no_match":                 0,
        "updated":                  0,
        "errors":                   0,
    }

    # Deduplicate by base URL
    url_to_rows: dict[str, list[dict]] = {}
    for row in matches:
        url = row.get("competitor_url") or ""
        if not url:
            continue
        if not is_dw_url(url):
            stats["skipped_non_dw"] += 1
            continue
        if not force and already_has_variant_url(url):
            stats["skipped_already_variant"] += 1
            log.debug(f"  Skipping {row['sku_id']} — already has ?variant= URL")
            continue
        key = base_url(url)
        if key not in url_to_rows:
            url_to_rows[key] = []
        url_to_rows[key].append(row)

    log.info(
        f"Processing {len(url_to_rows)} unique product URLs "
        f"covering {sum(len(v) for v in url_to_rows.values())} SKU matches"
    )

    sem = asyncio.Semaphore(concurrency)
    csv_rows = []

    async with httpx.AsyncClient() as client:
        for page_url, match_rows in url_to_rows.items():

            product = None

            # Strategy 1: Shopify /products/<handle>.json (fastest, most reliable)
            shopify_data = await fetch_shopify_json(client, page_url, sem)
            if shopify_data:
                product = parse_shopify_product_json(shopify_data, page_url)
                if product:
                    log.info(
                        f"  {handle_from_url(page_url)}: "
                        f"{len(product.variants)} variants via Shopify JSON"
                    )
                    stats["fetched"] += 1

            # Strategy 2: HTML parse (Gatsby embedded JSON)
            if product is None:
                html = await fetch_html(client, page_url, sem)
                if html:
                    product = parse_html_embedded_json(html, page_url)
                    if product:
                        log.info(
                            f"  {handle_from_url(page_url)}: "
                            f"{len(product.variants)} variants via HTML embedded JSON"
                        )
                        stats["fetched"] += 1
                    else:
                        log.info(f"  No variant data found at {page_url}")
                        stats["no_data"] += len(match_rows)
                        await asyncio.sleep(1.5)
                        continue
                else:
                    stats["errors"] += len(match_rows)
                    continue

            # Collect variants for CSV
            for v in all_variant_info(product):
                csv_rows.append({"page_url": page_url, **v})

            # Match each UKPOS SKU
            for row in match_rows:
                sku = skus.get(row["sku_id"])
                if not sku:
                    log.warning(f"  SKU {row['sku_id']} not found in skus table")
                    stats["errors"] += 1
                    continue

                sku_with_url = {**sku, "_existing_url": row.get("competitor_url", "")}
                vm = match_variant(product, sku_with_url)

                if vm is None:
                    log.warning(f"  No variant match for {sku['sku_id']} at {page_url}")
                    stats["no_match"] += 1
                    continue

                stats["matched"] += 1
                log.info(
                    f"  ✓ {sku['sku_id']:20s} → variant {vm.variant_id:12s} "
                    f"score={vm.score:3d} title='{vm.title}' "
                    f"£{vm.price} {'✓' if vm.available else 'OOS'}"
                )

                if dry_run:
                    log.info(f"    [DRY RUN] would update id={row['id']} url={vm.url}")
                    continue

                current_url = row.get("competitor_url", "")
                if vm.url == current_url:
                    log.debug(f"  {sku['sku_id']}: URL unchanged, skipping write")
                    continue

                update_payload = {
                    "competitor_url":  vm.url,
                    "match_status":    "amended",
                    "awaiting_scrape": True,
                    "updated_at":      datetime.now(timezone.utc).isoformat(),
                }
                try:
                    sb.table("competitor_matches").update(update_payload).eq("id", row["id"]).execute()
                    stats["updated"] += 1
                    log.info(f"    → Updated match id={row['id']}")
                except Exception as e:
                    log.error(f"    DB update failed for {sku['sku_id']}: {e}")
                    stats["errors"] += 1

            await asyncio.sleep(1.5)

    if csv_rows:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
            writer.writeheader()
            writer.writerows(csv_rows)
        log.info(f"Variant report written to {csv_path} ({len(csv_rows)} rows)")

    return stats


def main():
    parser = argparse.ArgumentParser(description="Discover DisplayWizard variant URLs")
    parser.add_argument("--dry-run",     action="store_true")
    parser.add_argument("--force",       action="store_true")
    parser.add_argument("--sku",         action="append", default=[], metavar="SKU_ID")
    parser.add_argument("--csv",         default="dw_variants.csv")
    parser.add_argument("--concurrency", type=int, default=3)
    args = parser.parse_args()

    sb = sb_client()

    matches = load_dw_matches(sb, specific_skus=args.sku)
    if not matches:
        log.info("No DisplayWizard matches found — nothing to process.")
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
    print(f"DisplayWizard variant discovery complete ({elapsed:.0f}s)")
    print(f"{'='*55}")
    for k, v in stats.items():
        print(f"  {k:35s} {v}")
    if args.dry_run:
        print("\n  [DRY RUN] No changes written.")
    else:
        print(f"\n  {stats['updated']} competitor_matches rows updated.")
        print("  Run scrape.py (scheduled mode) to pick up the new URLs.")


if __name__ == "__main__":
    main()
