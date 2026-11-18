-- 009_harrison_stock_checks.sql
-- "Check Competitor Stock" page: two check types per run, matched UKPOS SKUs per row,
-- and a latest-per-product view with previous stock level and OOS-since.

alter table public.harrison_stock_runs add column if not exists check_type text not null default 'oos';
alter table public.harrison_stock_runs drop constraint if exists harrison_stock_runs_check_type_check;
alter table public.harrison_stock_runs add constraint harrison_stock_runs_check_type_check check (check_type in ('oos','levels'));
alter table public.harrison_stock add column if not exists ukpos_skus text[];

-- Latest non-error reading per Harrison URL, plus:
--   prev_stock_level / prev_scraped_at : last earlier reading that had a stock number
--   oos_since                          : first OOS reading of the current unbroken OOS run
--   seen_in_stock_before               : false → OOS since at least the first check
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

-- One-off clean-up of rows written before URL cleaning (tracking params, braces, trailing slash)
update public.harrison_stock set competitor_url =
  regexp_replace(split_part(replace(replace(competitor_url,'{',''),'}',''),'?',1),'/?$','/')
  || coalesce('?sku=' || nullif(substring(replace(replace(competitor_url,'{',''),'}','') from '[?&]sku=([^&]+)'),''),'');

-- Backfill matched UKPOS SKUs onto existing rows
update public.harrison_stock s set ukpos_skus = m.skus
from (
  select regexp_replace(split_part(replace(replace(competitor_url,'{',''),'}',''),'?',1),'/?$','/')
         || coalesce('?sku=' || nullif(substring(replace(replace(competitor_url,'{',''),'}','') from '[?&]sku=([^&]+)'),''),'') as url,
         array_agg(distinct sku_id) skus
  from public.competitor_matches where competitor_id = 10 and match_status = 'matched' group by 1
) m where m.url = s.competitor_url;
