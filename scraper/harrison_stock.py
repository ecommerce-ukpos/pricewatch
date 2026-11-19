"""
scraper/harrison_stock.py
─────────────────────────
Harrison Products (competitor id 10, harrisonproducts.com — BigCommerce) stock scrape.

Run by the "harrison_stock" job in .github/workflows/scraper.yml, started from the
"Check Competitor Stock" page in the app (via the smart-function Edge Function).

For every Harrison product URL it records: product name, Harrison SKU, price (ex and
inc VAT), stock level and stock status. Results go to public.harrison_stock (one row
per product per run); each run gets a row in public.harrison_stock_runs.

Mode (env HARRISON_MODE) = <check>[_catalogue]:
  check  oos    "Check OOS items"   — reads every URL in scope (to find what is out of stock).
         levels "Run stock levels"  — reads only URLs whose latest reading is in stock,
                                      plus URLs never checked before.
  scope  (none)     every distinct Harrison URL in competitor_matches
                    (matched + review + rejected).
         _catalogue every product URL in Harrison's BigCommerce product sitemap
                    (/xmlsitemap.php → type=products pages). Parent product pages only:
                    products with size/colour options report the page's default variant.
Each row also stores ukpos_skus: the UKPOS SKUs confirmed-matched to that URL.

Where the data comes from (BigCommerce Stencil):
  window.BCData.product_attributes → sku, stock, instock, purchasable,
                                     price.without_tax / price.with_tax
  /remote/v1/product-attributes/<id> responses (fired when the theme selects a
                                     variant) → same fields for that variant
  [data-product-stock] element      → stock number, if shown on the page
  stock_level stays NULL when Harrison does not publish a number.

Accuracy rule: for ?sku=<code> variant URLs, figures are only kept when the
returned SKU matches <code>. Otherwise the row is written with stock_status='error'
and no price/stock, rather than storing the default variant's figures.

Products with sizes/colours (kind):
  Harrison's option pickers are driven by a page builder (Zeno), so options can't be
  clicked. Instead the variant codes are read from the page (spec table "Product Code"
  row + ProductGroup JSON-LD) and each variant is opened as <parent>/?sku=<code>, which
  Harrison renders with that variant's own price and stock (verified by probe).
    single         one product, one code                 → its own row
    variant        a size/colour opened via ?sku=<code>   → its own row
    variant_parent the parent page of a product with options → written with
                   stock_status 'unknown' (its "from" price is not a real price) and
                   expanded into variant rows
    options_unsplit options found but no variant codes   → 'unknown', not listed
    cut_to_order   no basket (e.g. "Cut to order" strips) → 'unknown', not listed

Daily schedule (.github/workflows/harrison_daily.yml) — "careful" settings, all env:
  HARRISON_TRIGGER  'daily' labels the run (harrison_stock_runs.triggered_by) and skips the
                    run if a daily run already started today (UK date).
  HARRISON_SHUFFLE  '1' = read URLs in random order, not alphabetically.
  HARRISON_STOP_AT  'HH:MM' UK time — stop taking new URLs at this time; the run is still
                    saved as done, with a note of how many URLs were not read.
  Block guard: after BLOCK_STREAK errors in a row the run pauses 3–6 minutes; if the next
  BLOCK_STREAK reads also all fail, it stops and is marked failed ("may be blocking us").
Times are stored to the minute (a database trigger also truncates them).

Next stock due (from the "Stock & Lead Times" block, lines like "2,000  Due in 19/11/2026"):
  due_in        all listed deliveries [{qty, date}]
  next_due_date / next_due_qty  earliest delivery dated today or later
  due_scope     'item'  = the lines belong to this product
                'range' = product has sizes/colours; Harrison shows the same delivery list on
                          every size, so the size each delivery is for is not stated
"""

import asyncio
import logging
import os
import random
import re
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional
from urllib.parse import urlparse, parse_qs

from playwright.async_api import async_playwright, BrowserContext
from supabase import create_client, Client

from common import detect_oos, launch_browser, new_stealth_context

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("harrison_stock")

