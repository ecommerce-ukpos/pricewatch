"""
scripts/sync_feed.py
────────────────────
Weekly refresh of UKPOS's OWN prices/stock from the Shoptimised Google Shopping feed.

  UKPOS_FEED_URL   the Shoptimised feed URL (GitHub secret — it contains a token).
                   It 302-redirects to a short-lived signed download link, so the
                   redirect is followed on every run; the resolved link is never stored.

Safety checks (any failure => nothing is written, a 'failed' row is logged in
sync_runs, and the process exits non-zero so GitHub emails the repo owner):
  - feed URL unreachable / non-200 after retries (e.g. Shoptimised changed the URL)
  - body is not parseable RSS/XML
  - fewer items than FEED_MIN_RATIO (default 85%) of the SKUs we already hold
  - fewer than 95% of items carry a parseable price

price_ex_vat is the live selling price (g:sale_price when set); the list price is kept in
regular_price_ex_vat with on_sale=true. Sales >50% off are listed in the run notes.
Existing SKUs only get price, availability, image, URL and last_feed_sync updated
(titles etc. are left alone). SKUs new to the feed are inserted in full.

Usage:
    python scripts/sync_feed.py [--dry-run] [--trigger scheduled|manual]
"""

import re
import argparse
import os
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from import_feed import NS, item_to_row, parse_price  # noqa: E402

BIG_SALE = 0.5  # flag sales deeper than 50% off for human review

MIN_RATIO = float(os.environ.get("FEED_MIN_RATIO", "0.85"))
MIN_PRICED = 0.95


class FeedError(Exception):
    pass



def own_page_url(sku_id: str, url: str) -> str:
    """Feed links are old '-doprw-...' URLs that redirect to the parent page's default variant.
    Store the parent page plus this SKU's own #sku: fragment instead."""
    base = re.sub(r"-doprw-[^/?#]*", "", (url or "").split("#")[0], flags=re.I)
    if "?" not in base:
        base += "?vat=0"
    return f"{base}#sku:{sku_id.lower()}"

def fetch_feed(url: str) -> bytes:
    last = None
    for attempt in range(1, 4):
        try:
            r = httpx.get(url, follow_redirects=True, timeout=120,
                          headers={"User-Agent": "PriceWatch-feed-sync/1.0"})
            if r.status_code == 200 and r.content:
                return r.content
            last = f"HTTP {r.status_code} from {r.url.host} (after redirects)"
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
        time.sleep(5 * attempt)
    raise FeedError(f"Could not download feed after 3 attempts — {last}. "
                    "Has Shoptimised changed the feed URL? Update the UKPOS_FEED_URL secret.")


def parse_items(body: bytes):
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise FeedError(f"Feed is not valid XML ({e}). First bytes: {body[:120]!r}") from e
    items = root.findall(".//item")
    if not items:
        raise FeedError("Feed parsed but contains no <item> elements.")
    return items


def norm_avail(a: str) -> str:
    return (a or "in stock").replace("_", " ").strip().lower()


def fetch_existing(sb):
    out, start = {}, 0
    while True:
        rows = (sb.table("skus").select("sku_id,price_ex_vat,availability")
                .range(start, start + 999).execute().data or [])
        for r in rows:
            out[r["sku_id"]] = r
        if len(rows) < 1000:
            return out
        start += 1000


def log_run(sb, run_id, status, attempted, ok, failed, notes, errors=None):
    payload = {
        "status": status, "completed_at": datetime.now(timezone.utc).isoformat(),
        "skus_attempted": attempted, "skus_succeeded": ok, "skus_failed": failed,
        "notes": notes, "error_log": errors,
    }
    if run_id:
        sb.table("sync_runs").update(payload).eq("id", run_id).execute()


