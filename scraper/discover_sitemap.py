"""
scraper/discover_sitemap.py
───────────────────────────
Sitemap-based competitor URL discovery.

Replaces the broken search-engine approach (Bing/Google block headless
requests from CI). Instead:

  1. Fetch each competitor's sitemap (or sitemap index) using plain httpx —
     no Playwright needed, sitemaps are plain XML served to any client.
  2. Filter URLs down to product pages using per-domain path rules.
  3. Score every candidate URL against every unmatched SKU using
     fuzzy_confidence() from common.py, which operates on the URL slug
     and — where we fetch it — the page title.
  4. Write anything above MIN_CONFIDENCE as match_status='review' into
     competitor_matches, exactly as the old discover.py did, so results
     appear immediately in Match Manager → Needs Review.

No Playwright is launched unless a competitor is on the FETCH_TITLE_DOMAINS
list, in which case the top candidate per pair gets its title fetched to
improve the score before writing.

Environment variables (same names as discover.py so the workflow is unchanged):
  SUPABASE_URL          – required
  SUPABASE_SERVICE_KEY  – required
  DISCOVER_SKUS         – comma-separated SKU IDs (optional; all unmatched if blank)
  DISCOVER_FORCE        – 'true' to re-run already-matched pairs
  DISCOVER_SKU_LIMIT    – max SKUs per run (default 500)
  COMPETITOR_IDS        – comma-separated competitor IDs to restrict to (optional)
  LOG_LEVEL             – DEBUG / INFO (default INFO)
  MIN_CONFIDENCE        – minimum score to write a match (default 30)
  TITLE_FETCH_TIMEOUT   – seconds to wait for a title fetch (default 15)

Usage (from /workspaces/pricewatch):
  python scraper/discover_sitemap.py

Or via the GitHub Actions workflow:
  job=discover  (the workflow already calls python scraper/discover.py — update
                 that step to call discover_sitemap.py, or rename this file)
"""

import asyncio
import gzip
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import httpx
from supabase import create_client

from common import (
    fuzzy_confidence,
    is_category_url,
    USER_AGENTS,
    STOP_WORDS,
)

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("pricewatch.discover_sitemap")

# ── Config ─────────────────────────────────────────────────────────────────────
SUPABASE_URL  = os.environ["SUPABASE_URL"]
SUPABASE_KEY  = os.environ["SUPABASE_SERVICE_KEY"]
SKU_LIMIT     = int(os.getenv("DISCOVER_SKU_LIMIT", "500"))
FORCE         = os.getenv("DISCOVER_FORCE", "false").lower() == "true"
MIN_CONF      = int(os.getenv("MIN_CONFIDENCE", "30"))
TITLE_TIMEOUT = int(os.getenv("TITLE_FETCH_TIMEOUT", "15"))

_specific_skus     = [s.strip() for s in os.getenv("DISCOVER_SKUS", "").split(",")     if s.strip()]
_competitor_ids    = [int(i.strip()) for i in os.getenv("COMPETITOR_IDS", "").split(",") if i.strip()]

# ── Per-competitor sitemap config ──────────────────────────────────────────────
# sitemap  : URL to fetch (the index or the leaf — we recurse either way)
# url_filter: callable(url:str)->bool — True = keep as product candidate
# Notes:
#   - 301 redirectors: httpx follows redirects automatically
#   - luminati / topregal blocked at 403 — excluded until UA workaround found
#   - 3ddisplays / bludisplay / chalkboardsuk / ghdisplay / verydisplays /
#     screenmoove / viking-direct / signwaves / ultimadisplays — sitemap present
#     but no hand-verified path rules yet; generic filter used as fallback

def _path_has_depth(url: str, min_depth: int = 2) -> bool:
    """URL must have at least min_depth non-empty path segments."""
    parts = [p for p in urlparse(url).path.rstrip("/").split("/") if p]
    return len(parts) >= min_depth

