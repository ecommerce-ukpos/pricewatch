-- PriceWatch Pro — Migration 003 (corrected)
-- Adds competitor_unit_qty to price_snapshots for per-unit price comparison.
-- Must drop worst_differentials first as it depends on latest_snapshots.

alter table price_snapshots
  add column if not exists competitor_unit_qty int;

-- Drop dependents first, then the base view
drop view if exists worst_differentials;
drop view if exists latest_snapshots;

create view latest_snapshots as
select distinct on (sku_id, competitor_id)
  ps.*,
  s.short_title,
  s.price_ex_vat  as our_price,
  s.product_url   as our_url,
  s.unit_qty      as our_unit_qty,
  c.name          as competitor_name,
  c.domain        as competitor_domain,
  c.vat_status    as competitor_vat_default
from price_snapshots ps
join skus s on s.sku_id = ps.sku_id
join competitors c on c.id = ps.competitor_id
order by sku_id, competitor_id, scraped_at desc;

-- Recreate worst_differentials on the new latest_snapshots
create view worst_differentials as
select
  sku_id, short_title, our_price, our_url,
  competitor_name, competitor_domain,
  competitor_price, competitor_vat, competitor_url,
  diff_pct, diff_pct_normalised,
  availability, scraped_at
from latest_snapshots
where availability <> all (array['unavailable','error'])
  and diff_pct is not null
order by diff_pct;
