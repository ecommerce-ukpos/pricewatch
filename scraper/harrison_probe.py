"""
scraper/harrison_probe.py
─────────────────────────
Diagnostic only — writes nothing to the database.
Run with the harrison_stock job and harrison_mode=probe.

For a few Harrison products: reads the ProductGroup JSON-LD (hasVariant list) and the
spec table "Product Code" row, then opens parent?sku=<code> for each variant and logs
what Harrison serves for that variant (BCData sku/price/stock/instock) and its
Stock & Lead Times lines (Ships today / Due in dates).
"""

import asyncio
import json
import logging

from playwright.async_api import async_playwright

from common import launch_browser, new_stealth_context

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("harrison_probe")

BASE = "https://www.harrisonproducts.com"
URLS = [
    "/adhesive-narrow-base-sign-grippers/",
    "/adhesive-hook-and-loop-tape-for-card-25m-roll/",
    "/products/adhesive-hook-and-loop-coins-for-card/",
    "/84-x-184mm-hinged-front-loading-shelf-barkers-pack-of-100/",
]

PAGE_JS = r"""
() => {
  const groups = [];
  document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
    try {
      const d = JSON.parse(s.textContent);
      for (const it of (Array.isArray(d) ? d : (d['@graph'] || [d]))) {
        if (it['@type'] === 'ProductGroup' || it.hasVariant) {
          groups.push({ name: it.name, sku: it.sku || it.mpn, variesBy: it.variesBy,
            variants: (it.hasVariant || []).map(v => {
              const o = Array.isArray(v.offers) ? v.offers[0] : (v.offers || {});
              return { sku: v.sku || v.mpn, name: v.name, price: o.price, availability: o.availability,
                       url: o.url || v.url, extra: Object.keys(v).join(',') };
            }) });
        }
      }
    } catch (e) {}
  });
  const specCodes = [];
  document.querySelectorAll('table tr').forEach(tr => {
    const cells = [...tr.children].map(c => c.textContent.trim());
    if (/^product code$/i.test(cells[0] || '')) specCodes.push(...cells.slice(1));
  });
  // Stock & Lead Times lines
  const txt = (document.querySelector('.productView') || document.body).innerText;
  const i = txt.search(/Stock\s*&\s*Lead\s*Times/i);
  const lead = i >= 0 ? txt.slice(i, i + 600).split('\n').map(s => s.trim()).filter(Boolean)
                          .filter(s => !/^Need (more|it now)\?|^Contact us|^Close/i.test(s)).slice(0, 12) : null;
  const pa = (window.BCData || {}).product_attributes || {};
  const p = pa.price || {};
  return { bc: { sku: pa.sku, stock: pa.stock, instock: pa.instock, purchasable: pa.purchasable,
                 ex: p.without_tax ? p.without_tax.value : null, inc: p.with_tax ? p.with_tax.value : null },
           groups, specCodes, lead };
}
"""


async def main():
    async with async_playwright() as pw:
        browser = await launch_browser(pw)
        ctx = await new_stealth_context(browser)
        page = await ctx.new_page()
        for path in URLS:
            url = BASE + path
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                await page.wait_for_timeout(2000)
                d = await page.evaluate(PAGE_JS)
                log.info("PROBE_JSON " + json.dumps({"url": url, "stage": "parent", **d}, ensure_ascii=False))
                codes = []
                for g in d["groups"]:
                    codes += [v["sku"] for v in g["variants"] if v.get("sku")]
                if not codes:
                    codes = d["specCodes"]
                for code in list(dict.fromkeys(codes))[:10]:
                    vurl = f"{url}?sku={code}"
                    await page.goto(vurl, wait_until="domcontentloaded", timeout=45_000)
                    await page.wait_for_timeout(2000)
                    v = await page.evaluate(PAGE_JS)
                    log.info("PROBE_JSON " + json.dumps({"url": vurl, "stage": "variant", "want": code,
                                                          "bc": v["bc"], "lead": v["lead"]}, ensure_ascii=False))
                    await asyncio.sleep(1.5)
            except Exception as e:
                log.info("PROBE_JSON " + json.dumps({"url": url, "error": str(e)[:300]}))
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
