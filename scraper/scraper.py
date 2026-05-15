"""
scraper/scraper.py
──────────────────
Nightly price comparison scraper for PriceWatch Pro.

Matching strategy (in priority order per SKU × competitor):
  1. Existing confirmed URL in competitor_matches → scrape directly
  2. Google Shopping search → find competitor's listing, extract price + URL
  3. Google Web search (site:competitor.com product name) → scrape that page
  4. Bing Web search fallback if Google blocks

Google Shopping is the primary discovery mechanism because:
  - Returns structured price + title + merchant data without visiting the page
  - Surfaces the exact product the competitor is selling for a given search term
  - Price is often extractable directly from the SERP (no page visit needed)
  - Naturally handles synonyms ("snap frame" / "click frame" / "poster frame")

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

STOP_WORDS = {
    "the","a","an","and","of","for","with","in","to","self","adhesive",
    "pack","set","lot","box","bag","new","uk","free","delivery","shipping",
    "holders","holder","signs","sign","displays","display",
}


# ── Query building ─────────────────────────────────────────────────────────────

def build_search_query(sku: dict) -> str:
    """
    Build a natural-language search query from the SKU short title.
    Never uses the SKU ID. Strips pack quantities, keeps everything else.

    "Spring Hinge Sign Holders x 100"  → "Spring Hinge Sign Holders"
    "A2 Pavement Sign Double Sided"    → "A2 Pavement Sign Double Sided"
    "Snap Frame A1 Silver 25mm"        → "Snap Frame A1 Silver 25mm"
    """
    title = sku["short_title"]
    clean = re.sub(r"\bx\s*\d+\b", "", title, flags=re.I)
    clean = re.sub(r"\bpack\s+of\s+\d+\b", "", clean, flags=re.I)
    clean = re.sub(r"\b\d+\s*pack\b", "", clean, flags=re.I)
    clean = re.sub(r"\s+", " ", clean).strip().rstrip("-—–").strip()
    return clean


# ── Confidence scoring ─────────────────────────────────────────────────────────

def fuzzy_confidence(sku: dict, comp_title: str, comp_url: str) -> int:
    """
    Score 0-100 for how well a competitor listing matches our SKU.

    Token overlap:      up to 40 pts
    Dimension match:    up to 30 pts  (penalty for mismatch)
    Pack qty match:     up to 20 pts  (penalty for mismatch)
    URL keyword hits:   up to 10 pts
    """
    score = 0
    st    = sku["short_title"].lower()
    ct    = (comp_title or "").lower()
    cu    = (comp_url or "").lower()

    # Token overlap
    s_tok = set(re.findall(r"\b[a-z0-9]{2,}\b", st)) - STOP_WORDS
    c_tok = set(re.findall(r"\b[a-z0-9]{2,}\b", ct)) - STOP_WORDS
    if s_tok:
        score += int((len(s_tok & c_tok) / len(s_tok)) * 40)

    # Dimension match
    dim_pat = r"\b(?:a[0-9]|[0-9]+(?:\.[0-9]+)?(?:cm|mm)|[0-9]+x[0-9]+(?:mm)?)\b"
    s_dims  = set(re.findall(dim_pat, st, re.I))
    c_dims  = set(re.findall(dim_pat, ct, re.I)) | set(re.findall(dim_pat, cu, re.I))
    if s_dims:
        if s_dims == c_dims:          score += 30
        elif s_dims & c_dims:         score += 15
        elif c_dims:                  score -= 15  # different size — wrong product

    # Pack quantity
    if sku.get("unit_qty"):
        qty = str(sku["unit_qty"])
        if re.search(r"\b" + qty + r"\b", ct) or re.search(r"\b" + qty + r"\b", cu):
            score += 20
        else:
            other = re.search(r"\b(x?\s*\d{2,4})\b", ct)
            if other and other.group(0).strip("x ") != qty:
                score -= 10

    # URL keyword hits
    key_words = [w for w in re.findall(r"\b[a-z]{4,}\b", st) if w not in STOP_WORDS][:5]
    score += min(10, sum(1 for w in key_words if w in cu) * 2)

    return max(0, min(100, score))


# ── Detection helpers ─────────────────────────────────────────────────────────

def detect_vat(text: str) -> str:
    t = text.lower()
    if any(re.search(p, t) for p in VAT_INC_PATTERNS): return "inc"
    if any(re.search(p, t) for p in VAT_EX_PATTERNS):  return "ex"
    return "unknown"

def detect_oos(text: str) -> bool:
    return any(re.search(p, text.lower()) for p in OOS_PATTERNS)

def parse_price(text: str) -> Optional[float]:
    t = text.replace(",", "").replace("£", "").strip()
    m = re.search(r"\b(\d{1,5}\.\d{2})\b", t)
    if m:
        val = float(m.group(1))
        if 0.01 < val < 99999:
            return val
    return None

def diff_pct(our: float, their: float) -> float:
    return round(((their - our) / our) * 100, 2) if our else 0.0

def normalise_price(price: float, vat: str) -> float:
    return round(price / 1.2, 2) if vat == "inc" else price


# ── Scraper class ──────────────────────────────────────────────────────────────

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

    # ── Strategy 1: Google Shopping ───────────────────────────────────────────

    async def search_google_shopping(
        self, context: BrowserContext, sku: dict, competitor_domain: str
    ) -> Optional[dict]:
        """
        Search Google Shopping for the product and find the competitor's listing.
        Returns dict with url, price, title, vat_hint if found — or None.

        Google Shopping URL format:
          https://www.google.com/search?tbm=shop&q=QUERY&gl=gb&hl=en-GB
        """
        query      = build_search_query(sku)
        search_url = (
            f"https://www.google.com/search?tbm=shop"
            f"&q={quote_plus(query)}"
            f"&gl=gb&hl=en-GB"
            f"&num=20"
        )
        clean_dom  = competitor_domain.lstrip("www.")
        page       = await context.new_page()

        try:
            log.debug(f"  GShop: '{query}' looking for {clean_dom}")
            await page.goto(search_url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
            await page.wait_for_timeout(2000)

            body_text = await page.inner_text("body")

            # Check for CAPTCHA
            if any(x in body_text.lower() for x in ["captcha", "unusual traffic", "i'm not a robot"]):
                log.warning(f"  Google Shopping CAPTCHA hit for '{query}'")
                await page.close()
                return None

            # Extract all shopping result blocks
            # Each result is typically in a div with class containing 'sh-dgr__content'
            # or similar — use JS to extract structured data
            results = await page.evaluate("""
                () => {
                    const items = [];
                    // Try multiple Google Shopping result selectors (layout changes frequently)
                    const selectors = [
                        '.sh-dgr__content',
                        '.sh-pr__product-results .g',
                        '[data-docid]',
                        '.KZmu8e',
                        '.Lq5OHe',
                    ];
                    let cards = [];
                    for (const sel of selectors) {
                        cards = document.querySelectorAll(sel);
                        if (cards.length > 0) break;
                    }
                    cards.forEach(card => {
                        const text  = card.innerText || '';
                        const links = Array.from(card.querySelectorAll('a[href]'))
                                          .map(a => a.href);
                        const title = card.querySelector('h3, h4, [role=heading]')?.innerText || '';
                        // Price: look for £ patterns
                        const priceMatch = text.match(/£\\s?([\\d,]+\\.?\\d*)/);
                        const price = priceMatch ? parseFloat(priceMatch[1].replace(',','')) : null;
                        items.push({ text, links, title, price });
                    });
                    return items;
                }
            """)

            # Find the result that belongs to our target competitor
            for result in results:
                links = result.get("links", [])
                title = result.get("title", "")
                price = result.get("price")

                # Check if any link belongs to our competitor
                comp_link = next(
                    (l for l in links if clean_dom in l and "google" not in l),
                    None
                )

                if not comp_link:
                    # Also check the text for the domain name
                    if clean_dom not in result.get("text", "").lower():
                        continue

                # Score this result
                conf = fuzzy_confidence(sku, title, comp_link or "")

                if conf >= 40:   # minimum threshold to consider a Shopping result valid
                    log.debug(
                        f"  GShop match: '{title[:50]}' "
                        f"£{price} conf={conf}% url={comp_link}"
                    )
                    return {
                        "url":       comp_link,
                        "price":     price,
                        "title":     title,
                        "confidence": conf,
                        "vat_hint":  detect_vat(result.get("text", "")),
                    }

            log.debug(f"  GShop: no result for {clean_dom} in Shopping results")
            return None

        except Exception as e:
            log.debug(f"  GShop error for {clean_dom}: {e}")
            return None
        finally:
            await page.close()

    # ── Strategy 2: Google/Bing site search ───────────────────────────────────

    async def search_web(
        self, context: BrowserContext, sku: dict, competitor_domain: str
    ) -> Optional[str]:
        """
        Fall back to a site: web search if Google Shopping didn't find the competitor.
        Tries Google then Bing.
        """
        query      = build_search_query(sku)
        clean_dom  = competitor_domain.lstrip("www.")

        search_engines = [
            f"https://www.google.com/search?q=site:{competitor_domain}+{quote_plus(query)}&num=10",
            f"https://www.bing.com/search?q=site:{competitor_domain}+{quote_plus(query)}&count=10",
        ]

        for search_url in search_engines:
            engine = "Google" if "google" in search_url else "Bing"
            page   = await context.new_page()
            try:
                await page.goto(search_url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
                await page.wait_for_timeout(2000)

                body = (await page.inner_text("body")).lower()
                if any(x in body for x in ["captcha", "unusual traffic", "blocked", "robot"]):
                    log.debug(f"  {engine} blocked for {competitor_domain}")
                    await page.close()
                    continue

                all_links = await page.evaluate("""
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

                # Prefer product-looking URLs
                product_urls = [
                    u for u in domain_links
                    if any(x in u.lower() for x in [
                        "/product", "/p/", "/item", "/buy", "/shop",
                        ".html", "/signs", "/display", "/frame", "/holder",
                        "/pavement", "/snap", "/poster", "/board", "/sign",
                    ])
                ]

                best = product_urls[0] if product_urls else (domain_links[0] if domain_links else None)
                await page.close()
                if best:
                    log.debug(f"  {engine} site search found: {best}")
                    return best

            except Exception as e:
                log.debug(f"  {engine} error: {e}")
                try: await page.close()
                except Exception: pass

        return None

    # ── Strategy 3: Scrape a known product page ───────────────────────────────

    async def scrape_product_page(self, context: BrowserContext, url: str) -> dict:
        page   = await context.new_page()
        result = {
            "price": None, "vat": "unknown", "availability": "in_stock",
            "title": "", "url": url, "error": None,
        }
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
            await page.wait_for_timeout(2000)

            full_text              = await page.inner_text("body")
            result["vat"]          = detect_vat(full_text)
            result["availability"] = "out_of_stock" if detect_oos(full_text) else "in_stock"
            result["title"]        = (await page.title()).strip()

            price = await self._extract_jsonld_price(page)
            if not price: price = await self._extract_meta_price(page)
            if not price:
                for sel in PRICE_SELECTORS:
                    try:
                        el = page.locator(sel).first
                        if await el.count() > 0:
                            raw   = await el.get_attribute("content") or await el.inner_text()
                            price = parse_price(raw)
                            if price: break
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

    async def _extract_meta_price(self, page: Page) -> Optional[float]:
        for attr in ["product:price:amount", "og:price:amount"]:
            try:
                el = page.locator(f'meta[property="{attr}"]').first
                if await el.count() > 0:
                    val = await el.get_attribute("content")
                    if val: return parse_price(val)
            except Exception:
                pass
        return None

    # ── Main per-SKU × competitor logic ───────────────────────────────────────

    async def process_sku_competitor(
        self,
        browser: Browser,
        sku: dict,
        competitor: dict,
        existing_match: Optional[dict],
    ) -> dict:
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
            "_comp_title":         None,
        }

        if existing_match and existing_match["match_status"] == "rejected":
            log.debug(f"Skipping {sku['sku_id']} × {competitor['domain']} — rejected")
            return snapshot

        ctx = await self.new_context(browser)
        try:
            url        = existing_match.get("competitor_url") if existing_match else None
            price      = None
            comp_title = None
            vat_hint   = "unknown"
            confidence = 0

            # ── Path A: existing confirmed URL — scrape directly ───────────────
            if url:
                log.debug(f"  Using existing URL: {url}")
                result     = await self.scrape_product_page(ctx, url)
                price      = result["price"]
                comp_title = result["title"]
                vat_hint   = result["vat"]
                confidence = fuzzy_confidence(sku, comp_title, url)
                snapshot["availability"] = result["availability"]
                snapshot["error_message"] = result["error"]

            else:
                # ── Path B: Google Shopping ────────────────────────────────────
                shopping = await self.search_google_shopping(ctx, sku, competitor["domain"])

                if shopping and shopping.get("url"):
                    url        = shopping["url"]
                    comp_title = shopping.get("title", "")
                    confidence = shopping.get("confidence", 0)

                    if shopping.get("price"):
                        # Price found directly from Shopping SERP — no page visit needed
                        price    = shopping["price"]
                        vat_hint = shopping.get("vat_hint", "unknown")
                        snapshot["availability"] = "in_stock"
                        log.debug(f"  GShop price extracted directly: £{price}")
                    else:
                        # Found URL but no price in SERP — visit the page
                        result     = await self.scrape_product_page(ctx, url)
                        price      = result["price"]
                        vat_hint   = result["vat"]
                        comp_title = result["title"] or comp_title
                        confidence = max(confidence, fuzzy_confidence(sku, comp_title, url))
                        snapshot["availability"]  = result["availability"]
                        snapshot["error_message"] = result["error"]

                else:
                    # ── Path C: Site web search fallback ──────────────────────
                    url = await self.search_web(ctx, sku, competitor["domain"])

                    if url:
                        result     = await self.scrape_product_page(ctx, url)
                        price      = result["price"]
                        comp_title = result["title"]
                        vat_hint   = result["vat"]
                        confidence = fuzzy_confidence(sku, comp_title, url)
                        snapshot["availability"]  = result["availability"]
                        snapshot["error_message"] = result["error"]
                    else:
                        snapshot["error_message"] = "No URL found via any method"
                        return snapshot

            # ── Populate snapshot ──────────────────────────────────────────────
            snapshot["competitor_url"] = url
            snapshot["confidence"]     = confidence
            snapshot["_comp_title"]    = comp_title

            if vat_hint != "unknown":
                snapshot["competitor_vat"] = vat_hint

            if price:
                our_price                       = float(sku["price_ex_vat"])
                their_ex                        = normalise_price(price, snapshot["competitor_vat"])
                snapshot["competitor_price"]    = price
                snapshot["diff_pct"]            = diff_pct(our_price, price)
                snapshot["diff_pct_normalised"] = diff_pct(our_price, their_ex)

                log.info(
                    f"  ✓ {competitor['domain']:35s} "
                    f"£{price:>7.2f} ({snapshot['competitor_vat']:7s}) "
                    f"diff {snapshot['diff_pct_normalised']:+.1f}%  "
                    f"conf {confidence}%"
                )
            else:
                log.info(
                    f"  ✗ {competitor['domain']:35s} "
                    f"no price  conf {confidence}%  "
                    f"'{(comp_title or '')[:50]}'"
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
        self, sku: dict, snapshots: list, competitors: list
    ):
        """
        Batch-upsert competitor_matches after all competitors processed for one SKU.
        Writes a row for every snapshot that has a URL.
        confidence >= 80 → matched (auto-approved)
        confidence < 80  → review (human review queue)
        """
        comp_map = {c["id"]: c for c in competitors}
        rows     = []

        for snap in snapshots:
            if not snap.get("competitor_url"):
                continue

            conf         = snap.get("confidence") or 0
            match_status = "matched" if conf >= 80 else "review"

            rows.append({
                "sku_id":           sku["sku_id"],
                "competitor_id":    snap["competitor_id"],
                "competitor_url":   snap["competitor_url"],
                "competitor_title": snap.get("_comp_title"),
                "match_status":     match_status,
                "confidence":       conf,
                "match_method":     "google_shopping" if snap.get("_source") == "shopping" else "scrape",
                "updated_at":       datetime.now(timezone.utc).isoformat(),
            })

        if rows:
            self.sb.table("competitor_matches").upsert(
                rows, on_conflict="sku_id,competitor_id"
            ).execute()
            matched = sum(1 for r in rows if r["match_status"] == "matched")
            review  = sum(1 for r in rows if r["match_status"] == "review")
            log.info(
                f"  → Flushed {len(rows)} matches for {sku['sku_id']}: "
                f"{matched} matched, {review} review, "
                f"{len(snapshots)-len(rows)} no URL found"
            )
        else:
            log.info(f"  → No matches flushed for {sku['sku_id']}")

    async def create_alerts(self, snapshot: dict, sku: dict, competitor: dict):
        our_price = float(sku["price_ex_vat"])
        diff      = snapshot.get("diff_pct_normalised") or snapshot.get("diff_pct")
        alerts    = []

        if snapshot["availability"] == "out_of_stock":
            alerts.append({
                "run_id":        self.run_id, "sku_id": sku["sku_id"],
                "competitor_id": competitor["id"], "alert_type": "oos_competitor",
                "message":       f"{competitor['name']} is out of stock for {sku['short_title']} — last known £{snapshot.get('competitor_price','?')}",
                "diff_pct":      diff, "our_price": our_price,
                "their_price":   snapshot.get("competitor_price"),
            })
        elif diff is not None:
            if diff <= -10:
                alerts.append({
                    "run_id":        self.run_id, "sku_id": sku["sku_id"],
                    "competitor_id": competitor["id"], "alert_type": "critical",
                    "message":       f"{competitor['name']} is {abs(diff):.1f}% cheaper — £{snapshot['competitor_price']:.2f} vs your £{our_price:.2f}",
                    "diff_pct":      diff, "our_price": our_price,
                    "their_price":   snapshot.get("competitor_price"),
                })
            elif diff <= -5:
                alerts.append({
                    "run_id":        self.run_id, "sku_id": sku["sku_id"],
                    "competitor_id": competitor["id"], "alert_type": "warning",
                    "message":       f"{competitor['name']} is {abs(diff):.1f}% cheaper — £{snapshot['competitor_price']:.2f} vs your £{our_price:.2f}",
                    "diff_pct":      diff, "our_price": our_price,
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
        sb.table("skus").select("*")
        .eq("active", True).limit(SKU_LIMIT).execute().data
    )
    comps = (
        sb.table("competitors").select("*")
        .eq("active", True).order("id").limit(COMPETITOR_LIMIT).execute().data
    )

    log.info(f"Loaded {len(skus)} SKUs, {len(comps)} competitors")

    match_map = {
        (m["sku_id"], m["competitor_id"]): m
        for m in sb.table("competitor_matches").select("*").execute().data
    }

    scraper = PriceScraper(sb, run_id)
    stats   = {"attempted": 0, "succeeded": 0, "failed": 0, "oos": 0}
    sem     = asyncio.Semaphore(WORKERS)

    async def process_sku(sku: dict):
        async with sem:
            log.info(
                f"\n{'='*60}\n"
                f"{sku['sku_id']} — {sku['short_title']}\n"
                f"Search query: '{build_search_query(sku)}'\n"
                f"{'='*60}"
            )
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
                    await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

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