def _not_category(url: str) -> bool:
    return not is_category_url(url)

def _product_path(url: str, *segments: str) -> bool:
    """URL path must contain one of the given segments."""
    path = urlparse(url).path.lower()
    return any(seg in path for seg in segments)

COMPETITOR_SITEMAPS = {
    # id: { sitemap, filter }

    # Alplas — WooCommerce, /product/ paths
    1: {
        "sitemap": "https://www.alplas.com/sitemap.xml",
        "filter":  lambda u: _product_path(u, "/product/"),
    },
    # Discount Displays — Magento, /products/ paths
    4: {
        "sitemap": "https://www.discountdisplays.co.uk/sitemap.xml",
        "filter":  lambda u: _product_path(u, "/products/") and _not_category(u),
    },
    # Display Pro — Shopify, /products/ paths
    5: {
        "sitemap": "https://displaypro.co.uk/sitemap.xml",
        "filter":  lambda u: _product_path(u, "/products/") and _not_category(u),
    },
    # Displaysense — custom, paths like /product-name.html
    6: {
        "sitemap": "https://displaysense.co.uk/sitemap.xml",
        "filter":  lambda u: _path_has_depth(u, 1) and _not_category(u) and "displaysense" in u,
    },
    # Display Wizard — Shopify
    7: {
        "sitemap": "https://www.displaywizard.co.uk/sitemap.xml",
        "filter":  lambda u: _product_path(u, "/products/") and _not_category(u),
    },
    # Gadsby — non-standard sitemap path declared in robots.txt
    8: {
        "sitemap": "https://www.gadsby.co.uk/sitemaps-1-sitemap.xml",
        "filter":  lambda u: _path_has_depth(u, 2) and _not_category(u),
    },
    # GH Display — sitemap index
    9: {
        "sitemap": "https://www.ghdisplay.co.uk/sitemap_index.xml",
        "filter":  lambda u: _product_path(u, "/product/", "/products/") and _not_category(u),
    },
    # Harrison Products — BigCommerce, custom xmlsitemap.php
    10: {
        "sitemap": "https://www.harrisonproducts.com/xmlsitemap.php",
        "filter":  lambda u: _product_path(u, "/products/") and _not_category(u),
    },
    # Indigo Displays
    11: {
        "sitemap": "https://indigodisplays.co.uk/sitemap.xml",
        "filter":  lambda u: _path_has_depth(u, 2) and _not_category(u),
    },
    # Pavement Signs — non-standard /gb/ path
    13: {
        "sitemap": "https://pavementsigns.com/gb/sitemap.xml",
        "filter":  lambda u: _path_has_depth(u, 2) and _not_category(u),
    },
    # Retail Acrylics — both root and index answered; root is fine
    14: {
        "sitemap": "https://www.retailacrylics.co.uk/sitemap.xml",
        "filter":  lambda u: _path_has_depth(u, 2) and _not_category(u),
    },
    # Shopfitting Warehouse
    15: {
        "sitemap": "https://www.shopfittingwarehouse.co.uk/sitemap.xml",
        "filter":  lambda u: _path_has_depth(u, 2) and _not_category(u),
    },
    # Sign Holders
    16: {
        "sitemap": "https://www.sign-holders.co.uk/sitemap.xml",
        "filter":  lambda u: _path_has_depth(u, 2) and _not_category(u),
    },
    # Signwaves
    17: {
        "sitemap": "https://www.signwaves.co.uk/sitemap.xml",
        "filter":  lambda u: _path_has_depth(u, 2) and _not_category(u),
    },
    # Snap Frames Warehouse
    18: {
        "sitemap": "https://www.snapframeswarehouse.co.uk/sitemap.xml",
        "filter":  lambda u: _path_has_depth(u, 2) and _not_category(u),
    },
    # The Retail Factory
    19: {
        "sitemap": "https://www.theretailfactory.co.uk/sitemap.xml",
        "filter":  lambda u: _path_has_depth(u, 2) and _not_category(u),
    },
    # UK Sign Shop
    20: {
        "sitemap": "https://www.uksignshop.co.uk/sitemap.xml",
        "filter":  lambda u: _path_has_depth(u, 2) and _not_category(u),
    },
    # Ultima Displays
    21: {
        "sitemap": "https://www.ultimadisplays.com/sitemap.xml",
        "filter":  lambda u: _path_has_depth(u, 2) and _not_category(u),
    },
    # Very Displays — index only
    22: {
        "sitemap": "https://www.verydisplays.com/sitemap_index.xml",
        "filter":  lambda u: _path_has_depth(u, 2) and _not_category(u),
    },
    # VKF Renzel
    23: {
        "sitemap": "https://www.vkf-renzel.co.uk/sitemap.xml",
        "filter":  lambda u: _path_has_depth(u, 2) and _not_category(u),
    },
    # Visual Displays
    24: {
        "sitemap": "https://visualdisplays.co.uk/sitemap.xml",
        "filter":  lambda u: _path_has_depth(u, 2) and _not_category(u),
    },
    # ScreenMoove
    26: {
        "sitemap": "https://screenmoove.com/sitemap.xml",
        "filter":  lambda u: _path_has_depth(u, 2) and _not_category(u),
    },
    # 3D Displays
    27: {
        "sitemap": "https://www.3ddisplays.co.uk/sitemap.xml",
        "filter":  lambda u: _path_has_depth(u, 2) and _not_category(u),
    },
    # Blu Display Systems
    28: {
        "sitemap": "https://www.bludisplay.co.uk/sitemap.xml",
        "filter":  lambda u: _path_has_depth(u, 2) and _not_category(u),
    },
    # Chalkboards UK
    2: {
        "sitemap": "https://www.chalkboardsuk.co.uk/sitemap.xml",
        "filter":  lambda u: _path_has_depth(u, 2) and _not_category(u),
    },
    # Viking Direct
    29: {
        "sitemap": "https://www.viking-direct.co.uk/sitemap.xml",
        "filter":  lambda u: _path_has_depth(u, 2) and _not_category(u),
    },

    # Luminati (12) — blocked at 403; excluded
    # Top Regal (25) — blocked at 403; excluded
}


