"""
scraper/scrape.py
─────────────────
Automated price scraper — runs every 3 days via GitHub Actions.

ONLY visits URLs already confirmed in competitor_matches
(match_status = 'matched' OR 'amended'). Never calls a search engine.
Fast, reliable, and free of CAPTCHA risk.

'amended' rows are URLs manually corrected by a reviewer — they get scraped
on the next run and, on success, transition to 'matched'. On any price
result (even OOS), awaiting_scrape is reset to false.

Per confirmed URL:
  - Scrapes price, VAT basis, availability, pack qty
  - Writes to price_snapshots
  - Raises alerts if diff > threshold
  - Refreshes competitor product image quarterly

Environment variables:
    SUPABASE_URL
    SUPABASE_SERVICE_KEY
    SCRAPER_WORKERS          (default: 5)
    SCRAPER_PAGE_TIMEOUT_MS  (default: 30000)
    SCRAPER_DELAY_MIN        (default: 3)
    SCRAPER_DELAY_MAX        (default: 7)
    SCRAPER_COMPETITOR_LIMIT (default: 23)
    SCRAPER_SKUS             comma-separated SKU IDs (optional — scrape specific SKUs only)
    IMAGE_REFRESH_DAYS       (default: 90)
    LOG_LEVEL                (default: INFO)
"""

import asyncio
import json
import logging
import os
import random
import re
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import quote_plus

from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from supabase import create_client, Client

from common import (
    build_search_query,
    detect_vat,
    detect_oos,
    diff_pct,
    extract_pack_qty,
    fuzzy_confidence,
    is_category_url,
    launch_browser,
    new_stealth_context,
    normalise_price,
    parse_price,
    per_unit_price,
    BIGCOMMERCE_DOMAINS,
    DISCOUNT_DISPLAYS_DOMAIN,
)

# ── Config ─────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("pricewatch.scrape")

SUPABASE_URL      = os.environ["SUPABASE_URL"]
SUPABASE_KEY      = os.environ["SUPABASE_SERVICE_KEY"]
WORKERS           = int(os.getenv("SCRAPER_WORKERS", "5"))
TIMEOUT_MS        = int(os.getenv("SCRAPER_PAGE_TIMEOUT_MS", "30000"))
DELAY_MIN         = float(os.getenv("SCRAPER_DELAY_MIN", "3"))
DELAY_MAX         = float(os.getenv("SCRAPER_DELAY_MAX", "7"))
COMPETITOR_LIMIT  = int(os.getenv("SCRAPER_COMPETITOR_LIMIT", "23"))
IMAGE_REFRESH_DAYS = int(os.getenv("IMAGE_REFRESH_DAYS", "90"))


# ── Image refresh helper ───────────────────────────────────────────────────────

def image_needs_refresh(match: Optional[dict]) -> bool:
    if not match or not match.get("competitor_image_url"):
        return True
    updated = match.get("updated_at")
    if not updated:
        return True
    try:
        last = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - last).days >= IMAGE_REFRESH_DAYS
    except Exception:
        return True


# ── Price extraction methods ───────────────────────────────────────────────────

async def _extract_shopify_json_price(url: str, context: BrowserContext) -> Optional[float]:
    try:
        base = url.split("?")[0].rstrip("/")
        if "/products/" not in base:
            return None
        page = await context.new_page()
        try:
            await page.goto(base + ".js", wait_until="domcontentloaded", timeout=10000)
            text = await page.inner_text("body")
            await page.close()
            data = json.loads(text)
            variants = data.get("variants", [])
            if variants:
                p = variants[0].get("price")
                if p: return round(float(p) / 100, 2)
            p = data.get("price")
            if p: return round(float(p) / 100, 2)
        except Exception:
            try: await page.close()
            except Exception: pass
    except Exception:
        pass
    return None


async def _extract_jsonld_price(page: Page) -> Optional[float]:
    for script in await page.locator('script[type="application/ld+json"]').all():
        try:
            data  = json.loads(await script.inner_text())
            items = data if isinstance(data, list) else [data]
            flat  = []
            for item in items:
                flat.extend(item.get("@graph", [item]))
            for item in flat:
                if item.get("@type") == "Product":
                    offers = item.get("offers", {})
                    if isinstance(offers, list): offers = offers[0]
                    price = offers.get("price") or offers.get("lowPrice")
                    if price: return float(str(price).replace(",", ""))
                if item.get("@type") == "Offer":
                    price = item.get("price")
                    if price: return float(str(price).replace(",", ""))
        except Exception:
            pass
    return None


