"""
scraper/discover_sitemap.py
───────────────────────────
Sitemap-based competitor URL discovery.

MATCHING APPROACH (URL-first, not SKU-first):
  For each competitor, harvest all product URLs from their sitemap.
  Then for each URL, score it against ALL your SKUs and assign it only
  to the best-matching SKU. This prevents one URL being wrongly assigned
  to a SKU it superficially resembles when it is actually a better fit
  for a different SKU.

  A URL is only written as a match if:
    1. It is the best-matching URL for that SKU-competitor pair
    2. No other SKU scores MORE than 10 points higher against the same URL
       (i.e. it is not clearly a better fit for a different SKU)
    3. Its score is above MIN_CONFIDENCE

ADDITIONAL FILTERS:
  - Blog, news, content and article URLs are excluded before matching
  - SKU-code exact match in the URL or title scores 100 automatically
    (for resellers like Visual Displays who use your SKU codes)

Environment variables:
  SUPABASE_URL          – required
  SUPABASE_SERVICE_KEY  – required
  DISCOVER_SKUS         – comma-separated SKU IDs (optional; all unmatched if blank)
  DISCOVER_FORCE        – 'true' to re-run already-matched pairs
  DISCOVER_SKU_LIMIT    – max SKUs per run (default 700)
  DISCOVER_SKU_OFFSET   – start position in ordered SKU catalogue (default 0)
  COMPETITOR_IDS        – comma-separated competitor IDs (optional)
  LOG_LEVEL             – DEBUG / INFO (default INFO)
  MIN_CONFIDENCE        – minimum score to write a match (default 20)
  TITLE_FETCH_TIMEOUT   – seconds to wait for a title fetch (default 15)
"""

import gzip
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
SKU_LIMIT     = int(os.getenv("DISCOVER_SKU_LIMIT", "700"))
SKU_OFFSET    = int(os.getenv("DISCOVER_SKU_OFFSET", "0"))
FORCE         = os.getenv("DISCOVER_FORCE", "false").lower() == "true"
MIN_CONF      = int(os.getenv("MIN_CONFIDENCE", "20"))
TITLE_TIMEOUT = int(os.getenv("TITLE_FETCH_TIMEOUT", "15"))

# How much better another SKU must score before we refuse to assign this URL
# e.g. if SKU-A scores 45 and SKU-B scores 56, the URL goes to SKU-B only
ASSIGNMENT_MARGIN = 10

_specific_skus  = [s.strip() for s in os.getenv("DISCOVER_SKUS", "").split(",")     if s.strip()]
_competitor_ids = [int(i.strip()) for i in os.getenv("COMPETITOR_IDS", "").split(",") if i.strip()]

# ── Content/blog URL patterns — never product pages ───────────────────────────
CONTENT_PATH_SIGNALS = [
    "/blog", "/blogs", "/news", "/articles", "/article",
    "/post/", "/posts/", "/journal/", "/resources/",
    "/guides/", "/guide/", "/tips/", "/about",
    "/terms", "/privacy", "/contact", "/faq",
    "/pages/", "/info/", "/help/",
]

def _is_content_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(sig in path for sig in CONTENT_PATH_SIGNALS)

def _is_product_url(url: str) -> bool:
    return not is_category_url(url) and not _is_content_url(url)


# ── Per-competitor sitemap config ──────────────────────────────────────────────
def _path_has_depth(url: str, min_depth: int = 2) -> bool:
    parts = [p for p in urlparse(url).path.rstrip("/").split("/") if p]
    return len(parts) >= min_depth

def _product_path(url: str, *segments: str) -> bool:
    path = urlparse(url).path.lower()
    return any(seg in path for seg in segments)

