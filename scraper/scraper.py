"""
scraper/scraper.py
──────────────────
Nightly price comparison scraper for PriceWatch Pro.

Matching strategy (in priority order per SKU × competitor):
  1. Existing confirmed URL in competitor_matches → scrape directly
  2. Google Shopping search → find competitor's listing, extract price + URL
  3. Google Web search (site:competitor.com product name) → scrape that page
  4. Bing Web search fallback if Google blocks

Per-competitor special handling:
  - harrisonproducts.com  → BigCommerce SKU lookup via ?sku= parameter
  - discountdisplays.co.uk → x-html="getFormattedBasePrice()" span selector

Environment variables:
    SUPABASE_URL
    SUPABASE_SERVICE_KEY
    SCRAPER_WORKERS          (default: 2)
    SCRAPER_PAGE_TIMEOUT_MS  (default: 30000)
    SCRAPER_DELAY_MIN        (default: 8)
    SCRAPER_DELAY_MAX        (default: 15)
    SCRAPER_SKU_LIMIT        (default: 250)
    SCRAPER_COMPETITOR_LIMIT (default: 23)
    SCRAPER_MODE             (default: matched) — matched | full | skus
    SCRAPER_SKUS             comma-separated SKU IDs (mode=skus only)
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

import httpx
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
SCRAPER_MODE       = os.getenv("SCRAPER_MODE", "matched")  # matched | full | skus

# Competitor domains with special handling
BIGCOMMERCE_DOMAINS = {"harrisonproducts.com"}
DISCOUNT_DISPLAYS_DOMAIN = "discountdisplays.co.uk"

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
    title = sku["short_title"]
    clean = re.sub(r"\bx\s*\d+\b", "", title, flags=re.I)
    clean = re.sub(r"\bpack\s+of\s+\d+\b", "", clean, flags=re.I)
    clean = re.sub(r"\b\d+\s*pack\b", "", clean, flags=re.I)
    clean = re.sub(r"\s+", " ", clean).strip().rstrip("-—–").strip()
    return clean


# ── Confidence scoring ─────────────────────────────────────────────────────────

def fuzzy_confidence(sku: dict, comp_title: str, comp_url: str) -> int:
    score = 0
    st    = sku["short_title"].lower()
    ct    = (comp_title or "").lower()
    cu    = (comp_url or "").lower()

    s_tok = set(re.findall(r"\b[a-z0-9]{2,}\b", st)) - STOP_WORDS
    c_tok = set(re.findall(r"\b[a-z0-9]{2,}\b", ct)) - STOP_WORDS
    if s_tok:
        score += int((len(s_tok & c_tok) / len(s_tok)) * 40)

    dim_pat = r"\b(?:a[0-9]|[0-9]+(?:\.[0-9]+)?(?:cm|mm)|[0-9]+x[0-9]+(?:mm)?)\b"
    s_dims  = set(re.findall(dim_pat, st, re.I))
    c_dims  = set(re.findall(dim_pat, ct, re.I)) | set(re.findall(dim_pat, cu, re.I))
    if s_dims:
        if s_dims == c_dims:          score += 30
        elif s_dims & c_dims:         score += 15
        elif c_dims:                  score -= 15

    if sku.get("unit_qty"):
        qty = str(sku["unit_qty"])
        if re.search(r"\b" + qty + r"\b", ct) or re.search(r"\b" + qty + r"\b", cu):
            score += 20
        else:
            other = re.search(r"\b(x?\s*\d{2,4})\b", ct)
            if other and other.group(0).strip("x ") != qty:
                score -= 10

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

# Pack quantity patterns — matches "x 100", "x100", "pack of 50", "50 pack", "pack of 1" etc.
PACK_QTY_PATTERNS = [
    r"\bx\s*(\d+)\b",
    r"\bpack\s+of\s+(\d+)\b",
    r"\b(\d+)\s*pack\b",
    r"\bset\s+of\s+(\d+)\b",
    r"\bbox\s+of\s+(\d+)\b",
    r"\bbag\s+of\s+(\d+)\b",
    r"\bper\s+(\d+)\b",
    r"\bqty\s*[:\-]?\s*(\d+)\b",
    r"\b(\d+)\s*x\b",
]

def extract_pack_qty(title: str) -> Optional[int]:
    """Extract pack quantity from a product title. Returns None if not found or qty=1."""
    if not title:
        return None
    t = title.lower()
    for pattern in PACK_QTY_PATTERNS:
        m = re.search(pattern, t, re.I)
        if m:
            qty = int(m.group(1))
            if 2 <= qty <= 10000:  # sanity bounds
                return qty
    return None

def per_unit_price(price: float, qty: Optional[int]) -> float:
    """Return price per single unit. If qty is None or 1, returns price unchanged."""
    if qty and qty > 1:
        return round(price / qty, 6)
    return price

IMAGE_REFRESH_DAYS = int(os.getenv("IMAGE_REFRESH_DAYS", "90"))

def image_needs_refresh(existing_match: Optional[dict]) -> bool:
    """True if match has no competitor image, or image is older than IMAGE_REFRESH_DAYS."""
    if not existing_match:
        return True
    if not existing_match.get("competitor_image_url"):
        return True
    updated = existing_match.get("updated_at")
    if not updated:
        return True
    try:
        last = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - last).days >= IMAGE_REFRESH_DAYS
    except Exception:
        return True


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
        ctx = await browser.new_context(
            user_agent=self.next_ua(),
            viewport={"width": 1280, "height": 800},
            locale="en-GB",
            extra_http_headers={
                "Accept-Language": "en-GB,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
            },
        )
        await ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-GB', 'en'] });
            window.chrome = { runtime: {} };
        """)
        return ctx

    # ── Strategy 1: Bing Shopping + Google Shopping ───────────────────────────

    async def search_google_shopping(
        self, context: BrowserContext, sku: dict, competitor_domain: str
    ) -> Optional[dict]:
        query     = build_search_query(sku)
        clean_dom = competitor_domain.lstrip("www.")

        shopping_engines = [
            {"name": "Bing Shopping",   "url": f"https://www.bing.com/shop?q={quote_plus(query)}&mkt=en-GB"},
            {"name": "Google Shopping", "url": f"https://www.google.com/search?tbm=shop&q={quote_plus(query)}&gl=gb&hl=en-GB&num=20"},
        ]

        for engine in shopping_engines:
            search_url  = engine["url"]
            engine_name = engine["name"]
            page        = await context.new_page()

            try:
                log.debug(f"  {engine_name}: '{query}' looking for {clean_dom}")
                await page.goto(search_url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
                await page.wait_for_timeout(random.uniform(2000, 3500))

                body_text = await page.inner_text("body")
                if any(x in body_text.lower() for x in ["captcha", "unusual traffic", "i'm not a robot", "prove you're human"]):
                    log.warning(f"  {engine_name} CAPTCHA — trying next engine")
                    await page.close()
                    continue

                results = await page.evaluate(r"""
                    () => {
                        const items = [];
                        const selectors = [
                            '.br-item', '.br-pdItem', '.pa_item', '.rl_product',
                            '.sh-dgr__content', '[data-docid]', '.KZmu8e', '.Lq5OHe',
                            'li[data-idx]', '.product-card',
                        ];
                        let cards = [];
                        for (const sel of selectors) {
                            cards = document.querySelectorAll(sel);
                            if (cards.length > 2) break;
                        }
                        if (cards.length === 0) {
                            const seen = new Set();
                            document.querySelectorAll('a[href]').forEach(a => {
                                const block = a.closest('div,li,article');
                                const text  = block?.innerText || '';
                                if (/£[\s\d]/.test(text) && !seen.has(text.slice(0,40))) {
                                    seen.add(text.slice(0,40));
                                    if (block) cards = [...cards, block];
                                }
                            });
                        }
                        cards.forEach(card => {
                            if (!card) return;
                            const text  = card.innerText || '';
                            const links = Array.from(card.querySelectorAll('a[href]'))
                                              .map(a => a.href)
                                              .filter(h => h.startsWith('http'));
                            const title = (card.querySelector('h3,h4,h2,[role=heading],.title,.name')?.innerText || '').trim();
                            const priceMatch = text.match(/£\s?([\d,]+\.?\d*)/);
                            const price = priceMatch ? parseFloat(priceMatch[1].replace(',','')) : null;
                            if (links.length > 0 || price) items.push({ text, links, title, price });
                        });
                        return items;
                    }
                """)

                await page.close()

                for result in results:
                    links     = result.get("links", [])
                    title     = result.get("title", "")
                    price     = result.get("price")
                    comp_link = next((l for l in links if clean_dom in l and "google" not in l and "bing" not in l), None)
                    if not comp_link:
                        if clean_dom not in result.get("text", "").lower():
                            continue
                    conf = fuzzy_confidence(sku, title, comp_link or "")
                    if conf >= 40:
                        log.debug(f"  {engine_name} match: '{title[:50]}' £{price} conf={conf}%")
                        return {"url": comp_link, "price": price, "title": title, "confidence": conf, "vat_hint": detect_vat(result.get("text", ""))}

                log.debug(f"  {engine_name}: no matching result for {clean_dom}")

            except Exception as e:
                log.debug(f"  {engine_name} error for {clean_dom}: {e}")
                try: await page.close()
                except Exception: pass

        log.debug(f"  Shopping: no result for {clean_dom} via any engine")
        return None

    # ── Strategy 2: Google/Bing site search ───────────────────────────────────

    async def search_web(self, context: BrowserContext, sku: dict, competitor_domain: str) -> Optional[str]:
        query     = build_search_query(sku)
        clean_dom = competitor_domain.lstrip("www.")

        for search_url in [
            f"https://www.google.com/search?q=site:{competitor_domain}+{quote_plus(query)}&num=10",
            f"https://www.bing.com/search?q=site:{competitor_domain}+{quote_plus(query)}&count=10",
        ]:
            engine = "Google" if "google" in search_url else "Bing"
            page   = await context.new_page()
            try:
                await page.goto(search_url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
                await page.wait_for_timeout(2000)
                body = (await page.inner_text("body")).lower()
                if any(x in body for x in ["captcha", "unusual traffic", "blocked", "robot"]):
                    await page.close(); continue

                all_links = await page.evaluate("() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href).filter(h => h.startsWith('http'))")
                domain_links = [u for u in all_links if clean_dom in u and "google" not in u and "bing.com" not in u and "cache" not in u]
                product_urls = [u for u in domain_links if any(x in u.lower() for x in ["/product","/p/","/item","/buy","/shop",".html","/signs","/display","/frame","/holder","/pavement","/snap","/poster","/board","/sign"])]
                best = product_urls[0] if product_urls else (domain_links[0] if domain_links else None)
                await page.close()
                if best: return best
            except Exception as e:
                log.debug(f"  {engine} error: {e}")
                try: await page.close()
                except Exception: pass

        return None

    # ── BigCommerce SKU lookup (Harrison Products) ────────────────────────────

    async def bigcommerce_sku_lookup(
        self, context: BrowserContext, sku: dict, domain: str
    ) -> Optional[dict]:
        """
        BigCommerce stores expose products at /<slug>?sku=<sku_code>.
        We try the UKPOS SKU ID directly, then stripped variants.
        Returns dict with url, price, title, confidence — or None.
        Results go to review queue (confidence capped at 70) for human approval.
        """
        sku_id    = sku["sku_id"]
        base_url  = f"https://www.{domain}"

        # Try the UKPOS SKU directly, then without common suffixes
        candidates = [sku_id]
        # Also try stripping trailing letter variants (e.g. SA13A4 → SA13A, SA13)
        stripped = re.sub(r"[A-Z]\d*$", "", sku_id)
        if stripped and stripped != sku_id:
            candidates.append(stripped)

        for candidate_sku in candidates:
            search_url = f"{base_url}/search.php?search_query={quote_plus(candidate_sku)}"
            page = await context.new_page()
            try:
                log.debug(f"  BigCommerce SKU search: {search_url}")
                await page.goto(search_url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
                await page.wait_for_timeout(3000)

                # Extract first product result link
                links = await page.evaluate("""
                    () => {
                        const results = document.querySelectorAll(
                            'article.card a[href], .productGrid .card a[href], ' +
                            'li.product a[href], .product-item a.card-title'
                        );
                        return Array.from(results).map(a => a.href).filter(h => h.startsWith('http')).slice(0, 3);
                    }
                """)
                await page.close()

                for product_url in links:
                    # Visit the product page to get title + price + verify SKU match
                    ppage = await context.new_page()
                    try:
                        await ppage.goto(product_url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
                        await ppage.wait_for_timeout(3000)

                        title     = (await ppage.title()).strip()
                        full_text = await ppage.inner_text("body")

                        # Check if our SKU ID or a close match appears on the page
                        sku_found = (
                            sku_id.lower() in full_text.lower() or
                            candidate_sku.lower() in full_text.lower()
                        )

                        # Try to get price from JSON-LD first, then meta, then main price
                        price = await self._extract_jsonld_price(ppage)
                        if not price: price = await self._extract_meta_price(ppage)
                        if not price: price = await self._extract_main_price(ppage)

                        await ppage.close()

                        if price:
                            conf = fuzzy_confidence(sku, title, product_url)
                            # Bonus for SKU appearing on page, but cap at 70 — always goes to review
                            if sku_found: conf = min(70, conf + 20)
                            conf = min(70, conf)   # always review queue

                            log.info(
                                f"  BigCommerce match: '{title[:50]}' "
                                f"£{price} conf={conf}% sku_found={sku_found}"
                            )
                            return {
                                "url":        product_url,
                                "price":      price,
                                "title":      title,
                                "confidence": conf,
                                "vat_hint":   detect_vat(full_text),
                            }
                    except Exception as e:
                        log.debug(f"  BigCommerce page error: {e}")
                        try: await ppage.close()
                        except Exception: pass

            except Exception as e:
                log.debug(f"  BigCommerce search error: {e}")
                try: await page.close()
                except Exception: pass

        return None

    # ── Price extraction methods ──────────────────────────────────────────────

    async def _extract_main_price(self, page: Page) -> Optional[float]:
        """
        Smart main-price extraction using DOM scoring.
        Filters out related/recommended sections and prefers cart-adjacent prices.
        """
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

    async def _extract_alplas_price(self, page: Page) -> Optional[float]:
        """
        Alplas WooCommerce price structure:
        .price_inner_container > .total_price_container > .price > span.amount
        The first .price div contains the ex-VAT price, confirmed by adjacent
        span.vat_span containing "ex VAT".
        """
        try:
            result = await page.evaluate(r"""
                () => {
                    function parsePrice(raw) {
                        const m = (raw || '').replace(/,/g,'').match(/[\d]+\.[\d]{2}/);
                        if (!m) return null;
                        const val = parseFloat(m[0]);
                        return (val > 0.01 && val < 99999) ? val : null;
                    }

                    // Primary: .price_inner_container total price, ex-VAT div
                    const container = document.querySelector(
                        '.price_inner_container .total_price_container'
                    );
                    if (container) {
                        // Find the .price div that has a sibling span.vat_span "ex VAT"
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
                        // Fallback: first .amount inside total_price_container
                        const first = container.querySelector('.price .amount bdi, .price .amount');
                        if (first) {
                            const val = parsePrice(first.innerText || first.textContent);
                            if (val) return val;
                        }
                    }

                    // Secondary: unit_container price (also ex-VAT)
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

    async def _extract_pavement_signs_price(self, page: Page) -> Optional[float]:
        """
        PavementSigns.com ex-VAT price:
        <span id="ContentPlaceHolder1_lblexVAT">£89</span>
        Unique ID makes this trivial — no ambiguity possible.
        """
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

    async def _extract_discount_displays_price(self, page: Page) -> Optional[float]:
        """
        Discount Displays main product price uses these specific classes:
          font-regular text-gray-900 price label
        with x-html="getFormattedBasePrice()" rendered by Alpine.js.

        Related product prices are static HTML and appear immediately in the DOM.
        The main price is Alpine-rendered — we must wait for it to be non-empty.

        Priority:
          1. span/element with class containing all of: price, label, text-gray-900
          2. [x-html*="getFormattedBasePrice"] after waiting for Alpine
          3. .price inside [x-data] scope, excluding related sections
        """
        try:
            # Wait for Alpine to render the main price span
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
                pass  # Continue anyway

            result = await page.evaluate(r"""
                () => {
                    function parsePrice(raw) {
                        const m = (raw || '').replace(/,/g,'').match(/[\d]+\.[\d]{2}/);
                        if (!m) return null;
                        const val = parseFloat(m[0]);
                        return (val > 0.50 && val < 99999) ? val : null;
                    }

                    // Strategy 1: main product price container
                    // Class is "price-excl-taxinline-block" (deliberate typo in their HTML)
                    // Contains span.price with x-html="getFormattedBasePrice()"
                    // Related product prices are inside .js_slides carousel — excluded here
                    const mainContainer = document.querySelector(
                        '[class*="price-excl-taxinline-block"]'
                    );
                    if (mainContainer) {
                        // Make sure it's NOT inside the related products carousel
                        const inCarousel = mainContainer.closest('.js_slides, [class*="js_slide"]');
                        if (!inCarousel) {
                            const priceSpan = mainContainer.querySelector('[x-html*="getFormattedBasePrice"], span.price');
                            if (priceSpan) {
                                const val = parsePrice(priceSpan.innerText || priceSpan.textContent);
                                if (val) return val;
                            }
                        }
                    }

                    // Strategy 2: x-html getFormattedBasePrice NOT inside carousel
                    for (const el of document.querySelectorAll('[x-html*="getFormattedBasePrice"]')) {
                        if (el.closest('.js_slides, [class*="js_slide"]')) continue;
                        const val = parsePrice(el.innerText || el.textContent);
                        if (val) return val;
                    }

                    // Strategy 3: price-excluding-tax active NOT inside carousel
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

    # ── Category page detection ───────────────────────────────────────────────

    CATEGORY_URL_SIGNALS = [
        "/collections/", "/categories/", "/category/", "/c/",
        "/search", "/shop/", "/products?", "/catalogue",
        ".htm?", ".aspx?", "/a-boards", "/pavement-signs/",
        "/wall-sign-holders", "/acrylic-sign-holder-acrylic-frame/",
        "/a-frame-chalkboard",
    ]

    def _is_category_url(self, url: str) -> bool:
        u = url.lower().split("?")[0]
        for signal in self.CATEGORY_URL_SIGNALS:
            if signal in u:
                return True
        parts = [p for p in u.rstrip("/").split("/") if p]
        if len(parts) <= 2:
            return True
        return False

    async def _extract_shopify_json_price(self, url: str, context: BrowserContext) -> Optional[float]:
        """Shopify /products/[slug].js endpoint — fast price without full page render."""
        try:
            base = url.split("?")[0].rstrip("/")
            if "/products/" not in base:
                return None
            json_url = base + ".js"
            page = await context.new_page()
            try:
                await page.goto(json_url, wait_until="domcontentloaded", timeout=10000)
                text = await page.inner_text("body")
                await page.close()
                data = json.loads(text)
                variants = data.get("variants", [])
                if variants:
                    price_pence = variants[0].get("price")
                    if price_pence:
                        return round(float(price_pence) / 100, 2)
                price = data.get("price")
                if price:
                    return round(float(price) / 100, 2)
            except Exception:
                try: await page.close()
                except Exception: pass
        except Exception:
            pass
        return None

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

    async def scrape_product_page(self, context: BrowserContext, url: str, competitor_domain: str = "", fetch_image: bool = False) -> dict:
        result = {
            "price": None, "vat": "unknown", "availability": "in_stock",
            "title": "", "url": url, "error": None, "og_image": None,
        }

        # ── Skip category/listing pages early ─────────────────────────────────
        if self._is_category_url(url):
            result["error"] = "Category page — no single product price"
            result["availability"] = "unavailable"
            log.debug(f"  Skipping category page: {url}")
            return result

        # ── Shopify JSON endpoint (fast, no JS render needed) ─────────────────
        shopify_price = await self._extract_shopify_json_price(url, context)

        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
            await page.wait_for_timeout(10000)

            full_text              = await page.inner_text("body")
            result["vat"]          = detect_vat(full_text)
            result["availability"] = "out_of_stock" if detect_oos(full_text) else "in_stock"
            result["title"]        = (await page.title()).strip()

            # ── OG image — only when due for quarterly refresh ─────────────────
            if fetch_image:
                try:
                    og_image = await page.evaluate("""
                        () => {
                            const og  = document.querySelector('meta[property="og:image"]');
                            const twi = document.querySelector('meta[name="twitter:image"]');
                            return (og?.content || twi?.content || '').trim() || null;
                        }
                    """)
                    if og_image and og_image.startswith('http'):
                        result["og_image"] = og_image
                except Exception:
                    pass

            price = shopify_price

            if not price: price = await self._extract_jsonld_price(page)
            if not price: price = await self._extract_meta_price(page)

            # ── Discount Displays specific selector ────────────────────────────
            if not price and DISCOUNT_DISPLAYS_DOMAIN in competitor_domain:
                price = await self._extract_discount_displays_price(page)
            if not price and 'alplas.com' in competitor_domain:
                price = await self._extract_alplas_price(page)
            if not price and 'pavementsigns.com' in competitor_domain:
                price = await self._extract_pavement_signs_price(page)

            # ── Generic smart extraction for everyone else ─────────────────────
            if not price:
                price = await self._extract_main_price(page)

            result["price"] = price

        except Exception as e:
            result["error"]        = str(e)[:200]
            result["availability"] = "error"
        finally:
            await page.close()
        return result

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
            "competitor_unit_qty": None,
            "pack_qty_flag":       None,
            "confidence":          None,
            "error_message":       None,
            "_comp_title":         None,
        }

        if existing_match and existing_match["match_status"] == "rejected":
            log.debug(f"Skipping {sku['sku_id']} × {competitor['domain']} — rejected")
            return snapshot

        domain = competitor["domain"].lstrip("www.")
        ctx    = await self.new_context(browser)

        try:
            url        = existing_match.get("competitor_url") if existing_match else None
            price      = None
            comp_title = None
            vat_hint   = "unknown"
            confidence = 0

            # ── Path A: existing confirmed URL — scrape directly ───────────────
            if url:
                log.debug(f"  Path A — existing URL: {url}")
                result     = await self.scrape_product_page(ctx, url, domain, fetch_image=image_needs_refresh(existing_match))
                price      = result["price"]
                comp_title = result["title"]
                vat_hint   = result["vat"]
                confidence = fuzzy_confidence(sku, comp_title, url)
                snapshot["availability"]  = result["availability"]
                snapshot["error_message"] = result["error"]
                snapshot["_og_image"]     = result.get("og_image")

            else:
                # ── Path B: BigCommerce SKU lookup (Harrison Products) ─────────
                if any(d in domain for d in BIGCOMMERCE_DOMAINS):
                    log.debug(f"  Path B (BigCommerce) — SKU lookup for {sku['sku_id']}")
                    bc_result = await self.bigcommerce_sku_lookup(ctx, sku, domain)
                    if bc_result and bc_result.get("url"):
                        url        = bc_result["url"]
                        price      = bc_result.get("price")
                        comp_title = bc_result.get("title", "")
                        confidence = bc_result.get("confidence", 0)
                        vat_hint   = bc_result.get("vat_hint", "unknown")
                        snapshot["availability"] = "in_stock" if price else "unavailable"

                # ── Path C: Google/Bing Shopping ───────────────────────────────
                if not url:
                    shopping = await self.search_google_shopping(ctx, sku, competitor["domain"])
                    if shopping and shopping.get("url"):
                        url        = shopping["url"]
                        comp_title = shopping.get("title", "")
                        confidence = shopping.get("confidence", 0)
                        if shopping.get("price"):
                            price    = shopping["price"]
                            vat_hint = shopping.get("vat_hint", "unknown")
                            snapshot["availability"] = "in_stock"
                        else:
                            result     = await self.scrape_product_page(ctx, url, domain, fetch_image=image_needs_refresh(existing_match))
                            price      = result["price"]
                            vat_hint   = result["vat"]
                            comp_title = result["title"] or comp_title
                            confidence = max(confidence, fuzzy_confidence(sku, comp_title, url))
                            snapshot["availability"]  = result["availability"]
                            snapshot["error_message"] = result["error"]
                            snapshot["_og_image"]     = result.get("og_image")

                # ── Path D: Site web search fallback ───────────────────────────
                if not url:
                    url = await self.search_web(ctx, sku, competitor["domain"])
                    if url:
                        result     = await self.scrape_product_page(ctx, url, domain, fetch_image=image_needs_refresh(existing_match))
                        price      = result["price"]
                        comp_title = result["title"]
                        vat_hint   = result["vat"]
                        confidence = fuzzy_confidence(sku, comp_title, url)
                        snapshot["availability"]  = result["availability"]
                        snapshot["error_message"] = result["error"]
                        snapshot["_og_image"]     = result.get("og_image")
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
                our_price = float(sku["price_ex_vat"])
                their_ex  = normalise_price(price, snapshot["competitor_vat"])

                # ── Per-unit normalisation ─────────────────────────────────────
                # If pack quantities differ on either side, normalise both prices
                # to per-unit before computing diff_pct_normalised.
                # Cases:
                #   our_qty=100, comp_qty=1   → we sell pack, they sell single
                #   our_qty=1,   comp_qty=100 → we sell single, they sell pack
                #   our_qty=100, comp_qty=100 → like-for-like, no normalisation
                #   our_qty=1,   comp_qty=1   → both singles, no normalisation
                # Establish pack quantities. TITLES are authoritative for the
                # maths. Our side: prefer a pack qty parsed from our own title
                # (e.g. "…x 100"), because the skus.unit_qty column is often
                # stale or defaulted to 1. Fall back to the column only when the
                # title yields no pack signal.
                our_title_qty = extract_pack_qty(sku.get("short_title", "")) or 1
                our_col_qty   = sku.get("unit_qty") or 1
                our_qty  = our_title_qty if our_title_qty > 1 else our_col_qty
                comp_qty = extract_pack_qty(comp_title) or 1

                # Persist the competitor's detected pack qty so the dashboard can
                # show a true per-unit comparison. Stored as-is (1 when no pack
                # signal found in their title).
                snapshot["competitor_unit_qty"] = comp_qty

                # ── Price-gap review flag (NEVER drives the maths) ─────────────
                # A large raw price gap CAN indicate an undetected pack-size
                # mismatch — but it can equally mean a genuinely cheaper rival,
                # a VAT-basis error, or a clearance price. So we only RAISE A
                # FLAG for human review; we never infer a multiple or normalise
                # by it. Trigger: qtys look like singles on both sides yet the
                # raw per-item prices differ by enough to look pack-like.
                if our_qty == comp_qty and their_ex and our_price:
                    ratio = max(our_price, their_ex) / min(our_price, their_ex)
                    if ratio >= 1.5:
                        snapshot["pack_qty_flag"] = (
                            f"raw price gap {ratio:.1f}× with no pack signal in "
                            f"either title — verify pack sizes"
                        )

                if our_qty != comp_qty:
                    our_per_unit   = per_unit_price(our_price, our_qty)
                    their_per_unit = per_unit_price(their_ex,  comp_qty)
                    normalised_diff = diff_pct(our_per_unit, their_per_unit)
                    log.info(
                        f"  ✓ {competitor['domain']:35s} "
                        f"£{price:>7.2f} ({snapshot['competitor_vat']:7s}) "
                        f"our_qty={our_qty} comp_qty={comp_qty} "
                        f"→ per-unit diff {normalised_diff:+.1f}%  conf {confidence}%"
                    )
                else:
                    normalised_diff = diff_pct(our_price, their_ex)
                    log.info(
                        f"  ✓ {competitor['domain']:35s} "
                        f"£{price:>7.2f} ({snapshot['competitor_vat']:7s}) "
                        f"diff {normalised_diff:+.1f}%  conf {confidence}%"
                    )

                snapshot["competitor_price"]    = price
                snapshot["diff_pct"]            = diff_pct(our_price, price)
                snapshot["diff_pct_normalised"] = normalised_diff
            else:
                log.info(
                    f"  ✗ {competitor['domain']:35s} "
                    f"no price  conf {confidence}%  '{(comp_title or '')[:50]}'"
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

    async def flush_matches_for_sku(self, sku: dict, snapshots: list, competitors: list):
        comp_map = {c["id"]: c for c in competitors}
        rows     = []
        for snap in snapshots:
            if not snap.get("competitor_url"): continue
            conf         = snap.get("confidence") or 0
            match_status = "matched" if conf >= 80 else "review"
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
            self.sb.table("competitor_matches").upsert(rows, on_conflict="sku_id,competitor_id").execute()
            matched = sum(1 for r in rows if r["match_status"] == "matched")
            review  = sum(1 for r in rows if r["match_status"] == "review")
            log.info(f"  → Flushed {len(rows)} matches for {sku['sku_id']}: {matched} matched, {review} review")
        else:
            log.info(f"  → No matches flushed for {sku['sku_id']}")

    async def create_alerts(self, snapshot: dict, sku: dict, competitor: dict):
        our_price = float(sku["price_ex_vat"])
        diff      = snapshot.get("diff_pct_normalised") or snapshot.get("diff_pct")
        alerts    = []
        if snapshot["availability"] == "out_of_stock":
            alerts.append({"run_id": self.run_id, "sku_id": sku["sku_id"], "competitor_id": competitor["id"], "alert_type": "oos_competitor",
                "message": f"{competitor['name']} is out of stock for {sku['short_title']} — last known £{snapshot.get('competitor_price','?')}",
                "diff_pct": diff, "our_price": our_price, "their_price": snapshot.get("competitor_price")})
        elif diff is not None:
            if diff <= -10:
                alerts.append({"run_id": self.run_id, "sku_id": sku["sku_id"], "competitor_id": competitor["id"], "alert_type": "critical",
                    "message": f"{competitor['name']} is {abs(diff):.1f}% cheaper — £{snapshot['competitor_price']:.2f} vs your £{our_price:.2f}",
                    "diff_pct": diff, "our_price": our_price, "their_price": snapshot.get("competitor_price")})
            elif diff <= -5:
                alerts.append({"run_id": self.run_id, "sku_id": sku["sku_id"], "competitor_id": competitor["id"], "alert_type": "warning",
                    "message": f"{competitor['name']} is {abs(diff):.1f}% cheaper — £{snapshot['competitor_price']:.2f} vs your £{our_price:.2f}",
                    "diff_pct": diff, "our_price": our_price, "their_price": snapshot.get("competitor_price")})
        for alert in alerts:
            self.sb.table("alerts").insert(alert).execute()


# ── Main runner ────────────────────────────────────────────────────────────────

async def run_scraper(trigger: str = "scheduled"):
    sb     = create_client(SUPABASE_URL, SUPABASE_KEY)
    run_id = uuid.uuid4()

    sb.table("sync_runs").insert({
        "id": str(run_id), "trigger": trigger,
        "status": "running", "started_at": datetime.now(timezone.utc).isoformat(),
    }).execute()

    mode          = SCRAPER_MODE
    specific_skus = [s.strip() for s in os.getenv("SCRAPER_SKUS", "").split(",") if s.strip()]
    if specific_skus:
        mode = "skus"

    log.info(f"Starting sync run {run_id} | mode={mode} | workers={WORKERS} | delay={DELAY_MIN}–{DELAY_MAX}s")

    comps = (
        sb.table("competitors")
        .select("*")
        .eq("active", True)
        .order("id")
        .limit(COMPETITOR_LIMIT)
        .execute()
        .data
    )

    from collections import defaultdict

    if mode == "matched":
        log.info("Mode: matched — re-scraping confirmed URLs only")
        matched_rows = (
            sb.table("competitor_matches")
            .select("sku_id, competitor_id, competitor_url, competitor_title, confidence, match_status, match_method, human_reviewed, notes")
            .eq("match_status", "matched")
            .not_.is_("competitor_url", "null")
            .execute()
            .data
        )
        if not matched_rows:
            log.info("No confirmed matches found — nothing to scrape. Run mode=full first.")
            sb.table("sync_runs").update({
                "status": "complete", "completed_at": datetime.now(timezone.utc).isoformat(),
                "skus_attempted": 0, "skus_succeeded": 0, "skus_failed": 0, "oos_flagged": 0,
            }).eq("id", str(run_id)).execute()
            return

        log.info(f"  {len(matched_rows)} confirmed matches to re-scrape")
        match_lookup    = {(r["sku_id"], r["competitor_id"]): r for r in matched_rows}
        matched_sku_ids = list({r["sku_id"] for r in matched_rows})

        skus = []
        for i in range(0, len(matched_sku_ids), 200):
            rows = sb.table("skus").select("*").in_("sku_id", matched_sku_ids[i:i+200]).execute().data
            skus.extend(rows or [])

        comp_map  = {c["id"]: c for c in comps}
        sku_work: dict = defaultdict(list)
        for sku in skus:
            for comp_id, comp in comp_map.items():
                key = (sku["sku_id"], comp_id)
                if key in match_lookup:
                    sku_work[sku["sku_id"]].append((sku, comp, match_lookup[key]))

    elif mode == "skus":
        log.info(f"Mode: skus — targeting {specific_skus}")
        skus        = sb.table("skus").select("*").in_("sku_id", specific_skus).execute().data
        all_matches = sb.table("competitor_matches").select("*").execute().data
        match_lookup = {(m["sku_id"], m["competitor_id"]): m for m in all_matches}
        sku_work: dict = defaultdict(list)
        for sku in skus:
            for comp in comps:
                sku_work[sku["sku_id"]].append((sku, comp, match_lookup.get((sku["sku_id"], comp["id"]))))

    else:
        log.info("Mode: full — all SKUs × all competitors")
        skus        = sb.table("skus").select("*").eq("active", True).limit(SKU_LIMIT).execute().data
        all_matches = sb.table("competitor_matches").select("*").execute().data
        match_lookup = {(m["sku_id"], m["competitor_id"]): m for m in all_matches}
        sku_work: dict = defaultdict(list)
        for sku in skus:
            for comp in comps:
                sku_work[sku["sku_id"]].append((sku, comp, match_lookup.get((sku["sku_id"], comp["id"]))))

    log.info(f"  {sum(len(v) for v in sku_work.values())} work items across {len(sku_work)} SKUs")

    scraper = PriceScraper(sb, run_id)
    stats   = {"attempted": 0, "succeeded": 0, "failed": 0, "oos": 0}
    sem     = asyncio.Semaphore(WORKERS)

    async def process_sku_group(sku_id: str, items: list):
        async with sem:
            sku = items[0][0]
            log.info(f"\n{'='*60}\n{sku['sku_id']} — {sku['short_title']}")
            if mode != "matched":
                log.info(f"Query: '{build_search_query(sku)}'")
            log.info('='*60)

            sku_snapshots = []
            for (_, comp, existing) in items:
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

            if mode != "matched":
                await scraper.flush_matches_for_sku(sku, sku_snapshots, comps)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-infobars",
                "--window-size=1280,800",
            ],
        )
        await asyncio.gather(*[
            process_sku_group(sku_id, items)
            for sku_id, items in sku_work.items()
        ])
        await browser.close()

    sb.table("sync_runs").update({
        "status": "complete", "completed_at": datetime.now(timezone.utc).isoformat(),
        "skus_attempted": stats["attempted"], "skus_succeeded": stats["succeeded"],
        "skus_failed": stats["failed"], "oos_flagged": stats["oos"],
    }).eq("id", str(run_id)).execute()
    log.info(f"Run {run_id} complete — mode={mode} — {stats}")


if __name__ == "__main__":
    import sys
    trigger = sys.argv[1] if len(sys.argv) > 1 else "scheduled"
    asyncio.run(run_scraper(trigger))