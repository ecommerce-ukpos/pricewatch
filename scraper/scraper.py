"""
scraper/scraper.py
──────────────────
Nightly price comparison scraper for PriceWatch Pro.

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

from playwright.async_api import async_playwright, Browser, BrowserContext
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
]

OOS_PATTERNS = [
    r"out\s+of\s+stock",
    r"currently\s+unavailable",
    r"temporarily\s+out",
    r"sold\s+out",
    r"no\s+stock",
    r"backordered?",
]


def detect_vat(page_text: str) -> str:
    text = page_text.lower()
    for pat in VAT_INC_PATTERNS:
        if re.search(pat, text):
            return "inc"
    for pat in VAT_EX_PATTERNS:
        if re.search(pat, text):
            return "ex"
    return "unknown"

def detect_oos(page_text: str) -> bool:
    text = page_text.lower()
    return any(re.search(p, text) for p in OOS_PATTERNS)

def parse_price(text: str) -> Optional[float]:
    m = re.search(r"£?\s*([\d,]+\.?\d*)", text.replace(",", ""))
    return float(m.group(1)) if m else None

def diff_pct(our_price: float, their_price: float) -> float:
    if our_price == 0:
        return 0.0
    return round(((their_price - our_price) / our_price) * 100, 2)

def normalise_price(price: float, vat_status: str, our_vat: str = "ex") -> float:
    if vat_status == "inc" and our_vat == "ex":
        return round(price / 1.2, 2)
    return price


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
        )

    async def scrape_direct(self, context: BrowserContext, url: str) -> dict:
        page   = await context.new_page()
        result = {
            "price": None, "vat": "unknown", "availability": "in_stock",
            "title": None, "url": url, "error": None,
        }
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
            await page.wait_for_timeout(1500)

            text                   = await page.inner_text("body")
            result["vat"]          = detect_vat(text)
            result["availability"] = "out_of_stock" if detect_oos(text) else "in_stock"

            price = await self._extract_jsonld_price(page)
            if price:
                result["price"] = price
            else:
                for sel in PRICE_SELECTORS:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        raw   = await el.inner_text()
                        price = parse_price(raw)
                        if price and price > 0:
                            result["price"] = price
                            break

            result["title"] = await page.title()

        except Exception as e:
            result["error"]        = str(e)[:200]
            result["availability"] = "error"
        finally:
            await page.close()
        return result

    async def _extract_jsonld_price(self, page) -> Optional[float]:
        scripts = await page.locator('script[type="application/ld+json"]').all()
        for script in scripts:
            try:
                text  = await script.inner_text()
                data  = json.loads(text)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if item.get("@type") in ("Product", "Offer"):
                        offers = item.get("offers") or item
                        if isinstance(offers, list):
                            offers = offers[0]
                        price = offers.get("price") or offers.get("lowPrice")
                        if price:
                            return float(price)
            except Exception:
                pass
        return None

    def fuzzy_confidence(self, sku_title: str, comp_title: str, sku: dict, our_price: float) -> int:
        score    = 0
        st       = sku_title.lower()
        ct       = (comp_title or "").lower()
        stop     = {"the", "a", "and", "of", "for", "with", "in"}
        s_tokens = set(re.findall(r"\b\w+\b", st)) - stop
        c_tokens = set(re.findall(r"\b\w+\b", ct)) - stop

        if s_tokens:
            overlap = len(s_tokens & c_tokens) / len(s_tokens)
            score  += int(overlap * 50)

        s_dims = set(re.findall(r"\b(?:A\d|[0-9]+(?:\.[0-9]+)?(?:cm|mm|m)|[0-9]+x[0-9]+)\b", st, re.I))
        c_dims = set(re.findall(r"\b(?:A\d|[0-9]+(?:\.[0-9]+)?(?:cm|mm|m)|[0-9]+x[0-9]+)\b", ct, re.I))
        if s_dims and c_dims:
            score += 30 if s_dims == c_dims else (15 if s_dims & c_dims else -10)
        elif s_dims and not c_dims:
            score -= 10

        if sku.get("unit_qty"):
            qty_str = str(sku["unit_qty"])
            if re.search(r"\b" + qty_str + r"\b", ct):
                score += 20

        return max(0, min(100, score))

    async def process_sku_competitor(
        self,
        browser: Browser,
        sku: dict,
        competitor: dict,
        match: Optional[dict],
    ) -> dict:
        snapshot = {
            "sku_id":              sku["sku_id"],
            "competitor_id":       competitor["id"],
            "run_id":              self.run_id,
            "scraped_at":          datetime.now(timezone.utc).isoformat(),
            "availability":        "error",
            "competitor_price":    None,
            "competitor_vat":      competitor.get("vat_status", "unknown"),
            "competitor_url":      match["competitor_url"] if match else None,
            "diff_pct":            None,
            "diff_pct_normalised": None,
            "confidence":          match["confidence"] if match else None,
            "error_message":       None,
        }

        if match and match["match_status"] in ("rejected", "pending"):
            snapshot["availability"] = "unavailable"
            return snapshot

        url = match["competitor_url"] if match else None
        if not url:
            snapshot["error_message"] = "No URL — needs matching first"
            snapshot["availability"]  = "unavailable"
            return snapshot

        ctx = await self.new_context(browser)
        try:
            result                     = await self.scrape_direct(ctx, url)
            snapshot["availability"]   = result["availability"]
            snapshot["error_message"]  = result["error"]
            snapshot["competitor_url"] = result["url"]

            if result["vat"] != "unknown":
                snapshot["competitor_vat"] = result["vat"]

            if result["price"]:
                snapshot["competitor_price"]    = result["price"]
                their_price_ex                  = normalise_price(result["price"], snapshot["competitor_vat"])
                our_price                       = float(sku["price_ex_vat"])
                snapshot["diff_pct"]            = diff_pct(our_price, result["price"])
                snapshot["diff_pct_normalised"] = diff_pct(our_price, their_price_ex)

                if result.get("title"):
                    snapshot["confidence"] = self.fuzzy_confidence(
                        sku["short_title"], result["title"], sku, our_price
                    )

        except Exception as e:
            snapshot["error_message"] = str(e)[:200]
        finally:
            await ctx.close()

        return snapshot

    async def write_snapshot(self, snapshot: dict):
        self.sb.table("price_snapshots").insert(snapshot).execute()

    async def flush_matches_for_sku(self, sku: dict, snapshots: list[dict], competitors: list[dict]):
        """
        Called once all 23 competitor snapshots for a single SKU are complete.
        Upserts a competitor_matches row for every snapshot that returned a price.
        """
        comp_map = {c["id"]: c for c in competitors}
        rows_to_upsert = []

        for snapshot in snapshots:
            if not snapshot.get("competitor_price"):
                continue
            if not snapshot.get("competitor_url"):
                continue

            confidence   = snapshot.get("confidence") or 0
            match_status = "matched" if confidence >= 80 else "review"
            comp         = comp_map.get(snapshot["competitor_id"], {})

            rows_to_upsert.append({
                "sku_id":           sku["sku_id"],
                "competitor_id":    snapshot["competitor_id"],
                "competitor_url":   snapshot["competitor_url"],
                "competitor_title": None,
                "match_status":     match_status,
                "confidence":       confidence,
                "match_method":     "scrape",
                "updated_at":       datetime.now(timezone.utc).isoformat(),
            })

            log.info(
                f"  Match queued: {sku['sku_id']} × {comp.get('domain','?')} "
                f"— {match_status} (conf {confidence}%)"
            )

        if rows_to_upsert:
            self.sb.table("competitor_matches").upsert(
                rows_to_upsert,
                on_conflict="sku_id,competitor_id",
            ).execute()
            log.info(
                f"Flushed {len(rows_to_upsert)} matches for SKU {sku['sku_id']} "
                f"({len(snapshots) - len(rows_to_upsert)} had no price)"
            )
        else:
            log.info(f"No matches to flush for SKU {sku['sku_id']} — no prices found")

    async def create_alerts(self, snapshot: dict, sku: dict, competitor: dict):
        alerts    = []
        our_price = float(sku["price_ex_vat"])
        diff      = snapshot.get("diff_pct_normalised") or snapshot.get("diff_pct")

        if snapshot["availability"] == "out_of_stock":
            alerts.append({
                "run_id":        self.run_id,
                "sku_id":        sku["sku_id"],
                "competitor_id": competitor["id"],
                "alert_type":    "oos_competitor",
                "message":       f"{competitor['name']} is out of stock for {sku['short_title']} — last price £{snapshot.get('competitor_price', '?')}",
                "diff_pct":      diff,
                "our_price":     our_price,
                "their_price":   snapshot.get("competitor_price"),
            })
        elif snapshot["availability"] == "unavailable":
            alerts.append({
                "run_id":        self.run_id,
                "sku_id":        sku["sku_id"],
                "competitor_id": competitor["id"],
                "alert_type":    "unavailable",
                "message":       f"{competitor['name']} no longer lists {sku['short_title']}",
                "our_price":     our_price,
                "their_price":   None,
                "diff_pct":      None,
            })
        elif diff is not None:
            if diff <= -10:
                alerts.append({
                    "run_id":        self.run_id,
                    "sku_id":        sku["sku_id"],
                    "competitor_id": competitor["id"],
                    "alert_type":    "critical",
                    "message":       f"{competitor['name']} is {abs(diff):.1f}% cheaper — £{snapshot['competitor_price']:.2f} vs your £{our_price:.2f}",
                    "diff_pct":      diff,
                    "our_price":     our_price,
                    "their_price":   snapshot.get("competitor_price"),
                })
            elif diff <= -5:
                alerts.append({
                    "run_id":        self.run_id,
                    "sku_id":        sku["sku_id"],
                    "competitor_id": competitor["id"],
                    "alert_type":    "warning",
                    "message":       f"{competitor['name']} is {abs(diff):.1f}% cheaper — £{snapshot['competitor_price']:.2f} vs your £{our_price:.2f}",
                    "diff_pct":      diff,
                    "our_price":     our_price,
                    "their_price":   snapshot.get("competitor_price"),
                })

        for alert in alerts:
            self.sb.table("alerts").insert(alert).execute()


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
        f"Starting sync run {run_id} "
        f"(workers={WORKERS}, delay={DELAY_MIN}–{DELAY_MAX}s, "
        f"sku_limit={SKU_LIMIT}, competitor_limit={COMPETITOR_LIMIT})"
    )

    # Load first N SKUs and first N competitors only
    skus_result  = (
        sb.table("skus")
        .select("*")
        .eq("active", True)
        .limit(SKU_LIMIT)
        .execute()
    )
    comps_result = (
        sb.table("competitors")
        .select("*")
        .eq("active", True)
        .order("id")
        .limit(COMPETITOR_LIMIT)
        .execute()
    )
    skus        = skus_result.data
    competitors = comps_result.data

    log.info(f"Loaded {len(skus)} SKUs and {len(competitors)} competitors")

    # Load all existing matches into a lookup map
    matches_result = sb.table("competitor_matches").select("*").execute()
    match_map      = {
        (m["sku_id"], m["competitor_id"]): m
        for m in matches_result.data
    }

    scraper = PriceScraper(sb, run_id)
    stats   = {"attempted": 0, "succeeded": 0, "failed": 0, "oos": 0}

    # ── Per-SKU semaphore: process one SKU at a time across all competitors ──
    # This ensures flush_matches_for_sku fires cleanly after all 23 competitor
    # checks for that SKU are done, before moving to the next SKU.
    sku_semaphore = asyncio.Semaphore(WORKERS)

    async def process_sku(sku: dict):
        """Scrape all competitors for one SKU, then flush matches in one batch."""
        async with sku_semaphore:
            log.info(f"Processing SKU {sku['sku_id']} — {sku['short_title']}")
            sku_snapshots = []

            for competitor in competitors:
                match    = match_map.get((sku["sku_id"], competitor["id"]))
                stats["attempted"] += 1

                try:
                    snapshot = await scraper.process_sku_competitor(
                        browser, sku, competitor, match
                    )
                    await scraper.write_snapshot(snapshot)
                    await scraper.create_alerts(snapshot, sku, competitor)
                    sku_snapshots.append(snapshot)

                    if snapshot["availability"] == "error":
                        stats["failed"] += 1
                    else:
                        stats["succeeded"] += 1
                        if snapshot["availability"] == "out_of_stock":
                            stats["oos"] += 1

                except Exception as e:
                    stats["failed"] += 1
                    log.error(
                        f"Error processing {sku['sku_id']} vs {competitor['domain']}: {e}"
                    )

                finally:
                    # Overnight pacing between each competitor check
                    delay = random.uniform(DELAY_MIN, DELAY_MAX)
                    log.debug(f"Sleeping {delay:.1f}s")
                    await asyncio.sleep(delay)

            # ── All 23 competitors done for this SKU — flush matches now ──
            await scraper.flush_matches_for_sku(sku, sku_snapshots, competitors)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        # Process SKUs with limited concurrency
        tasks   = [process_sku(sku) for sku in skus]
        await asyncio.gather(*tasks)
        await browser.close()

    sb.table("sync_runs").update({
        "status":         "complete",
        "completed_at":   datetime.now(timezone.utc).isoformat(),
        "skus_attempted": stats["attempted"],
        "skus_succeeded": stats["succeeded"],
        "skus_failed":    stats["failed"],
        "oos_flagged":    stats["oos"],
    }).eq("id", str(run_id)).execute()

    log.info(f"Sync run {run_id} complete: {stats}")


if __name__ == "__main__":
    import sys
    trigger = sys.argv[1] if len(sys.argv) > 1 else "scheduled"
    asyncio.run(run_scraper(trigger))