async def _extract_meta_price(page: Page) -> Optional[float]:
    for attr in ["product:price:amount", "og:price:amount"]:
        try:
            el = page.locator(f'meta[property="{attr}"]').first
            if await el.count() > 0:
                val = await el.get_attribute("content")
                if val: return parse_price(val)
        except Exception:
            pass
    return None


async def _extract_main_price(page: Page) -> Optional[float]:
    try:
        result = await page.evaluate(r"""
            () => {
                const EXCLUDE_SIGNALS = [
                    'related', 'similar', 'recommend', 'upsell', 'cross-sell',
                    'crosssell', 'cross_sell', 'recently', 'also-bought',
                    'also_bought', 'alsoBought', 'you-may', 'youmay',
                    'footer', 'nav', 'sidebar', 'widget', 'carousel',
                    'slick', 'swiper', 'featured-products', 'other-products',
                    'more-products', 'trending', 'popular',
                ];
                const CART_SIGNALS = [
                    'add-to-cart', 'addtocart', 'add_to_cart', 'basket',
                    'buy-now', 'buynow', 'purchase', 'checkout',
                ];
                function isExcluded(el) {
                    let node = el;
                    for (let i = 0; i < 8; i++) {
                        if (!node || node === document.body) break;
                        const cls = (node.className || '').toLowerCase();
                        const id  = (node.id || '').toLowerCase();
                        if (EXCLUDE_SIGNALS.some(s => cls.includes(s) || id.includes(s))) return true;
                        node = node.parentElement;
                    }
                    return false;
                }
                function hasCartButton(el) {
                    let node = el;
                    for (let i = 0; i < 10; i++) {
                        if (!node || node === document.body) break;
                        const html = (node.innerHTML || '').toLowerCase();
                        if (CART_SIGNALS.some(s => html.includes(s))) return true;
                        node = node.parentElement;
                    }
                    return false;
                }
                function getFontSize(el) {
                    try { return parseFloat(window.getComputedStyle(el).fontSize) || 0; }
                    catch { return 0; }
                }
                const SELECTORS = [
                    "[itemprop='price']", ".price", ".product-price", ".our-price",
                    ".sale-price", "#product-price", "[class*='price']", "[data-price]",
                    ".offer-price", "span.amount", ".woocommerce-Price-amount",
                    "p.price", ".product__price", ".pdp-price", ".main-price",
                    "[class*='product'][class*='price']",
                ];
                const seen = new Set();
                const candidates = [];
                for (const sel of SELECTORS) {
                    for (const el of document.querySelectorAll(sel)) {
                        if (seen.has(el)) continue;
                        seen.add(el);
                        if (isExcluded(el)) continue;
                        const raw = (el.getAttribute('content') || el.innerText || '').trim();
                        const m   = raw.replace(/,/g, '').match(/[\d]+\.?\d*/);
                        if (!m) continue;
                        const val = parseFloat(m[0]);
                        if (val < 0.01 || val > 99999) continue;
                        candidates.push({ price: val, hasCart: hasCartButton(el), fontSize: getFontSize(el) });
                    }
                }
                if (!candidates.length) return null;
                candidates.sort((a, b) => {
                    if (a.hasCart !== b.hasCart) return a.hasCart ? -1 : 1;
                    return b.fontSize - a.fontSize;
                });
                return candidates[0].price;
            }
        """)
        return float(result) if result else None
    except Exception:
        return None


