"""
scraper/indigo_displays.py
──────────────────────────
Indigo Displays (indigodisplays.co.uk) — Magento 2 simple products with a CUSTOM OPTION
dropdown (e.g. Swift Display Stand: "A4" +£0, "A3" +£10). The chosen option is not in the
URL and Indigo's site cannot preselect it, so the price shown on load is always the base.

We store the option on the match URL as a fragment, in the same style as ukpos.com's
"#sku:" links:

    https://www.indigodisplays.co.uk/swift-display-stand.html#option:a3
    https://www.indigodisplays.co.uk/swift-display-stand.html#option:18      (option value id)

The fragment is never sent to Indigo's server. This module reads, from the page HTML:
  - priceConfig.prices.finalPrice  → base single-unit price (tier prices ignored)
  - priceOptions.optionConfig      → each option value's added price + name
and returns base + chosen option.

Rules (silently wrong data is worse than no data):
  - URL has #option:… that matches exactly one option   → base + add-on
  - URL has #option:… that matches none / several        → NO price, error explains
  - no #option, but the options change the price         → NO price, error asks for #option
  - no #option and options don't change the price (or no options) → base price

Public API
──────────
  option_from_url(url) -> str | None
  scrape_indigo_page(html, url) -> IndigoResult
"""

import json
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import unquote

INDIGO_DOMAIN = "indigodisplays.co.uk"

_OPTION_FRAGMENT = re.compile(r"#option:([^#&]+)", re.I)


@dataclass
class IndigoOption:
    option_id: str      # custom option id, e.g. "29" (the dropdown)
    value_id: str       # option value id, e.g. "18"
    name: str           # e.g. "A3"
    add_on: float       # price added to the base, e.g. 10.0


@dataclass
class IndigoResult:
    success: bool = False
    price: Optional[float] = None          # base + add-on (what the customer pays for one)
    base_price: Optional[float] = None
    add_on: float = 0.0
    option_name: Optional[str] = None
    options: list = field(default_factory=list)
    error: Optional[str] = None


def option_from_url(url: str) -> Optional[str]:
    m = _OPTION_FRAGMENT.search(url or "")
    return unquote(m.group(1)).strip() if m else None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _json_after(html: str, key: str) -> Optional[dict]:
    """Return the JSON object that follows  "key":  in the page's x-magento-init scripts."""
    i = html.find(f'"{key}"')
    if i < 0:
        return None
    start = html.find("{", i)
    if start < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(html[start:])
        return obj
    except ValueError:
        return None


def _base_price(html: str) -> Optional[float]:
    cfg = _json_after(html, "priceConfig")
    try:
        p = float(cfg["prices"]["finalPrice"]["amount"])
        return p if p > 0 else None
    except (TypeError, KeyError, ValueError):
        pass
    m = re.search(r'itemprop="price"\s+content="([\d.]+)"', html)
    return float(m.group(1)) if m else None


def _options(html: str) -> list:
    cfg = _json_after(html, "optionConfig") or {}
    out = []
    for option_id, values in cfg.items():
        if not isinstance(values, dict):
            continue
        for value_id, v in values.items():
            try:
                add_on = float(v["prices"]["finalPrice"]["amount"])
            except (TypeError, KeyError, ValueError):
                continue
            out.append(IndigoOption(str(option_id), str(value_id), (v.get("name") or "").strip(), add_on))
    return out


def scrape_indigo_page(html: str, url: str) -> IndigoResult:
    res = IndigoResult()
    base = _base_price(html)
    if not base:
        res.error = "Indigo: no base price found on page"
        return res
    res.base_price = base
    opts = _options(html)
    res.options = opts
    wanted = option_from_url(url)

    if wanted:
        if wanted.isdigit():
            hits = [o for o in opts if o.value_id == wanted]
        else:
            hits = [o for o in opts if _norm(o.name) == _norm(wanted)]
        if len(hits) != 1:
            names = ", ".join(f"{o.name} (#{o.value_id})" for o in opts) or "none"
            res.error = (f"Indigo: option '{wanted}' matched {len(hits)} options — page has: {names}. "
                         "Fix the #option: part of the match URL.")
            return res
        o = hits[0]
        res.add_on, res.option_name = o.add_on, o.name
        res.price = round(base + o.add_on, 2)
        res.success = True
        return res

    if any(abs(o.add_on) > 0.004 for o in opts):
        names = ", ".join(f"{o.name} +£{o.add_on:.2f}" for o in opts)
        res.error = (f"Indigo: price depends on an option ({names}) — add #option:<name> to the "
                     "match URL, e.g. #option:a3")
        return res

    res.price = base
    res.success = True
    return res
