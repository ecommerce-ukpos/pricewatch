"""
scripts/sync_feed.py
────────────────────
Weekly refresh of UKPOS's OWN prices/stock from the ukpos.com Google product feed (Xtento):

  https://www.ukpos.com/feeds/google-product-feed-v3.xml

  UKPOS_FEED_URL   optional override of the URL above (normally not set).

How this feed differs from the old Shoptimised feed, and how each is handled:
  - g:id is "PARENT::CHILD" (e.g. "124g::124-300")  -> the CHILD part is the SKU.
  - Some ids differ from our skus.sku_id only by letter case (e.g. "TE39-988mm")
                                                     -> matched case-insensitively to the
                                                        existing sku_id, never inserted twice.
  - The same SKU can appear under two parents        -> de-duplicated (first item wins).
  - g:price / g:sale_price INCLUDE VAT               -> divided by (1 + FEED_VAT_RATE),
                                                        default 0.20, rounded to 2dp.
  - Links carry "?vat=2"                              -> stored as "?vat=0#sku:<sku>" as before.

Safety checks (any failure => nothing is written, a 'failed' row is logged in
sync_runs, and the process exits non-zero so GitHub emails the repo owner):
  - feed URL unreachable / non-200 after retries
  - body is not parseable RSS/XML
  - fewer unique SKUs than FEED_MIN_RATIO (default 85%) of the SKUs we already hold
  - fewer than 95% of items carry a parseable price
  - VAT-basis check: the median new/old list-price ratio across SKUs we already hold
    must be within 0.90-1.10. A ~1.20 or ~0.83 median means the feed switched between
    inc- and ex-VAT prices; writing that would make every comparison wrong.

price_ex_vat is the live selling price (g:sale_price when set and lower); the list price is
kept in regular_price_ex_vat with on_sale=true. Sales >50% off are listed in the run notes.
Existing SKUs only get price, availability, image, URL and last_feed_sync updated
(titles etc. are left alone). SKUs new to the feed are inserted in full.

Usage:
    python scripts/sync_feed.py [--dry-run] [--trigger scheduled|manual]
"""

import re
import argparse
import os
import statistics
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from import_feed import NS, item_to_row, parse_price  # noqa: E402

DEFAULT_FEED_URL = "https://www.ukpos.com/feeds/google-product-feed-v3.xml"

BIG_SALE = 0.5  # flag sales deeper than 50% off for human review

MIN_RATIO = float(os.environ.get("FEED_MIN_RATIO", "0.85"))
MIN_PRICED = 0.95
VAT_RATE = float(os.environ.get("FEED_VAT_RATE", "0.20"))
VAT_CHECK_LOW, VAT_CHECK_HIGH = 0.90, 1.10


class FeedError(Exception):
    pass


def own_page_url(sku_id: str, url: str) -> str:
    """Feed links are old '-doprw-...' URLs (with ?vat=2) that redirect to the parent page's
    default variant. Store the parent page, ?vat=0, plus this SKU's own #sku: fragment."""
    base = re.sub(r"-doprw-[^/?#]*", "", (url or "").split("#")[0].split("?")[0], flags=re.I)
    return f"{base}?vat=0#sku:{sku_id.lower()}"


def child_sku(raw_id: str) -> str:
    """'124g::124-300' -> '124-300'. Ids without '::' are returned unchanged."""
    return (raw_id or "").split("::")[-1].strip()


def ex_vat(price):
    return round(price / (1 + VAT_RATE), 2) if price else None