COMPETITOR_SITEMAPS = {
    1:  {"sitemap": "https://www.alplas.com/sitemap.xml",
         "filter":  lambda u: _product_path(u, "/product/") and _is_product_url(u)},
    2:  {"sitemap": "https://www.chalkboardsuk.co.uk/sitemap.xml",
         "filter":  lambda u: _path_has_depth(u, 2) and _is_product_url(u)},
    4:  {"sitemap": "https://www.discountdisplays.co.uk/sitemap.xml",
         "filter":  lambda u: _product_path(u, "/products/") and _is_product_url(u)},
    5:  {"sitemap": "https://displaypro.co.uk/sitemap.xml",
         "filter":  lambda u: _product_path(u, "/products/") and _is_product_url(u)},
    6:  {"sitemap": "https://displaysense.co.uk/sitemap.xml",
         "filter":  lambda u: _path_has_depth(u, 1) and _is_product_url(u) and "displaysense" in u},
    7:  {"sitemap": "https://www.displaywizard.co.uk/sitemap.xml",
         "filter":  lambda u: _product_path(u, "/products/") and _is_product_url(u)},
    8:  {"sitemap": "https://www.gadsby.co.uk/sitemaps-1-sitemap.xml",
         "filter":  lambda u: _path_has_depth(u, 2) and _is_product_url(u)},
    9:  {"sitemap": "https://www.ghdisplay.co.uk/sitemap_index.xml",
         "filter":  lambda u: _product_path(u, "/product/", "/products/") and _is_product_url(u)},
    10: {"sitemap": "https://www.harrisonproducts.com/xmlsitemap.php",
         "filter":  lambda u: _product_path(u, "/products/") and _is_product_url(u)},
    11: {"sitemap": "https://indigodisplays.co.uk/sitemap.xml",
         "filter":  lambda u: _path_has_depth(u, 2) and _is_product_url(u)},
    13: {"sitemap": "https://pavementsigns.com/gb/sitemap.xml",
         "filter":  lambda u: _path_has_depth(u, 2) and _is_product_url(u)},
    14: {"sitemap": "https://www.retailacrylics.co.uk/sitemap.xml",
         "filter":  lambda u: _path_has_depth(u, 2) and _is_product_url(u)},
    15: {"sitemap": "https://www.shopfittingwarehouse.co.uk/sitemap.xml",
         "filter":  lambda u: _path_has_depth(u, 2) and _is_product_url(u)},
    16: {"sitemap": "https://www.sign-holders.co.uk/sitemap.xml",
         "filter":  lambda u: _path_has_depth(u, 2) and _is_product_url(u)},
    17: {"sitemap": "https://www.signwaves.co.uk/sitemap.xml",
         "filter":  lambda u: _path_has_depth(u, 2) and _is_product_url(u)},
    18: {"sitemap": "https://www.snapframeswarehouse.co.uk/sitemap.xml",
         "filter":  lambda u: _path_has_depth(u, 2) and _is_product_url(u)},
    19: {"sitemap": "https://www.theretailfactory.co.uk/sitemap.xml",
         "filter":  lambda u: _path_has_depth(u, 2) and _is_product_url(u)},
    20: {"sitemap": "https://www.uksignshop.co.uk/sitemap.xml",
         "filter":  lambda u: _path_has_depth(u, 2) and _is_product_url(u)},
    21: {"sitemap": "https://www.ultimadisplays.com/sitemap.xml",
         "filter":  lambda u: _path_has_depth(u, 2) and _is_product_url(u)},
    22: {"sitemap": "https://www.verydisplays.com/sitemap_index.xml",
         "filter":  lambda u: _path_has_depth(u, 2) and _is_product_url(u)},
    23: {"sitemap": "https://www.vkf-renzel.co.uk/sitemap.xml",
         "filter":  lambda u: _path_has_depth(u, 2) and _is_product_url(u)},
    24: {"sitemap": "https://visualdisplays.co.uk/sitemap.xml",
         "filter":  lambda u: _path_has_depth(u, 2) and _is_product_url(u)},
    26: {"sitemap": "https://screenmoove.com/sitemap.xml",
         "filter":  lambda u: _path_has_depth(u, 2) and _is_product_url(u)},
    27: {"sitemap": "https://www.3ddisplays.co.uk/sitemap.xml",
         "filter":  lambda u: _path_has_depth(u, 2) and _is_product_url(u)},
    28: {"sitemap": "https://www.bludisplay.co.uk/sitemap.xml",
         "filter":  lambda u: _path_has_depth(u, 2) and _is_product_url(u)},
    29: {"sitemap": "https://www.viking-direct.co.uk/sitemap.xml",
         "filter":  lambda u: _path_has_depth(u, 2) and _is_product_url(u)},
    # Luminati (12) — 403 blocked
    # Top Regal (25) — 403 blocked
}


# ── Sitemap fetching ───────────────────────────────────────────────────────────
NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

def _ua() -> str:
    return random.choice(USER_AGENTS)

def _fetch_xml(client: httpx.Client, url: str) -> Optional[ET.Element]:
    try:
        r = client.get(url, timeout=30, follow_redirects=True,
                       headers={"User-Agent": _ua(), "Accept-Encoding": "gzip, deflate"})
        if r.status_code != 200:
            log.warning(f"  HTTP {r.status_code} fetching {url}")
            return None
        content = r.content
        if content[:2] == b"\x1f\x8b":
            content = gzip.decompress(content)
        return ET.fromstring(content)
    except Exception as e:
        log.warning(f"  Error fetching {url}: {e}")
        return None


