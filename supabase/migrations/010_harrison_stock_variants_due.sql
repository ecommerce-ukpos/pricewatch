-- 010_harrison_stock_variants_due.sql
-- Variant rows (kind) and Harrison "Stock & Lead Times" due-in dates.

alter table public.harrison_stock add column if not exists kind text;           -- single | variant | variant_parent | options_unsplit | cut_to_order | no_basket
alter table public.harrison_stock add column if not exists due_in jsonb;        -- [{qty, date}] all listed deliveries
alter table public.harrison_stock add column if not exists next_due_date date;  -- earliest delivery dated today or later
alter table public.harrison_stock add column if not exists next_due_qty integer;
alter table public.harrison_stock add column if not exists due_scope text;      -- item | range (product has sizes/colours; size not stated)

drop view if exists public.harrison_stock_latest;
create view public.harrison_stock_latest with (security_invoker = true) as
with ok as (
  select * from public.harrison_stock where stock_status <> 'error'
), latest as (
  select distinct on (competitor_url) * from ok order by competitor_url, scraped_at desc
)
select
  l.competitor_url, l.product_name, l.harrison_sku, l.ukpos_skus,
  l.price_ex_vat, l.price_inc_vat, l.stock_level, l.stock_status, l.scraped_at,
  l.kind, l.due_in, l.next_due_date, l.next_due_qty, l.due_scope, l.error_message,
  p.stock_level  as prev_stock_level,
  p.scraped_at   as prev_scraped_at,
  case when l.stock_status = 'out_of_stock' then (
    select min(o.scraped_at) from ok o
    where o.competitor_url = l.competitor_url and o.stock_status = 'out_of_stock'
      and o.scraped_at > coalesce((select max(i.scraped_at) from ok i
                                   where i.competitor_url = l.competitor_url
                                     and i.stock_status = 'in_stock'
                                     and i.scraped_at < l.scraped_at), '-infinity'::timestamptz)
  ) end as oos_since,
  exists (select 1 from ok i where i.competitor_url = l.competitor_url
          and i.stock_status = 'in_stock' and i.scraped_at < l.scraped_at) as seen_in_stock_before
from latest l
left join lateral (
  select x.stock_level, x.scraped_at from ok x
  where x.competitor_url = l.competitor_url and x.scraped_at < l.scraped_at and x.stock_level is not null
  order by x.scraped_at desc limit 1
) p on true;

grant select on public.harrison_stock_latest to authenticated;