# ── Sitemap fetching ───────────────────────────────────────────────────────────

NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

def _ua() -> str:
    return random.choice(USER_AGENTS)

def _fetch_xml(client: httpx.Client, url: str) -> Optional[ET.Element]:
    """Fetch a sitemap URL and parse it. Handles gzip transparently."""
    try:
        r = client.get(url, timeout=30, follow_redirects=True,
                       headers={"User-Agent": _ua(), "Accept-Encoding": "gzip, deflate"})
        if r.status_code != 200:
            log.warning(f"  HTTP {r.status_code} fetching {url}")
            return None
        content = r.content
        # Some servers gzip without declaring Content-Encoding
        if content[:2] == b"\x1f\x8b":
            content = gzip.decompress(content)
        return ET.fromstring(content)
    except Exception as e:
        log.warning(f"  Error fetching {url}: {e}")
        return None


def harvest_urls(client: httpx.Client, sitemap_url: str, url_filter, max_urls: int = 50000) -> list[str]:
    """
    Recursively fetch a sitemap or sitemap index and return all product URLs
    that pass url_filter, up to max_urls.
    """
    root = _fetch_xml(client, sitemap_url)
    if root is None:
        return []

    tag = root.tag.lower()

    # Sitemap index — recurse into each child sitemap
    if "sitemapindex" in tag:
        urls = []
        for sitemap_el in root.findall(".//sm:sitemap/sm:loc", NS):
            child_url = (sitemap_el.text or "").strip()
            if not child_url:
                continue
            log.debug(f"    → child sitemap: {child_url}")
            urls.extend(harvest_urls(client, child_url, url_filter, max_urls - len(urls)))
            if len(urls) >= max_urls:
                break
        return urls[:max_urls]

    # Leaf sitemap — collect <loc> entries
    urls = []
    for loc_el in root.findall(".//sm:url/sm:loc", NS):
        u = (loc_el.text or "").strip()
        if u and url_filter(u):
            urls.append(u)
        if len(urls) >= max_urls:
            break
    return urls


