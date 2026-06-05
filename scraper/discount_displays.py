"""
scraper/discount_displays.py
────────────────────────────
Discount Displays–specific logic for:

  1. Parsing the Alpine.js / initConfigurableOptions JSON blob embedded in
     every configurable product page — without any JS execution, purely from
     the raw HTML.  This gives us:
       • all attribute axes (Size, Colour, …)
       • every child product ID with its attribute combo and price
       • a ready-made index: {attr_id: option_id} → child_id

  2. Matching a UKPOS SKU to the best-fit child variant on a Discount Displays
     page, using dimension tokens and colour/finish keywords extracted from the
     UKPOS short_title and SKU ID.

  3. Building a canonical variant URL that encodes the selected
     super_attribute params — this URL is what gets stored in
     competitor_matches.competitor_url and re-scraped by scrape.py.

  4. Extracting the ex-VAT price for a matched child directly from the JSON
     blob, so scrape.py never needs to click anything or wait for Alpine.

Usage
─────
Called by discover.py (to populate competitor_matches) and by scrape.py
(to price-check an already-stored variant URL).

Public API
──────────
  parse_configurable_blob(html: str) -> ConfigurableProduct | None
  match_variant(cp: ConfigurableProduct, sku: dict) -> VariantMatch | None
  variant_url(base_url: str, cp: ConfigurableProduct, child_id: str) -> str
  price_from_blob(cp: ConfigurableProduct, child_id: str) -> float | None
  scrape_dd_page(url: str, sku: dict) -> DDScrapeResult
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse, urlencode, urlunparse, parse_qs, urljoin

log = logging.getLogger("pricewatch.discount_displays")

DISCOUNT_DISPLAYS_DOMAIN = "discountdisplays.co.uk"


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class AttributeOption:
    id: str           # e.g. "3832"
    label: str        # e.g. "A3"
    products: list    # child product IDs that have this option


@dataclass
class Attribute:
    id: str           # e.g. "207"
    code: str         # e.g. "notice_board_size"
    label: str        # e.g. "Size"
    options: list[AttributeOption]


@dataclass
class ChildPrice:
    base_price: float        # ex-VAT
    final_price: float       # inc-VAT
    old_price: float         # inc-VAT list price
    tier_prices: list        # [{qty, price, basePrice, percentage}]
    lead_time: Optional[str]
    in_stock: bool


@dataclass
class ConfigurableProduct:
    product_id: str
    base_url: str                         # canonical parent page URL (no params)
    attributes: list[Attribute]
    # child_id -> {attr_id: option_id}
    index: dict[str, dict[str, str]]
    # child_id -> ChildPrice
    prices: dict[str, ChildPrice]
    # child_id -> in_stock bool
    is_saleable: dict[str, bool]

    def option_label(self, attr_id: str, option_id: str) -> Optional[str]:
        for attr in self.attributes:
            if attr.id == attr_id:
                for opt in attr.options:
                    if opt.id == option_id:
                        return opt.label
        return None

    def child_labels(self, child_id: str) -> dict[str, str]:
        """Return {attr_label: option_label} for this child, e.g. {"Size": "A3", "Stain Colour": "Dark Oak"}"""
        combo = self.index.get(child_id, {})
        result = {}
        for attr_id, option_id in combo.items():
            for attr in self.attributes:
                if attr.id == attr_id:
                    label = self.option_label(attr_id, option_id)
                    if label:
                        result[attr.label] = label
        return result

    def all_children(self) -> list[str]:
        return list(self.index.keys())


@dataclass
class VariantMatch:
    child_id: str
    score: int
    labels: dict[str, str]        # e.g. {"Size": "A3", "Stain Colour": "Dark Oak"}
    url: str                      # canonical variant URL with super_attribute params
    price_ex_vat: Optional[float]
    in_stock: bool
    reasoning: str                # human-readable explanation of why this was chosen


# ── JSON blob extraction ───────────────────────────────────────────────────────

_BLOB_PATTERN = re.compile(
    r"initConfigurableOptions\(\s*'(\d+)'\s*,\s*(\{.*?\})\s*\)",
    re.DOTALL,
)


def parse_configurable_blob(html: str, base_url: str = "") -> Optional[ConfigurableProduct]:
    """
    Extract and parse the initConfigurableOptions JSON blob from raw HTML.

    Returns None if the page is not a configurable product or the blob
    cannot be parsed.
    """
    m = _BLOB_PATTERN.search(html)
    if not m:
        log.debug("No initConfigurableOptions blob found")
        return None

    product_id = m.group(1)
    try:
        config = json.loads(m.group(2))
    except json.JSONDecodeError as e:
        log.warning(f"  DD blob JSON parse error: {e}")
        return None

    # ── Attributes ────────────────────────────────────────────────────────────
    raw_attrs = config.get("attributes", {})
    attributes = []
    for attr_id, attr_data in raw_attrs.items():
        options = [
            AttributeOption(
                id=opt["id"],
                label=opt["label"],
                products=opt.get("products", []),
            )
            for opt in attr_data.get("options", [])
        ]
        attributes.append(Attribute(
            id=attr_id,
            code=attr_data.get("code", ""),
            label=attr_data.get("label", ""),
            options=options,
        ))

    # ── Option prices ─────────────────────────────────────────────────────────
    raw_prices = config.get("optionPrices", {})
    prices = {}
    for child_id, p in raw_prices.items():
        prices[child_id] = ChildPrice(
            base_price=float(p.get("basePrice", {}).get("amount", 0)),
            final_price=float(p.get("finalPrice", {}).get("amount", 0)),
            old_price=float(p.get("oldPrice", {}).get("amount", 0)),
            tier_prices=p.get("tierPrices", []),
            lead_time=p.get("lead_time"),
            in_stock=True,  # refined below from is_saleable
        )

    # ── Index ─────────────────────────────────────────────────────────────────
    index = config.get("index", {})  # {child_id: {attr_id: option_id}}

    # ── Saleable status ───────────────────────────────────────────────────────
    is_saleable = {
        child_id: bool(v)
        for child_id, v in config.get("is_saleable", {}).items()
    }
    # Update the in_stock flag on ChildPrice objects
    for child_id, cp in prices.items():
        cp.in_stock = is_saleable.get(child_id, True)

    # Strip params from base_url so we always store the canonical parent URL
    parsed = urlparse(base_url)
    clean_base = urlunparse(parsed._replace(query="", fragment=""))

    return ConfigurableProduct(
        product_id=product_id,
        base_url=clean_base,
        attributes=attributes,
        index=index,
        prices=prices,
        is_saleable=is_saleable,
    )


# ── Variant URL builder ────────────────────────────────────────────────────────

def variant_url(base_url: str, cp: ConfigurableProduct, child_id: str) -> str:
    """
    Build a URL like:
      https://…/product.html?super_attribute[207]=3832&super_attribute[519]=3602

    The params encode the attribute selection for this child variant.
    Note: Discount Displays uses these params as UI hints only (Alpine.js
    reads them on load). The canonical price lives in the JSON blob, not
    the server response to this URL.
    """
    combo = cp.index.get(child_id, {})
    # Sort by attr_id for determinism
    params = "&".join(
        f"super_attribute[{attr_id}]={option_id}"
        for attr_id, option_id in sorted(combo.items())
    )
    clean_base = base_url.split("?")[0]
    return f"{clean_base}?{params}" if params else clean_base


# ── Price extraction from blob ─────────────────────────────────────────────────

def price_from_blob(cp: ConfigurableProduct, child_id: str) -> Optional[float]:
    """Return the ex-VAT (basePrice) for a child variant, or None."""
    cp_obj = cp.prices.get(child_id)
    if cp_obj and cp_obj.base_price > 0:
        return round(cp_obj.base_price, 2)
    return None


# ── Variant matching ───────────────────────────────────────────────────────────

# Tokens we recognise as dimension/size signals in UKPOS titles
_SIZE_TOKENS = re.compile(
    r"\b("
    r"a[0-7]"                          # A4, A3, A2, A1, A0 …
    r"|[0-9]+\s*x\s*[0-9]+(?:\s*mm)?" # 400x600mm etc.
    r"|[0-9]+(?:\.[0-9]+)?\s*mm"       # 297mm etc.
    r"|[0-9]+(?:\.[0-9]+)?\s*cm"
    r")\b",
    re.IGNORECASE,
)

# Colour / finish keywords we try to match
_COLOUR_TOKENS = {
    "oak":            ["oak", "westminster oak", "dark oak"],
    "dark oak":       ["dark oak", "dark", "oak"],
    "westminster oak":["westminster", "westminster oak"],
    "white":          ["white", "white wash", "whitewash"],
    "black":          ["black", "black ash", "blackash"],
    "ash":            ["ash", "black ash"],
    "grey":           ["grey", "gray", "slate", "slate grey"],
    "slate grey":     ["slate", "grey", "gray", "slate grey"],
    "natural":        ["natural", "wood", "pine", "light"],
    "silver":         ["silver", "aluminium", "aluminum", "alu"],
    "gold":           ["gold", "brass"],
    "chrome":         ["chrome", "stainless"],
}


def _size_tokens(text: str) -> set[str]:
    return {m.group(0).lower().replace(" ", "") for m in _SIZE_TOKENS.finditer(text)}


def _colour_hints(text: str) -> set[str]:
    t = text.lower()
    found = set()
    for canonical, aliases in _COLOUR_TOKENS.items():
        if any(alias in t for alias in aliases):
            found.add(canonical)
    return found


def _score_child(child_id: str, cp: ConfigurableProduct, sku_title: str, sku_id: str = "") -> tuple[int, str]:
    """
    Score a child variant against a UKPOS SKU title and SKU ID.
    Returns (score 0-100, reasoning string).
    """
    labels = cp.child_labels(child_id)
    score = 0
    reasons = []

    sku_sizes   = _size_tokens(sku_title)
    sku_colours = _colour_hints(sku_title)

    # Extract A-size directly from SKU ID (e.g. "A2" from "HLEDA2")
    a_size_in_sku_id = re.search(r'A[0-7]', sku_id, re.IGNORECASE)
    sku_id_sizes = {a_size_in_sku_id.group(0).lower()} if a_size_in_sku_id else set()

    # Combined size signal from title + SKU ID
    all_sku_sizes = sku_sizes | sku_id_sizes

    for attr_label, option_label in labels.items():
        opt_lower = option_label.lower()

        # ── Size matching ──────────────────────────────────────────────────
        if any(k in attr_label.lower() for k in ("size", "dimension", "format", "graphic", "width", "height", "length", "depth", "notice_board")):
            opt_sizes = _size_tokens(option_label)
            if all_sku_sizes and opt_sizes:
                if all_sku_sizes & opt_sizes:
                    score += 50
                    reasons.append(f"size match: {option_label}")
                else:
                    score -= 20
                    reasons.append(f"size MISMATCH: sku={all_sku_sizes} opt={opt_sizes}")
            elif not all_sku_sizes:
                # No size in UKPOS title or SKU ID — treat as neutral (not a penalty)
                score += 5
                reasons.append("size: no token in sku title or id")
            else:
                score -= 5

        # ── Colour / finish matching ───────────────────────────────────────
        elif any(k in attr_label.lower() for k in ("colour", "color", "finish", "stain", "material")):
            opt_colours = _colour_hints(option_label)
            if sku_colours and opt_colours:
                if sku_colours & opt_colours:
                    score += 30
                    reasons.append(f"colour match: {option_label}")
                elif not sku_colours:
                    score += 5
                else:
                    score -= 10
                    reasons.append(f"colour mismatch: sku={sku_colours} opt={opt_colours}")
            elif not sku_colours:
                score += 3
            # if no colour hint in sku title, neutral

        # ── Generic text overlap for other axes ───────────────────────────
        else:
            sku_words = set(re.findall(r"\b[a-z]{3,}\b", sku_title.lower()))
            opt_words = set(re.findall(r"\b[a-z]{3,}\b", opt_lower))
            common = sku_words & opt_words
            if common:
                score += min(15, len(common) * 5)
                reasons.append(f"word overlap: {common}")

    return max(0, min(100, score)), "; ".join(reasons) if reasons else "no signal"


def match_variant(
    cp: ConfigurableProduct,
    sku: dict,
    prefer_in_stock: bool = True,
) -> Optional[VariantMatch]:
    """
    Find the best-matching child variant for a UKPOS SKU.

    Strategy:
      1. Score every child against the UKPOS short_title and sku_id.
      2. Break ties by preferring in-stock children.
      3. Return None only if the product has no children at all.

    The score intentionally doesn't require a perfect match — on single-axis
    products (size only, no colour) every child gets a meaningful score.
    """
    title  = sku.get("short_title", "")
    sku_id = sku.get("sku_id", "")
    children = cp.all_children()
    if not children:
        return None

    scored = []
    for child_id in children:
        score, reasoning = _score_child(child_id, cp, title, sku_id)
        in_stock = cp.is_saleable.get(child_id, True)
        scored.append((score, in_stock, child_id, reasoning))

    # Sort: highest score first; in-stock before OOS on equal scores
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)

    best_score, best_in_stock, best_id, best_reason = scored[0]

    # If prefer_in_stock and the best match is OOS, check if there's an
    # in-stock child within 10 points of the best
    if prefer_in_stock and not best_in_stock:
        for score, in_stock, child_id, reasoning in scored:
            if in_stock and (best_score - score) <= 10:
                log.debug(f"  DD: swapping OOS {best_id} for in-stock {child_id} (score diff {best_score - score})")
                best_score, best_in_stock, best_id, best_reason = score, in_stock, child_id, reasoning
                break

    labels = cp.child_labels(best_id)
    url    = variant_url(cp.base_url, cp, best_id)
    price  = price_from_blob(cp, best_id)

    log.info(
        f"  DD variant match: child={best_id} score={best_score} "
        f"labels={labels} price=£{price} in_stock={best_in_stock}"
    )
    log.debug(f"  DD match reasoning: {best_reason}")

    return VariantMatch(
        child_id=best_id,
        score=best_score,
        labels=labels,
        url=url,
        price_ex_vat=price,
        in_stock=best_in_stock,
        reasoning=best_reason,
    )


# ── All variants (for bulk discovery) ─────────────────────────────────────────

def all_variant_matches(cp: ConfigurableProduct) -> list[dict]:
    """
    Return a flat list of every child variant with its labels, price and URL.
    Useful for logging or bulk-inserting all variants into a staging table.

    Each dict has keys:
      child_id, labels, url, price_ex_vat, price_inc_vat, in_stock,
      tier_prices, lead_time
    """
    result = []
    for child_id in cp.all_children():
        p = cp.prices.get(child_id)
        result.append({
            "child_id":     child_id,
            "labels":       cp.child_labels(child_id),
            "url":          variant_url(cp.base_url, cp, child_id),
            "price_ex_vat": p.base_price  if p else None,
            "price_inc_vat":p.final_price if p else None,
            "in_stock":     cp.is_saleable.get(child_id, True),
            "tier_prices":  p.tier_prices if p else [],
            "lead_time":    p.lead_time   if p else None,
        })
    return result


# ── High-level scrape result ───────────────────────────────────────────────────

@dataclass
class DDScrapeResult:
    """Result from scraping a Discount Displays page for a specific UKPOS SKU."""
    success: bool
    price_ex_vat: Optional[float]
    in_stock: bool
    matched_variant: Optional[VariantMatch]
    configurable: Optional[ConfigurableProduct]
    error: Optional[str] = None
    # Pass these back to scrape.py for compatibility
    vat: str = "ex"
    availability: str = "in_stock"


async def scrape_dd_page(
    page,           # Playwright Page object (already navigated)
    sku: dict,
    url: str,
) -> DDScrapeResult:
    """
    Given a Playwright page already loaded at a Discount Displays product URL,
    extract all variant data from the JSON blob and return a DDScrapeResult.

    This replaces both the Alpine-wait heuristic in _extract_discount_displays_price
    and the need to click size/colour selectors.  Price comes directly from the
    blob — no JS execution required beyond getting the raw HTML.

    Handles three cases:
      A. url has super_attribute params → extract child_id from params, read
         price directly from blob.
      B. url has no params + sku has enough signals → match best variant,
         return its price.
      C. Simple (non-configurable) product → fall through, return None price
         so scrape.py uses its normal extraction chain.
    """
    try:
        html = await page.content()
    except Exception as e:
        return DDScrapeResult(
            success=False, price_ex_vat=None, in_stock=False,
            matched_variant=None, configurable=None,
            error=f"page.content() failed: {e}",
        )

    cp = parse_configurable_blob(html, base_url=url)

    # ── Case C: not a configurable product ────────────────────────────────────
    if cp is None:
        return DDScrapeResult(
            success=False, price_ex_vat=None, in_stock=False,
            matched_variant=None, configurable=None,
            error="Not a configurable product (no JSON blob found)",
        )

    # ── Detect pre-selected variant from URL params ───────────────────────────
    parsed   = urlparse(url)
    qs       = parse_qs(parsed.query)
    # Build selection map from params: {attr_id: option_id}
    selected = {}
    for key, vals in qs.items():
        m = re.match(r"super_attribute\[(\d+)\]", key)
        if m:
            selected[m.group(1)] = vals[0]

    child_id_from_url = None
    if selected:
        # Find the child whose index entry exactly matches the selection
        for cid, combo in cp.index.items():
            if all(combo.get(k) == v for k, v in selected.items()):
                child_id_from_url = cid
                break

    # ── Case A: URL encodes a specific variant ────────────────────────────────
    if child_id_from_url:
        price    = price_from_blob(cp, child_id_from_url)
        in_stock = cp.is_saleable.get(child_id_from_url, True)
        labels   = cp.child_labels(child_id_from_url)
        vurl     = variant_url(cp.base_url, cp, child_id_from_url)
        vm = VariantMatch(
            child_id=child_id_from_url, score=100, labels=labels,
            url=vurl, price_ex_vat=price, in_stock=in_stock,
            reasoning="child_id resolved from URL super_attribute params",
        )
        log.info(f"  DD Case A: child={child_id_from_url} price=£{price} in_stock={in_stock}")
        return DDScrapeResult(
            success=True,
            price_ex_vat=price,
            in_stock=in_stock,
            matched_variant=vm,
            configurable=cp,
            vat="ex",
            availability="in_stock" if in_stock else "out_of_stock",
        )

    # ── Case B: match best variant for this UKPOS SKU ─────────────────────────
    vm = match_variant(cp, sku)
    if vm is None:
        return DDScrapeResult(
            success=False, price_ex_vat=None, in_stock=False,
            matched_variant=None, configurable=cp,
            error="No children in configurable product",
        )

    log.info(
        f"  DD Case B: matched child={vm.child_id} "
        f"score={vm.score} price=£{vm.price_ex_vat} in_stock={vm.in_stock}"
    )
    return DDScrapeResult(
        success=True,
        price_ex_vat=vm.price_ex_vat,
        in_stock=vm.in_stock,
        matched_variant=vm,
        configurable=cp,
        vat="ex",
        availability="in_stock" if vm.in_stock else "out_of_stock",
    )