def run(sb, url, dry_run, trigger):
    run_id = None
    if not dry_run:
        run_id = sb.table("sync_runs").insert(
            {"trigger": trigger, "status": "running", "scrape_mode": "feed_sync"}
        ).execute().data[0]["id"]
    try:
        items = parse_items(fetch_feed(url))
        existing = fetch_existing(sb)

        if len(items) < MIN_RATIO * len(existing):
            raise FeedError(f"Feed has only {len(items)} items vs {len(existing)} SKUs held "
                            f"(< {MIN_RATIO:.0%}) — feed looks truncated or wrong. Nothing written.")

        rows, unpriced = {}, 0
        for it in items:
            r = item_to_row(it)
            if not r["sku_id"] or not r["product_url"]:
                unpriced += 1
                continue
            if not r["price_ex_vat"]:
                unpriced += 1
                continue
            r["product_url"] = own_page_url(r["sku_id"], r["product_url"])
            r["availability"] = norm_avail(r["availability"])
            # Live selling price = g:sale_price when present and lower, else g:price.
            el = it.find("g:sale_price", NS)
            sale = parse_price((el.text or "").strip()) if el is not None else None
            r["regular_price_ex_vat"] = r["price_ex_vat"]
            r["on_sale"] = bool(sale and 0 < sale < r["price_ex_vat"])
            if r["on_sale"]:
                r["price_ex_vat"] = sale
            rows[r["sku_id"]] = r
        if len(rows) < MIN_PRICED * len(items):
            raise FeedError(f"Only {len(rows)}/{len(items)} feed items had a usable id+price. Nothing written.")

        now = datetime.now(timezone.utc).isoformat()
        new_rows, upd_rows = [], []
        changed_price = changed_avail = 0
        for sku, r in rows.items():
            old = existing.get(sku)
            if old is None:
                new_rows.append(r)
                continue
            if old["price_ex_vat"] is None or abs(float(old["price_ex_vat"]) - r["price_ex_vat"]) > 0.004:
                changed_price += 1
            if norm_avail(old["availability"]) != r["availability"]:
                changed_avail += 1
            upd_rows.append({"sku_id": sku, "price_ex_vat": r["price_ex_vat"],
                             "regular_price_ex_vat": r["regular_price_ex_vat"], "on_sale": r["on_sale"],
                             "availability": r["availability"], "image_url": r["image_url"],
                             "product_url": own_page_url(sku, r["product_url"]), "last_feed_sync": now})
        missing = len(set(existing) - set(rows))
        big = sorted(f'{k} £{v["regular_price_ex_vat"]:.2f}->£{v["price_ex_vat"]:.2f}' for k, v in rows.items()
                     if v["on_sale"] and v["price_ex_vat"] < BIG_SALE * v["regular_price_ex_vat"])
        n_sale = sum(1 for v in rows.values() if v["on_sale"])

        summary = (f"{len(items)} feed items; {changed_price} prices changed, {changed_avail} stock "
                   f"changes, {len(new_rows)} new SKUs, {missing} SKUs absent from feed, {unpriced} unusable items; "
                   f"{n_sale} on sale" + (f"; >50% off, please check: {', '.join(big)}" if big else ""))
        print(summary)

        if dry_run:
            return 0
        for batch in (upd_rows, new_rows):
            for i in range(0, len(batch), 100):
                sb.table("skus").upsert(batch[i:i + 100], on_conflict="sku_id").execute()

        # Our prices just changed, so every stored competitor gap is stale: re-derive them all
        regap = sb.rpc("recompute_gaps").execute().data
        summary += f"; gaps recalculated on {regap} competitor prices"
        print(summary)
        log_run(sb, run_id, "complete", len(items), len(rows), unpriced, summary)
        return 0
    except Exception as e:  # noqa: BLE001
        msg = str(e) if isinstance(e, FeedError) else f"{type(e).__name__}: {e}"
        print(f"FEED SYNC FAILED: {msg}", file=sys.stderr)
        if not isinstance(e, FeedError):
            traceback.print_exc()
        try:
            log_run(sb, run_id, "failed", 0, 0, 0, f"FEED SYNC FAILED: {msg}", {"error": msg})
        except Exception:  # noqa: BLE001
            pass
        return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--trigger", default="scheduled", choices=["scheduled", "manual"])
    args = ap.parse_args()
    url = os.environ.get("UKPOS_FEED_URL", "").strip()
    if not url:
        print("UKPOS_FEED_URL is not set", file=sys.stderr)
        sys.exit(1)
    from supabase import create_client
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    sys.exit(run(sb, url, args.dry_run, args.trigger))


if __name__ == "__main__":
    main()
