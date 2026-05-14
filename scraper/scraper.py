"""
scraper/scraper.py
──────────────────
Nightly price comparison scraper for PriceWatch Pro.

Flow per SKU × competitor:
  1. Check competitor_matches for an existing confirmed URL
  2. If none, search DuckDuckGo: site:competitor.com "short_title"
  3. Fetch the best result URL and extract price + VAT + OOS
  4. Score match confidence (title similarity + dimensions + qty)
  5. Write price_snapshot
  6. After all 23 competitors done for a SKU, flush competitor_matches in one batch

Environment variables:
    SUPABASE_URL
    SUPABASE_SERVICE_KEY
    SCRAPER_WORKERS          (default: 2)
    SCRAPER_PAGE_TIMEOUT_MS  (default: 30000)
    SCRAPER_DELAY_MIN        (default: 8)
    SCRAPER_DELAY_MAX        (default: 15)
    SCRAPER_SKU_LIMIT        (default: 250)
    SCRAPER_COMPETITOR_LIMIT (default: 23)
    LOG_LEVEL                (default: INFO)
"""

import asyncio
import json
import logging
import os
import random
import re
import uuid
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote_plus

from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from supabase import create_client, Client

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("pricewatch")

SUPABASE_URL       = os.environ["SUPABASE_URL"]
SUPABASE_KEY       = os.environ["SUPABASE_SERVICE_KEY"]
WORKERS            = int(os.getenv("SCRAPER_WORKERS", "2"))
TIMEOUT_MS         = int(os.getenv("SCRAPER_PAGE_TIMEOUT_MS", "30000"))
DELAY_MIN          = float(os.getenv("SCRAPER_DELAY_MIN", "8"))
DELAY_MAX          = float(os.getenv("SCRAPER_DELAY_MAX", "15"))
SKU_LIMIT          = int(os.getenv("SCRAPER_SKU_LIMIT", "250"))
COMPETITOR_LIMIT   = int(os.getenv("SCRAPER_COMPETITOR_LIMIT", "23"))

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

VAT_INC_PATTERNS = [
    r"inc(?:l(?:uding)?)?\s*\.?\s*vat",
    r"inc\s+vat",
    r"including\s+vat",
    r"prices?\s+include\s+vat",
    r"vat\s+included",
]
VAT_EX_PATTERNS = [
    r"ex(?:cl(?:uding)?)?\s*\.?\s*vat",
    r"excl\s+vat",
    r"excluding\s+vat",
    r"\+\s*vat",
    r"prices?\s+exclude\s+vat",
    r"before\s+vat",
    r"nett\s+price",
]

PRICE_SELECTORS = [
    "[itemprop='price']",
    ".price",
    ".product-price",
    ".our-price",
    ".sale-price",
    "#product-price",
    "[class*='price']",
    "[data-price]",
    ".offer-price",
    "span.amount",
    ".woocommerce-Price-amount",
    "p.price",
    ".product__price",
]

OOS_PATTERNS = [
    r"out\s+of\s+stock",
    r"currently\s+unavailable",
    r"temporarily\s+out",
    r"sold\s+out",
    r"no\s+stock",
    r"backordered?",
    r"not\s+available",
]

# ── Stop words for title matching ──────────────────────────────────────────────
STOP_WORDS = {
    "the","a","an","and","of","for","with","in","to","self","adhesive",
    "pack","set","lot","box","bag","new","uk","free","delivery","shipping",
}


def detect_vat(page_text: str) -> str:
    text = page_text.lower()
    for pat in VAT_INC_PATTERNS:
        if re.search(pat, text):
            return "inc"
    for pat in VAT_EX_PATTERNS:
        if re.search(pat, text):
            return "ex"
    return "unknown"

def detect_oos(page_text: str) -> bool:
    return any(re.search(p, page_text.lower()) for p in OOS_PATTERNS)

