"""
scripts/discover_alplas_variants.py
────────────────────────────────────
One-shot script that:

  1. Reads all competitor_matches rows where competitor = Alplas and
     match_status = 'matched', pulling the stored competitor_url (which
     currently uses ?attribute_dimension=... params the server ignores,
     causing scrape.py to get the wrong price).

  2. For each unique Alplas parent page URL, fetches the raw HTML and
     parses the WooCommerce variation blob to extract every variation ID,
     dimension label, and ex-VAT price.

  3. Matches each UKPOS SKU to the best variation using dimension and
     orientation tokens from the UKPOS short_title.

  4. Updates competitor_matches.competitor_url to the canonical variation URL
     (with ?variation_id=XXXX&attribute_dimension=...) and sets
     match_status='amended' so scrape.py picks it up on the next run.

  5. Writes a local CSV report of all variations found on each parent page
     for auditing.

Run once after deploying alplas.py:

    cd pricewatch
    export SUPABASE_URL=...
    export SUPABASE_SERVICE_KEY=...
    python scripts/discover_alplas_variants.py

Optional flags:
    --dry-run       Print what would change, don't write to Supabase
    --force         Re-process even if competitor_url already has variation_id
    --sku SKU_ID    Process only this SKU ID (repeatable)
    --csv PATH      Write variant report to this CSV (default: alplas_variants.csv)
    --concurrency N HTTP concurrency (default: 2)
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

from alplas import (
    parse_variation_blob,
    match_variation,
    variation_url,
    price_from_variation,
    all_variation_matches,
    scrape_alplas_page,
    ALPLAS_DOMAIN,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s — %(message)s",
)
log = logging.getLogger("alplas_discover")

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


def load_alplas_matches(sb, specific_skus: list[str]) -> list[dict]:
    """Load all Alplas competitor_matches rows."""
    comps = (
        sb.table("competitors")
        .select("id,domain")
        .ilike("domain", f"%{ALPLAS_DOMAIN}%")
        .execute()
        .data
    )
    if not comps:
        log.error(f"No competitor found with domain matching '{ALPLAS_DOMAIN}'")
        return []

    comp_ids = [c["id"] for c in comps]
    log.info(f"Alplas competitor IDs: {comp_ids}")

    query = (
        sb.table("competitor_matches")
        .select("id,sku_id,competitor_id,competitor_url,match_status,confidence")
        .in_("competitor_id", comp_ids)
        .not_.is_("competitor_url", "null")
    )
    if specific_skus:
        query = query.in_("sku_id", specific_skus)

    rows = query.execute().data or []
    log.info(f"Loaded {len(rows)} Alplas matches")
    return rows


def load_skus(sb, sku_ids: list[str]) -> dict[str, dict]:
    """Return {sku_id: sku_row} for the given IDs."""
    skus = {}
    for i in range(0, len(sku_ids), 200):
        batch = sb.table("skus").select("*").in_("sku_id", sku_ids[i:i+200]).execute().data
        for s in (batch or []):
            skus[s["sku_id"]] = s
    return skus


def already_has_variation_id(url: str) -> bool:
    """True if the URL already encodes a specific variation_id."""
    return "variation_id=" in (url or "")


def is_alplas_url(url: str) -> bool:
    return ALPLAS_DOMAIN in (url or "").lower()


def parent_url(url: str) -> str:
    """Strip all params to get the canonical parent product URL."""
    return url.split("?")[0]


async def fetch_html(
    client: httpx.AsyncClient,
    url: str,
    sem: asyncio.Semaphore,
) -> Optional[str]:
    """Fetch raw HTML for an Alplas product page (no params — full variation blob)."""
    base = parent_url(url)
    async with sem:
        try:
            resp = await client.get(base, headers=HEADERS, timeout=30, follow_redirects=True)
            if resp.status_code == 200:
                return resp.text
            else:
                log.warning(f"  HTTP {resp.status_code} for {base}")
                return None
        except Exception as e:
            log.warning(f"  Fetch error for {base}: {e}")
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
        "total": len(matches),
        "skipped_non_alplas": 0,
        "skipped_already_variation": 0,
        "fetched": 0,
        "no_blob": 0,
        "matched": 0,
        "no_match": 0,
        "updated": 0,
        "errors": 0,
    }

    # Deduplicate: many SKUs may share the same parent page URL
    url_to_rows: dict[str, list[dict]] = {}
    for row in matches:
        url = row.get("competitor_url") or ""
        if not url:
            continue
        if not is_alplas_url(url):
            stats["skipped_non_alplas"] += 1
            continue
        if not force and already_has_variation_id(url):
            stats["skipped_already_variation"] += 1
            log.debug(f"  Skipping {row['sku_id']} — already has variation_id URL")
            continue
        base = parent_url(url)
        if base not in url_to_rows:
            url_to_rows[base] = []
        url_to_rows[base].append(row)

    log.info(
        f"Processing {len(url_to_rows)} unique parent URLs "
        f"covering {sum(len(v) for v in url_to_rows.values())} SKU matches"
    )

    sem = asyncio.Semaphore(concurrency)
    csv_rows = []

    async with httpx.AsyncClient() as client:
        fetch_tasks = {
            base_url: asyncio.create_task(fetch_html(client, base_url, sem))
            for base_url in url_to_rows
        }

        for base_url, task in fetch_tasks.items():
            html = await task
            match_rows = url_to_rows[base_url]

            if html is None:
                stats["errors"] += len(match_rows)
                continue

            stats["fetched"] += 1

            # Parse blob once per parent URL
            ap = parse_variation_blob(html, base_url=base_url)
            if ap is None:
                log.info(f"  No variation blob at {base_url} — may be a simple product")
                stats["no_blob"] += len(match_rows)
                continue

            # Collect all variations for CSV
            all_vars = all_variation_matches(ap)
            log.info(
                f"  {base_url.split('/')[-2] or base_url.split('/')[-1]}: "
                f"{len(all_vars)} variations, "
                f"{len(match_rows)} UKPOS SKUs to match"
            )
            for v in all_vars:
                csv_rows.append({"parent_url": base_url, **v})

            # Match each UKPOS SKU to its best variation
            for row in match_rows:
                sku = skus.get(row["sku_id"])
                if not sku:
                    log.warning(f"  SKU {row['sku_id']} not found in skus table")
                    stats["errors"] += 1
                    continue

                # Pass the existing URL so exact attribute_dimension match is tried first
                sku_with_url = {**sku, "_existing_url": row.get("competitor_url", "")}
                vm = match_variation(ap, sku_with_url)

                if vm is None:
                    log.warning(f"  No variation match for {sku['sku_id']} on {base_url}")
                    stats["no_match"] += 1
                    continue

                stats["matched"] += 1
                log.info(
                    f"  ✓ {sku['sku_id']:20s} → variation {vm.variation_id:8s} "
                    f"score={vm.score:3d} dim='{vm.attribute_dimension}' "
                    f"£{vm.price_ex_vat} ex VAT {'✓' if vm.in_stock else 'OOS'}"
                )

                if dry_run:
                    log.info(
                        f"    [DRY RUN] would update id={row['id']} "
                        f"url={vm.url}"
                    )
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

            # Polite delay between parent pages
            await asyncio.sleep(2.0)

    # Write CSV
    if csv_rows:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
            writer.writeheader()
            writer.writerows(csv_rows)
        log.info(f"Variant report written to {csv_path} ({len(csv_rows)} rows)")

    return stats


def main():
    parser = argparse.ArgumentParser(description="Discover Alplas variation URLs")
    parser.add_argument("--dry-run",     action="store_true", help="Don't write to DB")
    parser.add_argument("--force",       action="store_true", help="Re-process already-fixed URLs")
    parser.add_argument("--sku",         action="append", default=[], metavar="SKU_ID")
    parser.add_argument("--csv",         default="alplas_variants.csv")
    parser.add_argument("--concurrency", type=int, default=2)
    args = parser.parse_args()

    sb = sb_client()

    matches = load_alplas_matches(sb, specific_skus=args.sku)
    if not matches:
        log.info("No Alplas matches found — nothing to process.")
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
    print(f"Alplas variation discovery complete ({elapsed:.0f}s)")
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
