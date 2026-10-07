"""
scraper/visual_displays.py
──────────────────────────
Visual Displays (visualdisplays.co.uk) variant matching.

Visual Displays runs on Shopify. A product page with several sizes
(e.g. wooden-card-holder-base: CWB-1 10.5cm / CWB-2 14.8cm / CWB-3 21cm)
serves ONE URL, and the page shows the default (first) variant unless the URL
carries ?variant=ID. Reading the page, or variants[0] of the product feed,
therefore gives the wrong price for every non-default child SKU.

Their variant SKUs equal our child sku_id, so we match on SKU exactly:

  1. Fetch  /products/<handle>.js   (prices are integer pence)
  2. Find the variant whose sku == our sku_id (case-insensitive)
  3. Price comes from that variant; canonical URL is <page>?variant=<id>

Fail-closed rule (silently wrong data is worse than no data):
  - Multi-variant product and no variant carries our SKU → NO match, no price.
  - Single-variant product → that variant is the only possible one, accepted.

Public API
──────────
  VISUAL_DISPLAYS_DOMAIN
  handle_from_url(url)                      -> str
  js_url(url)                               -> str
  variant_url(url, variant_id)              -> str
  match_vd_variant(product, sku_id, url)    -> VDMatch | None
  all_variant_info(product)                 -> list[dict]
"""

import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

log = logging.getLogger("pricewatch.visual_displays")

VISUAL_DISPLAYS_DOMAIN = "visualdisplays.co.uk"


@dataclass
class VDMatch:
    variant_id: str
    sku: str
    title: str
    price: float          # as shown on their site (ex-VAT), pounds
    available: bool
    url: str              # canonical <page>?variant=<id>
    reason: str           # 'sku' | 'single_variant'


def handle_from_url(url: str) -> str:
    path = urlparse(url or "").path.rstrip("/")
    return path.split("/")[-1] if "/products/" in path else ""


def _base(url: str) -> str:
    return (url or "").split("?")[0].split("#")[0].rstrip("/")


def js_url(url: str) -> str:
    return _base(url) + ".js"


def variant_url(url: str, variant_id) -> str:
    return f"{_base(url)}?variant={variant_id}"


def _child_code(raw) -> str:
    """Their variant SKUs are sometimes 'PARENT / CHILD' (e.g. 'MAC2 / MAC2A1K');
    our sku_id is the child code, so compare on the part after the last '/'."""
    return str(raw or "").split("/")[-1].strip().upper()


def _price_gbp(v: dict) -> Optional[float]:
    p = v.get("price")
    if p is None:
        return None
    try:
        # Shopify .js returns integer pence (e.g. 458 → £4.58)
        return round(float(p) / 100, 2)
    except (TypeError, ValueError):
        return None


def match_vd_variant(product: dict, sku_id: str, url: str) -> Optional[VDMatch]:
    """Pick the variant of a Shopify /products/<handle>.js payload for sku_id."""
    variants = (product or {}).get("variants") or []
    if not variants or not sku_id:
        return None

    want = sku_id.strip().upper()
    hit = next((v for v in variants if _child_code(v.get("sku")) == want), None)
    reason = "sku"

    if hit is None and len(variants) == 1:
        hit, reason = variants[0], "single_variant"

    if hit is None:
        log.info(
            f"  VD: no variant with sku={sku_id} at {_base(url)} "
            f"(variants: {[v.get('sku') for v in variants]}) — refusing to guess"
        )
        return None

    price = _price_gbp(hit)
    if price is None:
        return None

    return VDMatch(
        variant_id=str(hit["id"]),
        sku=str(hit.get("sku") or ""),
        title=str(hit.get("title") or hit.get("public_title") or ""),
        price=price,
        available=bool(hit.get("available", True)),
        url=variant_url(url, hit["id"]),
        reason=reason,
    )


def all_variant_info(product: dict, page_url: str = "") -> list:
    rows = []
    for v in (product or {}).get("variants") or []:
        rows.append({
            "variant_id": str(v.get("id")),
            "sku":        v.get("sku") or "",
            "title":      v.get("title") or "",
            "price":      _price_gbp(v),
            "available":  bool(v.get("available", True)),
            "url":        variant_url(page_url, v.get("id")) if page_url else "",
        })
    return rows
