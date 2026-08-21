"""
scraper/discover_sitemap.py  —-  Option C: keyword pre-filter + Claude API matching
═══════════════════════════════════════════════════════════════════════════════════

ARCHITECTURE
────────────
Runs one competitor at a time. For each competitor:

  Phase 1 — Harvest
    Fetch their sitemap(s), collect all product URLs, filter out blog/content pages.

  Phase 2 — Fetch & profile each product page
    For each product URL, fetch the page via httpx (or CF Worker for blocked
    domains) and extract: title, description, price, any product codes.
    Build a text profile of what their product actually is.

  Phase 3 — Keyword pre-filter
    Score the competitor product profile against every UKPOS SKU using a
    synonym-aware keyword scorer. Keep only the top 20 candidates.
    Products that score 0 against every SKU are discarded without an API call.

  Phase 4 — Claude API matching
    Send the competitor product details + top 20 SKU candidates to Claude.
    Claude reads both and returns the best 1-3 matches with confidence scores
    and brief reasoning. Only matches above MIN_CONFIDENCE are written.

  Phase 5 — Write to competitor_matches
    Best match written as match_status='review'. Ambiguous matches (top 2
    within 10 points) both written. Confirmed matches (match_status='matched')
    are never overwritten.

ENVIRONMENT VARIABLES
─────────────────────
  SUPABASE_URL          required
  SUPABASE_SERVICE_KEY  required
  ANTHROPIC_API_KEY     required (for Claude matching step)
  CF_PROXY_URL          required for blocked domains (vkf-renzel etc.)
  COMPETITOR_IDS        comma-separated IDs to run (required — run one at a time)
  DISCOVER_FORCE        'true' to re-run already-matched pairs (default false)
  MIN_CONFIDENCE        minimum confidence to write a match (default 40)
  MAX_PAGES             max product pages to fetch per competitor (default 2000)
  LOG_LEVEL             DEBUG / INFO (default INFO)
  PAGE_FETCH_TIMEOUT    seconds per page fetch (default 15)
  ANTHROPIC_BATCH_SIZE  SKU candidates sent to Claude per call (default 20)
"""

import gzip
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import httpx
from supabase import create_client

from common import (
    is_category_url, USER_AGENTS,
    detect_vat, detect_oos, diff_pct, normalise_price, extract_pack_qty,
)

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("pricewatch.discover_sitemap")