# ── Slug tokenisation ──────────────────────────────────────────────────────────

def slug_tokens(url: str) -> set[str]:
    """Extract meaningful words from the final path segment of a URL."""
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    slug = re.sub(r"\.(html?|php|aspx?)$", "", slug, flags=re.I)
    slug = slug.replace("-", " ").replace("_", " ")
    tokens = set(re.findall(r"[a-z0-9]{2,}", slug.lower()))
    return tokens - STOP_WORDS


def slug_confidence(sku: dict, url: str) -> int:
    """
    Fast, slug-only confidence score — no page fetch needed.

    Uses full_title (which carries the size differentiator, e.g. A4 vs DL vs A5)
    rather than short_title, which is shared across many variants.
    Delegates to the existing fuzzy_confidence() for consistency, passing the
    slug as a synthetic title.
    """
    # Build a synthetic title from the slug for fuzzy_confidence
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    slug_as_title = slug.replace("-", " ").replace("_", " ")

    # Score against full_title if available, else short_title
    sku_for_scoring = dict(sku)
    if sku.get("full_title"):
        sku_for_scoring["short_title"] = sku["full_title"]

    return fuzzy_confidence(sku_for_scoring, slug_as_title, url)


# ── Title fetch (optional, for top candidates) ─────────────────────────────────

def fetch_title(client: httpx.Client, url: str) -> str:
    """Fetch a product page and extract its <title> tag. Returns '' on failure."""
    try:
        r = client.get(url, timeout=TITLE_TIMEOUT, follow_redirects=True,
                       headers={"User-Agent": _ua()})
        if r.status_code != 200:
            return ""
        m = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.I | re.S)
        if m:
            raw = m.group(1).strip()
            # Strip common suffixes: " | Company Name", " - Shop"
            raw = re.sub(r"\s*[\|\-–]\s*.{3,40}$", "", raw).strip()
            return raw
        return ""
    except Exception:
        return ""


# ── DB helpers ─────────────────────────────────────────────────────────────────

def upsert_match(sb, sku_id: str, competitor_id: int, url: str, title: str, confidence: int, method: str):
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


# ── Main ───────────────────────────────────────────────────────────────────────