async def _extract_alplas_price(page: Page) -> Optional[float]:
    try:
        result = await page.evaluate(r"""
            () => {
                function parsePrice(raw) {
                    const m = (raw || '').replace(/,/g,'').match(/[\d]+\.[\d]{2}/);
                    if (!m) return null;
                    const val = parseFloat(m[0]);
                    return (val > 0.01 && val < 99999) ? val : null;
                }
                const container = document.querySelector('.price_inner_container .total_price_container');
                if (container) {
                    for (const priceDiv of container.querySelectorAll('.price')) {
                        const vatSpan = priceDiv.querySelector('.vat_span');
                        if (vatSpan && vatSpan.innerText.toLowerCase().includes('ex')) {
                            const amount = priceDiv.querySelector('.amount bdi, .amount');
                            if (amount) {
                                const val = parsePrice(amount.innerText || amount.textContent);
                                if (val) return val;
                            }
                        }
                    }
                    const first = container.querySelector('.price .amount bdi, .price .amount');
                    if (first) {
                        const val = parsePrice(first.innerText || first.textContent);
                        if (val) return val;
                    }
                }
                const unit = document.querySelector('.unit_container .price .amount');
                if (unit) {
                    const val = parsePrice(unit.innerText || unit.textContent);
                    if (val) return val;
                }
                return null;
            }
        """)
        return float(result) if result else None
    except Exception:
        return None


async def _extract_pavement_signs_price(page: Page) -> Optional[float]:
    try:
        result = await page.evaluate(r"""
            () => {
                const el = document.querySelector('#ContentPlaceHolder1_lblexVAT');
                if (!el) return null;
                const raw = (el.innerText || el.textContent || '').replace(/,/g,'').trim();
                const m = raw.match(/[\d]+\.?[\d]*/);
                if (!m) return null;
                const val = parseFloat(m[0]);
                return (val > 0.01 && val < 99999) ? val : null;
            }
        """)
        return float(result) if result else None
    except Exception:
        return None


async def _extract_discount_displays_price(page: Page) -> Optional[float]:
    try:
        try:
            await page.wait_for_function(
                """() => {
                    const el = document.querySelector('.price.label, [class*="text-gray-900"][class*="price"]');
                    if (!el) return false;
                    const raw = (el.innerText || '').trim();
                    return raw.length > 0 && raw !== '£0.00' && /[1-9]/.test(raw);
                }""",
                timeout=8000
            )
        except Exception:
            pass

        result = await page.evaluate(r"""
            () => {
                function parsePrice(raw) {
                    const m = (raw || '').replace(/,/g,'').match(/[\d]+\.[\d]{2}/);
                    if (!m) return null;
                    const val = parseFloat(m[0]);
                    return (val > 0.50 && val < 99999) ? val : null;
                }
                const mainContainer = document.querySelector('[class*="price-excl-taxinline-block"]');
                if (mainContainer) {
                    const inCarousel = mainContainer.closest('.js_slides, [class*="js_slide"]');
                    if (!inCarousel) {
                        const priceSpan = mainContainer.querySelector('[x-html*="getFormattedBasePrice"], span.price');
                        if (priceSpan) {
                            const val = parsePrice(priceSpan.innerText || priceSpan.textContent);
                            if (val) return val;
                        }
                    }
                }
                for (const el of document.querySelectorAll('[x-html*="getFormattedBasePrice"]')) {
                    if (el.closest('.js_slides, [class*="js_slide"]')) continue;
                    const val = parsePrice(el.innerText || el.textContent);
                    if (val) return val;
                }
                for (const el of document.querySelectorAll('.price-excluding-tax.active')) {
                    if (el.closest('.js_slides, [class*="js_slide"]')) continue;
                    const val = parsePrice(el.innerText || el.textContent);
                    if (val) return val;
                }
                return null;
            }
        """)
        return float(result) if result else None
    except Exception:
        return None


# ── Page scraper ───────────────────────────────────────────────────────────────

