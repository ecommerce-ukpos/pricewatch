"""
import_feed.py
─────────────
Parses the UKPOS Google Shopping XML feed and upserts into the `skus` table.

Usage:
    python scripts/import_feed.py --feed path/to/feed.xml

Environment variables required:
    SUPABASE_URL
    SUPABASE_SERVICE_KEY
"""

import argparse
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from supabase import create_client

NS = {"g": "http://base.google.com/ns/1.0"}

def parse_price(price_str: str) -> float | None:
    if not price_str:
        return None
    m = re.search(r"[\d.]+", price_str)
    return float(m.group()) if m else None

def extract_unit_qty(title: str) -> int | None:
    """Extract pack quantity from title, e.g. 'x 100' or 'Pack of 50'."""
    m = re.search(r"(?:x\s*|pack\s+of\s+)(\d+)", title, re.IGNORECASE)
    return int(m.group(1)) if m else None

def slug_from_url(url: str) -> str:
    """Extract the slug portion from a UKPOS product URL."""
    url = url.split("?")[0].rstrip("/")
    return url.split("/")[-1]

def item_to_row(item: ET.Element) -> dict:
    def g(tag: str) -> str:
        el = item.find(f"g:{tag}", NS)
        return (el.text or "").strip() if el is not None else ""

    title     = g("title")
    short     = g("short_title") or title[:80]
    url       = g("link")
    price_raw = g("price")

    return {
        "sku_id":         g("id"),
        "mpn":            g("mpn") or None,
        "short_title":    short,
        "full_title":     title,
        "slug":           slug_from_url(url),
        "price_ex_vat":   parse_price(price_raw),
        "availability":   g("availability") or "in stock",
        "category":       g("google_product_category") or None,
        "material":       g("material") or None,
        "color":          g("color") or None,
        "unit_qty":       extract_unit_qty(title),
        "image_url":      g("image_link") or None,
        "product_url":    url,
        "last_feed_sync": datetime.now(timezone.utc).isoformat(),
    }

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--feed", required=True, help="Path to XML feed file")
    args = parser.parse_args()

    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]
    sb  = create_client(url, key)

    tree = ET.parse(args.feed)
    root = tree.getroot()
    items = root.findall(".//item")

    print(f"Found {len(items)} items in feed")

    rows = []
    skipped = 0
    for item in items:
        row = item_to_row(item)
        if not row["sku_id"] or not row["price_ex_vat"] or not row["product_url"]:
            skipped += 1
            continue
        rows.append(row)

    # Upsert in batches of 100
    batch_size = 100
    upserted = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        sb.table("skus").upsert(batch, on_conflict="sku_id").execute()
        upserted += len(batch)
        print(f"  Upserted {upserted}/{len(rows)}")

    print(f"Done. {upserted} SKUs upserted, {skipped} skipped (missing required fields).")

if __name__ == "__main__":
    main()
