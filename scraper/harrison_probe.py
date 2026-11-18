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
    "/adhesive-hook-and-loop-tape-for-card-25m-roll/",
    "/84-x-184mm-hinged-front-loading-shelf-barkers-pack-of-100/",
]

STATE_JS = r"""
() => {
  const q = s => document.querySelector(s);
  const t = s => ((q(s) || {}).textContent || '').replace(/\s+/g, ' ').trim();
  const prices = [...document.querySelectorAll('.productView [data-product-price-without-tax], .productView .price--withoutTax')]
                  .map(e => e.textContent.replace(/\s+/g, ' ').trim()).slice(0, 4);
  // Stock & Lead Times: climb from the heading until the container holds more than the heading
  let lead = null;
  const h = [...document.querySelectorAll('h1,h2,h3,h4,h5,strong,span,div')].find(e => e.children.length <= 2 && /Stock\s*&\s*Lead\s*Times/i.test(e.textContent));
  if (h) { let el = h; for (let i = 0; i < 5 && el.parentElement; i++) { el = el.parentElement; if (el.innerText.length > h.innerText.length + 20) break; } lead = el; }
  const sel = q('form[data-cart-item-add] [data-product-attribute] select, form[data-cart-item-add] [data-product-attribute] input[type=radio]');
  const scripts = [...document.scripts].map(sc => sc.textContent).filter(x => /variant|stock_level|inventory/i.test(x) && x.length < 200000)
                   .map(x => x.slice(0, 400));
  const tables = [...document.querySelectorAll('.productView table, [class*="variant"] table, table[class*="variant"], table')]
                   .map(tb => tb.innerText.slice(0, 700)).slice(0, 3);
  return {
    sku: t('[data-product-sku]'), stock: t('[data-product-stock]'), prices,
    lead_text: lead ? lead.innerText.slice(0, 1500) : null,
    lead_html: lead ? lead.outerHTML.replace(/\s+/g, ' ').slice(0, 3000) : null,
    bc: (window.BCData || {}).product_attributes || null,
    option_visible: sel ? { display: getComputedStyle(sel).display, vis: getComputedStyle(sel).visibility, w: sel.offsetWidth, h: sel.offsetHeight } : null,
    scripts, tables,
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
                        await page.evaluate("""(o) => {
                          const form = document.querySelector('form[data-cart-item-add]');
                          if (o.how === 'select') {
                            const el = form.querySelector(`select[name="${o.name}"]`);
                            el.value = o.value; el.dispatchEvent(new Event('change', { bubbles: true }));
                          } else {
                            const el = form.querySelector(`#${CSS.escape(o.id)}`);
                            el.checked = true; el.dispatchEvent(new Event('change', { bubbles: true }));
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                          }
                        }""", o)
                    except Exception as e:
                        log.info("PROBE_JSON " + json.dumps({"url": url, "stage": "pick-fail", "opt": o, "err": str(e)[:200]}))
                        continue
                    await page.wait_for_timeout(2500)
                    st = await page.evaluate(STATE_JS)
                    st.pop("lead_html", None); st.pop("scripts", None); st.pop("tables", None)
                    log.info("PROBE_JSON " + json.dumps({"url": url, "stage": "pick", "opt": {k: o[k] for k in ("label", "text", "how")},
                                                          "state": st, "responses": responses[:]}, ensure_ascii=False))
            except Exception as e:
                log.info("PROBE_JSON " + json.dumps({"url": url, "error": str(e)[:300]}))
            finally:
                await page.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