async def scrape_product_page(
    context: BrowserContext,
    url: str,
    competitor_domain: str = "",
    fetch_image: bool = False,
) -> dict:
    result = {
        "price": None, "vat": "unknown", "availability": "in_stock",
        "title": "", "url": url, "error": None, "og_image": None,
    }

    if is_category_url(url):
        result["error"]        = "Category page — no single product price"
        result["availability"] = "unavailable"
        log.debug(f"  Skipping category page: {url}")
        return result

    shopify_price = await _extract_shopify_json_price(url, context)

    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
        await page.wait_for_timeout(10000)

        full_text              = await page.inner_text("body")
        result["vat"]          = detect_vat(full_text)
        result["availability"] = "out_of_stock" if detect_oos(full_text) else "in_stock"
        result["title"]        = (await page.title()).strip()

        if fetch_image:
            try:
                og = await page.evaluate("""
                    () => {
                        const og  = document.querySelector('meta[property="og:image"]');
                        const twi = document.querySelector('meta[name="twitter:image"]');
                        return (og?.content || twi?.content || '').trim() || null;
                    }
                """)
                if og and og.startswith("http"):
                    result["og_image"] = og
            except Exception:
                pass

        price = shopify_price
        if not price: price = await _extract_jsonld_price(page)
        if not price: price = await _extract_meta_price(page)
        if not price and DISCOUNT_DISPLAYS_DOMAIN in competitor_domain:
            price = await _extract_discount_displays_price(page)
        if not price and "alplas.com" in competitor_domain:
            price = await _extract_alplas_price(page)
        if not price and "pavementsigns.com" in competitor_domain:
            price = await _extract_pavement_signs_price(page)
        if not price:
            price = await _extract_main_price(page)

        result["price"] = price

    except Exception as e:
        result["error"]        = str(e)[:200]
        result["availability"] = "error"
    finally:
        await page.close()

    return result


# ── Snapshot writer ────────────────────────────────────────────────────────────

def write_snapshot(sb: Client, snapshot: dict):
    row = {k: v for k, v in snapshot.items() if not k.startswith("_")}
    sb.table("price_snapshots").insert(row).execute()


def create_alerts(sb: Client, snapshot: dict, sku: dict, competitor: dict, run_id: str):
    our_price = float(sku["price_ex_vat"])
    diff      = snapshot.get("diff_pct_normalised") or snapshot.get("diff_pct")
    alerts    = []

    if snapshot["availability"] == "out_of_stock":
        alerts.append({
            "run_id": run_id, "sku_id": sku["sku_id"], "competitor_id": competitor["id"],
            "alert_type": "oos_competitor",
            "message": f"{competitor['name']} is out of stock for {sku['short_title']} — last known £{snapshot.get('competitor_price','?')}",
            "diff_pct": diff, "our_price": our_price, "their_price": snapshot.get("competitor_price"),
        })
    elif diff is not None:
        if diff <= -10:
            alerts.append({
                "run_id": run_id, "sku_id": sku["sku_id"], "competitor_id": competitor["id"],
                "alert_type": "critical",
                "message": f"{competitor['name']} is {abs(diff):.1f}% cheaper — £{snapshot['competitor_price']:.2f} vs your £{our_price:.2f}",
                "diff_pct": diff, "our_price": our_price, "their_price": snapshot.get("competitor_price"),
            })
        elif diff <= -5:
            alerts.append({
                "run_id": run_id, "sku_id": sku["sku_id"], "competitor_id": competitor["id"],
                "alert_type": "warning",
                "message": f"{competitor['name']} is {abs(diff):.1f}% cheaper — £{snapshot['competitor_price']:.2f} vs your £{our_price:.2f}",
                "diff_pct": diff, "our_price": our_price, "their_price": snapshot.get("competitor_price"),
            })

    for alert in alerts:
        sb.table("alerts").insert(alert).execute()


# ── Per-match scrape ───────────────────────────────────────────────────────────

