"""
scraper/harrison_probe.py
─────────────────────────
Diagnostic only — writes nothing to the database.
Run with the harrison_stock job and harrison_mode=probe.

For a fixed sample of Harrison pages it selects each option the way a customer would
(click the radio label / choose the select value), lets Harrison's own theme request the
variant data, and logs:
  • the /remote/v1/product-attributes response the theme received (raw, trimmed)
  • the price, product code and stock the page shows after the change
  • the "Stock & Lead Times" section (text + trimmed HTML) so due-in dates can be mapped
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
    "/products/adhesive-hook-and-loop-coins-for-card/",
    "/pre-cut-steel-ball-chain-connector-2-4mm-nickel/",
    "/adhesive-hook-and-loop-tape-for-card-25m-roll/",
    "/84-x-184mm-hinged-front-loading-shelf-barkers-pack-of-100/",
    "/30mm-data-strips-with-9mm-red-liner-tape-cut-to-order/",
]

STATE_JS = r"""
() => {
  const q = s => document.querySelector(s);
  const t = s => ((q(s) || {}).textContent || '').replace(/\s+/g, ' ').trim();
  const prices = [...document.querySelectorAll('.productView [data-product-price-without-tax], .productView [data-product-price-with-tax], .productView .price--withoutTax, .productView .price--withTax')]
                  .map(e => e.className + ' = ' + e.textContent.replace(/\s+/g, ' ').trim()).slice(0, 6);
  // Stock & Lead Times block
  let block = null;
  for (const el of document.querySelectorAll('.productView *')) {
    if (el.children.length < 60 && /Stock\s*&\s*Lead\s*Times/i.test(el.textContent) && el.textContent.length < 3000) block = el;
  }
  return {
    sku: t('[data-product-sku]'), stock: t('[data-product-stock]'), prices,
    lead_text: block ? block.innerText.slice(0, 1200) : null,
    lead_html: block ? block.outerHTML.replace(/\s+/g, ' ').slice(0, 2500) : null,
    bc: (window.BCData || {}).product_attributes || null,
    has_form: !!q('form[data-cart-item-add]'),
    body_head: q('form[data-cart-item-add]') ? null : (document.body.innerText || '').slice(0, 600),
  };
}
"""

OPTIONS_JS = r"""
() => {
  const form = document.querySelector('form[data-cart-item-add]');
  if (!form) return [];
  const out = [];
  form.querySelectorAll('[data-product-attribute]').forEach((f, fi) => {
    const kind = f.getAttribute('data-product-attribute');
    const label = ((f.querySelector('.form-label') || {}).textContent || '').replace(/\s+/g, ' ').trim();
    const sel = f.querySelector('select');
    if (sel) {
      [...sel.options].filter(o => o.value).forEach(o => out.push({ fi, kind, label, how: 'select', name: sel.name, value: o.value, text: o.textContent.trim() }));
    }
    f.querySelectorAll('input[type=radio]').forEach(i => {
      const l = form.querySelector(`label[for="${i.id}"]`);
      out.push({ fi, kind, label, how: 'radio', id: i.id, name: i.name, value: i.value, text: (l ? (l.getAttribute('title') || l.textContent) : '').trim() });
    });
    f.querySelectorAll('input[type=text], input[type=number], textarea, input[type=checkbox]').forEach(i =>
      out.push({ fi, kind, label, how: 'other', name: i.name, type: i.type, required: i.required }));
  });
  return out;
}
"""


async def main():
    async with async_playwright() as pw:
        browser = await launch_browser(pw)
        ctx = await new_stealth_context(browser)
        for path in URLS:
            url = BASE + path
            page = await ctx.new_page()
            responses = []

            async def on_resp(resp):
                if "/remote/v1/product-attributes/" in resp.url:
                    try:
                        body = await resp.text()
                    except Exception as e:
                        body = f"<unreadable {e}>"
                    responses.append({"status": resp.status, "method": resp.request.method,
                                      "post": (resp.request.post_data or "")[:300], "body": body[:900]})
            page.on("response", on_resp)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                await page.wait_for_timeout(3000)
                base = await page.evaluate(STATE_JS)
                log.info("PROBE_JSON " + json.dumps({"url": url, "stage": "load", "state": base,
                                                      "responses": responses[:]}, ensure_ascii=False))
                opts = await page.evaluate(OPTIONS_JS)
                log.info("PROBE_JSON " + json.dumps({"url": url, "stage": "options", "options": opts}, ensure_ascii=False))
                for o in [x for x in opts if x["how"] in ("select", "radio")][:8]:
                    responses.clear()
                    try:
                        if o["how"] == "select":
                            await page.select_option(f'select[name="{o["name"]}"]', o["value"])
                        else:
                            await page.click(f'label[for="{o["id"]}"]', timeout=5000)
                    except Exception as e:
                        log.info("PROBE_JSON " + json.dumps({"url": url, "stage": "pick-fail", "opt": o, "err": str(e)[:200]}))
                        continue
                    await page.wait_for_timeout(2500)
                    st = await page.evaluate(STATE_JS)
                    st.pop("lead_html", None); st.pop("body_head", None)
                    log.info("PROBE_JSON " + json.dumps({"url": url, "stage": "pick", "opt": {k: o[k] for k in ("label", "text", "how")},
                                                          "state": st, "responses": responses[:]}, ensure_ascii=False))
            except Exception as e:
                log.info("PROBE_JSON " + json.dumps({"url": url, "error": str(e)[:300]}))
            finally:
                await page.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
