"""
scripts/vd_sku_confirm.py
─────────────────────────
Fetch each Visual Displays review page, extract their on-page SKU,
and auto-confirm competitor_matches rows where their SKU == our sku_id.

Run from repo root:
    SUPABASE_URL=... SUPABASE_SERVICE_KEY=... python scripts/vd_sku_confirm.py

Optional env vars:
    DRY_RUN=true   — report only, don't write to DB (default false)
    COMPETITOR_ID  — default 24 (Visual Displays)
"""

import json, os, re, time
import httpx
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
COMPETITOR_ID = int(os.getenv("COMPETITOR_ID", "24"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

sb = create_client(SUPABASE_URL, SUPABASE_KEY)

# Matches SKU/product code labels followed by the value
SKU_RE = re.compile(
    r'(?:sku|product[\s_\-]?code|item[\s_\-]?code|ref(?:erence)?)'
    r'["\s:=]*([A-Za-z0-9][A-Za-z0-9\-]{1,20})',
    re.IGNORECASE
)

print(f"Fetching review matches for competitor {COMPETITOR_ID}...")
resp = sb.table('competitor_matches') \
    .select('id, sku_id, competitor_url') \
    .eq('competitor_id', COMPETITOR_ID) \
    .eq('match_status', 'review') \
    .execute()

rows = resp.data
print(f"  {len(rows)} rows to process")

# Deduplicate by URL — one fetch covers multiple sku rows pointing to same page
url_to_rows: dict = {}
for r in rows:
    url_to_rows.setdefault(r['competitor_url'], []).append(r)

confirmed_ids = []
no_sku        = []
no_match      = []
errors        = []

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36"
}

with httpx.Client(timeout=15, follow_redirects=True, headers=headers) as client:
    urls = list(url_to_rows.items())
    for i, (url, url_rows) in enumerate(urls):
        try:
            r = client.get(url)
            html = r.text
            skus_found = list(dict.fromkeys(
                m.group(1).upper() for m in SKU_RE.finditer(html)
            ))

            for row in url_rows:
                our_sku = row['sku_id'].upper()
                if our_sku in skus_found:
                    confirmed_ids.append(row['id'])
                    print(f"  ✓ {our_sku}  {url}")
                elif skus_found:
                    no_match.append({**row, 'vd_skus': skus_found})
                    print(f"  ✗ {our_sku} vs {skus_found}  {url}")
                else:
                    no_sku.append(row)

        except Exception as e:
            errors.append({'url': url, 'error': str(e)})
            print(f"  ! ERROR {url}: {e}")

        if (i + 1) % 25 == 0:
            print(f"  [{i+1}/{len(urls)}] confirmed={len(confirmed_ids)} "
                  f"no_sku={len(no_sku)} no_match={len(no_match)} errors={len(errors)}")
            time.sleep(0.5)

print(f"\nSummary: confirmed={len(confirmed_ids)} no_sku={len(no_sku)} "
      f"no_match={len(no_match)} errors={len(errors)}")

if confirmed_ids and not DRY_RUN:
    print(f"\nWriting {len(confirmed_ids)} confirmations to DB...")
    BATCH = 50
    for i in range(0, len(confirmed_ids), BATCH):
        batch = confirmed_ids[i:i+BATCH]
        sb.table('competitor_matches').update({
            'match_status': 'matched',
            'human_reviewed': True,
            'reviewed_by': 'vd-sku-confirm-script',
            'notes': 'Auto-confirmed: VD on-page SKU matched sku_id exactly',
        }).in_('id', batch).execute()
    print("Done.")
elif DRY_RUN:
    print("DRY_RUN — no DB writes.")

# Save full report
report = {
    'confirmed_ids': confirmed_ids,
    'no_match': no_match,
    'no_sku': [r['sku_id'] for r in no_sku],
    'errors': errors,
}
out = 'vd_confirm_report.json'
with open(out, 'w') as f:
    json.dump(report, f, indent=2)
print(f"Report: {out}")