async def scrape_match(
    browser: Browser,
    sb: Client,
    sku: dict,
    competitor: dict,
    match: dict,
    run_id: str,
) -> dict:
    domain         = competitor["domain"].lstrip("www.")
    url            = match["competitor_url"]
    was_amended    = match.get("match_status") == "amended"

    snapshot = {
        "sku_id":              sku["sku_id"],
        "competitor_id":       competitor["id"],
        "run_id":              run_id,
        "scraped_at":          datetime.now(timezone.utc).isoformat(),
        "availability":        "unavailable",
        "competitor_price":    None,
        "competitor_vat":      competitor.get("vat_status", "unknown"),
        "competitor_url":      url,
        "diff_pct":            None,
        "diff_pct_normalised": None,
        "competitor_unit_qty": None,
        "pack_qty_flag":       None,
        "confidence":          match.get("confidence"),
        "error_message":       None,
        "_comp_title":         match.get("competitor_title"),
        "_was_amended":        was_amended,
    }

    ctx = await new_stealth_context(browser)
    try:
        result = await scrape_product_page(
            ctx, url, domain,
            fetch_image=image_needs_refresh(match),
        )
        price      = result["price"]
        comp_title = result["title"] or match.get("competitor_title", "")
        vat_hint   = result["vat"]

        snapshot["availability"]  = result["availability"]
        snapshot["error_message"] = result["error"]
        snapshot["_comp_title"]   = comp_title
        snapshot["_og_image"]     = result.get("og_image")

        if vat_hint != "unknown":
            snapshot["competitor_vat"] = vat_hint

        if price:
            our_price  = float(sku["price_ex_vat"])
            their_ex   = normalise_price(price, snapshot["competitor_vat"])

            our_title_qty = extract_pack_qty(sku.get("short_title", "")) or 1
            our_col_qty   = sku.get("unit_qty") or 1
            our_qty  = our_title_qty if our_title_qty > 1 else our_col_qty
            comp_qty = extract_pack_qty(comp_title) or 1

            snapshot["competitor_unit_qty"] = comp_qty

            if our_qty == comp_qty and their_ex and our_price:
                ratio = max(our_price, their_ex) / min(our_price, their_ex)
                if ratio >= 1.5:
                    snapshot["pack_qty_flag"] = (
                        f"raw price gap {ratio:.1f}× with no pack signal — verify pack sizes"
                    )

            if our_qty != comp_qty:
                our_per_unit   = per_unit_price(our_price, our_qty)
                their_per_unit = per_unit_price(their_ex,  comp_qty)
                normalised_diff = diff_pct(our_per_unit, their_per_unit)
                log.info(
                    f"  ✓ {competitor['domain']:35s} "
                    f"£{price:>7.2f} ({snapshot['competitor_vat']:7s}) "
                    f"our_qty={our_qty} comp_qty={comp_qty} "
                    f"→ per-unit diff {normalised_diff:+.1f}%"
                    f"{' [amended→matched]' if was_amended else ''}"
                )
            else:
                normalised_diff = diff_pct(our_price, their_ex)
                log.info(
                    f"  ✓ {competitor['domain']:35s} "
                    f"£{price:>7.2f} ({snapshot['competitor_vat']:7s}) "
                    f"diff {normalised_diff:+.1f}%"
                    f"{' [amended→matched]' if was_amended else ''}"
                )

            snapshot["competitor_price"]    = price
            snapshot["diff_pct"]            = diff_pct(our_price, price)
            snapshot["diff_pct_normalised"] = normalised_diff
        else:
            log.info(
                f"  ✗ {competitor['domain']:35s} "
                f"no price — {result.get('error', '')[:60]}"
                f"{' [amended, no price found]' if was_amended else ''}"
            )

    except Exception as e:
        snapshot["error_message"] = str(e)[:200]
        log.error(f"  Exception {sku['sku_id']} × {competitor['domain']}: {e}")
    finally:
        await ctx.close()

    return snapshot


# ── Main runner ────────────────────────────────────────────────────────────────

