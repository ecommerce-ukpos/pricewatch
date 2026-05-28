"""
scraper/discover.py
───────────────────
One-time (or occasional) URL discovery pass.

For each unmatched SKU × competitor pair, searches Bing Shopping then
Google Shopping (and falls back to site: web search) to find the
competitor's product page URL and title. Writes {url, title, confidence}
to competitor_matches with match_status='review' for human confirmation.

Does NOT extract prices — that is scrape.py's job.
Run manually (or on a long schedule) when you need to populate or
refresh the competitor_matches table.

Strategy per SKU × competitor:
  1. Bing Shopping  — most reliable, least aggressive CAPTCHA
  2. Google Shopping — fallback if Bing finds nothing
  3. Bing site: web search — last resort
  4. Google site: web search — final fallback

Extraction uses JSON-LD / og: meta / microdata / stable link patterns
rather than rotating CSS class names, so it remains durable over time.

Environment variables:
    SUPABASE_URL
    SUPABASE_SERVICE_KEY
    DISCOVER_WORKERS         (default: 2)
    DISCOVER_PAGE_TIMEOUT_MS (default: 30000)
    DISCOVER_DELAY_MIN       (default: 8)    seconds between requests
    DISCOVER_DELAY_MAX       (default: 15)
    DISCOVER_SKU_LIMIT       (default: 250)  SKUs per run
    DISCOVER_COMPETITOR_LIMIT(default: 23)
    DISCOVER_SKUS            comma-separated SKU IDs (optional override)
    DISCOVER_FORCE           set to 'true' to re-discover already-matched SKUs
    LOG_LEVEL                (default: INFO)
"""

import asyncio
import logging
import os
import random
import re
import uuid
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote_plus

from playwright.async_api import async_playwright
from supabase import create_client

from common import (
    build_search_query,
    fuzzy_confidence,
    launch_browser,
    new_stealth_context,
    BIGCOMMERCE_DOMAINS,
)

# ── Config ─────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("pricewatch.discover")

SUPABASE_URL       = os.environ["SUPABASE_URL"]
SUPABASE_KEY       = os.environ["SUPABASE_SERVICE_KEY"]
WORKERS            = int(os.getenv("DISCOVER_WORKERS", "2"))
TIMEOUT_MS         = int(os.getenv("DISCOVER_PAGE_TIMEOUT_MS", "30000"))
DELAY_MIN          = float(os.getenv("DISCOVER_DELAY_MIN", "8"))
DELAY_MAX          = float(os.getenv("DISCOVER_DELAY_MAX", "15"))
SKU_LIMIT          = int(os.getenv("DISCOVER_SKU_LIMIT", "250"))
COMPETITOR_LIMIT   = int(os.getenv("DISCOVER_COMPETITOR_LIMIT", "23"))
FORCE              = os.getenv("DISCOVER_FORCE", "false").lower() == "true"


# ── SERP extraction — name + URL only ─────────────────────────────────────────

async def _extract_shopping_results(page, clean_dom: str) -> list[dict]:
    """
    Parse a Shopping SERP page for product cards that link to clean_dom.
    Returns list of {url, title} dicts — no prices extracted.

    Uses stable signals only:
      - JSON-LD Product blocks (never changes)
      - og:title / og:url meta (stable)
      - <a href> links containing clean_dom with h3/heading title nearby
    Deliberately avoids rotating CSS class names (.KZmu8e etc).
    """
    return await page.evaluate("""
        (clean_dom) => {
            const results = [];
            const seen    = new Set();

            // ── Strategy 1: JSON-LD ──────────────────────────────────────────
            for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
                try {
                    const data  = JSON.parse(script.textContent);
                    const items = Array.isArray(data) ? data : (data['@graph'] || [data]);
                    for (const item of items) {
                        const offers = item.offers || {};
                        const offerList = Array.isArray(offers) ? offers : [offers];
                        for (const offer of offerList) {
                            const url = offer.url || offer.link || '';
                            if (url && url.includes(clean_dom) && !seen.has(url)) {
                                seen.add(url);
                                results.push({ url, title: item.name || '' });
                            }
                        }
                    }
                } catch(e) {}
            }

            // ── Strategy 2: All anchors pointing at clean_dom ─────────────────
            for (const a of document.querySelectorAll('a[href]')) {
                const href = a.href || '';
                if (!href.includes(clean_dom)) continue;
                if (href.includes('google.') || href.includes('bing.com')) continue;
                if (href.includes('/url?') || href.includes('cache:')) continue;
                if (seen.has(href)) continue;

                // Try to find a nearby heading (h2/h3/h4 or role=heading)
                const block = a.closest('div,li,article') || a;
                const heading = block.querySelector('h2,h3,h4,[role="heading"]');
                const title = (heading?.innerText || a.innerText || a.title || '').trim().slice(0, 200);

                if (title) {
                    seen.add(href);
                    results.push({ url: href, title });
                }
            }

            return results;
        }
    """, clean_dom)


