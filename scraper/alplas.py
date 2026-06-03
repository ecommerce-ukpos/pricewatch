"""
scraper/alplas.py
─────────────────
Alplas-specific logic for WooCommerce variable products.

Alplas builds its prices entirely in JS — the static HTML page contains
all variation data embedded in the <form> element as a JSON blob on the
data-product_variations attribute.  Prices are NOT visible until a
dimension option is selected client-side.

This module:
  1. Parses the woocommerce variation JSON from raw HTML (no browser needed)
     to get all variation IDs, attribute values, and 1+ tier prices.
  2. Matches a UKPOS SKU to the best-fit variation using dimension tokens
     extracted from the UKPOS short_title.
  3. Builds a canonical URL with ?variation_id=XXXX&attribute_dimension=...
     that is stored in competitor_matches so scrape.py can use it on
     every subsequent run.
  4. Extracts the ex-VAT price directly from the blob — no page interaction,
     no waiting for JS to calculate.

Usage
─────
Called by scrape.py (to price-check an already-stored variation URL) and by
scripts/discover_alplas_variants.py (to bulk-fix all Alplas competitor_matches
rows to use canonical variation URLs).

Public API
──────────
  parse_variation_blob(html: str, base_url: str) -> AlplasProduct | None
  match_variation(ap: AlplasProduct, sku: dict) -> VariationMatch | None
  variation_url(base_url: str, variation_id: str, attr_dim: str) -> str
  price_from_variation(ap: AlplasProduct, variation_id: str) -> float | None
  scrape_alplas_page(html: str, url: str, sku: dict) -> AlplasScrapeResult
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse, urlencode, urlunparse, parse_qs

log = logging.getLogger("pricewatch.alplas")

ALPLAS_DOMAIN = "alplas.com"

# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class AlplasVariation:
    variation_id: str
    attribute_dimension: str    # e.g. "A5 landscape - Product No. 911-A5L"
    price_ex_vat: Optional[float]
    price_inc_vat: Optional[float]
    in_stock: bool
    sku: str                    # Alplas internal SKU, e.g. "911-A5L"


@dataclass
class AlplasProduct:
    product_id: str
    base_url: str               # canonical parent URL (no params)
    variations: list[AlplasVariation]

    def all_dimensions(self) -> list[str]:
        return [v.attribute_dimension for v in self.variations]

    def by_id(self, variation_id: str) -> Optional[AlplasVariation]:
        for v in self.variations:
            if v.variation_id == variation_id:
                return v
        return None

    def by_dimension(self, dim: str) -> Optional[AlplasVariation]:
        dl = dim.lower()
        for v in self.variations:
            if v.attribute_dimension.lower() == dl:
                return v
        # Partial match fallback
        for v in self.variations:
            if dl in v.attribute_dimension.lower():
                return v
        return None


@dataclass
class VariationMatch:
    variation_id: str
    attribute_dimension: str
    url: str
    price_ex_vat: Optional[float]
    in_stock: bool
    score: int
    reasoning: str


@dataclass
class AlplasScrapeResult:
    success: bool
    price_ex_vat: Optional[float]
    in_stock: bool
    matched_variation: Optional[VariationMatch]
    product: Optional[AlplasProduct]
    error: Optional[str] = None
    vat: str = "ex"
    availability: str = "in_stock"


# ── HTML blob extraction ───────────────────────────────────────────────────────

# WooCommerce embeds variation data in two ways:
# 1. data-product_variations='[...]' on the <form class="variations_form">
# 2. A <script> block: var productVariations = [...]
# We try both.

_FORM_PATTERN = re.compile(
    r'data-product_variations=["\'](\[.*?\])["\']',
    re.DOTALL,
)

_SCRIPT_PATTERN = re.compile(
    r'(?:productVariations|wc_product_variations)\s*[=:]\s*(\[.*?\]);',
    re.DOTALL,
)


def _parse_price(val) -> Optional[float]:
    """Parse a price value from the WooCommerce JSON blob."""
    if val is None:
        return None
    try:
        f = float(str(val).replace(",", ""))
        return round(f, 4) if 0.001 < f < 99999 else None
    except (ValueError, TypeError):
        return None


def parse_variation_blob(html: str, base_url: str = "") -> Optional[AlplasProduct]:
    """
    Extract and parse WooCommerce variation data from raw Alplas product page HTML.

    Returns None if no variation data is found (simple product or parse failure).
    """
    raw_json = None

    # Strategy 1: data-product_variations attribute on the form
    m = _FORM_PATTERN.search(html)
    if m:
        raw_json = m.group(1)
        log.debug("  Alplas: found variation blob via data-product_variations attr")

    # Strategy 2: inline script variable
    if not raw_json:
        m = _SCRIPT_PATTERN.search(html)
        if m:
            raw_json = m.group(1)
            log.debug("  Alplas: found variation blob via script variable")

    if not raw_json:
        log.debug("  Alplas: no variation blob found in HTML")
        return None

    try:
        variations_data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        log.warning(f"  Alplas: variation JSON parse error: {e}")
        return None

    if not isinstance(variations_data, list) or not variations_data:
        log.debug("  Alplas: empty variation list")
        return None

    # ── Extract product ID from the form ──────────────────────────────────────
    product_id_match = re.search(r'data-product_id=["\'](\d+)["\']', html)
    product_id = product_id_match.group(1) if product_id_match else "unknown"

    # ── Strip params from base_url ─────────────────────────────────────────────
    parsed = urlparse(base_url)
    clean_base = urlunparse(parsed._replace(query="", fragment=""))

    # ── Parse each variation ───────────────────────────────────────────────────
    variations = []
    for v in variations_data:
        if not isinstance(v, dict):
            continue

        variation_id = str(v.get("variation_id", ""))
        if not variation_id:
            continue

        # Attributes dict — e.g. {"attribute_dimension": "A5 landscape - Product No. 911-A5L"}
        attrs = v.get("attributes", {})
        attr_dim = ""
        for key, val in attrs.items():
            if "dimension" in key.lower() or "size" in key.lower():
                attr_dim = val or ""
                break
        # Fallback: take any attribute value if no dimension key found
        if not attr_dim and attrs:
            attr_dim = list(attrs.values())[0] or ""

        # Price — WooCommerce exposes display_price (inc VAT) and
        # display_regular_price. The ex-VAT price is in price_html or
        # can be derived. Some Alplas themes also emit a custom
        # "price_ex_vat" or "base_price" key. Try in priority order:
        price_ex_vat  = _parse_price(v.get("price_ex_vat"))
        price_inc_vat = _parse_price(v.get("display_price"))

        if price_ex_vat is None and price_inc_vat is not None:
            # Derive ex-VAT at standard UK rate (20%)
            price_ex_vat = round(price_inc_vat / 1.2, 4)

        if price_ex_vat is None:
            # Try parsing from price_html — last resort
            price_html = v.get("price_html", "")
            prices_in_html = re.findall(r'[\d]+\.[\d]{2}', price_html.replace(",", ""))
            if prices_in_html:
                # Take the smallest price found in the HTML (usually ex-VAT on Alplas)
                price_ex_vat = min(float(p) for p in prices_in_html)

        in_stock = bool(v.get("is_in_stock", True))
        alplas_sku = str(v.get("sku", ""))

        variations.append(AlplasVariation(
            variation_id=variation_id,
            attribute_dimension=attr_dim,
            price_ex_vat=price_ex_vat,
            price_inc_vat=price_inc_vat,
            in_stock=in_stock,
            sku=alplas_sku,
        ))

    if not variations:
        log.debug("  Alplas: parsed 0 valid variations from blob")
        return None

    log.debug(f"  Alplas: parsed {len(variations)} variations from product {product_id}")
    return AlplasProduct(
        product_id=product_id,
        base_url=clean_base,
        variations=variations,
    )


# ── Variation URL builder ──────────────────────────────────────────────────────

def variation_url(base_url: str, variation_id: str, attr_dim: str) -> str:
    """
    Build a canonical Alplas variation URL:
      https://www.alplas.com/product/pvc-sleeves/?variation_id=12345&attribute_dimension=A5+landscape+...

    WooCommerce reads variation_id on page load and pre-selects the variant,
    making the price available in the DOM immediately.
    """
    clean_base = base_url.split("?")[0]
    params = urlencode({
        "variation_id": variation_id,
        "attribute_dimension": attr_dim,
    })
    return f"{clean_base}?{params}"


# ── Price extraction from blob ─────────────────────────────────────────────────

def price_from_variation(ap: AlplasProduct, variation_id: str) -> Optional[float]:
    """Return ex-VAT price for a variation ID, or None."""
    v = ap.by_id(variation_id)
    if v and v.price_ex_vat and v.price_ex_vat > 0:
        return round(v.price_ex_vat, 4)
    return None


# ── Variation matching ─────────────────────────────────────────────────────────

# Dimension tokens we look for in UKPOS titles
_DIM_PATTERN = re.compile(
    r"\b(a[0-7](?:\s+(?:portrait|landscape|p|l))?|"
    r"[0-9]+\s*x\s*[0-9]+(?:\s*mm)?|"
    r"[0-9]+(?:\.[0-9]+)?\s*mm)\b",
    re.IGNORECASE,
)

_ORIENT_PATTERN = re.compile(
    r"\b(portrait|landscape)\b",
    re.IGNORECASE,
)


def _dim_tokens(text: str) -> set[str]:
    return {m.group(0).lower().replace(" ", "") for m in _DIM_PATTERN.finditer(text)}


def _orientation(text: str) -> Optional[str]:
    m = _ORIENT_PATTERN.search(text)
    return m.group(0).lower() if m else None


def _score_variation(var: AlplasVariation, sku_title: str) -> tuple[int, str]:
    """Score a variation against a UKPOS SKU title. Returns (score, reasoning)."""
    score = 0
    reasons = []

    sku_dims   = _dim_tokens(sku_title)
    sku_orient = _orientation(sku_title)
    var_dims   = _dim_tokens(var.attribute_dimension)
    var_orient = _orientation(var.attribute_dimension)

    # ── Size match ────────────────────────────────────────────────────────────
    if sku_dims and var_dims:
        if sku_dims & var_dims:
            score += 60
            reasons.append(f"size match: {sku_dims & var_dims}")
        else:
            score -= 30
            reasons.append(f"size mismatch: sku={sku_dims} var={var_dims}")
    elif not sku_dims:
        score += 5
        reasons.append("no size token in sku title")

    # ── Orientation match ─────────────────────────────────────────────────────
    if sku_orient and var_orient:
        if sku_orient == var_orient:
            score += 30
            reasons.append(f"orientation match: {sku_orient}")
        else:
            score -= 20
            reasons.append(f"orientation mismatch: sku={sku_orient} var={var_orient}")
    elif sku_orient and not var_orient:
        # Var has no orientation signal — slight penalty
        score -= 5

    # ── Alplas SKU code match (e.g. "911-A5L" in UKPOS title or sku_id) ──────
    if var.sku:
        sku_code = var.sku.replace("-", "").lower()
        if sku_code in sku_title.lower().replace("-", "").replace(" ", ""):
            score += 20
            reasons.append(f"Alplas SKU code match: {var.sku}")

    return max(0, min(100, score)), "; ".join(reasons) if reasons else "no signal"


def match_variation(
    ap: AlplasProduct,
    sku: dict,
    prefer_in_stock: bool = True,
) -> Optional[VariationMatch]:
    """
    Find the best-matching variation for a UKPOS SKU.

    Also checks the existing URL in competitor_matches:
    if it already contains attribute_dimension=..., try that label first
    as an exact match before scoring.
    """
    title = sku.get("short_title", "")
    if not ap.variations:
        return None

    # ── Check if the current URL already has an exact attribute_dimension ─────
    # (passed via sku dict as sku.get("_existing_url"))
    existing_url = sku.get("_existing_url", "")
    if existing_url:
        qs = parse_qs(urlparse(existing_url).query)
        existing_dim = qs.get("attribute_dimension", [None])[0]
        if existing_dim:
            exact = ap.by_dimension(existing_dim)
            if exact:
                url = variation_url(ap.base_url, exact.variation_id, exact.attribute_dimension)
                return VariationMatch(
                    variation_id=exact.variation_id,
                    attribute_dimension=exact.attribute_dimension,
                    url=url,
                    price_ex_vat=exact.price_ex_vat,
                    in_stock=exact.in_stock,
                    score=100,
                    reasoning=f"exact match from existing URL: {existing_dim}",
                )

    # ── Score all variations ───────────────────────────────────────────────────
    scored = []
    for var in ap.variations:
        score, reasoning = _score_variation(var, title)
        scored.append((score, var.in_stock, var, reasoning))

    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    best_score, best_in_stock, best_var, best_reason = scored[0]

    # Prefer in-stock within 10 points
    if prefer_in_stock and not best_in_stock:
        for score, in_stock, var, reasoning in scored:
            if in_stock and (best_score - score) <= 10:
                best_score, best_in_stock, best_var, best_reason = score, in_stock, var, reasoning
                log.debug(f"  Alplas: swapping OOS {best_var.variation_id} for in-stock {var.variation_id}")
                break

    url = variation_url(ap.base_url, best_var.variation_id, best_var.attribute_dimension)
    log.info(
        f"  Alplas variation match: id={best_var.variation_id} "
        f"dim='{best_var.attribute_dimension}' score={best_score} "
        f"price_ex=£{best_var.price_ex_vat} in_stock={best_var.in_stock}"
    )
    log.debug(f"  Alplas match reasoning: {best_reason}")

    return VariationMatch(
        variation_id=best_var.variation_id,
        attribute_dimension=best_var.attribute_dimension,
        url=url,
        price_ex_vat=best_var.price_ex_vat,
        in_stock=best_var.in_stock,
        score=best_score,
        reasoning=best_reason,
    )


# ── All variations (for bulk discovery / CSV audit) ───────────────────────────

def all_variation_matches(ap: AlplasProduct) -> list[dict]:
    """
    Return every variation as a flat dict.
    Used by discover_alplas_variants.py for audit CSV output.
    """
    return [
        {
            "variation_id":       v.variation_id,
            "attribute_dimension": v.attribute_dimension,
            "alplas_sku":          v.sku,
            "url":                 variation_url(ap.base_url, v.variation_id, v.attribute_dimension),
            "price_ex_vat":        v.price_ex_vat,
            "price_inc_vat":       v.price_inc_vat,
            "in_stock":            v.in_stock,
        }
        for v in ap.variations
    ]


# ── High-level scrape helper ───────────────────────────────────────────────────

def scrape_alplas_page(html: str, url: str, sku: dict) -> AlplasScrapeResult:
    """
    Given the raw HTML of an Alplas product page, extract the best variation
    price for the given UKPOS SKU.

    Three cases:
      A. URL has ?variation_id=... → read price directly from blob for that ID.
      B. URL has ?attribute_dimension=... → look up that dimension label.
      C. Neither → match best variation by scoring against sku title.

    Returns AlplasScrapeResult.  The caller (scrape.py) writes the price and
    may update competitor_matches.competitor_url to the canonical variation URL.
    """
    ap = parse_variation_blob(html, base_url=url)

    if ap is None:
        return AlplasScrapeResult(
            success=False, price_ex_vat=None, in_stock=False,
            matched_variation=None, product=None,
            error="No WooCommerce variation blob found — may be a simple product",
        )

    # ── Parse URL params ───────────────────────────────────────────────────────
    qs = parse_qs(urlparse(url).query)
    variation_id_from_url = qs.get("variation_id", [None])[0]
    attr_dim_from_url     = qs.get("attribute_dimension", [None])[0]

    # ── Case A: variation_id in URL ────────────────────────────────────────────
    if variation_id_from_url:
        var = ap.by_id(variation_id_from_url)
        if var:
            vurl = variation_url(ap.base_url, var.variation_id, var.attribute_dimension)
            vm = VariationMatch(
                variation_id=var.variation_id,
                attribute_dimension=var.attribute_dimension,
                url=vurl,
                price_ex_vat=var.price_ex_vat,
                in_stock=var.in_stock,
                score=100,
                reasoning="variation_id resolved from URL",
            )
            log.info(f"  Alplas Case A: variation={variation_id_from_url} price_ex=£{var.price_ex_vat}")
            return AlplasScrapeResult(
                success=True,
                price_ex_vat=var.price_ex_vat,
                in_stock=var.in_stock,
                matched_variation=vm,
                product=ap,
                vat="ex",
                availability="in_stock" if var.in_stock else "out_of_stock",
            )

    # ── Case B: attribute_dimension in URL ─────────────────────────────────────
    if attr_dim_from_url:
        var = ap.by_dimension(attr_dim_from_url)
        if var:
            vurl = variation_url(ap.base_url, var.variation_id, var.attribute_dimension)
            vm = VariationMatch(
                variation_id=var.variation_id,
                attribute_dimension=var.attribute_dimension,
                url=vurl,
                price_ex_vat=var.price_ex_vat,
                in_stock=var.in_stock,
                score=90,
                reasoning=f"dimension label resolved from URL: {attr_dim_from_url}",
            )
            log.info(
                f"  Alplas Case B: dim='{attr_dim_from_url}' "
                f"variation={var.variation_id} price_ex=£{var.price_ex_vat}"
            )
            return AlplasScrapeResult(
                success=True,
                price_ex_vat=var.price_ex_vat,
                in_stock=var.in_stock,
                matched_variation=vm,
                product=ap,
                vat="ex",
                availability="in_stock" if var.in_stock else "out_of_stock",
            )

    # ── Case C: match by SKU title ─────────────────────────────────────────────
    sku_with_url = {**sku, "_existing_url": url}
    vm = match_variation(ap, sku_with_url)
    if vm is None:
        return AlplasScrapeResult(
            success=False, price_ex_vat=None, in_stock=False,
            matched_variation=None, product=ap,
            error="No variation matched",
        )

    log.info(
        f"  Alplas Case C: matched variation={vm.variation_id} "
        f"score={vm.score} price_ex=£{vm.price_ex_vat}"
    )
    return AlplasScrapeResult(
        success=True,
        price_ex_vat=vm.price_ex_vat,
        in_stock=vm.in_stock,
        matched_variation=vm,
        product=ap,
        vat="ex",
        availability="in_stock" if vm.in_stock else "out_of_stock",
    )
