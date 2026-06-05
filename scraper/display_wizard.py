"""
scraper/display_wizard.py
─────────────────────────
DisplayWizard-specific logic for their Shopify headless storefront.

DisplayWizard runs a custom Gatsby SSG frontend backed by Shopify.
Products with multiple variants (e.g. size options) expose variant data
via Shopify's standard public endpoints and Gatsby's pre-built page data.

Architecture:
  - Frontend: Gatsby 5 + DatoCMS (static HTML, no JS needed for variant data)
  - Commerce: Shopify headless (checkout at secure.displaywizard.co.uk)
  - Variant URL format: /product-slug/?variant=VARIANT_ID

Price discovery flow:
  1. Try Shopify product JSON endpoint: /products/<handle>.json
     → Returns all variants with IDs, SKUs, titles, prices
  2. Fallback: parse Gatsby page-data JSON: /page-data/<handle>/page-data.json
     → Pre-built at deploy time, contains Shopify Storefront API response
  3. Last resort: fetch the HTML page and parse embedded JSON from
     a <script> tag (Gatsby embeds product state in the page)

Variant matching:
  Uses size suffix from UKPOS SKU ID (e.g. "300" from "WIRE-C-300")
  and token scoring against variant titles (e.g. "300mm").

VAT status:
  DisplayWizard shows prices inc-VAT on their storefront.
  Prices returned here are inc-VAT; scrape.py normalises via the
  competitor's vat_status = 'inc' row in the competitors table.

Public API
──────────
  parse_shopify_product_json(data: dict, base_url: str) -> DWProduct | None
  match_variant(product: DWProduct, sku: dict) -> DWVariantMatch | None
  variant_url(base_url: str, variant_id: str) -> str
  scrape_dw_page(html: str, url: str, sku: dict) -> DWScrapeResult
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

log = logging.getLogger("pricewatch.display_wizard")

DISPLAY_WIZARD_DOMAIN = "displaywizard.co.uk"


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class DWVariant:
    variant_id: str          # Shopify numeric variant ID
    title: str               # e.g. "300mm"
    sku: str                 # e.g. "WIRE-C-300"
    price: float             # inc-VAT (Shopify always stores inc-VAT for UK stores)
    available: bool


@dataclass
class DWProduct:
    handle: str              # Shopify product handle / URL slug
    base_url: str            # canonical page URL (no params)
    title: str
    variants: list[DWVariant]

    def by_id(self, variant_id: str) -> Optional[DWVariant]:
        for v in self.variants:
            if v.variant_id == variant_id:
                return v
        return None

    def by_sku(self, sku: str) -> Optional[DWVariant]:
        sl = sku.upper()
        for v in self.variants:
            if v.sku.upper() == sl:
                return v
        return None

    def by_title(self, title: str) -> Optional[DWVariant]:
        tl = title.lower().strip()
        for v in self.variants:
            if v.title.lower().strip() == tl:
                return v
        # Partial match
        for v in self.variants:
            if tl in v.title.lower() or v.title.lower() in tl:
                return v
        return None


@dataclass
class DWVariantMatch:
    variant_id: str
    title: str
    sku: str
    url: str
    price: float             # inc-VAT
    available: bool
    score: int
    reasoning: str


@dataclass
class DWScrapeResult:
    success: bool
    price: Optional[float]   # inc-VAT — scrape.py normalises via vat_status='inc'
    available: bool
    matched_variant: Optional[DWVariantMatch]
    product: Optional[DWProduct]
    error: Optional[str] = None
    vat: str = "inc"
    availability: str = "in_stock"


# ── Variant URL ────────────────────────────────────────────────────────────────

def variant_url(base_url: str, variant_id: str) -> str:
    """Build canonical variant URL: /product-slug/?variant=12345"""
    clean = base_url.rstrip("/").split("?")[0]
    return f"{clean}/?variant={variant_id}"


# ── Shopify product.json parser ────────────────────────────────────────────────

def _parse_price(val) -> Optional[float]:
    """Parse Shopify price string (e.g. '2.50') to float."""
    if val is None:
        return None
    try:
        f = float(str(val).replace(",", ""))
        return round(f, 2) if 0.001 < f < 99999 else None
    except (ValueError, TypeError):
        return None


def parse_shopify_product_json(data: dict, base_url: str) -> Optional["DWProduct"]:
    """
    Parse the response from /products/<handle>.json

    Shopify returns:
      { "product": { "handle": "...", "title": "...", "variants": [...] } }

    Each variant:
      { "id": 123, "title": "300mm", "sku": "WIRE-C-300", "price": "2.50",
        "available": true, ... }
    """
    product_data = data.get("product", data)  # handle both wrapped and bare
    if not product_data or not isinstance(product_data, dict):
        return None

    handle = product_data.get("handle", "")
    title  = product_data.get("title", "")
    raw_variants = product_data.get("variants", [])

    if not raw_variants:
        return None

    variants = []
    for v in raw_variants:
        if not isinstance(v, dict):
            continue
        vid = str(v.get("id", ""))
        if not vid:
            continue
        price = _parse_price(v.get("price"))
        if price is None:
            # compare_at_price fallback
            price = _parse_price(v.get("compare_at_price"))
        variants.append(DWVariant(
            variant_id=vid,
            title=str(v.get("title", "")),
            sku=str(v.get("sku", "")),
            price=price or 0.0,
            available=bool(v.get("available", True)),
        ))

    if not variants:
        return None

    parsed = urlparse(base_url)
    clean_base = urlunparse(parsed._replace(query="", fragment=""))

    return DWProduct(
        handle=handle,
        base_url=clean_base,
        title=title,
        variants=variants,
    )


# ── Gatsby page-data parser ────────────────────────────────────────────────────

def parse_gatsby_page_data(data: dict, base_url: str) -> Optional[DWProduct]:
    """
    Parse Gatsby's /page-data/<handle>/page-data.json

    The structure varies by theme but typically:
      result.data.shopifyProduct.variants[].shopifyId / price / title / sku
    or
      result.data.shopifyProduct.variants[].storefrontId / priceV2.amount
    """
    try:
        # Navigate to the product object — try common Gatsby/Shopify paths
        product_data = (
            data.get("result", {})
                .get("data", {})
                .get("shopifyProduct", {})
        )
        if not product_data:
            product_data = (
                data.get("result", {})
                    .get("pageContext", {})
                    .get("product", {})
            )
        if not product_data:
            return None

        raw_variants = product_data.get("variants", [])
        if not raw_variants:
            return None

        title  = product_data.get("title", "")
        handle = product_data.get("handle", "")

        variants = []
        for v in raw_variants:
            # Shopify Storefront GID: "gid://shopify/ProductVariant/12345"
            gid = v.get("storefrontId") or v.get("shopifyId") or v.get("id") or ""
            vid = str(gid).split("/")[-1] if "/" in str(gid) else str(gid)
            if not vid:
                continue

            # Price: priceV2.amount or price (string)
            price = None
            price_v2 = v.get("priceV2", {})
            if price_v2:
                price = _parse_price(price_v2.get("amount"))
            if price is None:
                price = _parse_price(v.get("price"))

            vtitle = ""
            # selectedOptions: [{"name": "Size", "value": "300mm"}]
            options = v.get("selectedOptions", [])
            if options:
                vtitle = " / ".join(o.get("value", "") for o in options)
            if not vtitle:
                vtitle = v.get("title", "")

            variants.append(DWVariant(
                variant_id=vid,
                title=vtitle,
                sku=str(v.get("sku", "")),
                price=price or 0.0,
                available=bool(v.get("availableForSale", True)),
            ))

        if not variants:
            return None

        parsed = urlparse(base_url)
        clean_base = urlunparse(parsed._replace(query="", fragment=""))

        return DWProduct(
            handle=handle,
            base_url=clean_base,
            title=title,
            variants=variants,
        )
    except Exception as e:
        log.debug(f"  DW: Gatsby page-data parse error: {e}")
        return None


# ── HTML embedded JSON parser ──────────────────────────────────────────────────

# Gatsby embeds server-side data in a <script> tag as window.__GATSBY_DATA__
# or similar. We look for common patterns.
_GATSBY_STATE_PATTERNS = [
    re.compile(r'window\.__GATSBY_DATA__\s*=\s*(\{.*?\})\s*;', re.DOTALL),
    re.compile(r'window\.__gatsby_state__\s*=\s*(\{.*?\})\s*;', re.DOTALL),
    re.compile(r'"shopifyProduct"\s*:\s*(\{.*?"variants".*?\})', re.DOTALL),
    re.compile(r'"variants"\s*:\s*(\[.*?\])', re.DOTALL),
]

# Shopify embeds product JSON in a <script type="application/json"> tag
_SHOPIFY_PRODUCT_JSON_PATTERN = re.compile(
    r'<script[^>]+type=["\']application/json["\'][^>]*>\s*(\{[^<]*"variants"[^<]*\})\s*</script>',
    re.DOTALL | re.IGNORECASE,
)


def parse_html_embedded_json(html: str, base_url: str) -> Optional[DWProduct]:
    """
    Last-resort: scan the HTML for embedded product/variant JSON blobs.
    """
    # Try Shopify product JSON in <script type="application/json">
    m = _SHOPIFY_PRODUCT_JSON_PATTERN.search(html)
    if m:
        try:
            data = json.loads(m.group(1))
            product = parse_shopify_product_json(data, base_url)
            if product:
                log.debug("  DW: found product JSON in <script> tag")
                return product
        except (json.JSONDecodeError, Exception):
            pass

    # Try Gatsby state patterns
    for pattern in _GATSBY_STATE_PATTERNS:
        m = pattern.search(html)
        if m:
            try:
                data = json.loads(m.group(1))
                # Try both Shopify and Gatsby formats
                product = parse_shopify_product_json(data, base_url)
                if product:
                    log.debug("  DW: found product JSON via Gatsby state pattern")
                    return product
                product = parse_gatsby_page_data({"result": {"data": {"shopifyProduct": data}}}, base_url)
                if product:
                    return product
            except (json.JSONDecodeError, Exception):
                pass

    return None


# ── Variant matching ───────────────────────────────────────────────────────────

_SIZE_TOKEN_PATTERN = re.compile(
    r"\b(\d+)\s*mm\b|"            # e.g. 300mm
    r"\b(a[0-7])\b|"              # A4, A3, A2, A1, A0
    r"\b(\d+(?:\.\d+)?)\s*cm\b|"  # e.g. 30cm
    r"\b(\d+)\b",                  # bare number — lowest priority
    re.IGNORECASE,
)


def _size_tokens(text: str) -> set[str]:
    """Extract normalised size tokens from text."""
    tokens = set()
    for m in _SIZE_TOKEN_PATTERN.finditer(text):
        for group in m.groups():
            if group:
                tokens.add(group.lower().strip())
    return tokens


def _score_variant(var: DWVariant, sku_id: str, sku_title: str) -> tuple[int, str]:
    """Score a DW variant against a UKPOS SKU. Returns (score, reasoning)."""
    score = 0
    reasons = []

    # ── Direct SKU match ──────────────────────────────────────────────────────
    if var.sku.upper() == sku_id.upper():
        return 100, f"exact SKU match: {var.sku}"

    # ── Size suffix from UKPOS SKU ID ─────────────────────────────────────────
    # e.g. WIRE-C-300 → suffix "300", match against "300mm" variant title
    sku_parts = sku_id.upper().split("-")
    sku_suffix = sku_parts[-1] if sku_parts else ""

    var_title_lower = var.title.lower().replace(" ", "")
    sku_suffix_lower = sku_suffix.lower()

    if sku_suffix and re.search(r"^\d+$", sku_suffix):
        # Numeric suffix — check if it appears in variant title
        if sku_suffix_lower in var_title_lower:
            score += 70
            reasons.append(f"SKU numeric suffix match: {sku_suffix} in '{var.title}'")
        elif sku_suffix_lower + "mm" in var_title_lower:
            score += 70
            reasons.append(f"SKU suffix+mm match: {sku_suffix}mm in '{var.title}'")
        else:
            score -= 20
            reasons.append(f"SKU suffix mismatch: {sku_suffix} not in '{var.title}'")

    elif sku_suffix:
        # Non-numeric suffix — text overlap
        if sku_suffix_lower in var_title_lower:
            score += 50
            reasons.append(f"SKU suffix text match: {sku_suffix} in '{var.title}'")

    # ── Size token matching from UKPOS title ──────────────────────────────────
    sku_sizes = _size_tokens(sku_title)
    var_sizes = _size_tokens(var.title)
    if sku_sizes and var_sizes:
        if sku_sizes & var_sizes:
            score += 20
            reasons.append(f"size token overlap: {sku_sizes & var_sizes}")
        else:
            score -= 10

    # ── Availability bonus ────────────────────────────────────────────────────
    if var.available:
        score += 5

    return max(0, min(100, score)), "; ".join(reasons) if reasons else "no signal"


def match_variant(product: DWProduct, sku: dict) -> Optional[DWVariantMatch]:
    """
    Find the best-matching variant for a UKPOS SKU.

    Priority:
      1. Exact SKU code match (sku field on variant == UKPOS sku_id)
      2. Variant ID already in stored URL (?variant=ID)
      3. Token scoring against variant titles
    """
    sku_id = sku.get("sku_id", "")
    title  = sku.get("short_title", "")

    if not product.variants:
        return None

    # ── Check existing URL for ?variant=ID ────────────────────────────────────
    existing_url = sku.get("_existing_url", "")
    if existing_url:
        qs = parse_qs(urlparse(existing_url).query)
        existing_vid = qs.get("variant", [None])[0]
        if existing_vid:
            var = product.by_id(existing_vid)
            if var:
                url = variant_url(product.base_url, var.variant_id)
                return DWVariantMatch(
                    variant_id=var.variant_id,
                    title=var.title,
                    sku=var.sku,
                    url=url,
                    price=var.price,
                    available=var.available,
                    score=100,
                    reasoning=f"variant_id from existing URL: {existing_vid}",
                )

    # ── Score all variants ─────────────────────────────────────────────────────
    scored = []
    for var in product.variants:
        score, reasoning = _score_variant(var, sku_id, title)
        scored.append((score, var.available, var, reasoning))

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    best_score, best_avail, best_var, best_reason = scored[0]

    # Prefer in-stock within 10 points
    if not best_avail:
        for score, avail, var, reasoning in scored:
            if avail and (best_score - score) <= 10:
                best_score, best_avail, best_var, best_reason = score, avail, var, reasoning
                break

    url = variant_url(product.base_url, best_var.variant_id)
    log.info(
        f"  DW variant match: id={best_var.variant_id} title='{best_var.title}' "
        f"sku='{best_var.sku}' score={best_score} price=£{best_var.price} "
        f"available={best_var.available}"
    )
    log.debug(f"  DW reasoning: {best_reason}")

    return DWVariantMatch(
        variant_id=best_var.variant_id,
        title=best_var.title,
        sku=best_var.sku,
        url=url,
        price=best_var.price,
        available=best_var.available,
        score=best_score,
        reasoning=best_reason,
    )


def all_variant_info(product: DWProduct) -> list[dict]:
    """Return all variants as flat dicts for CSV audit output."""
    return [
        {
            "variant_id": v.variant_id,
            "title":      v.title,
            "sku":        v.sku,
            "url":        variant_url(product.base_url, v.variant_id),
            "price":      v.price,
            "available":  v.available,
        }
        for v in product.variants
    ]


# ── High-level scrape helper ───────────────────────────────────────────────────

def scrape_dw_page(html: str, url: str, sku: dict) -> DWScrapeResult:
    """
    Given the raw HTML of a DisplayWizard product page, extract the best
    variant price for the given UKPOS SKU.

    Three cases:
      A. URL has ?variant=ID → look up that variant directly in the blob.
      B. URL has no variant param → match best variant by SKU/title scoring.
      C. No product data found in HTML → return failure.

    Prices are inc-VAT. The caller (scrape.py) reads the competitor's
    vat_status='inc' and normalises before computing diff_pct.

    Returns DWScrapeResult. The canonical variant URL is stored in
    _dw_variant_url so scrape.py can persist it to competitor_matches,
    enabling the fast Case A path on all future runs.
    """
    product = parse_html_embedded_json(html, base_url=url)

    if product is None:
        return DWScrapeResult(
            success=False, price=None, available=False,
            matched_variant=None, product=None,
            error="No Shopify product/variant data found in page HTML",
        )

    # ── Check for ?variant=ID in URL ──────────────────────────────────────────
    qs = parse_qs(urlparse(url).query)
    variant_id_from_url = qs.get("variant", [None])[0]

    # ── Case A: variant ID in URL ─────────────────────────────────────────────
    if variant_id_from_url:
        var = product.by_id(variant_id_from_url)
        if var:
            vurl = variant_url(product.base_url, var.variant_id)
            vm = DWVariantMatch(
                variant_id=var.variant_id,
                title=var.title,
                sku=var.sku,
                url=vurl,
                price=var.price,
                available=var.available,
                score=100,
                reasoning=f"variant_id resolved from URL: {variant_id_from_url}",
            )
            log.info(f"  DW Case A: variant={variant_id_from_url} price=£{var.price}")
            return DWScrapeResult(
                success=True,
                price=var.price,
                available=var.available,
                matched_variant=vm,
                product=product,
                vat="inc",
                availability="in_stock" if var.available else "out_of_stock",
            )

    # ── Case B: match by SKU/title scoring ────────────────────────────────────
    sku_with_url = {**sku, "_existing_url": url}
    vm = match_variant(product, sku_with_url)
    if vm is None:
        return DWScrapeResult(
            success=False, price=None, available=False,
            matched_variant=None, product=product,
            error="No variants in product data",
        )

    log.info(
        f"  DW Case B: matched variant={vm.variant_id} "
        f"score={vm.score} price=£{vm.price}"
    )
    return DWScrapeResult(
        success=True,
        price=vm.price,
        available=vm.available,
        matched_variant=vm,
        product=product,
        vat="inc",
        availability="in_stock" if vm.available else "out_of_stock",
    )
