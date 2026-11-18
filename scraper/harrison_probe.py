"""
scraper/harrison_probe.py
─────────────────────────
Diagnostic only — writes nothing to the database.

Run with the harrison_stock job and harrison_mode=probe. Picks Harrison product pages that
have no stock number (usually products with options) plus any whose name mentions
cutting/custom sizes, and logs for each:
  • every option field on the add-to-cart form (BigCommerce data-product-attribute type,
    label, required, values)
  • for each value of each option, what Harrison's own page returns from
    /remote/v1/product-attributes/<product_id>: sku, price ex/inc VAT, stock, instock,
    purchasable
This shows which options are true variants (own SKU / price / stock) and which are
add-ons such as "cut to size" that don't give a price or stock count.
"""

import asyncio
import json
import logging
import os
import random

from playwright.async_api import async_playwright
from supabase import create_client

from common import launch_browser, new_stealth_context

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("harrison_probe")

SAMPLE = int(os.environ.get("PROBE_SAMPLE") or "25")

FORM_JS = r"""
() => {
  const form = document.querySelector('form[data-cart-item-add]') ||
               document.querySelector('form[action*="cart.php"]');
  if (!form) return { form: false };
  const pid = (form.querySelector('input[name="product_id"]') || {}).value || null;
  const fields = [...form.querySelectorAll('[data-product-attribute]')].map(f => {
    const lab = f.querySelector('.form-label, label');
    const inputs = [...f.querySelectorAll('input, select, textarea')]
      .filter(i => i.type !== 'hidden')
      .map(i => {
        const o = { tag: i.tagName.toLowerCase(), type: i.type || null, name: i.name, required: !!i.required };
        if (i.tagName === 'SELECT') o.values = [...i.options].map(x => ({ v: x.value, t: x.textContent.trim() }));
        if (i.type === 'radio' || i.type === 'checkbox') {
          const l = form.querySelector(`label[for="${i.id}"]`);
          o.values = [{ v: i.value, t: (l ? (l.getAttribute('title') || l.textContent) : '').trim() }];
        }
        return o;
      });
    return { kind: f.getAttribute('data-product-attribute'),
             label: lab ? lab.textContent.replace(/\s+/g, ' ').trim() : '', inputs };
  });
  return { form: true, product_id: pid, fields, pa: (window.BCData || {}).product_attributes || null };
}
"""

ATTR_JS = r"""
async ({ pid, name, value }) => {
  const form = document.querySelector('form[data-cart-item-add]') ||
               document.querySelector('form[action*="cart.php"]');
  const fd = new FormData(form);
  if (name) fd.set(name, value);
  fd.set('action', 'add');
  fd.set('product_id', pid);
  try {
    const r = await fetch(`/remote/v1/product-attributes/${pid}`, {
      method: 'POST', body: fd, credentials: 'same-origin',
      headers: { 'X-Requested-With': 'XMLHttpRequest', 'stencil-config': '{}' },
    });
    const j = await r.json();
    const d = j.data || {};
    const p = d.price || {};
    return { http: r.status, sku: d.sku ?? null, stock: d.stock ?? null, instock: d.instock ?? null,
             purchasable: d.purchasable ?? null, stock_message: d.stock_message ?? null,
             ex: p.without_tax ? p.without_tax.value : null, inc: p.with_tax ? p.with_tax.value : null,
             variantId: d.variantId ?? d.v3_variant_id ?? null };
  } catch (e) { return { error: String(e) }; }
}
"""


def pick_urls(sb) -> list[str]:
    rows = (sb.table("harrison_stock_latest").select("competitor_url,product_name,stock_level")
              .range(0, 1999).execute().data or [])
    parents = [r for r in rows if "?sku=" not in r["competitor_url"]]
    cut = [r["competitor_url"] for r in parents
           if any(w in (r["product_name"] or "").lower() for w in ("cut", "custom", "bespoke", "made to"))]
    no_level = [r["competitor_url"] for r in parents if r["stock_level"] is None and r["competitor_url"] not in cut]
    random.seed(7)
    random.shuffle(no_level)
    return (cut[:10] + no_level)[:SAMPLE]


async def main():
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    urls = pick_urls(sb)
    log.info(f"PROBE {len(urls)} Harrison product pages")
    async with async_playwright() as pw:
        browser = await launch_browser(pw)
        ctx = await new_stealth_context(browser)
        for n, url in enumerate(urls, 1):
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                await page.wait_for_timeout(2500)
                info = await page.evaluate(FORM_JS)
                out = {"n": n, "url": url, "product_id": info.get("product_id"),
                       "default": {k: (info.get("pa") or {}).get(k) for k in ("sku", "stock", "instock", "purchasable")},
                       "fields": []}
                pid = info.get("product_id")
                for f in info.get("fields", []):
                    entry = {"kind": f["kind"], "label": f["label"][:80], "inputs": []}
                    for inp in f["inputs"]:
                        ie = {k: inp.get(k) for k in ("tag", "type", "name", "required")}
                        vals = [v for v in (inp.get("values") or []) if v["v"]][:12]
                        ie["values"] = []
                        for v in vals:
                            res = await page.evaluate(ATTR_JS, {"pid": pid, "name": inp["name"], "value": v["v"]}) if pid else {}
                            ie["values"].append({"value": v["t"][:60], "result": res})
                        entry["inputs"].append(ie)
                    out["fields"].append(entry)
                log.info("PROBE_JSON " + json.dumps(out, ensure_ascii=False))
            except Exception as e:
                log.info("PROBE_JSON " + json.dumps({"n": n, "url": url, "error": str(e)[:200]}))
            finally:
                await page.close()
            await asyncio.sleep(random.uniform(1.5, 3))
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
