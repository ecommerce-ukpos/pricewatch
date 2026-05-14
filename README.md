# PriceWatch Pro — UKPOS Competitive Price Monitor

500 SKUs × 23 competitors. Nightly scrapes at 1am. Supabase (Postgres) + Vercel.

---

## Quick start (30 minutes)

### 1. Supabase — create project & schema

1. Go to [supabase.com](https://supabase.com) → New project
   - Name: `pricewatch`
   - Region: `eu-west-2` (London) — closest to your competitors
   - Generate a strong DB password and save it

2. In your new project → **SQL Editor** → paste and run:
   ```
   supabase/migrations/001_initial_schema.sql
   ```
   This creates all tables, views, indexes, and seeds the 23 competitors.

3. Save your credentials (Settings → API):
   - `SUPABASE_URL` — e.g. `https://xxxxx.supabase.co`
   - `SUPABASE_SERVICE_KEY` — the `service_role` key (not `anon`)

### 2. Import your product feed

```bash
pip install -r requirements.txt
playwright install chromium

export SUPABASE_URL=https://xxxxx.supabase.co
export SUPABASE_SERVICE_KEY=your-service-key

python scripts/import_feed.py --feed path/to/your-feed.xml
```

This upserts all SKUs from your Google Shopping XML feed into Supabase.
Re-run whenever you update the feed — it's safe to run repeatedly.

### 3. Deploy to Vercel

```bash
npm i -g vercel
vercel login

cd pricewatch
vercel

# Set environment variables
vercel env add SUPABASE_URL
vercel env add SUPABASE_SERVICE_KEY
vercel env add CRON_SECRET   # any random string, e.g. openssl rand -hex 32

vercel --prod
```

Your dashboard will be live at `https://pricewatch-xxx.vercel.app`

### 4. Run your first scrape (manual)

Before the nightly cron kicks in, run a test scrape locally:

```bash
export SUPABASE_URL=...
export SUPABASE_SERVICE_KEY=...
export SCRAPER_WORKERS=3       # start low for testing

python -m scraper.scraper manual
```

Watch the logs — it will attempt to scrape all 23 competitors for every SKU
that has a `competitor_url` in the `competitor_matches` table.

**Important:** The scraper needs matches populated first. On first run,
matches will be empty — you'll need to either:
- Manually add URLs for key SKUs via Supabase Table Editor, or
- Build/run the Google Shopping matching script (see below)

### 5. Nightly cron

Vercel Cron (defined in `vercel.json`) calls `/api/cron/nightly` at 01:00am.

For the actual long-running scrape (can take 2+ hours for 500 × 23), the
recommended approach is:
- **Option A:** Railway.app — deploy the scraper separately, trigger via webhook
- **Option B:** Supabase Edge Function — runs in Deno, good for medium loads
- **Option C:** A simple VPS (£5/mo DigitalOcean) running a cron job directly

---

## Populating competitor matches

The `competitor_matches` table needs a URL per (SKU, competitor) pair.

**Recommended initial approach:**
1. Export your top 50 SKUs from Supabase
2. For each SKU, Google Shopping search: `"{short_title}"` on each competitor's domain
3. Paste the matching URLs into Supabase Table Editor
4. Set `match_status = 'matched'` and `confidence = 95` for manually confirmed matches

Once you have the first batch in, the scraper will keep prices current nightly.

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `SUPABASE_URL` | ✓ | Your Supabase project URL |
| `SUPABASE_SERVICE_KEY` | ✓ | Supabase service role key |
| `CRON_SECRET` | recommended | Secures the cron endpoint |
| `SCRAPER_WORKERS` | optional | Parallel workers (default: 5) |
| `SCRAPER_PAGE_TIMEOUT_MS` | optional | Page timeout ms (default: 30000) |
| `SCRAPER_EDGE_FN_URL` | optional | Supabase Edge Function URL for scraper |

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Vercel (frontend + API)                            │
│  ┌──────────────┐   ┌─────────────────────────────┐ │
│  │ index.html   │   │ FastAPI (/api/*)             │ │
│  │ Dashboard    │◄──│ dashboard, skus, alerts,    │ │
│  │ (vanilla JS) │   │ competitors, review, runs   │ │
│  └──────────────┘   └──────────────┬──────────────┘ │
│                     Vercel Cron 1am│                 │
└─────────────────────────────────────────────────────┘
                              │
                    ┌─────────▼──────────┐
                    │ Scraper process    │
                    │ (Railway/VPS/local)│
                    │ Playwright + async │
                    │ 5 workers          │
                    │ UA rotation        │
                    └─────────┬──────────┘
                              │ reads/writes
                    ┌─────────▼──────────┐
                    │ Supabase Postgres  │
                    │                    │
                    │ skus               │
                    │ competitors        │
                    │ competitor_matches │
                    │ price_snapshots    │
                    │ sync_runs          │
                    │ alerts             │
                    └────────────────────┘
```

---

## Key decisions & notes

- **Your prices are ex-VAT** (confirmed — feed uses `?vat=0` throughout)
- **Competitor VAT** — auto-detected from page text, manually confirmable in dashboard
- **Pack normalisation** — scraper extracts unit qty from titles and normalises per-unit price
- **Confidence scoring** — token overlap + dimension match + pack qty match = 0-100 score
- **OOS handling** — included in comparisons, availability field set, last-known price retained
- **reddit.com** — removed from competitor list per your instruction
- **morplan.com / officefurnitureonline.co.uk** — excluded (active=false) per your instruction

---

## Files

```
pricewatch/
├── supabase/
│   └── migrations/
│       └── 001_initial_schema.sql   ← Run this first in Supabase SQL Editor
├── scripts/
│   └── import_feed.py               ← Import UKPOS XML feed → Supabase
├── scraper/
│   └── scraper.py                   ← Core nightly scrape engine
├── api/
│   ├── main.py                      ← FastAPI backend (all dashboard API routes)
│   └── cron/
│       └── nightly.py               ← Vercel cron trigger endpoint
├── frontend/
│   └── index.html                   ← Full dashboard (vanilla JS, no build step)
├── vercel.json                      ← Vercel config + cron schedule
├── requirements.txt
└── README.md
```
