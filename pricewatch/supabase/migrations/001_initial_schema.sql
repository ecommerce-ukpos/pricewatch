-- PriceWatch Pro — Initial Schema
-- Run this in Supabase SQL Editor or via `supabase db push`

-- ─── COMPETITORS ──────────────────────────────────────────────────────────────
create table if not exists competitors (
  id          serial primary key,
  domain      text not null unique,
  name        text not null,
  vat_status  text not null default 'unknown' check (vat_status in ('ex', 'inc', 'unknown')),
  active      boolean not null default true,
  feed_url    text,           -- discovered Google Shopping / sitemap feed URL
  notes       text,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

-- ─── SKUS ─────────────────────────────────────────────────────────────────────
create table if not exists skus (
  id              serial primary key,
  sku_id          text not null unique,   -- e.g. 124-300-FL
  mpn             text,
  short_title     text not null,
  full_title      text not null,
  slug            text not null,          -- URL slug for ukpos.com product page
  price_ex_vat    numeric(10,2) not null,
  availability    text not null default 'in stock',
  category        text,
  material        text,
  color           text,
  unit_qty        int,                    -- e.g. 100 for x100 packs
  image_url       text,
  product_url     text not null,
  active          boolean not null default true,
  last_feed_sync  timestamptz,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

-- ─── COMPETITOR MATCHES ───────────────────────────────────────────────────────
-- Stores the confirmed/reviewed match between a UKPOS SKU and a competitor product
create table if not exists competitor_matches (
  id              serial primary key,
  sku_id          text not null references skus(sku_id) on delete cascade,
  competitor_id   int not null references competitors(id) on delete cascade,
  competitor_url  text,                   -- direct URL to competitor product page
  competitor_title text,                  -- their product title as matched
  match_status    text not null default 'pending'
                  check (match_status in ('matched','review','rejected','unavailable','pending')),
  confidence      int check (confidence between 0 and 100),
  match_method    text,                   -- 'google_shopping','feed','scrape','manual'
  human_reviewed  boolean not null default false,
  reviewed_by     text,
  reviewed_at     timestamptz,
  notes           text,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  unique (sku_id, competitor_id)
);

-- ─── PRICE SNAPSHOTS ──────────────────────────────────────────────────────────
-- Every price observation — this is the historical time-series table
create table if not exists price_snapshots (
  id              bigserial primary key,
  sku_id          text not null references skus(sku_id) on delete cascade,
  competitor_id   int not null references competitors(id) on delete cascade,
  scraped_at      timestamptz not null default now(),
  run_id          uuid,                   -- groups all snapshots from one nightly run
  competitor_price numeric(10,2),
  competitor_vat  text check (competitor_vat in ('ex','inc','unknown')),
  competitor_url  text,                   -- URL actually scraped (may change over time)
  availability    text not null default 'in_stock'
                  check (availability in ('in_stock','out_of_stock','unavailable','error')),
  diff_pct        numeric(6,2),           -- ((their_price - our_price) / our_price) * 100
  diff_pct_normalised numeric(6,2),       -- diff after VAT normalisation
  confidence      int,
  raw_html_hash   text,                   -- for dedup / change detection
  error_message   text
);

-- Index for fast time-series queries
create index if not exists idx_snapshots_sku_time
  on price_snapshots (sku_id, scraped_at desc);

create index if not exists idx_snapshots_competitor_time
  on price_snapshots (competitor_id, scraped_at desc);

create index if not exists idx_snapshots_run
  on price_snapshots (run_id);

-- ─── SYNC RUNS ────────────────────────────────────────────────────────────────
create table if not exists sync_runs (
  id              uuid primary key default gen_random_uuid(),
  started_at      timestamptz not null default now(),
  completed_at    timestamptz,
  trigger         text not null default 'scheduled' check (trigger in ('scheduled','manual')),
  status          text not null default 'running'
                  check (status in ('running','complete','failed','partial')),
  skus_attempted  int not null default 0,
  skus_succeeded  int not null default 0,
  skus_failed     int not null default 0,
  oos_flagged     int not null default 0,
  review_queue    int not null default 0,
  error_log       jsonb,
  notes           text
);

-- ─── ALERTS ───────────────────────────────────────────────────────────────────
create table if not exists alerts (
  id              bigserial primary key,
  created_at      timestamptz not null default now(),
  run_id          uuid references sync_runs(id),
  sku_id          text not null references skus(sku_id) on delete cascade,
  competitor_id   int references competitors(id) on delete cascade,
  alert_type      text not null
                  check (alert_type in ('critical','warning','oos_us','oos_competitor','unavailable','price_drop_them','price_rise_them')),
  message         text not null,
  diff_pct        numeric(6,2),
  our_price       numeric(10,2),
  their_price     numeric(10,2),
  dismissed       boolean not null default false,
  dismissed_at    timestamptz
);

create index if not exists idx_alerts_undismissed
  on alerts (created_at desc) where dismissed = false;

-- ─── SEED COMPETITORS ─────────────────────────────────────────────────────────
insert into competitors (domain, name, vat_status) values
  ('alplas.com',                  'Alplas',                  'inc'),
  ('chalkboardsuk.co.uk',         'Chalkboards UK',          'unknown'),
  ('clear-display.co.uk',         'Clear Display',           'ex'),
  ('discountdisplays.co.uk',      'Discount Displays',       'ex'),
  ('displaypro.co.uk',            'Display Pro',             'unknown'),
  ('displaysense.co.uk',          'Displaysense',            'inc'),
  ('displaywizard.co.uk',         'Display Wizard',          'unknown'),
  ('gadsby.co.uk',                'Gadsby',                  'unknown'),
  ('ghdisplay.co.uk',             'GH Display',              'ex'),
  ('harrisonproducts.com',        'Harrison Products',       'unknown'),
  ('indigodisplays.co.uk',        'Indigo Displays',         'unknown'),
  ('luminati.co.uk',              'Luminati',                'inc'),
  ('pavementsigns.com',           'Pavement Signs',          'unknown'),
  ('retailacrylics.co.uk',        'Retail Acrylics',         'unknown'),
  ('shopfittingwarehouse.co.uk',  'Shopfitting Warehouse',   'unknown'),
  ('sign-holders.co.uk',          'Sign Holders',            'ex'),
  ('signwaves.co.uk',             'Signwaves',               'unknown'),
  ('snapframeswarehouse.co.uk',   'Snap Frames Warehouse',   'unknown'),
  ('theretailfactory.co.uk',      'The Retail Factory',      'unknown'),
  ('uksignshop.co.uk',            'UK Sign Shop',            'inc'),
  ('ultimadisplays.com',          'Ultima Displays',         'ex'),
  ('verydisplays.com',            'Very Displays',           'unknown'),
  ('vkf-renzel.co.uk',            'VKF Renzel',              'ex')
on conflict (domain) do nothing;

-- ─── USEFUL VIEWS ─────────────────────────────────────────────────────────────

-- Latest snapshot per SKU+competitor
create or replace view latest_snapshots as
select distinct on (sku_id, competitor_id)
  ps.*,
  s.short_title,
  s.price_ex_vat as our_price,
  s.product_url  as our_url,
  c.name         as competitor_name,
  c.domain       as competitor_domain,
  c.vat_status   as competitor_vat_default
from price_snapshots ps
join skus s on s.sku_id = ps.sku_id
join competitors c on c.id = ps.competitor_id
order by sku_id, competitor_id, scraped_at desc;

-- Dashboard summary: worst differentials
create or replace view worst_differentials as
select
  ls.sku_id,
  ls.short_title,
  ls.our_price,
  ls.our_url,
  ls.competitor_name,
  ls.competitor_domain,
  ls.competitor_price,
  ls.competitor_vat,
  ls.competitor_url,
  ls.diff_pct,
  ls.diff_pct_normalised,
  ls.availability,
  ls.scraped_at
from latest_snapshots ls
where ls.availability not in ('unavailable','error')
  and ls.diff_pct is not null
order by ls.diff_pct asc;  -- most expensive (negative = we're pricier) first
