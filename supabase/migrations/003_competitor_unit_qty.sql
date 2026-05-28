-- PriceWatch Pro — Migration 003
-- Adds competitor_unit_qty to price_snapshots so the dashboard can show a
-- true per-unit comparison (e.g. competitor sells singles, we sell packs of 100).
-- The scraper already derives this value via extract_pack_qty() at scrape time;
-- this column simply persists it so the UI can display both sides' per-unit price.

alter table price_snapshots
  add column if not exists competitor_unit_qty int;

-- Recreate latest_snapshots so the new column is exposed to the frontend.
-- NOTE: we DROP then CREATE (rather than CREATE OR REPLACE) because adding
-- competitor_unit_qty to ps.* plus our_unit_qty changes the view's column
-- ordering, and Postgres won't let CREATE OR REPLACE reorder existing columns.
drop view if exists latest_snapshots;

create view latest_snapshots as
select distinct on (sku_id, competitor_id)
  ps.*,
  s.short_title,
  s.price_ex_vat as our_price,
  s.product_url  as our_url,
  s.unit_qty     as our_unit_qty,      -- NEW: our pack size, for per-unit display
  c.name         as competitor_name,
  c.domain       as competitor_domain,
  c.vat_status   as competitor_vat_default
from price_snapshots ps
join skus s on s.sku_id = ps.sku_id
join competitors c on c.id = ps.competitor_id
order by sku_id, competitor_id, scraped_at desc;