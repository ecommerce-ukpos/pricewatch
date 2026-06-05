"""
scraper/common.py
─────────────────
Shared constants, helpers, and browser factory used by both
discover.py (URL discovery) and scrape.py (price scraping).

Nothing in here touches Supabase or launches a browser — it is
pure utility so both scripts can import it without side-effects.
"""

import json
import logging
import os
import random
import re
from typing import Optional
from urllib.parse import quote_plus

from playwright.async_api import Browser, BrowserContext, Page

log = logging.getLogger("pricewatch")

# ── Competitor domain flags ────────────────────────────────────────────────────
BIGCOMMERCE_DOMAINS    = {"harrisonproducts.com"}
DISCOUNT_DISPLAYS_DOMAIN = "discountdisplays.co.uk"

# ── User-agent rotation ────────────────────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

# ── VAT detection patterns ─────────────────────────────────────────────────────
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

# ── Out-of-stock patterns ──────────────────────────────────────────────────────
OOS_PATTERNS = [
    r"out\s+of\s+stock",
    r"currently\s+unavailable",
    r"temporarily\s+out",
    r"sold\s+out",
    r"no\s+stock",
    r"backordered?",
    r"not\s+available",
]

# ── Stop-words for confidence scoring ─────────────────────────────────────────
STOP_WORDS = {
    "the","a","an","and","of","for","with","in","to","self","adhesive",
    "pack","set","lot","box","bag","new","uk","free","delivery","shipping",
    "holders","holder","signs","sign","displays","display",
}

# ── Pack qty patterns ──────────────────────────────────────────────────────────
PACK_QTY_PATTERNS = [
    r"(?<!\d\s)(?<!\d)\bx\s*(\d+)\b",
    r"\bpack\s+of\s+(\d+)\b",
    r"\b(\d+)\s*pack\b",
    r"\bset\s+of\s+(\d+)\b",
    r"\bbox\s+of\s+(\d+)\b",
    r"\bbag\s+of\s+(\d+)\b",
    r"\bper\s+(\d+)\b",
    r"\bqty\s*[:\-]?\s*(\d+)\b",
    r"\b(\d+)\s*x\b(?!\s*\d)",
]

# ── Category URL signals (used by scrape.py to skip listing pages) ─────────────
CATEGORY_URL_SIGNALS = [
    "/collections/", "/categories/", "/category/", "/c/",
    "/search", "/shop/", "/products?", "/catalogue",
    ".htm?", ".aspx?", "/a-boards", "/pavement-signs/",
    "/wall-sign-holders", "/acrylic-sign-holder-acrylic-frame/",
    "/a-frame-chalkboard",
]


# ── Pure helpers ───────────────────────────────────────────────────────────────

def build_search_query(sku: dict) -> str:
    """
    Build a clean search query from a SKU title.

    For printed/branded SKUs (sku_id ending in -PRINTED or -BRANDED),
    competitors stock the base product without custom print — strip all
    custom-print/branding language so we find their equivalent.
    Also strips pack-quantity noise from all SKUs.
    """
    title  = sku["short_title"]
    sku_id = sku.get("sku_id", "")

    if re.search(r"-(PRINTED|BRANDED)$", sku_id, re.I):
        title = re.sub(r"\bcustom[- ]?print(?:ed)?\b",          "", title, flags=re.I)
        title = re.sub(r"\bwith[- ]?print(?:ed)?[- ]?poster\b", "", title, flags=re.I)
        title = re.sub(r"\bInc(?:\.)?[- ]?Printed\b",           "", title, flags=re.I)
        title = re.sub(r"\bCustom[- ]?Printed\b",               "", title, flags=re.I)
        title = re.sub(r"\bPrinted\b",                          "", title, flags=re.I)
        title = re.sub(r"\bBespoke[- ]?Brand(?:ing|ed)?\b",     "", title, flags=re.I)
        title = re.sub(r"\bBranded\b",                          "", title, flags=re.I)

    clean = re.sub(r"\bx\s*\d+\b",          "", title, flags=re.I)
    clean = re.sub(r"\bpack\s+of\s+\d+\b",  "", clean, flags=re.I)
    clean = re.sub(r"\b\d+\s*pack\b",       "", clean, flags=re.I)
    clean = re.sub(r"\s{2,}", " ", clean).strip().rstrip("-—–").strip()
    return clean


def fuzzy_confidence(sku: dict, comp_title: str, comp_url: str) -> int:
    """0-100 confidence that comp_title/comp_url is the same product as sku."""
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
        if s_dims == c_dims:    score += 30
        elif s_dims & c_dims:   score += 15
        elif c_dims:            score -= 15

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


def extract_pack_qty(title: str) -> Optional[int]:
    """Extract pack quantity from a product title. Returns None if not found or qty=1."""
    if not title:
        return None
    t = title.lower()
    for pattern in PACK_QTY_PATTERNS:
        m = re.search(pattern, t, re.I)
        if m:
            qty = int(m.group(1))
            if 2 <= qty <= 500:  # was 10000 — anything larger is almost certainly a dimension
                return qty
    return None


def per_unit_price(price: float, qty: Optional[int]) -> float:
    if qty and qty > 1:
        return round(price / qty, 6)
    return price


def is_category_url(url: str) -> bool:
    u = url.lower().split("?")[0]
    for signal in CATEGORY_URL_SIGNALS:
        if signal in u:
            return True
    parts = [p for p in u.rstrip("/").split("/") if p]
    if len(parts) <= 2:
        return True
    return False


# ── Browser factory ────────────────────────────────────────────────────────────

_ua_counter = 0

def next_ua() -> str:
    global _ua_counter
    ua = USER_AGENTS[_ua_counter % len(USER_AGENTS)]
    _ua_counter += 1
    return ua


async def new_stealth_context(browser: Browser) -> BrowserContext:
    """Return a browser context that looks like a real user."""
    ctx = await browser.new_context(
        user_agent=next_ua(),
        viewport={"width": 1280, "height": 800},
        locale="en-GB",
        extra_http_headers={
            "Accept-Language":          "en-GB,en;q=0.9",
            "Accept":                   "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Encoding":          "gzip, deflate, br",
            "Upgrade-Insecure-Requests":"1",
            "Sec-Fetch-Dest":           "document",
            "Sec-Fetch-Mode":           "navigate",
            "Sec-Fetch-Site":           "none",
        },
    )
    await ctx.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins',  { get: () => [1, 2, 3] });
        Object.defineProperty(navigator, 'languages',{ get: () => ['en-GB', 'en'] });
        window.chrome = { runtime: {} };
    """)
    return ctx


async def launch_browser(playwright) -> Browser:
    return await playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--disable-infobars",
            "--window-size=1280,800",
        ],
    )
