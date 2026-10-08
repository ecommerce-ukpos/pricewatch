"""
scripts/import_site_skus.py
───────────────────────────
ONE-OFF: scrape ukpos.com product pages and add any variant SKUs missing from `skus`
(the Shoptimised/Google feed omits variants that aren't published to Google).

Every product page embeds a schema.org Product JSON-LD block whose `offers` array lists each
variant's sku, name, ex-VAT price (with ?vat=0) and availability — no JavaScript needed.

Skips made-to-order variants (-PRINTED, -BRANDED, -CB, -PP, -CMS...) which competitors don't sell
like-for-like. Existing SKUs are never changed (price mismatches are only counted in the run notes).

Usage: python scripts/import_site_skus.py [--dry-run] [--limit N]
"""
import argparse, json, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import httpx

BASE = "https://www.ukpos.com"
SKIP = re.compile(r"(PRINTED|BRANDED|-CB|-PP|-CMS)", re.I)
LD = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
HDR = {"User-Agent": "Mozilla/5.0 (compatible; PriceWatch-site-import/1.0)"}


ERRORS = {}


def get(client, url):
    for a in range(3):
        try:
            r = client.get(url, headers=HDR, timeout=45, follow_redirects=True)
            if r.status_code == 200:
                return r.text
            ERRORS[f"HTTP {r.status_code}"] = ERRORS.get(f"HTTP {r.status_code}", 0) + 1
        except Exception as e:
            k = type(e).__name__
            ERRORS[k] = ERRORS.get(k, 0) + 1
        time.sleep(2 * (a + 1))
    return None


def _unused_log(status, notes, attempted=0, ok=0, failed=0):
    try:
        from supabase import create_client
        sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
        sb.table("sync_runs").insert({"trigger": "manual", "status": status, "scrape_mode": "site_import",
                                      "completed_at": datetime.now(timezone.utc).isoformat(),
                                      "skus_attempted": attempted, "skus_succeeded": ok, "skus_failed": failed,
                                      "notes": notes[:4000]}).execute()
    except Exception as e:
        print("could not log:", e)


def parse_page(url, html):
    out = []
    for m in LD.finditer(html):
        try:
            j = json.loads(m.group(1))
        except Exception:
            continue
        if not isinstance(j, dict) or j.get("@type") != "Product":
            continue
        offers = j.get("offers")
        offers = offers if isinstance(offers, list) else (offers or {}).get("offers", [offers] if offers else [])
        for o in offers:
            sku, price = (o or {}).get("sku"), (o or {}).get("price")
            if not sku or price in (None, ""):
                continue
            try:
                price = round(float(price), 2)
            except ValueError:
                continue
            av = str(o.get("availability", "")).lower()
            out.append({"sku": sku.strip(), "price": price, "parent": j.get("name") or "",
                        "variant": (o.get("itemOffered") or {}).get("name") or j.get("name") or sku,
                        "avail": "out of stock" if "outofstock" in av else "in stock",
                        "page": url.split("?")[0]})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    from supabase import create_client
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    have, start = {}, 0
    while True:
        rows = sb.table("skus").select("sku_id,price_ex_vat").range(start, start + 999).execute().data or []
        for r in rows:
            have[r["sku_id"].upper()] = float(r["price_ex_vat"])
        if len(rows) < 1000:
            break
        start += 1000

    mode = "site_import_dry" if args.dry_run else "site_import"
    run_id = sb.table("sync_runs").insert({"trigger": "manual", "status": "running", "scrape_mode": mode,
                                           "notes": "starting"}).execute().data[0]["id"]

    def progress(status, notes, attempted=0, ok=0, failed=0, done=False):
        payload = {"status": status, "notes": notes[:4000], "skus_attempted": attempted,
                   "skus_succeeded": ok, "skus_failed": failed}
        if done:
            payload["completed_at"] = datetime.now(timezone.utc).isoformat()
        sb.table("sync_runs").update(payload).eq("id", run_id).execute()

    with httpx.Client() as c:
        sm = get(c, f"{BASE}/sitemap.xml")
        if not sm:
            progress("failed", f"site import: could not download sitemap.xml {ERRORS}", done=True)
            sys.exit("Could not download sitemap.xml")
        urls = [u for u in re.findall(r"<loc>([^<]+)</loc>", sm)
                if u.count("/") == 3 and u.rstrip("/") != BASE]
        if args.limit:
            urls = urls[:args.limit]
        print(f"{len(urls)} candidate pages", flush=True)

        def work(u):
            h = get(c, u + "?vat=0")
            time.sleep(0.2)
            return u, (None if h is None else parse_page(u, h))

        seen, added, skipped, buf = set(), [], [], []
        failed = nonproduct = done = mism = 0

        def flush():
            nonlocal buf
            if buf and not args.dry_run:
                sb.table("skus").upsert(buf, on_conflict="sku_id", ignore_duplicates=True).execute()
            buf = []

        with ThreadPoolExecutor(max_workers=4) as ex:
            for u, rows in ex.map(work, urls):
                done += 1
                if rows is None:
                    failed += 1
                elif not rows:
                    nonproduct += 1
                for r in rows or []:
                    k = r["sku"].upper()
                    if k in seen:
                        continue
                    seen.add(k)
                    if k in have:
                        mism += abs(have[k] - r["price"]) > 0.01
                    elif SKIP.search(r["sku"]):
                        skipped.append(r["sku"])
                    else:
                        added.append(r["sku"])
                        handle = r["page"].rsplit("/", 1)[-1]
                        buf.append({"sku_id": r["sku"], "short_title": r["parent"][:200], "full_title": r["variant"][:500],
                                    "slug": f"{handle}-doprw-{r['sku']}", "price_ex_vat": r["price"],
                                    "regular_price_ex_vat": r["price"], "on_sale": False, "availability": r["avail"],
                                    "product_url": f"{r['page']}?vat=0#sku:{r['sku'].lower()}", "active": True})
                if done % 100 == 0 or done == len(urls):
                    flush()
                    note = (f"{done}/{len(urls)} pages ({nonproduct} non-product, {failed} failed); {len(seen)} SKUs on site; "
                            f"{len(added)} {'would be ' if args.dry_run else ''}added, {len(skipped)} made-to-order skipped, "
                            f"{mism} existing with different price (not changed)")
                    print(note, flush=True)
                    progress("running", note, done, len(added), failed)
                if done >= 100 and failed > 0.5 * done:
                    msg = f"site import FAILED: {failed}/{done} pages failed, errors {ERRORS}"
                    progress("failed", msg, done, len(added), failed, done=True)
                    sys.exit(msg)
    final = (f"{'DRY RUN - ' if args.dry_run else ''}site import complete: {len(urls)} pages ({nonproduct} non-product, "
             f"{failed} failed); {len(seen)} SKUs on site; {len(added)} added, {len(skipped)} made-to-order skipped, "
             f"{mism} existing with different price (not changed). Added: " + ", ".join(added))
    print(final)
    progress("complete", final, len(urls), len(added), failed, done=True)


if __name__ == "__main__":
    main()