async def _extract_web_search_urls(page, clean_dom: str) -> list[str]:
    """
    Extract competitor domain URLs from a site: web search result page.
    Returns a list of candidate product URLs.
    """
    PRODUCT_SIGNALS = [
        "/product", "/p/", "/item", "/buy", "/shop", ".html",
        "/signs", "/display", "/frame", "/holder", "/pavement",
        "/snap", "/poster", "/board", "/sign",
    ]
    all_links: list[str] = await page.evaluate("""
        () => Array.from(document.querySelectorAll('a[href]'))
                   .map(a => a.href)
                   .filter(h => h.startsWith('http'))
    """)
    domain_links = [
        u for u in all_links
        if clean_dom in u
        and "google" not in u
        and "bing.com" not in u
        and "cache" not in u
    ]
    product_urls = [
        u for u in domain_links
        if any(sig in u.lower() for sig in PRODUCT_SIGNALS)
    ]
    return product_urls or domain_links


# ── Main discovery logic per SKU × competitor ─────────────────────────────────

async def discover_url(
    browser,
    sku: dict,
    competitor: dict,
) -> Optional[dict]:
    """
    Try all four search strategies in order.
    Returns {url, title, confidence, method} or None.
    """
    query     = build_search_query(sku)
    domain    = competitor["domain"]
    clean_dom = domain.lstrip("www.")

    ctx = await new_stealth_context(browser)
    try:

        # ── Strategies 1 & 2: Bing Shopping, then Google Shopping ─────────────
        shopping_engines = [
            {
                "name": "Bing Shopping",
                "url":  f"https://www.bing.com/shop?q={quote_plus(query)}&mkt=en-GB",
            },
            {
                "name": "Google Shopping",
                "url":  f"https://www.google.com/search?tbm=shop&q={quote_plus(query)}&gl=gb&hl=en-GB&num=20",
            },
        ]

        for engine in shopping_engines:
            page = await ctx.new_page()
            try:
                log.debug(f"  {engine['name']}: '{query}' → {clean_dom}")
                await page.goto(engine["url"], wait_until="domcontentloaded", timeout=TIMEOUT_MS)
                await page.wait_for_timeout(random.uniform(2000, 3500))

                body = (await page.inner_text("body")).lower()
                if any(x in body for x in ["captcha", "unusual traffic", "i'm not a robot", "prove you're human"]):
                    log.warning(f"  {engine['name']} CAPTCHA — trying next engine")
                    await page.close()
                    continue

                results = await _extract_shopping_results(page, clean_dom)
                await page.close()

                for r in results:
                    url   = r.get("url", "")
                    title = r.get("title", "")
                    if not url or not title:
                        continue
                    conf = fuzzy_confidence(sku, title, url)
                    if conf >= 30:   # low bar — human reviews everything anyway
                        log.info(f"  ✓ {engine['name']}: '{title[:60]}' conf={conf}%")
                        return {"url": url, "title": title, "confidence": conf, "method": engine["name"].lower().replace(" ", "_")}

                log.debug(f"  {engine['name']}: no result for {clean_dom}")

            except Exception as e:
                log.debug(f"  {engine['name']} error: {e}")
                try: await page.close()
                except Exception: pass

        # ── Strategies 3 & 4: site: web search — Bing then Google ─────────────
        web_engines = [
            ("Bing web",   f"https://www.bing.com/search?q=site:{domain}+{quote_plus(query)}&count=10"),
            ("Google web", f"https://www.google.com/search?q=site:{domain}+{quote_plus(query)}&num=10"),
        ]

        for engine_name, search_url in web_engines:
            page = await ctx.new_page()
            try:
                await page.goto(search_url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
                await page.wait_for_timeout(2000)

                body = (await page.inner_text("body")).lower()
                if any(x in body for x in ["captcha", "unusual traffic", "blocked", "robot"]):
                    log.debug(f"  {engine_name} blocked")
                    await page.close()
                    continue

                candidates = await _extract_web_search_urls(page, clean_dom)
                await page.close()

                if candidates:
                    url  = candidates[0]
                    # No title from web search — use query as a placeholder; scrape.py gets the real title
                    conf = fuzzy_confidence(sku, "", url)
                    log.info(f"  ✓ {engine_name} site-search: {url[:70]} conf={conf}%")
                    return {"url": url, "title": "", "confidence": conf, "method": "site_search"}

                log.debug(f"  {engine_name}: no links for {clean_dom}")

            except Exception as e:
                log.debug(f"  {engine_name} error: {e}")
                try: await page.close()
                except Exception: pass

    finally:
        await ctx.close()

    return None


# ── DB helpers ─────────────────────────────────────────────────────────────────

def upsert_match(sb, sku_id: str, competitor_id: int, result: dict):
    sb.table("competitor_matches").upsert(
        {
            "sku_id":           sku_id,
            "competitor_id":    competitor_id,
            "competitor_url":   result["url"],
            "competitor_title": result["title"] or None,
            "match_status":     "review",
            "confidence":       result["confidence"],
            "match_method":     result["method"],
            "updated_at":       datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="sku_id,competitor_id",
    ).execute()


# ── Main runner ────────────────────────────────────────────────────────────────

async def run_discovery():
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    specific_skus = [s.strip() for s in os.getenv("DISCOVER_SKUS", "").split(",") if s.strip()]

    comps = (
        sb.table("competitors")
        .select("*")
        .eq("active", True)
        .order("id")
        .limit(COMPETITOR_LIMIT)
        .execute()
        .data
    )

    # ── Build work list: SKUs that need discovery ──────────────────────────────
    if specific_skus:
        skus = sb.table("skus").select("*").in_("sku_id", specific_skus).execute().data
    else:
        skus = (
            sb.table("skus")
            .select("*")
            .eq("active", True)
            .limit(SKU_LIMIT)
            .execute()
            .data
        )

    all_matches = sb.table("competitor_matches").select("sku_id,competitor_id,match_status").execute().data
    # Already-matched pairs (skip unless FORCE)
    matched_pairs = {
        (m["sku_id"], m["competitor_id"])
        for m in all_matches
        if m["match_status"] in ("matched", "review") and not FORCE
    }

    work_items = [
        (sku, comp)
        for sku in skus
        for comp in comps
        if (sku["sku_id"], comp["id"]) not in matched_pairs
    ]

    log.info(
        f"Discovery run | SKUs={len(skus)} competitors={len(comps)} "
        f"pairs_to_discover={len(work_items)} force={FORCE}"
    )

    if not work_items:
        log.info("Nothing to discover — all pairs already matched. Use DISCOVER_FORCE=true to re-run.")
        return

    stats = {"attempted": 0, "found": 0, "not_found": 0}
    sem   = asyncio.Semaphore(WORKERS)

    async def process_pair(sku: dict, comp: dict):
        async with sem:
            stats["attempted"] += 1
            log.info(f"  {sku['sku_id']} × {comp['domain']}")
            try:
                result = await discover_url(browser, sku, comp)
                if result:
                    upsert_match(sb, sku["sku_id"], comp["id"], result)
                    stats["found"] += 1
                else:
                    log.info(f"  ✗ {sku['sku_id']} × {comp['domain']} — no URL found")
                    stats["not_found"] += 1
            except Exception as e:
                log.error(f"  Error {sku['sku_id']} × {comp['domain']}: {e}")
                stats["not_found"] += 1
            finally:
                await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    async with async_playwright() as pw:
        browser = await launch_browser(pw)
        await asyncio.gather(*[process_pair(sku, comp) for sku, comp in work_items])
        await browser.close()

    log.info(
        f"Discovery complete — attempted={stats['attempted']} "
        f"found={stats['found']} not_found={stats['not_found']}"
    )
    log.info("Review the results at: Settings → Review Queue in the dashboard.")


if __name__ == "__main__":
    asyncio.run(run_discovery())
