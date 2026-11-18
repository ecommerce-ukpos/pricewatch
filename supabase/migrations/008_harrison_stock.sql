-- 008_harrison_stock.sql
-- Harrison Products stock scrape: one run row per button press, one result row per
-- product per run. History is kept; the "Harrison stock" page shows the latest run.

create table if not exists public.harrison_stock_runs (
  id            uuid primary key default gen_random_uuid(),
  scope         text not null default 'matches' check (scope in ('matches','catalogue')),
  status        text not null default 'running' check (status in ('running','done','failed')),
  started_at    timestamptz not null default now(),
  finished_at   timestamptz,
  url_count     integer,
  ok_count      integer,
  error_message text
);

create table if not exists public.harrison_stock (
  id             bigserial primary key,
  run_id         uuid not null references public.harrison_stock_runs(id) on delete cascade,
  scraped_at     timestamptz not null default now(),
  competitor_url text not null,
  product_name   text,
  harrison_sku   text,
  price_ex_vat   numeric(10,2),
  price_inc_vat  numeric(10,2),
  stock_level    integer,          -- null when Harrison does not publish a number
  stock_status   text not null check (stock_status in ('in_stock','out_of_stock','unknown','error')),
  error_message  text
);

create index if not exists harrison_stock_run_idx on public.harrison_stock(run_id);
create index if not exists harrison_stock_url_idx on public.harrison_stock(competitor_url, scraped_at desc);
create index if not exists harrison_stock_runs_started_idx on public.harrison_stock_runs(started_at desc);

alter table public.harrison_stock_runs enable row level security;
alter table public.harrison_stock      enable row level security;

drop policy if exists "approved users read harrison_stock_runs" on public.harrison_stock_runs;
create policy "approved users read harrison_stock_runs" on public.harrison_stock_runs
  for select using (is_approved_user());

drop policy if exists "approved users read harrison_stock" on public.harrison_stock;
create policy "approved users read harrison_stock" on public.harrison_stock
  for select using (is_approved_user());
-- Writes come from the scraper with the service key (bypasses RLS).