# ── Config ─────────────────────────────────────────────────────────────────────
SUPABASE_URL      = os.environ["SUPABASE_URL"]
SUPABASE_KEY      = os.environ["SUPABASE_SERVICE_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CF_PROXY_URL      = os.getenv("CF_PROXY_URL", "")
FORCE             = os.getenv("DISCOVER_FORCE", "false").lower() == "true"
MIN_CONF          = int(os.getenv("MIN_CONFIDENCE", "40"))
MAX_PAGES         = int(os.getenv("MAX_PAGES", "2000"))
PAGE_TIMEOUT      = int(os.getenv("PAGE_FETCH_TIMEOUT", "15"))
BATCH_SIZE        = int(os.getenv("ANTHROPIC_BATCH_SIZE", "20"))
AMBIGUITY_MARGIN  = 10  # write top 2 if scores within this many points

_competitor_ids = [int(i.strip()) for i in os.getenv("COMPETITOR_IDS", "").split(",") if i.strip()]

# ── Domains routed via Cloudflare Worker ───────────────────────────────────────
CF_PROXY_DOMAINS = {
    "vkf-renzel.co.uk", "displaypro.co.uk", "shopfittingwarehouse.co.uk",
    "signwaves.co.uk", "sign-holders.co.uk", "signholdersdirect.co.uk",
}

def _needs_proxy(url: str) -> bool:
    host = urlparse(url).hostname or ""
    clean = host.replace("www.", "")
    return any(clean == d or clean.endswith("." + d) for d in CF_PROXY_DOMAINS)

# ── Content/blog URL filter ────────────────────────────────────────────────────
CONTENT_SIGNALS = [
    "/blog", "/blogs", "/news", "/articles", "/article", "/post/", "/posts/",
    "/journal/", "/resources/", "/guides/", "/guide/", "/tips/", "/about",
    "/terms", "/privacy", "/contact", "/faq", "/pages/", "/info/", "/help/",
]

def _is_content_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(s in path for s in CONTENT_SIGNALS)

def _is_product_url(url: str) -> bool:
    return not is_category_url(url) and not _is_content_url(url)

# ── Per-competitor sitemap config ──────────────────────────────────────────────
def _has_depth(url: str, n: int = 2) -> bool:
    return len([p for p in urlparse(url).path.rstrip("/").split("/") if p]) >= n

def _in_path(url: str, *segs: str) -> bool:
    path = urlparse(url).path.lower()
    return any(s in path for s in segs)

COMPETITOR_SITEMAPS = {
    1:  {"sitemap": "https://www.alplas.com/sitemap.xml",
         "filter":  lambda u: _in_path(u, "/product/") and _is_product_url(u)},
    2:  {"sitemap": "https://www.chalkboardsuk.co.uk/store-products-sitemap.xml",
         # Was https://www.chalkboardsuk.co.uk/sitemap.xml with filter
         # _has_depth(u,2) and _is_product_url(u) — but every real product
         # on this Wix site is a single path segment (e.g. /welcome-sign-stand),
         # and is_category_url() in common.py auto-rejects ANY url with <=2
         # path segments, so that filter could never pass a single URL here.
         # Wix already splits its sitemap into a dedicated products-only file
         # (vs. blog-posts/blog-categories/store-categories/pages), so we
         # just harvest that file directly and accept everything in it.
         "filter":  lambda u: True},
    4:  {"sitemap": "https://www.discountdisplays.co.uk/sitemap.xml",
         # Was filtering on _in_path(u, "/products/") — but this site has
         # no "/products/" segment anywhere; it's a legacy static-HTML site
         # where both products AND category/info pages live flat under
         # /html/*.html at identical depth, so URL shape alone can't fully
         # separate them. We filter to /html/ + exclude known non-product
         # pages by name; any category page that slips through just gets a
         # wasted (harmless) Claude match attempt rather than a bad write,
         # since matches are still gated by confidence threshold downstream.
         "filter":  lambda u: _in_path(u, "/html/") and not _is_content_url(u) and not any(
             seg in u for seg in (
                 "delivery-returns", "resource-centre", "trade.html",
                 "digital-signage.html", "indoor-displays.html", "outdoor-displays.html",
                 "products-a-z.html", "new-products.html", "promotional-products.html",
                 "special-offers.html", "bespoke-projects", "testimonials",
                 "guides-and-advice", "request-a-quote", "plant-list", "cutting-capabilities",
                 "artwork-preparation", "artwork-design-service", "artwork-upload",
             )
         )},
    5:  {"sitemap": "https://displaypro.co.uk/sitemap.xml",
         "filter":  lambda u: _in_path(u, "/products/") and _is_product_url(u)},
    6:  {"sitemap": "https://displaysense.co.uk/sitemap.xml",
         "filter":  lambda u: _has_depth(u, 1) and _is_product_url(u) and "displaysense" in u},
    7:  {"sitemap": "https://www.displaywizard.co.uk/sitemap.xml",
         # Was filtering on _in_path(u, "/products/") — this WordPress site
         # has no "/products/" segment; real products sit at 2+ path
         # segments like /roller-banners/luxe-roller-banner-stands/, and
         # category landing pages are mostly 1 segment (/roller-banners/).
         # Deliberately NOT using _is_product_url()/is_category_url() here —
         # that helper auto-rejects any URL with <=2 path segments, which
         # would kill exactly the 2-segment product URLs this site uses.
         "filter":  lambda u: _has_depth(u, 2) and not _is_content_url(u)},
    8:  {"sitemap": "https://www.gadsby.co.uk/sitemaps-1-sitemap.xml",
         "filter":  lambda u: _has_depth(u, 2) and _is_product_url(u)},
    9:  {"sitemap": "https://www.ghdisplay.co.uk/sitemap_index.xml",
         "filter":  lambda u: _in_path(u, "/product/", "/products/") and _is_product_url(u)},
    10: {"sitemap": "https://www.harrisonproducts.com/xmlsitemap.php",
         "filter":  lambda u: _in_path(u, "/products/") and _is_product_url(u)},
    11: {"sitemap": "https://indigodisplays.co.uk/sitemap.xml",
         "filter":  lambda u: _has_depth(u, 2) and _is_product_url(u)},
    13: {
        "mode": "category_crawl",  # no working /sitemap.xml on this site — see notes below
        "category_urls": [
            "https://pavementsigns.com/gb/categories/",
            "https://pavementsigns.com/gb/categories/pavement-sign-accessories.aspx",
            # NOTE: seed list only — walk https://pavementsigns.com/gb/categories/
            # once to pull the full set of category URLs and add them here.
        ],
        "product_link_pattern": re.compile(r"/gb/products/[^/]+\.aspx$"),
    },
    14: {
        "mode": "category_crawl",  # no working /sitemap.xml on this site — see notes below
        "category_urls": [
            "https://www.retailacrylics.co.uk/cubes--boxes-99-c.asp",
            "https://www.retailacrylics.co.uk/pedestals-100-c.asp",
            "https://www.retailacrylics.co.uk/trays-101-c.asp",
            "https://www.retailacrylics.co.uk/display-cases-102-c.asp",
            "https://www.retailacrylics.co.uk/product-display-103-c.asp",
            "https://www.retailacrylics.co.uk/acrylic-sheets-72-c.asp",
            "https://www.retailacrylics.co.uk/wedding--event-149-c.asp",
            "https://www.retailacrylics.co.uk/printing-148-c.asp",
            # confirmed live from the site's own homepage nav — spot-check
            # against https://www.retailacrylics.co.uk/sitemap.asp for gaps.
        ],
        "product_link_pattern": re.compile(r"-\d+-p\.asp$"),
    },
    15: {"sitemap": "https://www.shopfittingwarehouse.co.uk/sitemap.xml",
         "filter":  lambda u: _has_depth(u, 2) and _is_product_url(u)},
    16: {"sitemap": "https://www.sign-holders.co.uk/sitemap.xml",
         "filter":  lambda u: _has_depth(u, 2) and _is_product_url(u)},
    17: {"sitemap": "https://www.signwaves.co.uk/sitemap.xml",
         "filter":  lambda u: _has_depth(u, 2) and _is_product_url(u)},
    18: {
        "mode": "category_crawl",  # no working /sitemap.xml on this site — see notes below
        "category_urls": [
            "https://www.snapframeswarehouse.co.uk/snap-frames",
            "https://www.snapframeswarehouse.co.uk/bespoke-snap-frames-poster-frames-clip-frame",
            "https://www.snapframeswarehouse.co.uk/external-lockable-poster-cases-from-snap-frames-warehouse",
            "https://www.snapframeswarehouse.co.uk/snap-frames-warehouse-full-product-catalogue",
            # NOTE: seed list only — this OpenCart site's full nav wasn't
            # visible from search alone. Pull the top-level category list
            # directly from the site's own nav menu and add the rest here.
        ],
        "product_link_pattern": re.compile(
            r"snapframeswarehouse\.co\.uk/[A-Za-z0-9][A-Za-z0-9\-]+$"
        ),
    },
    19: {"sitemap": "https://www.theretailfactory.co.uk/sitemap.xml",
         "filter":  lambda u: _has_depth(u, 2) and _is_product_url(u)},
    20: {"sitemap": "https://www.uksignshop.co.uk/sitemap.xml",
         "filter":  lambda u: _has_depth(u, 2) and _is_product_url(u)},
    21: {"sitemap": "https://www.ultimadisplays.com/sitemap.xml",
         "filter":  lambda u: _has_depth(u, 2) and _is_product_url(u)},
    22: {"sitemap": "https://www.verydisplays.com/sitemap_index.xml",
         "filter":  lambda u: _has_depth(u, 2) and _is_product_url(u)},
    23: {"sitemap": "https://www.vkf-renzel.co.uk/sitemap.xml",
         "filter":  lambda u: _has_depth(u, 2) and _is_product_url(u)},
    24: {"sitemap": "https://visualdisplays.co.uk/sitemap.xml",
         "filter":  lambda u: _has_depth(u, 2) and _is_product_url(u)},
    26: {"sitemap": "https://screenmoove.com/sitemap.xml",
         "filter":  lambda u: _has_depth(u, 2) and _is_product_url(u)},
    27: {
        "mode": "category_crawl",  # /sitemap.xml is a real 404 on this site — see notes below
        "category_urls": [
            "https://www.3ddisplays.co.uk/sitemap",
            # The site's own human sitemap page — lists every top-level
            # category (Retail Displays & POS, Trays and Tubs, Shelving,
            # Card Display Stands, Slatwall Panels, Exhibition Displays &
            # Panels, Display Cabinets, etc.). Seeding with this one page
            # should surface the top-level category links; add specific
            # category URLs below if coverage from this alone proves thin.
            "https://www.3ddisplays.co.uk/exhibition-displays-panels-c276",
            "https://www.3ddisplays.co.uk/card-display-stands-c11",
            "https://www.3ddisplays.co.uk/display-cabinets-c10",
        ],
        # This platform uses a 3-tier taxonomy: -c### for categories/
        # subcategories, -t### for product/type listing pages (e.g.
        # /card-display-stands-c11/exhibition-card-racks-c54/card-racks-t478).
        # We don't have visibility into individual per-SKU product-detail
        # URLs (if they exist separately from -t### listing pages), so this
        # pattern accepts both tiers as candidates — some -c### entries may
        # be pure subcategories rather than individual products. Confidence
        # here is lower than the other category_crawl configs; worth
        # spot-checking match quality after the first run.
        "product_link_pattern": re.compile(r"-[ct]\d+(?:/|$)"),
    },
    28: {"sitemap": "https://www.bludisplay.co.uk/sitemap.xml",
         "filter":  lambda u: _has_depth(u, 2) and _is_product_url(u)},
    29: {"sitemap": "https://www.viking-direct.co.uk/sitemap.xml",
         "filter":  lambda u: _has_depth(u, 2) and _is_product_url(u)},
}

# ── Synonym groups ─────────────────────────────────────────────────────────────
# Each group is a set of terms that mean the same product type.
# A competitor product containing ANY term in a group scores a hit
# against a UKPOS SKU that also contains ANY term in that same group.
SYNONYM_GROUPS = [
    # Snap frames
    {"snap frame", "click frame", "poster frame", "aluminium frame", "display frame",
     "snap frames", "click frames", "poster frames", "snapframe", "clickframe"},
    # A-boards / pavement signs
    {"a board", "a-board", "aboard", "pavement sign", "sandwich board", "forecourt sign",
     "footpath sign", "sidewalk sign", "pavement board", "street sign", "a frame sign",
     "a-frame sign", "display board", "outdoor sign board"},
    # Showcard / card stands
    {"showcard", "show card", "card stand", "pos stand", "price card", "ticket stand",
     "card holder", "showcard stand", "show card stand", "ticket holder stand",
     "price ticket", "label holder"},
    # Leaflet / brochure holders
    {"leaflet holder", "brochure holder", "literature holder", "pamphlet holder",
     "flyer holder", "leaflet dispenser", "brochure dispenser", "literature stand",
     "leaflet rack", "brochure rack", "magazine holder", "catalogue holder",
     "leaflet display", "brochure display", "flyer display"},
    # Acrylic poster holders / sleeves
    {"acrylic poster", "poster sleeve", "poster pocket", "sign holder", "display pocket",
     "poster holder", "acrylic sign", "acrylic frame", "perspex poster", "clear poster",
     "poster display", "wall poster holder"},
    # Noticeboards
    {"noticeboard", "notice board", "bulletin board", "pin board", "pinboard",
     "message board", "felt board", "cork board", "corkboard"},
    # Slatwall
    {"slatwall", "slat wall", "slotted panel", "slatboard", "gondola panel",
     "slot panel", "slat panel", "slatwall panel"},
    # Gridwall / mesh
    {"gridwall", "grid panel", "mesh panel", "wire grid", "display grid",
     "grid display", "wire mesh", "grid wall", "mesh display"},
    # Cafe / queue barriers
    {"cafe barrier", "queue barrier", "crowd barrier", "stanchion", "belt barrier",
     "rope barrier", "barrier system", "crowd control", "queue management",
     "retractable barrier", "rope stanchion"},
    # Cable / wire display systems
    {"cable display", "wire display", "hanging poster", "suspended display",
     "cable kit", "wire kit", "poster cable", "window cable", "cable system",
     "rod system", "poster rod", "hanging system"},
    # LED / illuminated displays
    {"led display", "led poster", "led window", "illuminated display", "light box",
     "lightbox", "led light box", "led frame", "led panel", "backlit display",
     "illuminated frame", "led sign"},
    # Fabric tension / pop-up displays
    {"fabric tension", "tension display", "fabric display", "pop up fabric",
     "seg display", "fabric frame", "fabric banner", "tension fabric",
     "fabric exhibition", "popup display", "pull up", "roll up", "banner stand"},
    # Sign holders / wall sign holders
    {"sign holder", "wall sign holder", "wall mount sign", "wall mounted sign",
     "sign stand", "sign frame", "sign display", "poster sign holder"},
    # Acrylic / perspex materials
    {"acrylic", "perspex", "plexiglass", "clear plastic", "pmma", "clear acrylic"},
    # Aluminium / metal
    {"aluminium", "aluminum", "anodised", "anodized", "metal frame", "alloy frame"},
    # Wooden / MDF
    {"mdf", "wood", "wooden", "timber", "oak", "beech", "pine"},
    # Chalkboard / blackboard
    {"chalkboard", "chalk board", "blackboard", "black board", "chalk sign",
     "chalkboard sign", "writeable board", "erasable board"},
    # Window displays / shop window
    {"window display", "shop window", "retail window", "window kit",
     "window hanging", "window sign"},
    # Suction / adhesive fixings
    {"suction cup", "suction hook", "suction pad", "adhesive hook",
     "adhesive pad", "sticky hook"},
    # Magnetic
    {"magnetic", "magnet", "magnetic frame", "magnetic sign", "magnetic holder"},
    # Ticket / price labels
    {"ticket holder", "price label", "price tag", "shelf ticket", "shelf label",
     "price strip", "data strip", "label strip", "shelf edge"},
]

# Build a flat lookup: term → group_id
_TERM_TO_GROUP: dict[str, int] = {}
for _gid, _group in enumerate(SYNONYM_GROUPS):
    for _term in _group:
        _TERM_TO_GROUP[_term.lower()] = _gid

# Standard sizes — must match exactly
SIZE_PATTERN = re.compile(
    r"\b(a0|a1|a2|a3|a4|a5|a6|a7|dl|1/3\s*a4|half\s*a4|"
    r"\d+\s*x\s*\d+\s*(?:mm|cm)?)\b",
    re.I
)


def _extract_groups(text: str) -> set[int]:
    """Return set of synonym group IDs present in text."""
    t = text.lower()
    hits = set()
    # Check multi-word terms first (longest first to avoid partial matches)
    for term in sorted(_TERM_TO_GROUP.keys(), key=len, reverse=True):
        if term in t:
            hits.add(_TERM_TO_GROUP[term])
    return hits


def _extract_sizes(text: str) -> set[str]:
    return {m.group(0).upper().replace(" ", "") for m in SIZE_PATTERN.finditer(text)}


def keyword_score(comp_profile: str, sku_text: str) -> int:
    """
    Score how well a competitor product profile matches a UKPOS SKU.
    Returns 0-100.

    Scoring:
      - Each matching synonym group: +25 (max 50 from groups)
      - Size match: +30 if sizes present and match, -20 if sizes present and mismatch
      - Price proximity: scored separately and added by caller
      - Bonus +10 for SKU code found in competitor text
    """
    score = 0

    comp_groups = _extract_groups(comp_profile)
    sku_groups  = _extract_groups(sku_text)

    matching_groups = comp_groups & sku_groups
    score += min(50, len(matching_groups) * 25)

    # Size matching
    comp_sizes = _extract_sizes(comp_profile)
    sku_sizes  = _extract_sizes(sku_text)
    if sku_sizes:
        if comp_sizes & sku_sizes:
            score += 30
        elif comp_sizes:
            score -= 20  # sizes present but mismatched — penalise

    return max(0, min(100, score))


# ── Sitemap fetching ───────────────────────────────────────────────────────────
NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

def _ua() -> str:
    return random.choice(USER_AGENTS)

def _fetch_xml(client: httpx.Client, url: str) -> Optional[ET.Element]:
    try:
        r = client.get(url, timeout=30, follow_redirects=True,
                       headers={"User-Agent": _ua(), "Accept-Encoding": "gzip, deflate"})
        if r.status_code != 200:
            log.warning(f"  HTTP {r.status_code} fetching sitemap {url}")
            return None
        content = r.content
        if content[:2] == b"\x1f\x8b":
            content = gzip.decompress(content)
        return ET.fromstring(content)
    except Exception as e:
        log.warning(f"  Sitemap fetch error {url}: {e}")
        return None


def harvest_urls(client: httpx.Client, sitemap_url: str, url_filter,
                 max_urls: int = 50000, _depth: int = 0) -> list[str]:
    root = _fetch_xml(client, sitemap_url)
    if root is None:
        return []

    tag = root.tag.lower()
    indent = "  " * _depth

    def _find_all(parent, local_name):
        # Namespace-agnostic fallback: some sitemap generators declare a
        # nonstandard or missing default namespace, which silently breaks
        # the sm:-prefixed findall() below (0 results, no error, no
        # exception — just a wrong empty answer). {*} matches any
        # namespace (or none) for the given local tag name.
        found = parent.findall(f".//sm:{local_name}", NS)
        if not found:
            found = parent.findall(f".//{{*}}{local_name}")
        return found

    if "sitemapindex" in tag:
        children = _find_all(root, "sitemap")
        log.info(f"  {indent}Sitemap index {sitemap_url}: {len(children)} child sitemap(s)")
        urls = []
        for sm_el in children:
            loc_el = sm_el.find("sm:loc", NS)
            if loc_el is None:
                loc_el = sm_el.find("{*}loc")
            child = (loc_el.text or "").strip() if loc_el is not None else ""
            if child:
                urls.extend(harvest_urls(client, child, url_filter, max_urls - len(urls), _depth + 1))
            if len(urls) >= max_urls:
                break
        return urls[:max_urls]

    loc_els = _find_all(root, "loc")
    raw_urls = [(el.text or "").strip() for el in loc_els]
    raw_urls = [u for u in raw_urls if u]

    urls = [u for u in raw_urls if url_filter(u)]
    log.info(
        f"  {indent}{sitemap_url}: {len(raw_urls)} raw URL(s) in file, "
        f"{len(urls)} passed the product-URL filter"
    )
    if raw_urls and not urls:
        log.info(f"  {indent}Filtered out everything — sample raw URLs: {raw_urls[:3]}")
    elif not raw_urls:
        log.info(f"  {indent}Root tag was '{root.tag}' — 0 <loc> elements found at all (check for a namespace or format the parser doesn't recognise)")

    return urls[:max_urls]


def harvest_category_urls(client: httpx.Client, category_urls: list[str],
                          product_link_pattern: "re.Pattern",
                          max_pages_per_category: int = 15,
                          page_param: str = "page") -> list[str]:
    """
    Fallback harvester for sites with no working /sitemap.xml.
    Crawls each category/listing URL (and paginates it) collecting links
    that match product_link_pattern. Stops paginating a category once a
    page yields no new URLs.
    """
    from urllib.parse import urljoin

    all_urls: set[str] = set()
    for cat_url in category_urls:
        page = 1
        while page <= max_pages_per_category:
            if page == 1:
                url = cat_url
            else:
                sep = "&" if "?" in cat_url else "?"
                url = f"{cat_url}{sep}{page_param}={page}"

            html = _fetch_html(client, url)
            if not html:
                break

            found_this_page = set()
            for href in re.findall(r'href=["\']([^"\']+)["\']', html):
                full = urljoin(url, href).split("#")[0]
                if product_link_pattern.search(full):
                    found_this_page.add(full)

            new = found_this_page - all_urls
            all_urls |= found_this_page

            if page > 1 and not new:
                break  # this page added nothing new — end of pagination
            page += 1

    return sorted(all_urls)


# ── Page fetching & extraction ─────────────────────────────────────────────────

def _fetch_html(client: httpx.Client, url: str) -> str:
    """Fetch a page, routing via CF Worker if needed. Returns raw HTML or ''."""
    if _needs_proxy(url):
        if not CF_PROXY_URL:
            log.warning(f"  CF_PROXY_URL not set, skipping {url}")
            return ""
        try:
            r = client.post(CF_PROXY_URL, json={"url": url}, timeout=30)
            data = r.json()
            return data.get("html", "") or ""
        except Exception as e:
            log.debug(f"  CF proxy error for {url}: {e}")
            return ""
    try:
        r = client.get(url, timeout=PAGE_TIMEOUT, follow_redirects=True,
                       headers={"User-Agent": _ua()})
        return r.text if r.status_code == 200 else ""
    except Exception:
        return ""


def extract_product_profile(html: str, url: str) -> dict:
    """
    Extract product details from a page.
    Returns: {title, description, price, sku_codes, profile_text}
    """
    if not html:
        return {}

    # ── Title ──────────────────────────────────────────────────────────────────
    title = ""
    # JSON-LD first
    for jld_text in re.findall(
        r'<script[^>]*application/ld.json[^>]*>(.*?)</script>', html, re.I | re.S
    ):
        try:
            data = json.loads(jld_text)
            items = data if isinstance(data, list) else [data]
            flat = []
            for item in items:
                flat.extend(item.get("@graph", [item]))
            for item in flat:
                if item.get("@type") == "Product":
                    title = item.get("name", "")
                    break
            if title:
                break
        except Exception:
            pass

    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        if m:
            raw = re.sub(r"<[^>]+>", "", m.group(1)).strip()
            title = re.sub(r"\s*[\|\-–]\s*.{3,60}$", "", raw).strip()

    # ── Description ───────────────────────────────────────────────────────────
    description = ""
    # JSON-LD
    for jld_text in re.findall(
        r'<script[^>]*application/ld.json[^>]*>(.*?)</script>', html, re.I | re.S
    ):
        try:
            data = json.loads(jld_text)
            items = data if isinstance(data, list) else [data]
            flat = []
            for item in items:
                flat.extend(item.get("@graph", [item]))
            for item in flat:
                if item.get("@type") == "Product" and item.get("description"):
                    description = item["description"][:500]
                    break
            if description:
                break
        except Exception:
            pass

    if not description:
        m = re.search(r'<meta[^>]*name=["\']description["\'][^>]*content=["\']([^"\']{10,400})',
                      html, re.I)
        if not m:
            m = re.search(r'content=["\']([^"\']{10,400})["\'][^>]*name=["\']description["\']',
                          html, re.I)
        if m:
            description = m.group(1).strip()

    # ── Price ─────────────────────────────────────────────────────────────────
    price = None
    for jld_text in re.findall(
        r'<script[^>]*application/ld.json[^>]*>(.*?)</script>', html, re.I | re.S
    ):
        try:
            data = json.loads(jld_text)
            items = data if isinstance(data, list) else [data]
            flat = []
            for item in items:
                flat.extend(item.get("@graph", [item]))
            for item in flat:
                if item.get("@type") == "Product":
                    offers = item.get("offers", {})
                    if isinstance(offers, list):
                        offers = offers[0]
                    p = offers.get("price") or offers.get("lowPrice")
                    if p:
                        price = float(str(p).replace(",", ""))
                        break
            if price:
                break
        except Exception:
            pass

    if not price:
        m = re.search(r'itemprop=[^>]*price[^>]*content="([^"]+)"', html, re.I)
        if not m:
            m = re.search(r'content="([^"]+)"[^>]*itemprop=[^>]*price', html, re.I)
        if m:
            try:
                price = float(m.group(1).replace(",", ""))
            except Exception:
                pass

    if not price:
        prices = []
        for p in re.findall(r"£\s*([\d,]+\.\d{2})", html):
            try:
                v = float(p.replace(",", ""))
                if 0.01 < v < 9999:
                    prices.append(v)
            except Exception:
                pass
        if prices:
            prices.sort()
            price = prices[len(prices) // 2]

    # ── SKU codes ─────────────────────────────────────────────────────────────
    sku_codes = []
    for pattern in [
        r'sku["\']?\s*[:=]\s*["\']?([A-Z0-9\-]{3,20})',
        r'product.?code["\']?\s*[:=]\s*["\']?([A-Z0-9\-]{3,20})',
        r'model["\']?\s*[:=]\s*["\']?([A-Z0-9\-]{3,20})',
        r'mpn["\']?\s*[:=]\s*["\']?([A-Z0-9\-]{3,20})',
        r'item_number["\']?\s*[:=]\s*["\']?([A-Z0-9\-]{3,20})',
    ]:
        for m in re.finditer(pattern, html, re.I):
            code = m.group(1).strip().upper()
            if 3 <= len(code) <= 20 and code not in sku_codes:
                sku_codes.append(code)

    # ── Combined profile text ──────────────────────────────────────────────────
    profile = " ".join(filter(None, [title, description])).strip()

    return {
        "title":       title,
        "description": description,
        "price":       price,
        "sku_codes":   sku_codes[:5],
        "profile":     profile,
        "url":         url,
    }


# ── Price proximity scoring ────────────────────────────────────────────────────

def price_score(comp_price: Optional[float], sku_price: float,
                sku_unit_qty: Optional[int]) -> int:
    """
    Score price proximity. Returns 0-20.
    Accounts for inc-VAT by testing both comp_price and comp_price/1.2.
    Accounts for pack sizes by testing comp_price / sku_unit_qty.
    """
    if not comp_price or not sku_price:
        return 0

    # Candidate prices to test: raw, ex-VAT, and per-unit versions
    candidates = [comp_price, comp_price / 1.2]
    if sku_unit_qty and sku_unit_qty > 1:
        candidates += [comp_price / sku_unit_qty, (comp_price / 1.2) / sku_unit_qty]

    best = 0
    for c in candidates:
        ratio = c / sku_price if sku_price > 0 else 99
        if 0.7 <= ratio <= 1.3:
            best = 20   # within 30%
        elif 0.5 <= ratio <= 2.0:
            best = max(best, 10)  # within 2x
        elif 0.3 <= ratio <= 3.0:
            best = max(best, 5)   # within 3x

    return best


# ── Claude API matching ────────────────────────────────────────────────────────

def claude_match(comp: dict, candidates: list[dict]) -> list[dict]:
    """
    Ask Claude to match a competitor product to the best UKPOS SKU(s).

    comp: {title, description, price, sku_codes, url}
    candidates: list of {sku_id, short_title, full_title, price_ex_vat, unit_qty, keyword_score}

    Returns list of {sku_id, confidence, reasoning} sorted by confidence desc.
    """
    if not candidates:
        return []

    cand_text = "\n".join([
        f"  [{i+1}] SKU={c['sku_id']} | {c['full_title'] or c['short_title']} "
        f"| £{c['price_ex_vat']} ex-VAT"
        + (f" (pack of {c['unit_qty']})" if c.get('unit_qty') and c['unit_qty'] > 1 else "")
        for i, c in enumerate(candidates)
    ])

    price_str = f"£{comp['price']:.2f}" if comp.get("price") else "unknown"
    codes_str = ", ".join(comp["sku_codes"]) if comp.get("sku_codes") else "none found"

    prompt = f"""You are a product matching expert for a UK point-of-sale and display products business.

A competitor is selling the following product:
  URL: {comp['url']}
  Title: {comp['title']}
  Description: {comp['description'] or 'not available'}
  Price: {price_str} (may be inc or ex VAT — unknown)
  Product codes on page: {codes_str}

Our SKU catalogue candidates (pre-filtered by keyword relevance):
{cand_text}

Task: Identify which of our SKUs this competitor product matches, if any.
A match means they are selling the same product (same type, same size/format, same material where relevant).
Price proximity is a useful signal but not a hard requirement.
Pack size differences are acceptable if the underlying unit is the same product.

Rules:
- Only match if you are genuinely confident this is the same product
- Size must match (A4≠A3, A3≠A2 etc.) — this is a hard requirement
- Material must broadly match (acrylic≠wood, aluminium≠plastic)
- Product type must match (a snap frame is not a leaflet holder)
- If no candidate is a genuine match, say so

Respond ONLY with a JSON array. No preamble, no explanation outside the JSON.
Format:
[
  {{"sku_id": "SKU123", "confidence": 75, "reasoning": "one sentence"}},
  {{"sku_id": "SKU456", "confidence": 60, "reasoning": "one sentence"}}
]

Return up to 3 matches in descending confidence order. Return [] if no match."""

    try:
        r = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-sonnet-4-6",
                "max_tokens": 500,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        data = r.json()
        text = data["content"][0]["text"].strip()
        # Strip markdown code fences if present
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
        matches = json.loads(text)
        if isinstance(matches, list):
            return [m for m in matches if isinstance(m, dict) and "sku_id" in m]
        return []
    except Exception as e:
        log.warning(f"  Claude API error: {e}")
        return []


# ── DB helpers ─────────────────────────────────────────────────────────────────

def upsert_match(sb, sku_id: str, competitor_id: int, url: str, title: str,
                 confidence: int, method: str, reasoning: str = ""):
    sb.table("competitor_matches").upsert(
        {
            "sku_id":           sku_id,
            "competitor_id":    competitor_id,
            "competitor_url":   url,
            "competitor_title": title or None,
            "match_status":     "review",
            "confidence":       confidence,
            "match_method":     method,
            "match_source":     "scraper_auto",
            "human_reviewed":   False,
            "updated_at":       datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="sku_id,competitor_id",
    ).execute()


# ── Snapshot writer ───────────────────────────────────────────────────────────

def write_snapshot(sb, sku: dict, competitor_id: int, url: str,
                   raw_price: Optional[float], vat_status: str,
                   html: str, confidence: int, run_id: str):
    """
    Write a provisional price snapshot at discovery time.
    Uses the price already extracted from the page so we don't fetch it again.
    VAT is normalised to ex-VAT using detect_vat() result.
    The scrape run will overwrite this with a properly extracted price on its
    next cycle — this just means the dashboard shows a price immediately
    rather than waiting days for the next scheduled scrape.
    """
    if not raw_price:
        return

    our_price  = float(sku["_price"])
    unit_qty   = sku.get("unit_qty") or 1
    body_text  = re.sub(r"<[^>]+>", " ", html)

    # Normalise to ex-VAT
    vat    = vat_status or detect_vat(body_text)
    ex_vat = normalise_price(raw_price, vat)

    # Per-unit normalisation — if competitor sells different pack size
    comp_qty = extract_pack_qty(body_text) or 1
    our_qty  = unit_qty or 1

    # Normalised diff (per-unit comparison)
    our_per_unit  = our_price / our_qty  if our_qty  > 1 else our_price
    comp_per_unit = ex_vat    / comp_qty if comp_qty > 1 else ex_vat

    dp            = diff_pct(our_per_unit, comp_per_unit) if our_per_unit else 0
    dp_normalised = dp  # discovery snapshots don't have separate normalised diff

    availability = "out_of_stock" if detect_oos(body_text) else "in_stock"

    try:
        sb.table("price_snapshots").insert({
            "sku_id":               sku["sku_id"],
            "competitor_id":        competitor_id,
            "scraped_at":           datetime.now(timezone.utc).isoformat(),
            "run_id":               run_id,
            "competitor_price":     ex_vat,
            "competitor_vat":       vat,
            "competitor_url":       url,
            "availability":         availability,
            "diff_pct":             dp,
            "diff_pct_normalised":  dp_normalised,
            "confidence":           confidence,
            "competitor_unit_qty":  comp_qty if comp_qty > 1 else None,
            "pack_qty_flag":        "discovery_provisional",
        }).execute()
        log.debug(f"    Snapshot written: £{ex_vat:.2f} {vat} diff={dp:+.1f}%")
    except Exception as e:
        log.warning(f"    Snapshot write failed for {sku['sku_id']} × {competitor_id}: {e}")


# ── Main ───────────────────────────────────────────────────────────────────────

def run_discovery():
    if not _competitor_ids:
        log.error("COMPETITOR_IDS must be set — run one competitor at a time")
        return

    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    # ── Load all active SKUs into memory ──────────────────────────────────────
    log.info("Loading SKU catalogue...")
    all_skus = sb.table("skus").select(
        "sku_id,short_title,full_title,price_ex_vat,unit_qty"
    ).eq("active", True).execute().data

    # Build SKU text for keyword scoring: full_title preferred
    for s in all_skus:
        s["_score_text"] = (s.get("full_title") or s.get("short_title") or "").lower()
        try:
            s["_price"] = float(s["price_ex_vat"])
        except Exception:
            s["_price"] = 0.0

    log.info(f"Loaded {len(all_skus)} SKUs")

    # ── Load existing confirmed matches to never overwrite ─────────────────────
    confirmed = sb.table("competitor_matches").select(
        "sku_id,competitor_id,match_status"
    ).execute().data
    confirmed_pairs = {
        (m["sku_id"], m["competitor_id"])
        for m in confirmed
        if m["match_status"] == "matched" and not FORCE
    }
    log.info(f"Confirmed pairs to protect: {len(confirmed_pairs)}")

    # Create a discovery run_id so snapshots are grouped in sync_runs-adjacent queries
    import uuid as _uuid
    discovery_run_id = str(_uuid.uuid4())
    log.info(f"Discovery run ID: {discovery_run_id}")

    # ── Process each competitor ───────────────────────────────────────────────
    comps = sb.table("competitors").select("*").eq("active", True)\
              .in_("id", _competitor_ids).execute().data

    for comp in comps:
        cid   = comp["id"]
        cname = comp["name"]
        cfg   = COMPETITOR_SITEMAPS.get(cid)
        discovery_run_db_id = None  # reset every iteration — never inherit a stale id from the previous competitor

        if not cfg:
            log.warning(f"No sitemap config for {cname} (id={cid}), skipping")
            if discovery_run_db_id:
                try:
                    sb.table("discovery_runs").update({
                        "status": "failed", "completed_at": datetime.now(timezone.utc).isoformat(),
                        "notes": "No sitemap config",
                    }).eq("id", discovery_run_db_id).execute()
                except Exception:
                    pass
            continue

        log.info(f"\n{'='*60}")
        log.info(f"Processing competitor: {cname} (id={cid})")
        log.info(f"{'='*60}")

        stats = {
            "urls_harvested": 0, "pages_fetched": 0, "pages_empty": 0,
            "filtered_no_keywords": 0, "claude_calls": 0,
            "matches_written": 0, "no_match": 0,
        }

        # Insert discovery_runs row — marks that we have attempted this competitor
        try:
            dr = sb.table("discovery_runs").insert({
                "competitor_id": cid,
                "status":        "running",
                "started_at":    datetime.now(timezone.utc).isoformat(),
            }).execute()
            discovery_run_db_id = dr.data[0]["id"]
            log.info(f"  discovery_runs row created: id={discovery_run_db_id}")
        except Exception as e:
            log.warning(f"  Could not create discovery_runs row: {e}")
            discovery_run_db_id = None

        with httpx.Client(follow_redirects=True) as client:

            # Phase 1 — Harvest (sitemap, or category-crawl fallback for sites with no working sitemap.xml)
            if cfg.get("mode") == "category_crawl":
                log.info(f"Phase 1: Category-crawling {len(cfg['category_urls'])} listing pages (no sitemap available)")
                urls = harvest_category_urls(
                    client, cfg["category_urls"], cfg["product_link_pattern"]
                )
            else:
                log.info(f"Phase 1: Harvesting sitemap {cfg['sitemap']}")
                urls = harvest_urls(client, cfg["sitemap"], cfg["filter"], MAX_PAGES)
            stats["urls_harvested"] = len(urls)
            log.info(f"  → {len(urls)} product URLs")

            if not urls:
                log.warning(f"  No URLs harvested for {cname}")
                if discovery_run_db_id:
                    try:
                        sb.table("discovery_runs").update({
                            "status": "failed", "completed_at": datetime.now(timezone.utc).isoformat(),
                            "notes": "No URLs harvested from sitemap",
                        }).eq("id", discovery_run_db_id).execute()
                    except Exception:
                        pass
                continue

            # Phase 2 + 3 + 4 — Fetch, profile, pre-filter, Claude match
            log.info(f"Phase 2-4: Fetching pages and matching...")

            for i, url in enumerate(urls):
                if i > 0 and i % 100 == 0:
                    log.info(
                        f"  Progress: {i}/{len(urls)} — "
                        f"matched={stats['matches_written']} "
                        f"no_match={stats['no_match']} "
                        f"filtered={stats['filtered_no_keywords']}"
                    )

                # Phase 2 — Fetch page
                html = _fetch_html(client, url)
                stats["pages_fetched"] += 1
                if not html:
                    stats["pages_empty"] += 1
                    continue

                profile = extract_product_profile(html, url)
                if not profile.get("profile"):
                    stats["pages_empty"] += 1
                    continue

                comp_profile_text = profile["profile"].lower()
                comp_price        = profile.get("price")
                comp_title        = profile.get("title", "")
                comp_codes        = set(profile.get("sku_codes", []))

                # Phase 3 — Keyword pre-filter
                # Score competitor product against every SKU
                # Keep top BATCH_SIZE candidates for Claude
                candidates = []
                for sku in all_skus:
                    # Skip if already confirmed for this competitor
                    if (sku["sku_id"], cid) in confirmed_pairs:
                        continue

                    # SKU code exact match — instant high score
                    if comp_codes and sku["sku_id"].upper() in comp_codes:
                        kw_score = 90
                    else:
                        kw_score = keyword_score(comp_profile_text, sku["_score_text"])
                        # Add price proximity bonus
                        kw_score += price_score(comp_price, sku["_price"], sku.get("unit_qty"))

                    if kw_score >= 25:
                        candidates.append({**sku, "keyword_score": kw_score})

                if not candidates:
                    stats["filtered_no_keywords"] += 1
                    log.debug(f"  ✗ No keyword candidates for {url[:70]}")
                    continue

                # Sort and take top BATCH_SIZE
                candidates.sort(key=lambda x: x["keyword_score"], reverse=True)
                candidates = candidates[:BATCH_SIZE]

                # Phase 4 — Claude matching
                stats["claude_calls"] += 1
                matches = claude_match(profile, candidates)

                if not matches:
                    stats["no_match"] += 1
                    log.debug(f"  ✗ Claude: no match for {comp_title[:60] or url[:60]}")
                    time.sleep(0.5)
                    continue

                # Write top match, and second if within AMBIGUITY_MARGIN
                written = 0
                for idx, match in enumerate(matches):
                    if match.get("confidence", 0) < MIN_CONF:
                        break
                    if idx > 0:
                        # Only write second match if ambiguous (close to first)
                        if matches[0]["confidence"] - match["confidence"] > AMBIGUITY_MARGIN:
                            break

                    sku_id     = match["sku_id"]
                    confidence = match["confidence"]
                    reasoning  = match.get("reasoning", "")

                    # Verify SKU exists in our catalogue
                    if not any(s["sku_id"] == sku_id for s in all_skus):
                        log.warning(f"  Claude returned unknown SKU {sku_id}, skipping")
                        continue

                    # Never overwrite a confirmed match
                    if (sku_id, cid) in confirmed_pairs:
                        log.debug(f"  Already confirmed: {sku_id} × {cname}")
                        continue

                    upsert_match(sb, sku_id, cid, url, comp_title,
                                 confidence, "claude_sitemap", reasoning)

                    # Write provisional price snapshot using the price already on hand
                    sku_obj = next((s for s in all_skus if s["sku_id"] == sku_id), None)
                    if sku_obj and comp_price:
                        write_snapshot(sb, sku_obj, cid, url,
                                      comp_price, "", html,
                                      confidence, discovery_run_id)

                    confirmed_pairs.add((sku_id, cid))  # prevent duplicate writes
                    stats["matches_written"] += 1
                    written += 1
                    log.info(
                        f"  ✓ {sku_id} × {cname} "
                        f"conf={confidence}% — {reasoning[:80]}"
                    )

                if written == 0:
                    stats["no_match"] += 1

                # Polite rate limiting — avoid hammering competitor servers
                time.sleep(random.uniform(0.5, 1.5))

        log.info(f"\n{cname} complete:")
        log.info(f"  URLs harvested:      {stats['urls_harvested']}")
        log.info(f"  Pages fetched:       {stats['pages_fetched']}")
        log.info(f"  Pages empty/failed:  {stats['pages_empty']}")
        log.info(f"  Filtered (no match): {stats['filtered_no_keywords']}")
        log.info(f"  Claude calls:        {stats['claude_calls']}")
        log.info(f"  Matches written:     {stats['matches_written']}")
        log.info(f"  No match found:      {stats['no_match']}")

        # Update discovery_runs row with completion stats
        if discovery_run_db_id:
            try:
                sb.table("discovery_runs").update({
                    "status":          "complete",
                    "completed_at":    datetime.now(timezone.utc).isoformat(),
                    "urls_found":      stats["urls_harvested"],
                    "matches_written": stats["matches_written"],
                }).eq("id", discovery_run_db_id).execute()
            except Exception as e:
                log.warning(f"  Could not update discovery_runs row: {e}")


if __name__ == "__main__":
    run_discovery()
