-- 013: Check Competitor Stock shows one line per Harrison code (latest reading from any of its
-- web addresses), not one per address. Harrison renamed some product pages; the old addresses
-- (from earlier matches) still redirect, so a code could have two lines, one frozen on an old
-- reading. Rows without a Harrison code are still keyed by address. Applied 23 Nov 2026.
-- Same day (data fix, not repeated here): 66 Harrison rows in competitor_matches on 44 renamed
-- pages were pointed at the current page address; the old one kept in previous_url.

create or replace view public.harrison_stock_latest with (security_invoker = true) as
with ok as (
  select h.*, coalesce(h.harrison_sku, h.competitor_url) as k,
         row_number() over (partition by coalesce(h.harrison_sku, h.competitor_url) order by h.scraped_at desc, h.id desc) as rn
  from public.harrison_stock h
  where h.stock_status <> 'error'
), agg as (
  select k, max(scraped_at) filter (where stock_status = 'in_stock' and rn > 1) as last_in_before
  from ok group by k
), oos as (
  select o.k, min(o.scraped_at) as oos_since
  from ok o join agg a using (k)
  where o.stock_status = 'out_of_stock' and o.scraped_at > coalesce(a.last_in_before, '-infinity'::timestamptz)
  group by o.k
), prev as (
  select distinct on (k) k, stock_level, scraped_at
  from ok where rn > 1 and stock_level is not null
  order by k, scraped_at desc
)
select l.competitor_url, l.product_name, l.harrison_sku, l.ukpos_skus, l.price_ex_vat, l.price_inc_vat,
       l.stock_level, l.stock_status, l.scraped_at, l.kind, l.due_in, l.next_due_date, l.next_due_qty,
       l.due_scope, l.error_message,
       p.stock_level as prev_stock_level,
       p.scraped_at  as prev_scraped_at,
       case when l.stock_status = 'out_of_stock' then o.oos_since else null::timestamptz end as oos_since,
       (a.last_in_before is not null) as seen_in_stock_before
from ok l
join agg a on a.k = l.k
left join oos o on o.k = l.k
left join prev p on p.k = l.k
where l.rn = 1;