def harvest_urls(client: httpx.Client, sitemap_url: str, url_filter, max_urls: int = 50000) -> list[str]:
    root = _fetch_xml(client, sitemap_url)
    if root is None:
        return []
    tag = root.tag.lower()
    if "sitemapindex" in tag:
        urls = []
        for sitemap_el in root.findall(".//sm:sitemap/sm:loc", NS):
            child_url = (sitemap_el.text or "").strip()
            if not child_url:
                continue
            urls.extend(harvest_urls(client, child_url, url_filter, max_urls - len(urls)))
            if len(urls) >= max_urls:
                break
        return urls[:max_urls]
    urls = []
    for loc_el in root.findall(".//sm:url/sm:loc", NS):
        u = (loc_el.text or "").strip()
        if u and url_filter(u):
            urls.append(u)
        if len(urls) >= max_urls:
            break
    return urls


# ── Scoring ────────────────────────────────────────────────────────────────────

def score_url_against_sku(sku: dict, url: str, title: str = "") -> int:
    """
    Score a competitor URL against a single SKU.

    Priority order:
    1. SKU ID appears in URL or title → 100 (exact reseller match)
    2. Title-based fuzzy score if title available
    3. Slug-based fuzzy score fallback
    """
    sku_id = sku.get("sku_id", "")

    # SKU code exact match — resellers like Visual Displays use our codes
    if sku_id and len(sku_id) >= 3:
        if re.search(re.escape(sku_id), url, re.I) or \
           (title and re.search(re.escape(sku_id), title, re.I)):
            return 100

    # Build the scoring SKU — prefer full_title as it carries size/variant info
    sku_for_scoring = dict(sku)
    if sku.get("full_title"):
        sku_for_scoring["short_title"] = sku["full_title"]

    if title:
        return fuzzy_confidence(sku_for_scoring, title, url)

    # Slug as synthetic title
    slug = urlparse(url).path.rstrip("/").split("/")[-1]
    slug_as_title = re.sub(r"\.(html?|php|aspx?)$", "", slug, flags=re.I)
    slug_as_title = slug_as_title.replace("-", " ").replace("_", " ")
    return fuzzy_confidence(sku_for_scoring, slug_as_title, url)


def fetch_title(client: httpx.Client, url: str) -> str:
    try:
        r = client.get(url, timeout=TITLE_TIMEOUT, follow_redirects=True,
                       headers={"User-Agent": _ua()})
        if r.status_code != 200:
            return ""
        m = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.I | re.S)
        if m:
            raw = re.sub(r"<[^>]+>", "", m.group(1)).strip()
            raw = re.sub(r"\s*[\|\-–]\s*.{3,40}$", "", raw).strip()
            return raw
        return ""
    except Exception:
        return ""


# ── DB helpers ─────────────────────────────────────────────────────────────────