def fetch_feed(url: str) -> bytes:
    last = None
    for attempt in range(1, 4):
        try:
            r = httpx.get(url, follow_redirects=True, timeout=180,
                          headers={"User-Agent": "PriceWatch-feed-sync/2.0"})
            if r.status_code == 200 and r.content:
                return r.content
            last = f"HTTP {r.status_code} from {r.url.host} (after redirects)"
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
        time.sleep(5 * attempt)
    raise FeedError(f"Could not download feed {url} after 3 attempts — {last}. "
                    "Has the ukpos.com feed URL changed?")


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
        rows = (sb.table("skus").select("sku_id,price_ex_vat,regular_price_ex_vat,availability")
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


def build_rows(items, existing):
    """Turn feed items into skus rows keyed by OUR sku_id. Returns (rows, unusable, dupes)."""
    by_upper = {k.upper(): k for k in existing}
    rows, unusable, dupes = {}, 0, 0
    for it in items:
        r = item_to_row(it)
        feed_sku = child_sku(r["sku_id"])
        if not feed_sku or not r["product_url"]:
            unusable += 1
            continue
        sku = by_upper.get(feed_sku.upper(), feed_sku)  # keep our existing spelling/case
        if sku in rows:
            dupes += 1
            continue
        list_inc = r["price_ex_vat"]  # item_to_row reads g:price as-is; this feed is inc VAT
        if not list_inc:
            unusable += 1
            continue
        el = it.find("g:sale_price", NS)
        sale_inc = parse_price((el.text or "").strip()) if el is not None else None

        r["sku_id"] = sku
        r["product_url"] = own_page_url(sku, r["product_url"])
        r["availability"] = norm_avail(r["availability"])
        r["regular_price_ex_vat"] = ex_vat(list_inc)
        r["on_sale"] = bool(sale_inc and 0 < sale_inc < list_inc)
        r["price_ex_vat"] = ex_vat(sale_inc) if r["on_sale"] else r["regular_price_ex_vat"]
        rows[sku] = r
    return rows, unusable, dupes


def vat_basis_check(rows, existing):
    """Median ratio of new list price to stored list price for SKUs we already hold."""
    ratios = []
    for sku, r in rows.items():
        old = existing.get(sku)
        if not old:
            continue
        old_list = old.get("regular_price_ex_vat") or old.get("price_ex_vat")
        try:
            old_list = float(old_list)
        except (TypeError, ValueError):
            continue
        if old_list > 0 and r["regular_price_ex_vat"]:
            ratios.append(r["regular_price_ex_vat"] / old_list)
    if len(ratios) < 100:
        raise FeedError(f"Only {len(ratios)} feed SKUs match SKUs we hold — feed or SKU format "
                        "looks wrong. Nothing written.")
    med = statistics.median(ratios)
    if not (VAT_CHECK_LOW <= med <= VAT_CHECK_HIGH):
        raise FeedError(f"Median new/old price ratio is {med:.3f} across {len(ratios)} SKUs "
                        f"(expected ~1.00). The feed's VAT basis has probably changed "
                        f"(FEED_VAT_RATE={VAT_RATE}). Nothing written.")
    return med, len(ratios)


def run(sb, url, dry_run, trigger):
    run_id = None
    if not dry_run:
        run_id = sb.table("sync_runs").insert(
            {"trigger": trigger, "status": "running", "scrape_mode": "feed_sync"}
        ).execute().data[0]["id"]
    try:
        items = parse_items(fetch_feed(url))
        existing = fetch_existing(sb)
        rows, unusable, dupes = build_rows(items, existing)

        if len(rows) < MIN_RATIO * len(existing):
            raise FeedError(f"Feed has only {len(rows)} unique SKUs vs {len(existing)} SKUs held "
                            f"(< {MIN_RATIO:.0%}) — feed looks truncated or wrong. Nothing written.")
        if len(rows) < MIN_PRICED * (len(items) - dupes):
            raise FeedError(f"Only {len(rows)}/{len(items) - dupes} unique feed items had a usable "
                            "id+price. Nothing written.")
        med, n_ratio = vat_basis_check(rows, existing)

        now = datetime.now(timezone.utc).isoformat()
        new_rows, upd_rows = [], []
        changed_price = changed_avail = 0
        for sku, r in rows.items():
            old = existing.get(sku)
            if old is None:
                r["last_feed_sync"] = now
                new_rows.append(r)
                continue
            if old["price_ex_vat"] is None or abs(float(old["price_ex_vat"]) - r["price_ex_vat"]) > 0.004:
                changed_price += 1
            if norm_avail(old["availability"]) != r["availability"]:
                changed_avail += 1
            upd_rows.append({"sku_id": sku, "price_ex_vat": r["price_ex_vat"],
                             "regular_price_ex_vat": r["regular_price_ex_vat"], "on_sale": r["on_sale"],
                             "availability": r["availability"], "image_url": r["image_url"],
                             "product_url": r["product_url"], "last_feed_sync": now})
        missing = len(set(existing) - set(rows))
        big = sorted(f'{k} £{v["regular_price_ex_vat"]:.2f}->£{v["price_ex_vat"]:.2f}' for k, v in rows.items()
                     if v["on_sale"] and v["price_ex_vat"] < BIG_SALE * v["regular_price_ex_vat"])
        n_sale = sum(1 for v in rows.values() if v["on_sale"])

        summary = (f"ukpos.com feed: {len(items)} items, {len(rows)} unique SKUs ({dupes} duplicates); "
                   f"{changed_price} prices changed, {changed_avail} stock changes, {len(new_rows)} new SKUs, "
                   f"{missing} SKUs absent from feed, {unusable} unusable items; {n_sale} on sale; "
                   f"VAT check median {med:.3f} over {n_ratio} SKUs"
                   + (f"; >50% off, please check: {', '.join(big)}" if big else ""))
        print(summary)

        if dry_run:
            return 0
        # Existing SKUs: plain UPDATE via apply_feed_updates(). An upsert of these partial rows
        # fails, because Postgres checks NOT NULL columns (short_title…) on the insert attempt
        # before ON CONFLICT turns it into an update.
        updated = 0
        for i in range(0, len(upd_rows), 500):
            updated += sb.rpc("apply_feed_updates", {"p_rows": upd_rows[i:i + 500]}).execute().data or 0
        if updated != len(upd_rows):
            raise FeedError(f"Only {updated}/{len(upd_rows)} existing SKUs were updated.")
        # New SKUs carry every column from the feed, so a normal insert works.
        for i in range(0, len(new_rows), 100):
            sb.table("skus").upsert(new_rows[i:i + 100], on_conflict="sku_id").execute()

        # Our prices just changed, so every stored competitor gap is stale: re-derive them all
        regap = sb.rpc("recompute_gaps").execute().data
        summary += f"; {updated} SKUs updated, {len(new_rows)} added; gaps recalculated on {regap} competitor prices"
        print(summary)
        log_run(sb, run_id, "complete", len(items), len(rows), unusable, summary)
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
    url = os.environ.get("UKPOS_FEED_URL", "").strip() or DEFAULT_FEED_URL
    from supabase import create_client
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    sys.exit(run(sb, url, args.dry_run, args.trigger))


if __name__ == "__main__":
    main()