async def run_scraper(trigger: str = "scheduled"):
    sb     = create_client(SUPABASE_URL, SUPABASE_KEY)
    run_id = str(uuid.uuid4())

    sb.table("sync_runs").insert({
        "id": run_id, "trigger": trigger,
        "status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
    }).execute()

    specific_skus = [s.strip() for s in os.getenv("SCRAPER_SKUS", "").split(",") if s.strip()]

    # ── Load confirmed matches AND amended matches ──────────────────────────────
    # 'amended' rows are URLs manually corrected by a reviewer awaiting first scrape.
    # They are scraped here exactly like 'matched' rows; on success they transition
    # to 'matched' and awaiting_scrape is cleared.
    query = (
        sb.table("competitor_matches")
        .select(
            "sku_id, competitor_id, competitor_url, competitor_title, "
            "confidence, match_status, updated_at, competitor_image_url, "
            "previous_url, awaiting_scrape"
        )
        .in_("match_status", ["matched", "amended"])
        .not_.is_("competitor_url", "null")
    )
    if specific_skus:
        query = query.in_("sku_id", specific_skus)
    matched_rows = query.execute().data

    if not matched_rows:
        log.info("No confirmed/amended matches found — nothing to scrape.")
        sb.table("sync_runs").update({
            "status": "complete", "completed_at": datetime.now(timezone.utc).isoformat(),
            "skus_attempted": 0, "skus_succeeded": 0, "skus_failed": 0, "oos_flagged": 0,
        }).eq("id", run_id).execute()
        return

    amended_count = sum(1 for r in matched_rows if r["match_status"] == "amended")
    log.info(
        f"Scrape run {run_id} | {len(matched_rows)} confirmed matches "
        f"({amended_count} amended/awaiting rescrape) | workers={WORKERS}"
    )

    comps = {
        c["id"]: c for c in
        sb.table("competitors").select("*").eq("active", True).limit(COMPETITOR_LIMIT).execute().data
    }

    matched_sku_ids = list({r["sku_id"] for r in matched_rows})
    skus = {}
    for i in range(0, len(matched_sku_ids), 200):
        for row in sb.table("skus").select("*").in_("sku_id", matched_sku_ids[i:i+200]).execute().data:
            skus[row["sku_id"]] = row

    work_items = [
        (skus[r["sku_id"]], comps[r["competitor_id"]], r)
        for r in matched_rows
        if r["sku_id"] in skus and r["competitor_id"] in comps
    ]

    log.info(f"  {len(work_items)} work items")

    stats = {"attempted": 0, "succeeded": 0, "failed": 0, "oos": 0, "amended_promoted": 0}
    sem   = asyncio.Semaphore(WORKERS)

    async def process_item(sku: dict, comp: dict, match: dict):
        async with sem:
            stats["attempted"] += 1
            was_amended = match.get("match_status") == "amended"
            try:
                snap = await scrape_match(browser, sb, sku, comp, match, run_id)
                write_snapshot(sb, snap)
                create_alerts(sb, snap, sku, comp, run_id)

                if snap["availability"] == "error":
                    stats["failed"] += 1
                else:
                    stats["succeeded"] += 1
                    if snap["availability"] == "out_of_stock":
                        stats["oos"] += 1

                # Build the competitor_matches update payload
                match_updates = {"updated_at": datetime.now(timezone.utc).isoformat()}

                if snap.get("_comp_title"):
                    match_updates["competitor_title"] = snap["_comp_title"]
                if snap.get("_og_image"):
                    match_updates["competitor_image_url"] = snap["_og_image"]

                # ── Promote 'amended' → 'matched' once we get any usable result ──
                # We promote even on OOS — the URL is valid, price data exists.
                # We only leave it as 'amended' if the page errored or was unavailable
                # (suggesting the URL might still be wrong).
                if was_amended:
                    if snap["availability"] not in ("error", "unavailable"):
                        match_updates["match_status"]    = "matched"
                        match_updates["awaiting_scrape"] = False
                        stats["amended_promoted"] += 1
                        log.info(
                            f"  ↑ {sku['sku_id']} × {comp['domain']} "
                            f"amended→matched (availability={snap['availability']})"
                        )
                    else:
                        # URL still looks bad — keep as amended so reviewer can see it
                        log.warning(
                            f"  ⚠ {sku['sku_id']} × {comp['domain']} "
                            f"amended but page error/unavailable — keeping as amended"
                        )

                if len(match_updates) > 1:
                    sb.table("competitor_matches").update(match_updates).eq(
                        "sku_id", sku["sku_id"]
                    ).eq("competitor_id", comp["id"]).execute()

            except Exception as e:
                stats["failed"] += 1
                log.error(f"Unhandled: {sku['sku_id']} × {comp['domain']}: {e}")
            finally:
                await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    async with async_playwright() as pw:
        browser = await launch_browser(pw)
        await asyncio.gather(*[process_item(sku, comp, match) for sku, comp, match in work_items])
        await browser.close()

    sb.table("sync_runs").update({
        "status": "complete", "completed_at": datetime.now(timezone.utc).isoformat(),
        "skus_attempted": stats["attempted"], "skus_succeeded": stats["succeeded"],
        "skus_failed": stats["failed"], "oos_flagged": stats["oos"],
    }).eq("id", run_id).execute()

    log.info(
        f"Run {run_id} complete — attempted={stats['attempted']} "
        f"succeeded={stats['succeeded']} failed={stats['failed']} "
        f"oos={stats['oos']} amended_promoted={stats['amended_promoted']}"
    )


if __name__ == "__main__":
    import sys
    trigger = sys.argv[1] if len(sys.argv) > 1 else "scheduled"
    asyncio.run(run_scraper(trigger))
