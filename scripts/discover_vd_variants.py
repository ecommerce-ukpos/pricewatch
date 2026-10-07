"""
scripts/discover_vd_variants.py
───────────────────────────────
One-shot deep discovery for Visual Displays (Shopify).

Problem: a multi-size Visual Displays product has ONE page URL that shows the
default (first) variant. Matches stored against the bare URL therefore get the
default variant's price, not the price of OUR child SKU.

For every Visual Displays competitor_matches row this script:

  1. Fetches <product-url>.js  (all variants, with their SKUs, prices in pence)
  2. Finds the variant whose sku == our sku_id
  3. Rewrites competitor_url to <product-url>?variant=<variant_id>
  4. match_status 'matched' → 'amended' + awaiting_scrape=True (so scrape.py
     re-prices it); 'review' rows get the URL fixed but STAY in review so
     human confirmation in Match Manager is not bypassed.

Multi-variant products with no variant carrying our SKU are reported and left
untouched (never guess).

Run (Codespaces — env vars do not persist between sessions):

    cd /workspaces/pricewatch
    export SUPABASE_URL=...
    export SUPABASE_SERVICE_KEY=...
    python scripts/discover_vd_variants.py --dry-run
    python scripts/discover_vd_variants.py

Optional flags:
    --dry-run        Report only, no DB writes
    --force          Re-process rows that already have ?variant=
    --sku SKU_ID     Only this SKU (repeatable)
    --csv PATH       Variant audit CSV (default: vd_variants.csv)
    --concurrency N  Parallel fetches (default 3)
"""

import argparse
import asyncio
import csv
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))

from visual_displays import (
    VISUAL_DISPLAYS_DOMAIN,
    all_variant_info,
    js_url,
    match_vd_variant,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
log = logging.getLogger("vd_discover")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/javascript,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}


def base_url(url: str) -> str:
    return (url or "").split("?")[0].split("#")[0].rstrip("/")


def load_matches(sb, only_skus):
    comps = (
        sb.table("competitors").select("id,domain")
        .ilike("domain", f"%{VISUAL_DISPLAYS_DOMAIN}%").execute().data
    )
    if not comps:
        log.error(f"No competitor with domain like '{VISUAL_DISPLAYS_DOMAIN}'")
        return []
    ids = [c["id"] for c in comps]
    log.info(f"Visual Displays competitor ids: {ids}")

    q = (
        sb.table("competitor_matches")
        .select("id,sku_id,competitor_id,competitor_url,match_status")
        .in_("competitor_id", ids)
        .in_("match_status", ["matched", "amended", "review"])
        .not_.is_("competitor_url", "null")
    )
    if only_skus:
        q = q.in_("sku_id", only_skus)
    rows = q.execute().data or []
    log.info(f"Loaded {len(rows)} Visual Displays matches")
    return rows


async def fetch_product_js(client, url, sem):
    async with sem:
        try:
            r = await client.get(js_url(url), headers=HEADERS, timeout=20, follow_redirects=True)
            if r.status_code == 200:
                return r.json()
            log.warning(f"  HTTP {r.status_code} for {js_url(url)}")
        except Exception as e:
            log.warning(f"  Fetch error for {js_url(url)}: {e}")
        await asyncio.sleep(1.0)
        return None


async def run(rows, dry_run, force, csv_path, concurrency, sb):
    stats = dict.fromkeys(
        ["rows", "products", "fetch_failed", "matched", "no_sku_match",
         "already_correct", "updated", "skipped_has_variant", "errors"], 0)
    stats["rows"] = len(rows)

    by_page = {}
    for r in rows:
        if "/products/" not in (r["competitor_url"] or ""):
            continue
        if not force and "variant=" in r["competitor_url"]:
            stats["skipped_has_variant"] += 1
            continue
        by_page.setdefault(base_url(r["competitor_url"]), []).append(r)
    stats["products"] = len(by_page)
    log.info(f"{len(by_page)} unique product pages to inspect")

    sem = asyncio.Semaphore(concurrency)
    csv_rows, no_match = [], []

    async with httpx.AsyncClient() as client:
        for page_url, page_rows in by_page.items():
            product = await fetch_product_js(client, page_url, sem)
            if not product:
                stats["fetch_failed"] += len(page_rows)
                continue

            for v in all_variant_info(product, page_url):
                csv_rows.append({"page_url": page_url, **v})

            for row in page_rows:
                vm = match_vd_variant(product, row["sku_id"], page_url)
                if vm is None:
                    stats["no_sku_match"] += 1
                    no_match.append((row["sku_id"], page_url))
                    continue

                stats["matched"] += 1
                log.info(
                    f"  ✓ {row['sku_id']:18s} variant={vm.variant_id} "
                    f"'{vm.title}' £{vm.price:.2f} ({vm.reason})"
                )

                if vm.url == row["competitor_url"]:
                    stats["already_correct"] += 1
                    continue
                if dry_run:
                    log.info(f"    [DRY RUN] id={row['id']} → {vm.url}")
                    continue

                payload = {
                    "competitor_url": vm.url,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                if row["match_status"] in ("matched", "amended"):
                    payload["match_status"] = "amended"
                    payload["awaiting_scrape"] = True
                try:
                    sb.table("competitor_matches").update(payload).eq("id", row["id"]).execute()
                    stats["updated"] += 1
                except Exception as e:
                    log.error(f"    DB update failed for {row['sku_id']}: {e}")
                    stats["errors"] += 1

            await asyncio.sleep(1.0)

    if csv_rows:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
            w.writeheader()
            w.writerows(csv_rows)
        log.info(f"Variant audit written to {csv_path} ({len(csv_rows)} rows)")

    if no_match:
        log.warning("Multi-variant pages with NO variant carrying our SKU (left untouched):")
        for sku_id, page in no_match:
            log.warning(f"    {sku_id}  {page}")

    return stats


def main():
    ap = argparse.ArgumentParser(description="Deep variant discovery for Visual Displays")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--sku", action="append", default=[], metavar="SKU_ID")
    ap.add_argument("--csv", default="vd_variants.csv")
    ap.add_argument("--concurrency", type=int, default=3)
    args = ap.parse_args()

    from supabase import create_client
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

    rows = load_matches(sb, args.sku)
    if not rows:
        return
    if args.dry_run:
        log.info("DRY RUN — nothing will be written")

    stats = asyncio.run(run(rows, args.dry_run, args.force, args.csv, args.concurrency, sb))

    print("\n" + "=" * 52)
    print("Visual Displays variant discovery complete")
    print("=" * 52)
    for k, v in stats.items():
        print(f"  {k:24s} {v}")
    if not args.dry_run:
        print("\n  Run scrape.py (or 'Send all for rescrape') to re-price the amended rows.")


if __name__ == "__main__":
    main()