def parse_price(text: str) -> Optional[float]:
    """Extract the first plausible GBP price from a string."""
    text = text.replace(",", "").replace("£", "")
    m = re.search(r"\b(\d{1,5}\.\d{2})\b", text)
    if m:
        val = float(m.group(1))
        if 0.01 < val < 99999:
            return val
    return None

def diff_pct(our_price: float, their_price: float) -> float:
    if our_price == 0:
        return 0.0
    return round(((their_price - our_price) / our_price) * 100, 2)

def normalise_price(price: float, vat_status: str) -> float:
    """Convert inc-VAT price to ex-VAT for fair comparison."""
    if vat_status == "inc":
        return round(price / 1.2, 2)
    return price

def build_search_query(sku: dict) -> str:
    """
    Build a tight search query from the SKU title.
    Strips pack quantities and keeps key descriptive tokens + dimensions.
    Example: 'A2 Pavement Sign Double Sided Water Base' → '"A2 Pavement Sign" "Water Base"'
    """
    title = sku["short_title"]

    # Extract dimension tokens — these must match
    dims = re.findall(
        r"\b(?:A[0-9]|[0-9]+(?:\.[0-9]+)?(?:cm|mm|m)|[0-9]+x[0-9]+mm?)\b",
        title, re.I
    )

    # Strip qty tokens like "x 100", "Pack of 50"
    clean = re.sub(r"\b(?:x\s*\d+|pack\s+of\s+\d+|\d+\s*pack)\b", "", title, flags=re.I)
    clean = re.sub(r"\s+", " ", clean).strip()

    # Build query: title tokens + required dimensions
    if dims:
        # Quote the cleaned title and append dimension requirement
        query = f'"{clean}" {" ".join(dims)}'
    else:
        query = f'"{clean}"'

    return query

def fuzzy_confidence(sku: dict, comp_title: str, comp_url: str) -> int:
    """
    Score 0-100. Weights:
      - Token overlap with SKU short_title:  up to 40 pts
      - Dimension match (A2, 5cm etc):       up to 30 pts
      - Pack quantity match:                 up to 20 pts
      - URL contains SKU-like terms:         up to 10 pts
    """
    score = 0
    st    = sku["short_title"].lower()
    ct    = (comp_title or "").lower()
    cu    = (comp_url or "").lower()

    # Token overlap
    s_tok = set(re.findall(r"\b[a-z0-9]{2,}\b", st)) - STOP_WORDS
    c_tok = set(re.findall(r"\b[a-z0-9]{2,}\b", ct)) - STOP_WORDS
    if s_tok:
        overlap = len(s_tok & c_tok) / len(s_tok)
        score  += int(overlap * 40)

    # Dimension match
    s_dims = set(re.findall(r"\b(?:a[0-9]|[0-9]+(?:\.[0-9]+)?(?:cm|mm)|[0-9]+x[0-9]+)\b", st, re.I))
    c_dims = set(re.findall(r"\b(?:a[0-9]|[0-9]+(?:\.[0-9]+)?(?:cm|mm)|[0-9]+x[0-9]+)\b", ct, re.I))
    u_dims = set(re.findall(r"\b(?:a[0-9]|[0-9]+(?:\.[0-9]+)?(?:cm|mm)|[0-9]+x[0-9]+)\b", cu, re.I))
    all_comp_dims = c_dims | u_dims

    if s_dims:
        if s_dims == all_comp_dims:
            score += 30
        elif s_dims & all_comp_dims:
            score += 15
        else:
            score -= 15   # dimension mismatch is a strong negative signal

    # Pack quantity
    if sku.get("unit_qty"):
        qty_str = str(sku["unit_qty"])
        if re.search(r"\b" + qty_str + r"\b", ct) or re.search(r"\b" + qty_str + r"\b", cu):
            score += 20
        else:
            score -= 5    # pack qty mismatch also penalised

    # URL keyword bonus
    key_words = [w for w in re.findall(r"\b[a-z]{4,}\b", st) if w not in STOP_WORDS][:4]
    url_hits  = sum(1 for w in key_words if w in cu)
    score    += min(10, url_hits * 3)

    return max(0, min(100, score))