HARRISON_ID   = 10
HARRISON_BASE = "https://www.harrisonproducts.com"
TIMEOUT_MS    = 45_000
WORKERS       = int(os.environ.get("SCRAPER_WORKERS") or "3")
DELAY_MIN     = float(os.environ.get("SCRAPER_DELAY_MIN") or "2")
DELAY_MAX     = float(os.environ.get("SCRAPER_DELAY_MAX") or "5")
MODE          = (os.environ.get("HARRISON_MODE") or "oos").strip().lower()
CHECK         = MODE.split("_")[0]
SCOPE         = "catalogue" if MODE.endswith("_catalogue") else "matches"
BATCH         = 100
TRIGGER       = (os.environ.get("HARRISON_TRIGGER") or "manual").strip().lower()
SHUFFLE       = (os.environ.get("HARRISON_SHUFFLE") or "") in ("1", "true", "yes")
STOP_AT       = (os.environ.get("HARRISON_STOP_AT") or "").strip()     # 'HH:MM' UK time
BLOCK_STREAK  = 8
UK            = ZoneInfo("Europe/London")


def now_min() -> str:
    """UTC now, to the minute: '2026-11-18 17:30+00'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M+00")


def past_stop_time() -> bool:
    if not STOP_AT:
        return False
    hh, mm = (int(x) for x in STOP_AT.split(":"))
    n = datetime.now(UK)
    return (n.hour, n.minute) >= (hh, mm)

PAGE_JS = r"""
() => {
  const q   = s => document.querySelector(s);
  const txt = s => ((q(s) || {}).textContent || '').trim();
  const bc  = window.BCData || {};
  const stockEl = q('[data-product-stock]');
  let stockVisible = false;
  if (stockEl) {
    const wrap = stockEl.closest('.form-field') || stockEl;
    stockVisible = getComputedStyle(wrap).display !== 'none' && getComputedStyle(stockEl).display !== 'none';
  }
  let jsonldPrice = null;
  document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
    if (jsonldPrice !== null) return;
    try {
      const d = JSON.parse(s.textContent);
      const items = Array.isArray(d) ? d : (d['@graph'] || [d]);
      for (const it of items) {
        if (it['@type'] === 'Product' && it.offers) {
          const o = Array.isArray(it.offers) ? it.offers[0] : it.offers;
          if (o && (o.price || o.lowPrice)) { jsonldPrice = String(o.price || o.lowPrice); break; }
        }
      }
    } catch (e) {}
  });
  // Add-to-basket form and variant option fields
  const form = q('form[data-cart-item-add]');
  const variantFields = form ? form.querySelectorAll('[data-product-attribute^="set-"], [data-product-attribute="swatch"]').length : 0;
  // Variant codes: spec table "Product Code" row, and every "sku" in the ProductGroup JSON-LD
  const codes = [];
  document.querySelectorAll('table tr').forEach(tr => {
    const cells = [...tr.children].map(c => c.textContent.trim());
    if (/^product code$/i.test(cells[0] || '')) codes.push(...cells.slice(1).filter(Boolean));
  });
  document.querySelectorAll('script[type="application/ld+json"]').forEach(sc => {
    const t = sc.textContent || '';
    if (/"ProductGroup"/.test(t)) for (const m of t.matchAll(/"sku"\s*:\s*"([^"]+)"/g)) codes.push(m[1]);
  });
  // Stock & Lead Times lines
  const viewTxt = (q('.productView') || document.body).innerText || '';
  const li = viewTxt.search(/Stock\s*&\s*Lead\s*Times/i);
  const lead = li >= 0 ? viewTxt.slice(li, li + 1500).split('\n').map(x => x.trim()).filter(Boolean) : [];
  return {
    has_form: !!form,
    variant_fields: variantFields,
    variant_codes: [...new Set(codes)],
    lead,
    title: txt('h1.productView-title') || txt('h1') ||
           ((q('meta[property="og:title"]') || {}).content || '') || document.title,
    pa: bc.product_attributes || null,
    product_id: (q('input[name="product_id"]') || {}).value || null,
    stock_dom: stockEl ? stockEl.textContent.trim() : null,
    stock_dom_visible: stockVisible,
    sku_dom: txt('[data-product-sku]'),
    jsonld_price: jsonldPrice,
    body: (document.body && document.body.innerText || '').slice(0, 20000),
  };
}
"""


# ── Helpers ────────────────────────────────────────────────────────────────────

def sb_client() -> Client:
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])


def _num(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return round(float(str(v).replace(",", "").replace("£", "").strip()), 2)
    except ValueError:
        return None


def _int(v) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    m = re.search(r"-?\d[\d,]*", str(v))
    return int(m.group(0).replace(",", "")) if m else None


def _price(pa: dict, key: str) -> Optional[float]:
    p = (pa or {}).get("price") or {}
    node = p.get(key) or {}
    return _num(node.get("value")) if isinstance(node, dict) else None


def wanted_sku(url: str) -> Optional[str]:
    vals = parse_qs(urlparse(url).query).get("sku")
    v = vals[0].strip().strip("{}").strip() if vals else ""
    return v or None


def clean_url(url: str) -> str:
    """Drop tracking params (srsltid etc.) and trailing-slash differences; keep only ?sku=."""
    u = urlparse(url.strip())
    path = u.path if u.path.endswith("/") else u.path + "/"
    sku = wanted_sku(url)
    return f"{u.scheme or 'https'}://{u.netloc}{path}" + (f"?sku={sku}" if sku else "")


def same_sku(a: Optional[str], b: Optional[str]) -> bool:
    return bool(a and b and a.strip().lower() == b.strip().lower())


def parse_due(lines: list[str]) -> list[dict]:
    """'2,000\tDue in 19/11/2026' → [{'qty': 2000, 'date': '2026-11-19'}] (stops at 'Need it now?')."""
    out = []
    for ln in lines or []:
        if re.match(r"^need it now", ln, re.I):
            break
        m = re.match(r"^([\d,]+)\s+Due in\s+(\d{1,2})/(\d{1,2})/(\d{4})", ln, re.I)
        if m:
            qty = int(m.group(1).replace(",", ""))
            d, mth, y = int(m.group(2)), int(m.group(3)), int(m.group(4))
            out.append({"qty": qty, "date": f"{y:04d}-{mth:02d}-{d:02d}"})
    return out


def next_due(due: list[dict]) -> tuple[Optional[str], Optional[int]]:
    today = datetime.now(timezone.utc).date().isoformat()
    future = sorted([x for x in due if x["date"] >= today], key=lambda x: x["date"])
    if not future:
        return None, None
    first = future[0]["date"]
    return first, sum(x["qty"] for x in future if x["date"] == first)


CUT_RE = re.compile(r"cut[\s-]*to[\s-]*(order|size|length)", re.I)


# ── URL sources ────────────────────────────────────────────────────────────────

def load_matches(sb: Client) -> tuple[list[str], dict[str, list[str]]]:
    """All distinct Harrison URLs in competitor_matches, and clean URL → matched UKPOS SKUs."""
    urls, matched, start = set(), {}, 0
    while True:
        rows = (sb.table("competitor_matches").select("competitor_url,sku_id,match_status")
                  .eq("competitor_id", HARRISON_ID)
                  .range(start, start + 999).execute().data or [])
        for r in rows:
            if not r.get("competitor_url"):
                continue
            u = clean_url(r["competitor_url"])
            urls.add(u)
            if r.get("match_status") == "matched" and r.get("sku_id"):
                matched.setdefault(u, set()).add(r["sku_id"])
        if len(rows) < 1000:
            break
        start += 1000
    return sorted(urls), {u: sorted(s) for u, s in matched.items()}


def last_out_of_stock(sb: Client) -> set[str]:
    """URLs whose latest (non-error) reading is out of stock."""
    out, start = set(), 0
    while True:
        rows = (sb.table("harrison_stock_latest").select("competitor_url")
                  .eq("stock_status", "out_of_stock")
                  .range(start, start + 999).execute().data or [])
        out.update(r["competitor_url"] for r in rows)
        if len(rows) < 1000:
            break
        start += 1000
    return out


async def urls_from_sitemap(context: BrowserContext) -> list[str]:
    """BigCommerce sitemap index → every type=products page → product <loc> URLs."""
    req = context.request
    idx = await req.get(f"{HARRISON_BASE}/xmlsitemap.php", timeout=TIMEOUT_MS)
    if not idx.ok:
        raise RuntimeError(f"Sitemap index returned HTTP {idx.status}")
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", await idx.text())
    product_maps = [l.replace("&amp;", "&") for l in locs if "type=products" in l]
    if not product_maps:
        raise RuntimeError("No type=products sitemaps found in /xmlsitemap.php")
    urls = set()
    for sm in product_maps:
        r = await req.get(sm, timeout=TIMEOUT_MS)
        if not r.ok:
            log.warning(f"  sitemap {sm} → HTTP {r.status}")
            continue
        for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", await r.text()):
            urls.add(clean_url(loc.replace("&amp;", "&")))
    return sorted(urls)


# ── Page read ──────────────────────────────────────────────────────────────────

async def read_product(context: BrowserContext, url: str) -> dict:
    row = {
        "competitor_url": url, "product_name": None, "harrison_sku": None,
        "price_ex_vat": None, "price_inc_vat": None, "stock_level": None,
        "stock_status": "unknown", "error_message": None,
        "kind": None, "due_in": None, "next_due_date": None, "next_due_qty": None, "due_scope": None,
    }
    attr_payloads: list[dict] = []

    async def on_response(resp):
        if "/remote/v1/product-attributes/" in resp.url:
            try:
                body = await resp.json()
                if isinstance(body, dict) and isinstance(body.get("data"), dict):
                    attr_payloads.append(body["data"])
            except Exception:
                pass

    page = await context.new_page()
    page.on("response", on_response)
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
        if resp and resp.status >= 400:
            row.update(stock_status="error", error_message=f"HTTP {resp.status}")
            return row
        await page.wait_for_timeout(2500)
        info = await page.evaluate(PAGE_JS)
    except Exception as e:
        row.update(stock_status="error", error_message=f"Load failed: {str(e)[:200]}")
        return row
    finally:
        try:
            await page.close()
        except Exception:
            pass

    row["product_name"] = (info.get("title") or "").strip()[:300] or None
    pa   = info.get("pa") or {}
    want = wanted_sku(url)

    # Next stock due
    due = parse_due(info.get("lead") or [])
    row["due_in"] = due or None
    row["next_due_date"], row["next_due_qty"] = next_due(due)
    has_options = (info.get("variant_fields") or 0) > 0
    row["due_scope"] = "range" if has_options else "item"

    # No basket → cut to order / enquiry-only: not a stocked product
    if not info.get("has_form"):
        row["kind"] = "cut_to_order" if CUT_RE.search(f"{url} {row['product_name'] or ''}") else "no_basket"
        row["harrison_sku"] = pa.get("sku") or info.get("sku_dom") or None
        row["error_message"] = "Cut to order — no stock or fixed price" if row["kind"] == "cut_to_order" \
                               else "No add-to-basket on page — no stock or fixed price"
        return row

    # Parent page of a product with sizes/colours: never use its "from" price; expand to variants
    if has_options and not want:
        parent_sku = (pa.get("sku") or "").strip()
        codes = [c for c in (info.get("variant_codes") or []) if c and c.strip() != parent_sku]
        row["harrison_sku"] = parent_sku or None
        if codes:
            row["kind"] = "variant_parent"
            row["error_message"] = f"Has options — split into {len(codes)} variants"
            row["_expand"] = codes
        else:
            row["kind"] = "options_unsplit"
            row["error_message"] = "Has options but no variant codes found on page"
        return row

    row["kind"] = "variant" if want and has_options else "single"

    # Pick the attribute set that belongs to this URL
    data = None
    if want:
        for cand in [pa] + attr_payloads[::-1]:
            if same_sku(cand.get("sku"), want):
                data = cand
                break
        if data is None:
            got = pa.get("sku") or info.get("sku_dom") or "none"
            row.update(stock_status="error", harrison_sku=want,
                       error_message=f"Variant {want} not confirmed on page (page SKU: {got})")
            return row
    else:
        data = attr_payloads[-1] if attr_payloads and not pa else pa

    row["harrison_sku"]  = data.get("sku") or info.get("sku_dom") or want
    row["price_ex_vat"]  = _price(data, "without_tax")
    row["price_inc_vat"] = _price(data, "with_tax")
    if row["price_ex_vat"] is None and row["price_inc_vat"] is None and info.get("jsonld_price"):
        # Harrison prices are ex VAT (competitors.vat_status = 'ex')
        row["price_ex_vat"] = _num(info["jsonld_price"])

    # Stock level: BCData first, then the visible stock element
    level = _int(data.get("stock"))
    if level is None and info.get("stock_dom_visible"):
        level = _int(info.get("stock_dom"))
    row["stock_level"] = level

    # Stock status
    instock = data.get("instock")
    if isinstance(instock, bool):
        row["stock_status"] = "in_stock" if instock else "out_of_stock"
    elif level is not None:
        row["stock_status"] = "in_stock" if level > 0 else "out_of_stock"
    elif data.get("purchasable") is False or detect_oos(info.get("body") or ""):
        row["stock_status"] = "out_of_stock"
    elif pa or attr_payloads:
        row["stock_status"] = "in_stock"
    else:
        row["error_message"] = "No BigCommerce product data on page"

    if level is not None and level <= 0:
        row["stock_status"] = "out_of_stock"
    return row


# ── Run ────────────────────────────────────────────────────────────────────────

def write_rows(sb: Client, rows: list[dict]):
    for i in range(0, len(rows), BATCH):
        sb.table("harrison_stock").insert(rows[i:i + BATCH]).execute()


async def main():
    if CHECK not in ("oos", "levels"):
        log.error(f"Unknown HARRISON_MODE '{MODE}' — use oos, levels, oos_catalogue or levels_catalogue")
        sys.exit(1)

    sb  = sb_client()
    if TRIGGER == "daily":
        today_uk = datetime.now(UK).date()
        since = datetime(today_uk.year, today_uk.month, today_uk.day, tzinfo=UK).astimezone(timezone.utc).isoformat()
        already = (sb.table("harrison_stock_runs").select("id").eq("triggered_by", "daily")
                   .gte("started_at", since).limit(1).execute().data)
        if already:
            log.info("A daily Harrison run already started today (UK) — nothing to do.")
            return
        if past_stop_time():
            log.info(f"Already past {STOP_AT} UK — not starting today's daily run.")
            return
    run = sb.table("harrison_stock_runs").insert(
        {"scope": SCOPE, "check_type": CHECK, "status": "running", "triggered_by": TRIGGER,
         "started_at": now_min()}).execute().data[0]
    run_id = run["id"]
    log.info(f"Harrison stock run {run_id} — check={CHECK} scope={SCOPE} trigger={TRIGGER} "
             f"workers={WORKERS} delay={DELAY_MIN}-{DELAY_MAX}s shuffle={SHUFFLE} stop_at={STOP_AT or '-'}")

    try:
        async with async_playwright() as pw:
            browser = await launch_browser(pw)
            contexts = [await new_stealth_context(browser) for _ in range(max(1, WORKERS))]

            match_urls, matched = load_matches(sb)
            urls = match_urls if SCOPE == "matches" else await urls_from_sitemap(contexts[0])
            skip: set[str] = set()
            if CHECK == "levels":
                oos = last_out_of_stock(sb)
                skip = oos
                before = len(urls)
                urls = [u for u in urls if u not in oos]
                log.info(f"Stock levels: skipping {before - len(urls)} URLs last seen out of stock")
            if SHUFFLE:
                random.shuffle(urls)
            log.info(f"{len(urls)} Harrison URLs to read")
            sb.table("harrison_stock_runs").update({"url_count": len(urls)}).eq("id", run_id).execute()

            queue: asyncio.Queue = asyncio.Queue()
            seen: set[str] = set()
            for u in urls:
                if u not in seen:
                    seen.add(u)
                    queue.put_nowait(u)
            pending: list[dict] = []
            done = {"n": 0, "ok": 0, "total": len(seen), "skipped": 0}
            guard = {"streak": 0, "paused": False, "stop": None}   # stop: None | 'time' | 'blocked'
            pause_lock = asyncio.Lock()

            async def handle(ctx: BrowserContext, u: str):
                row = await read_product(ctx, u)
                expand = row.pop("_expand", None) or []
                row["run_id"] = run_id
                row["ukpos_skus"] = matched.get(u) or None
                row["scraped_at"] = now_min()
                pending.append(row)
                done["n"] += 1
                if row["stock_status"] != "error":
                    done["ok"] += 1
                    guard["streak"] = 0
                    guard["paused"] = False
                else:
                    await note_error()
                base = u.split("?")[0]
                added = 0
                for code in expand:
                    vu = clean_url(f"{base}?sku={code}")
                    if vu not in seen and vu not in skip:
                        seen.add(vu)
                        queue.put_nowait(vu)
                        added += 1
                done["total"] += added
                lvl = row["stock_level"] if row["stock_level"] is not None else "—"
                due = f" due={row['next_due_date']}x{row['next_due_qty']}" if row.get("next_due_date") else ""
                log.info(f"  [{done['n']}/{done['total']}] {row['stock_status']:<12} {str(row.get('kind')):<15} lvl={lvl:<6} "
                         f"£{row['price_ex_vat']}{due} {u}" +
                         (f"  ({row['error_message']})" if row["error_message"] else ""))
                if len(pending) >= BATCH:
                    chunk = pending[:BATCH]
                    del pending[:BATCH]
                    write_rows(sb, chunk)

            async def note_error():
                # Many errors in a row = Harrison may be refusing us. Pause once, then give up.
                guard["streak"] += 1
                if guard["streak"] < BLOCK_STREAK or guard["stop"]:
                    return
                async with pause_lock:
                    if guard["streak"] < BLOCK_STREAK or guard["stop"]:
                        return
                    if not guard["paused"]:
                        wait = random.uniform(180, 360)
                        log.warning(f"  {guard['streak']} errors in a row — pausing {wait/60:.1f} min")
                        await asyncio.sleep(wait)
                        guard["paused"] = True
                        guard["streak"] = 0
                    else:
                        log.error(f"  still failing after a pause — stopping (Harrison may be blocking us)")
                        guard["stop"] = "blocked"

            async def worker(ctx: BrowserContext):
                while True:
                    u = await queue.get()
                    if not guard["stop"] and past_stop_time():
                        guard["stop"] = "time"
                        log.warning(f"  reached {STOP_AT} UK — not reading the rest")
                    if guard["stop"]:
                        done["skipped"] += 1
                        queue.task_done()
                        continue
                    try:
                        await handle(ctx, u)
                    except Exception as e:
                        log.warning(f"  worker error on {u}: {e}")
                        await note_error()
                    finally:
                        queue.task_done()
                    await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

            tasks = [asyncio.create_task(worker(c)) for c in contexts]
            await queue.join()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if pending:
                write_rows(sb, pending)
            sb.table("harrison_stock_runs").update({"url_count": done["n"]}).eq("id", run_id).execute()
            await browser.close()

        note = None
        if guard["stop"] == "time":
            note = f"Stopped at {STOP_AT} UK: {done['skipped']} URLs not read"
        elif guard["stop"] == "blocked":
            note = f"Stopped early: many errors in a row, Harrison may be blocking us ({done['skipped']} URLs not read)"
        sb.table("harrison_stock_runs").update({
            "status": "failed" if guard["stop"] == "blocked" else "done", "ok_count": done["ok"],
            "error_message": note, "finished_at": now_min(),
        }).eq("id", run_id).execute()
        log.info(f"Done — {done['ok']}/{done['n']} read OK" + (f" · {note}" if note else ""))
        if guard["stop"] == "blocked":
            sys.exit(1)
    except Exception as e:
        log.exception("Harrison stock run failed")
        sb.table("harrison_stock_runs").update({
            "status": "failed", "error_message": str(e)[:500],
            "finished_at": now_min(),
        }).eq("id", run_id).execute()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