def run_discovery():
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)

    # ── Load competitors ───────────────────────────────────────────────────────
    comps_q = sb.table("competitors").select("*").eq("active", True)
    if _competitor_ids:
        comps_q = comps_q.in_("id", _competitor_ids)
    all_comps = {c["id"]: c for c in comps_q.execute().data}

    # Restrict to competitors we have a sitemap config for
    comps = {cid: c for cid, c in all_comps.items() if cid in COMPETITOR_SITEMAPS}
    if _competitor_ids:
        comps = {cid: c for cid, c in comps.items() if cid in _competitor_ids}

    log.info(f"Competitors with sitemap config: {len(comps)} / {len(all_comps)}")

    # ── Load SKUs ──────────────────────────────────────────────────────────────
    SKU_OFFSET = int(os.getenv("DISCOVER_SKU_OFFSET", "0"))

    SKU_OFFSET = int(os.getenv("DISCOVER_SKU_OFFSET", "0"))

    if _specific_skus:
        skus = sb.table("skus").select("*").in_("sku_id", _specific_skus).execute().data
    else:
        skus = (
            sb.table("skus")
            .select("*")
            .eq("active", True)
            .order("sku_id")
            .range(SKU_OFFSET, SKU_OFFSET + SKU_LIMIT - 1)
            .execute()
            .data
        )

    # ── Load existing matches to skip ──────────────────────────────────────────
    all_matches = sb.table("competitor_matches").select("sku_id,competitor_id,match_status").execute().data
    matched_pairs = {
        (m["sku_id"], m["competitor_id"])
        for m in all_matches
        if m["match_status"] in ("matched", "review") and not FORCE
    }

    log.info(f"SKUs={len(skus)}  pairs already matched={len(matched_pairs)}  FORCE={FORCE}")

    # ── Phase 1: harvest all sitemap URLs per competitor ───────────────────────
    # One httpx.Client per competitor so keep-alive is reused within a domain
    comp_urls: dict[int, list[str]] = {}

    with httpx.Client() as client:
        for cid, comp in comps.items():
            cfg = COMPETITOR_SITEMAPS[cid]
            log.info(f"Harvesting sitemap: {comp['name']} ({cfg['sitemap']})")
            t0 = time.time()
            urls = harvest_urls(client, cfg["sitemap"], cfg["filter"])
            elapsed = round(time.time() - t0, 1)
            comp_urls[cid] = urls
            log.info(f"  → {len(urls)} product URLs in {elapsed}s")
            time.sleep(random.uniform(1.0, 2.5))  # polite pause between competitors

    total_candidate_pool = sum(len(v) for v in comp_urls.values())
    log.info(f"Total candidate URLs harvested: {total_candidate_pool}")

    # ── Phase 2: match SKUs against harvested URLs ─────────────────────────────
    stats = {"pairs_checked": 0, "matches_written": 0, "skipped": 0}

    with httpx.Client() as client:
        for sku in skus:
            sku_id = sku["sku_id"]

            for cid, comp in comps.items():
                if (sku_id, cid) in matched_pairs:
                    stats["skipped"] += 1
                    continue

                urls = comp_urls.get(cid, [])
                if not urls:
                    continue

                stats["pairs_checked"] += 1

                # Score every candidate URL by slug only (fast, no fetches)
                scored = []
                for u in urls:
                    sc = slug_confidence(sku, u)
                    if sc >= MIN_CONF:
                        scored.append((sc, u))

                if not scored:
                    continue

                # Sort by score descending; take the best
                scored.sort(key=lambda x: x[0], reverse=True)
                best_score, best_url = scored[0]

                # ── Optional title fetch for the top candidate ─────────────────
                # Fetching a title page for every candidate would be too slow.
                # Only do it when the slug score is borderline (30–59) so we
                # can either promote or discard it with better information.
                title = ""
                method = "sitemap_slug"
                if MIN_CONF <= best_score < 60:
                    title = fetch_title(client, best_url)
                    if title:
                        # Re-score with the real title
                        sku_for_scoring = dict(sku)
                        if sku.get("full_title"):
                            sku_for_scoring["short_title"] = sku["full_title"]
                        best_score = fuzzy_confidence(sku_for_scoring, title, best_url)
                        method = "sitemap_title"
                        log.debug(f"  Title fetch: '{title[:60]}' → conf={best_score}")
                        if best_score < MIN_CONF:
                            log.debug(f"  ✗ {sku_id} × {comp['name']}: score dropped to {best_score} after title fetch")
                            continue
                        time.sleep(random.uniform(1.0, 3.0))

                upsert_match(sb, sku_id, cid, best_url, title, best_score, method)
                stats["matches_written"] += 1
                log.info(f"  ✓ {sku_id} × {comp['name']} conf={best_score}% [{method}] {best_url[:80]}")

    log.info(
        f"Discovery complete — "
        f"pairs_checked={stats['pairs_checked']} "
        f"matches_written={stats['matches_written']} "
        f"skipped_already_matched={stats['skipped']}"
    )
    log.info("Review results in Match Manager → Needs Review tab.")


if __name__ == "__main__":
    run_discovery()