class PriceScraper:
    def __init__(self, sb: Client, run_id: uuid.UUID):
        self.sb     = sb
        self.run_id = str(run_id)
        self.ua_idx = 0

    def next_ua(self) -> str:
        ua = USER_AGENTS[self.ua_idx % len(USER_AGENTS)]
        self.ua_idx += 1
        return ua

    async def new_context(self, browser: Browser) -> BrowserContext:
        return await browser.new_context(
            user_agent=self.next_ua(),
            viewport={"width": 1280, "height": 800},
            locale="en-GB",
            extra_http_headers={"Accept-Language": "en-GB,en;q=0.9"},
        )

    # ── Step 1: Find the competitor product URL via search ─────────────────────

    async def find_competitor_url(
        self, context: BrowserContext, sku: dict, competitor_domain: str
    ) -> Optional[str]:
        """
        Search DuckDuckGo for the SKU on the competitor domain.
        Returns the best matching product URL or None.
        """
        query      = build_search_query(sku)
        search_url = f"https://html.duckduckgo.com/html/?q=site:{competitor_domain}+{quote_plus(query)}"
        page       = await context.new_page()
        found_url  = None

        try:
            await page.goto(search_url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
            await page.wait_for_timeout(1000)

            # Extract result links from DuckDuckGo HTML results
            links = await page.locator("a.result__url, a.result__a, h2.result__title a").all()

            for link in links[:5]:
                href = await link.get_attribute("href")
                if not href:
                    continue
                # DuckDuckGo wraps URLs — unwrap if needed
                if "duckduckgo.com" in href:
                    m = re.search(r"[?&]uddg=([^&]+)", href)
                    if m:
                        from urllib.parse import unquote
                        href = unquote(m.group(1))
                # Only accept URLs from the target domain
                clean_domain = competitor_domain.replace("www.", "")
                if clean_domain in href and href.startswith("http"):
                    # Prefer product pages over category/search pages
                    if any(x in href for x in ["/product", "/p/", "/item", "/buy", "/shop"]):
                        found_url = href
                        break
                    elif found_url is None:
                        found_url = href

        except Exception as e:
            log.debug(f"Search failed for {sku['sku_id']} on {competitor_domain}: {e}")
        finally:
            await page.close()

        return found_url

    # ── Step 2: Scrape the competitor product page ─────────────────────────────

    async def scrape_product_page(self, context: BrowserContext, url: str) -> dict:
        page   = await context.new_page()
        result = {
            "price": None, "vat": "unknown", "availability": "in_stock",
            "title": "", "url": url, "error": None,
        }
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
            await page.wait_for_timeout(2000)   # give JS time to render prices

            full_text          = await page.inner_text("body")
            result["vat"]      = detect_vat(full_text)
            result["availability"] = "out_of_stock" if detect_oos(full_text) else "in_stock"
            result["title"]    = (await page.title()).strip()

            # JSON-LD structured data — most reliable
            price = await self._extract_jsonld_price(page)

            # Meta tags — second most reliable
            if not price:
                price = await self._extract_meta_price(page)

            # CSS selector fallback
            if not price:
                for sel in PRICE_SELECTORS:
                    try:
                        el = page.locator(sel).first
                        if await el.count() > 0:
                            raw   = await el.get_attribute("content") or await el.inner_text()
                            price = parse_price(raw)
                            if price:
                                break
                    except Exception:
                        continue

            result["price"] = price

        except Exception as e:
            result["error"]        = str(e)[:200]
            result["availability"] = "error"
        finally:
            await page.close()
        return result

    async def _extract_jsonld_price(self, page: Page) -> Optional[float]:
        scripts = await page.locator('script[type="application/ld+json"]').all()
        for script in scripts:
            try:
                data  = json.loads(await script.inner_text())
                items = data if isinstance(data, list) else [data]
                # Flatten @graph
                flat  = []
                for item in items:
                    if "@graph" in item:
                        flat.extend(item["@graph"])
                    else:
                        flat.append(item)
                for item in flat:
                    if item.get("@type") in ("Product",):
                        offers = item.get("offers", {})
                        if isinstance(offers, list):
                            offers = offers[0]
                        price = offers.get("price") or offers.get("lowPrice")
                        if price:
                            return float(str(price).replace(",", ""))
                    if item.get("@type") in ("Offer",):
                        price = item.get("price")
                        if price:
                            return float(str(price).replace(",", ""))
            except Exception:
                pass
        return None

    async def _extract_meta_price(self, page: Page) -> Optional[float]:
        for attr in ["product:price:amount", "og:price:amount"]:
            try:
                el = page.locator(f'meta[property="{attr}"]').first
                if await el.count() > 0:
                    val = await el.get_attribute("content")
                    if val:
                        return parse_price(val)
            except Exception:
                pass
        return None

    # ── Main per-SKU×competitor logic ──────────────────────────────────────────

    async def process_sku_competitor(
        self,
        browser: Browser,
        sku: dict,
        competitor: dict,
        existing_match: Optional[dict],
    ) -> dict:
        """
        Returns a snapshot dict. Also discovers the product URL if not already known.
        """
        snapshot = {
            "sku_id":              sku["sku_id"],
            "competitor_id":       competitor["id"],
            "run_id":              self.run_id,
            "scraped_at":          datetime.now(timezone.utc).isoformat(),
            "availability":        "unavailable",
            "competitor_price":    None,
            "competitor_vat":      competitor.get("vat_status", "unknown"),
            "competitor_url":      None,
            "diff_pct":            None,
            "diff_pct_normalised": None,
            "confidence":          None,
            "error_message":       None,
            "_comp_title":         None,   # internal, stripped before DB insert
        }

        # Skip if previously manually rejected
        if existing_match and existing_match["match_status"] == "rejected":
            log.debug(f"Skipping {sku['sku_id']} × {competitor['domain']} — rejected")
            return snapshot

        ctx = await self.new_context(browser)
        try:
            # ── Resolve URL ────────────────────────────────────────────────────
            url = existing_match["competitor_url"] if existing_match else None

            if not url:
                log.debug(f"Searching for {sku['sku_id']} on {competitor['domain']}")
                url = await self.find_competitor_url(ctx, sku, competitor["domain"])

                if not url:
                    log.info(f"No URL found: {sku['sku_id']} × {competitor['domain']}")
                    snapshot["error_message"] = "No URL found via search"
                    return snapshot

            snapshot["competitor_url"] = url

            # ── Scrape ─────────────────────────────────────────────────────────
            result = await self.scrape_product_page(ctx, url)
            snapshot["availability"]   = result["availability"]
            snapshot["error_message"]  = result["error"]
            snapshot["_comp_title"]    = result["title"]

            if result["vat"] != "unknown":
                snapshot["competitor_vat"] = result["vat"]

            # ── Score confidence ───────────────────────────────────────────────
            conf = fuzzy_confidence(sku, result["title"], url)
            snapshot["confidence"] = conf

            # ── Price & diff ───────────────────────────────────────────────────
            if result["price"]:
                our_price                       = float(sku["price_ex_vat"])
                their_price_ex                  = normalise_price(result["price"], snapshot["competitor_vat"])
                snapshot["competitor_price"]    = result["price"]
                snapshot["diff_pct"]            = diff_pct(our_price, result["price"])
                snapshot["diff_pct_normalised"] = diff_pct(our_price, their_price_ex)

                log.info(
                    f"  {competitor['domain']:35s} £{result['price']:>7.2f} "
                    f"({snapshot['competitor_vat']:7s}) "
                    f"diff {snapshot['diff_pct_normalised']:+.1f}%  "
                    f"conf {conf}%  {result['title'][:50]}"
                )
            else:
                log.info(
                    f"  {competitor['domain']:35s} no price found  "
                    f"conf {conf}%  {result['title'][:50]}"
                )

        except Exception as e:
            snapshot["error_message"] = str(e)[:200]
            log.error(f"Exception {sku['sku_id']} × {competitor['domain']}: {e}")
        finally:
            await ctx.close()

        return snapshot

    # ── DB writes ──────────────────────────────────────────────────────────────

    async def write_snapshot(self, snapshot: dict):
        row = {k: v for k, v in snapshot.items() if not k.startswith("_")}
        self.sb.table("price_snapshots").insert(row).execute()

    async def flush_matches_for_sku(
        self, sku: dict, snapshots: list[dict], competitors: list[dict]
    ):
        """
        Batch-upsert competitor_matches after all 23 competitors processed for one SKU.
        Only inserts rows where we found a URL (regardless of whether a price was found).
        Confidence determines matched vs review status.
        """
        comp_map = {c["id"]: c for c in competitors}
        rows     = []

        for snap in snapshots:
            if not snap.get("competitor_url"):
                continue   # no URL found — nothing to record

            conf         = snap.get("confidence") or 0
            match_status = "matched" if conf >= 80 else "review"
            comp         = comp_map.get(snap["competitor_id"], {})

            rows.append({
                "sku_id":           sku["sku_id"],
                "competitor_id":    snap["competitor_id"],
                "competitor_url":   snap["competitor_url"],
                "competitor_title": snap.get("_comp_title"),
                "match_status":     match_status,
                "confidence":       conf,
                "match_method":     "scrape",
                "updated_at":       datetime.now(timezone.utc).isoformat(),
            })

        if rows:
            self.sb.table("competitor_matches").upsert(
                rows, on_conflict="sku_id,competitor_id"
            ).execute()
            matched = sum(1 for r in rows if r["match_status"] == "matched")
            review  = sum(1 for r in rows if r["match_status"] == "review")
            log.info(
                f"Flushed {len(rows)} matches for {sku['sku_id']} "
                f"— {matched} matched, {review} review, "
                f"{len(snapshots) - len(rows)} no URL"
            )
        else:
            log.info(f"No matches to flush for {sku['sku_id']}")

    async def create_alerts(self, snapshot: dict, sku: dict, competitor: dict):
        our_price = float(sku["price_ex_vat"])
        diff      = snapshot.get("diff_pct_normalised") or snapshot.get("diff_pct")
        alerts    = []

        if snapshot["availability"] == "out_of_stock":
            alerts.append({
                "run_id":        self.run_id,
                "sku_id":        sku["sku_id"],
                "competitor_id": competitor["id"],
                "alert_type":    "oos_competitor",
                "message":       f"{competitor['name']} is out of stock for {sku['short_title']} — last known £{snapshot.get('competitor_price', '?')}",
                "diff_pct":      diff,
                "our_price":     our_price,
                "their_price":   snapshot.get("competitor_price"),
            })
        elif snapshot["availability"] == "unavailable":
            pass   # no alert for simply not found
        elif diff is not None:
            if diff <= -10:
                alerts.append({
                    "run_id":        self.run_id,
                    "sku_id":        sku["sku_id"],
                    "competitor_id": competitor["id"],
                    "alert_type":    "critical",
                    "message":       f"{competitor['name']} is {abs(diff):.1f}% cheaper — £{snapshot['competitor_price']:.2f} vs your £{our_price:.2f}",
                    "diff_pct":      diff,
                    "our_price":     our_price,
                    "their_price":   snapshot.get("competitor_price"),
                })
            elif diff <= -5:
                alerts.append({
                    "run_id":        self.run_id,
                    "sku_id":        sku["sku_id"],
                    "competitor_id": competitor["id"],
                    "alert_type":    "warning",
                    "message":       f"{competitor['name']} is {abs(diff):.1f}% cheaper — £{snapshot['competitor_price']:.2f} vs your £{our_price:.2f}",
                    "diff_pct":      diff,
                    "our_price":     our_price,
                    "their_price":   snapshot.get("competitor_price"),
                })

        for alert in alerts:
            self.sb.table("alerts").insert(alert).execute()


# ── Main runner ────────────────────────────────────────────────────────────────

async def run_scraper(trigger: str = "scheduled"):
    sb     = create_client(SUPABASE_URL, SUPABASE_KEY)
    run_id = uuid.uuid4()

    sb.table("sync_runs").insert({
        "id":         str(run_id),
        "trigger":    trigger,
        "status":     "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }).execute()

    log.info(
        f"Starting sync run {run_id} | "
        f"workers={WORKERS} delay={DELAY_MIN}–{DELAY_MAX}s | "
        f"SKUs={SKU_LIMIT} competitors={COMPETITOR_LIMIT}"
    )

    skus  = (
        sb.table("skus").select("*").eq("active", True)
        .limit(SKU_LIMIT).execute().data
    )
    comps = (
        sb.table("competitors").select("*").eq("active", True)
        .order("id").limit(COMPETITOR_LIMIT).execute().data
    )

    log.info(f"Loaded {len(skus)} SKUs, {len(comps)} competitors")

    # Build match lookup: (sku_id, competitor_id) → match row
    match_map = {
        (m["sku_id"], m["competitor_id"]): m
        for m in sb.table("competitor_matches").select("*").execute().data
    }

    scraper = PriceScraper(sb, run_id)
    stats   = {"attempted": 0, "succeeded": 0, "failed": 0, "oos": 0}
    sem     = asyncio.Semaphore(WORKERS)

    async def process_sku(sku: dict):
        async with sem:
            log.info(f"\n{'='*60}\nSKU {sku['sku_id']} — {sku['short_title']}\n{'='*60}")
            sku_snapshots = []

            for comp in comps:
                existing = match_map.get((sku["sku_id"], comp["id"]))
                stats["attempted"] += 1

                try:
                    snap = await scraper.process_sku_competitor(browser, sku, comp, existing)
                    await scraper.write_snapshot(snap)
                    await scraper.create_alerts(snap, sku, comp)
                    sku_snapshots.append(snap)

                    if snap["availability"] == "error":
                        stats["failed"] += 1
                    else:
                        stats["succeeded"] += 1
                        if snap["availability"] == "out_of_stock":
                            stats["oos"] += 1

                except Exception as e:
                    stats["failed"] += 1
                    log.error(f"Unhandled: {sku['sku_id']} × {comp['domain']}: {e}")

                finally:
                    delay = random.uniform(DELAY_MIN, DELAY_MAX)
                    await asyncio.sleep(delay)

            # Flush all matches for this SKU in one DB write
            await scraper.flush_matches_for_sku(sku, sku_snapshots, comps)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        await asyncio.gather(*[process_sku(sku) for sku in skus])
        await browser.close()

    sb.table("sync_runs").update({
        "status":         "complete",
        "completed_at":   datetime.now(timezone.utc).isoformat(),
        "skus_attempted": stats["attempted"],
        "skus_succeeded": stats["succeeded"],
        "skus_failed":    stats["failed"],
        "oos_flagged":    stats["oos"],
    }).eq("id", str(run_id)).execute()

    log.info(f"Run {run_id} complete — {stats}")


if __name__ == "__main__":
    import sys
    trigger = sys.argv[1] if len(sys.argv) > 1 else "scheduled"
    asyncio.run(run_scraper(trigger))
