-- 011: Harrison test-data flag, daily-run label, minute-precision times, faster latest view.
-- Applied 19 Nov 2026.

alter table public.harrison_stock      add column if not exists is_test boolean not null default false;
alter table public.harrison_stock_runs add column if not exists is_test boolean not null default false;
alter table public.harrison_stock_runs add column if not exists triggered_by text not null default 'manual';
create index if not exists harrison_stock_is_test_idx on public.harrison_stock (is_test) where is_test;

-- Store Harrison times to the minute (2026-11-18 17:30), not microseconds.
create or replace function public.harrison_minute_times() returns trigger
language plpgsql set search_path = public as $$
begin
  if tg_table_name = 'harrison_stock' then
    new.scraped_at := date_trunc('minute', new.scraped_at);
  else
    new.started_at  := date_trunc('minute', new.started_at);
    new.finished_at := date_trunc('minute', new.finished_at);
  end if;
  return new;
end $$;
drop trigger if exists harrison_stock_minute on public.harrison_stock;
create trigger harrison_stock_minute before insert or update of scraped_at on public.harrison_stock
  for each row execute function public.harrison_minute_times();
drop trigger if exists harrison_runs_minute on public.harrison_stock_runs;
create trigger harrison_runs_minute before insert or update of started_at, finished_at on public.harrison_stock_runs
  for each row execute function public.harrison_minute_times();

-- Latest reading per URL, computed in one pass (the old correlated subqueries timed out
-- once history grew). Same columns as before.
create index if not exists harrison_stock_url_time_idx on public.harrison_stock (competitor_url, scraped_at desc);
create or replace view public.harrison_stock_latest with (security_invoker = true) as
with ok as (
  select h.*, row_number() over (partition by h.competitor_url order by h.scraped_at desc) as rn
  from public.harrison_stock h
  where h.stock_status <> 'error'
), agg as (
  select competitor_url,
         max(scraped_at) filter (where stock_status = 'in_stock' and rn > 1) as last_in_before
  from ok group by competitor_url
), oos as (
  select o.competitor_url, min(o.scraped_at) as oos_since
  from ok o join agg a using (competitor_url)
  where o.stock_status = 'out_of_stock' and o.scraped_at > coalesce(a.last_in_before, '-infinity'::timestamptz)
  group by o.competitor_url
), prev as (
  select distinct on (competitor_url) competitor_url, stock_level, scraped_at
  from ok where rn > 1 and stock_level is not null
  order by competitor_url, scraped_at desc
)
select l.competitor_url, l.product_name, l.harrison_sku, l.ukpos_skus, l.price_ex_vat, l.price_inc_vat,
       l.stock_level, l.stock_status, l.scraped_at, l.kind, l.due_in, l.next_due_date, l.next_due_qty,
       l.due_scope, l.error_message,
       p.stock_level as prev_stock_level,
       p.scraped_at  as prev_scraped_at,
       case when l.stock_status = 'out_of_stock' then o.oos_since else null::timestamptz end as oos_since,
       (a.last_in_before is not null) as seen_in_stock_before
from ok l
join agg a on a.competitor_url = l.competitor_url
left join oos o on o.competitor_url = l.competitor_url
left join prev p on p.competitor_url = l.competitor_url
where l.rn = 1;

-- Remove the 1–15 Nov test data when finished testing:
--   delete from public.harrison_stock where is_test;
--   delete from public.harrison_stock_runs where is_test;