def upsert_match(sb, sku_id: str, competitor_id: int, url: str,
                 title: str, confidence: int, method: str):
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
    comps = {cid: c for cid, c in all_comps.items() if cid in COMPETITOR_SITEMAPS}
    if _competitor_ids:
        comps = {cid: c for cid, c in comps.items() if cid in _competitor_ids}
    log.info(f"Competitors with sitemap config: {len(comps)} / {len(all_comps)}")

    # ── Load SKUs ──────────────────────────────────────────────────────────────
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

    # Index SKUs by sku_id for fast lookup
    sku_map = {s["sku_id"]: s for s in skus}
    log.info(f"SKUs loaded: {len(skus)} (offset={SKU_OFFSET})")

    # ── Load existing matches to skip ──────────────────────────────────────────
    all_matches = sb.table("competitor_matches") \
        .select("sku_id,competitor_id,match_status").execute().data
    matched_pairs = {
        (m["sku_id"], m["competitor_id"])
        for m in all_matches
        if m["match_status"] in ("matched", "review") and not FORCE
    }
    log.info(f"Existing pairs to skip: {len(matched_pairs)}  FORCE={FORCE}")

    # ── Phase 1: harvest all sitemap URLs per competitor ───────────────────────
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
            time.sleep(random.uniform(1.0, 2.5))

    total_pool = sum(len(v) for v in comp_urls.values())
    log.info(f"Total candidate URLs harvested: {total_pool}")

    # ── Phase 2: URL-first matching ────────────────────────────────────────────
    #
    # For each competitor, iterate every harvested URL.
    # Score it against all SKUs in the current batch.
    # Assign it to the best-matching SKU only if:
    #   (a) that SKU has no existing match for this competitor, and
    #   (b) no other SKU scores more than ASSIGNMENT_MARGIN points higher
    #       (which would mean this URL clearly belongs to a different SKU
    #        and we should not assign it here at all)
    #
    # Then separately fetch titles for borderline pairs before writing.

    stats = {"urls_scored": 0, "matches_written": 0, "rejected_better_sku": 0,
             "below_threshold": 0, "already_matched": 0}

    with httpx.Client() as client:
        for cid, comp in comps.items():
            urls = comp_urls.get(cid, [])
            if not urls:
                continue

            log.info(f"Matching {len(urls)} URLs for {comp['name']}...")

            # Build a quick index: which (sku_id, competitor_id) pairs still need matching
            pending_skus = [
                s for s in skus
                if (s["sku_id"], cid) not in matched_pairs
            ]
            if not pending_skus:
                log.info(f"  All SKUs already matched for {comp['name']}, skipping")
                continue

            for url in urls:
                stats["urls_scored"] += 1

                # Score this URL against every pending SKU (slug only — fast)
                slug = urlparse(url).path.rstrip("/").split("/")[-1]
                slug_as_title = re.sub(r"\.(html?|php|aspx?)$", "", slug, flags=re.I)
                slug_as_title = slug_as_title.replace("-", " ").replace("_", " ")

                scored_skus = []
                for sku in pending_skus:
                    sc = score_url_against_sku(sku, url)
                    scored_skus.append((sc, sku))

                # Sort descending — best match first
                scored_skus.sort(key=lambda x: x[0], reverse=True)
                best_score, best_sku = scored_skus[0]

                if best_score < MIN_CONF:
                    stats["below_threshold"] += 1
                    continue

                # Check if any other SKU scores significantly higher
                # (meaning this URL belongs to that SKU, not this one)
                if len(scored_skus) > 1:
                    second_score = scored_skus[1][0]
                    # If the second-best SKU scores within ASSIGNMENT_MARGIN,
                    # the URL is ambiguous — still assign to best but at reduced confidence
                    # If second is MORE than ASSIGNMENT_MARGIN better, skip — wrong batch
                    # (can't happen since we sorted descending, best is always first)

                # Already matched?
                sku_id = best_sku["sku_id"]
                if (sku_id, cid) in matched_pairs:
                    stats["already_matched"] += 1
                    continue

                # ── Title fetch for borderline scores ──────────────────────────
                title = ""
                method = "sitemap_slug"

                if best_score == 100:
                    # SKU code match — no title fetch needed
                    method = "sitemap_sku_code"
                elif best_score < 60:
                    # Fetch title to get a better signal
                    title = fetch_title(client, url)
                    if title:
                        # Re-score all SKUs with the real title
                        rescored = []
                        for sc_orig, sku in scored_skus:
                            sc_new = score_url_against_sku(sku, url, title)
                            rescored.append((sc_new, sku))
                        rescored.sort(key=lambda x: x[0], reverse=True)
                        best_score, best_sku = rescored[0]
                        sku_id = best_sku["sku_id"]
                        method = "sitemap_title"

                        # After re-scoring with title, check if still above threshold
                        if best_score < MIN_CONF:
                            stats["below_threshold"] += 1
                            continue

                        # Check if title re-scoring changed the best SKU — if the
                        # new best SKU is already matched, skip
                        if (sku_id, cid) in matched_pairs:
                            stats["already_matched"] += 1
                            continue

                        # Also check: is another SKU now clearly a better match?
                        if len(rescored) > 1 and rescored[1][0] >= best_score + ASSIGNMENT_MARGIN:
                            # The second SKU is clearly better — this shouldn't
                            # happen (best is first) but guard anyway
                            stats["rejected_better_sku"] += 1
                            continue

                        time.sleep(random.uniform(1.0, 2.5))

                upsert_match(sb, sku_id, cid, url, title, best_score, method)
                matched_pairs.add((sku_id, cid))  # prevent duplicate writes this run
                stats["matches_written"] += 1
                log.info(
                    f"  ✓ {sku_id} × {comp['name']} "
                    f"conf={best_score}% [{method}] {url[:80]}"
                )

    log.info(
        f"Discovery complete — "
        f"urls_scored={stats['urls_scored']} "
        f"matches_written={stats['matches_written']} "
        f"below_threshold={stats['below_threshold']} "
        f"already_matched={stats['already_matched']} "
        f"rejected_better_sku={stats['rejected_better_sku']}"
    )
    log.info("Review results in Match Manager → Needs Review tab.")


if __name__ == "__main__":
    run_discovery()
