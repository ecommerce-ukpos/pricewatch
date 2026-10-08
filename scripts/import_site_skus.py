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


def get(client, url):
    for a in range(3):
        try:
            r = client.get(url, headers=HDR, timeout=60, follow_redirects=True)
            if r.status_code == 200:
                return r.text
        except Exception:
            pass
        time.sleep(2 * (a + 1))
    return None


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

    with httpx.Client() as c:
        sm = get(c, f"{BASE}/sitemap.xml")
        if not sm:
            sys.exit("Could not download sitemap.xml")
        urls = [u for u in re.findall(r"<loc>([^<]+)</loc>", sm)
                if u.count("/") == 3 and u.rstrip("/") != BASE]
        if args.limit:
            urls = urls[:args.limit]
        print(f"{len(urls)} candidate pages")

        def work(u):
            h = get(c, u + "?vat=0")
            time.sleep(0.2)
            return u, (None if h is None else parse_page(u, h))

        found, failed, nonproduct = {}, 0, 0
        with ThreadPoolExecutor(max_workers=4) as ex:
            for u, rows in ex.map(work, urls):
                if rows is None:
                    failed += 1
                elif not rows:
                    nonproduct += 1
                for r in rows or []:
                    found.setdefault(r["sku"].upper(), r)
    if failed > 0.3 * len(urls):
        sys.exit(f"Too many failed pages ({failed}/{len(urls)}) — site may be blocking this runner")

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

    missing = [r for k, r in found.items() if k not in have]
    excluded = [r for r in missing if SKIP.search(r["sku"])]
    to_add = [r for r in missing if not SKIP.search(r["sku"])]
    mism = sum(1 for k, r in found.items() if k in have and abs(have[k] - r["price"]) > 0.01)
    summary = (f"site import: {len(urls)} pages ({nonproduct} non-product, {failed} failed); {len(found)} SKUs on site; "
               f"{len(found) - len(missing)} already held ({mism} with a different price - not changed); "
               f"{len(excluded)} made-to-order SKUs skipped; {len(to_add)} added: "
               + ", ".join(r["sku"] for r in to_add))
    print(summary)
    if args.dry_run:
        sb.table("sync_runs").insert({"trigger": "manual", "status": "complete", "scrape_mode": "site_import_dry",
                                      "completed_at": datetime.now(timezone.utc).isoformat(),
                                      "skus_attempted": len(found), "skus_succeeded": 0, "skus_failed": failed,
                                      "notes": ("DRY RUN - nothing added. " + summary)[:4000]}).execute()
        return

    rows = []
    for r in to_add:
        handle = r["page"].rsplit("/", 1)[-1]
        rows.append({"sku_id": r["sku"], "short_title": r["parent"][:200], "full_title": r["variant"][:500],
                     "slug": f"{handle}-doprw-{r['sku']}", "price_ex_vat": r["price"],
                     "regular_price_ex_vat": r["price"], "on_sale": False, "availability": r["avail"],
                     "product_url": f"{r['page']}?vat=0#sku:{r['sku'].lower()}", "active": True})
    for i in range(0, len(rows), 100):
        sb.table("skus").upsert(rows[i:i + 100], on_conflict="sku_id", ignore_duplicates=True).execute()
    sb.table("sync_runs").insert({"trigger": "manual", "status": "complete", "scrape_mode": "site_import",
                                  "completed_at": datetime.now(timezone.utc).isoformat(),
                                  "skus_attempted": len(found), "skus_succeeded": len(rows),
                                  "skus_failed": failed, "notes": summary[:4000]}).execute()


if __name__ == "__main__":
    main()
