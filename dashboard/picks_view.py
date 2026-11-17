"""
dashboard/picks_view.py — plain-language stock cards for the dashboard's picks page.

Pure functions (no Streamlit) so the wording is testable: each pick's numbers become
short, everyday sentences ("Cheaper than 85% of similar companies"), and the cards are
rendered as one HTML block that follows the dashboard's light/dark theme.

Company one-liners come from data/company_profiles.json, written by
scripts/fetch_company_profiles.py (yfinance business summaries, trimmed to one
sentence). No AI service is called anywhere.
"""

from __future__ import annotations

import html
import json
import math
import re
from pathlib import Path

PROFILES = Path(__file__).resolve().parent.parent / "data" / "company_profiles.json"

TIER_LABEL = {
    "TIER1_STRONG_BUY": "Top conviction",
    "TIER2_VALUE_BUY": "Value pick",
    "TIER3_MOMENTUM_WATCH": "Momentum watch",
    "TIER5_AVOID": "Ranked low today",   # never "Avoid" on a held name: not a sell signal
}

ACCENT = "#3987e5"          # one validated hue (reference palette, slot 1, dark step)


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


def load_profiles(path: Path = PROFILES) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def short_description(summary: str | None, max_chars: int = 170) -> str | None:
    """First sentence of a business summary, without boilerplate, cut at a word."""
    if not summary:
        return None
    s = " ".join(summary.split())
    first = re.split(r"(?<=[a-z0-9\)])\.\s+(?=[A-Z])", s, maxsplit=1)[0].rstrip(".")
    first = re.sub(r",? (together|along) with its subsidiaries,?", "", first)
    first = re.sub(r",? through its subsidiaries,?", "", first)
    if len(first) > max_chars:
        first = first[:max_chars].rsplit(" ", 1)[0].rstrip(",;") + "…"
    return first + ("" if first.endswith("…") else ".")


def reasons(row: dict) -> tuple[list[str], list[str]]:
    """(why it is on the list, things worth knowing) in everyday language."""
    why, watch = [], []
    v, q, f = _num(row.get("value_score")), _num(row.get("quality_score")), _num(row.get("f_score"))
    roic, mom = _num(row.get("roic")), _num(row.get("momentum_12_1"))
    if v is not None:
        if v >= 80:
            why.append(f"Very cheap: priced lower than {v:.0f}% of similar companies")
        elif v >= 60:
            why.append(f"Cheap: priced lower than {v:.0f}% of similar companies")
        else:
            why.append(f"Not especially cheap (cheaper than {v:.0f}% of peers) - "
                       f"picked mainly for other strengths")
    if f is not None:
        f = int(f)
        if f >= 8:
            why.append(f"Very strong finances: passes {f} of 9 health checks")
        elif f >= 6:
            why.append(f"Healthy finances: passes {f} of 9 health checks")
        else:
            why.append(f"Passes the minimum health bar ({f} of 9 checks)")
    if roic is not None:
        if roic >= 0.15:
            why.append(f"Highly profitable: earns {roic:.0%} a year on the money invested in it")
        elif roic >= 0.05:
            why.append(f"Profitable: earns {roic:.0%} a year on its invested capital")
        else:
            watch.append(f"Thin profits: earns only {roic:.0%} on its invested capital")
    else:
        watch.append("Profitability figure not available")
    if mom is not None:
        if mom >= 1.0:
            why.append(f"Strong price trend: up {mom:.0%} over the past year")
            watch.append("Big run-up already - prices that rise this fast can fall back sharply")
        elif mom >= 0.2:
            why.append(f"Rising price: up {mom:.0%} over the past year")
        elif mom >= 0:
            why.append(f"Price roughly flat to slightly up ({mom:+.0%}) over the past year")
        else:
            watch.append(f"Price has fallen {mom:.0%} over the past year")
    if q is not None and q < 40:
        watch.append(f"Quality score is low ({q:.0f}/100) compared with other picks")
    return why, watch


def card_html(row: dict, profile: dict | None, held: bool) -> str:
    e = html.escape
    t = str(row.get("ticker", ""))
    name = (profile or {}).get("name") or row.get("company_name") or ""
    desc = short_description((profile or {}).get("summary")) or (
        f"{row.get('industry')} company." if row.get("industry") else "No description yet.")
    sector = row.get("sector") or (profile or {}).get("sector") or ""
    tier = TIER_LABEL.get(str(row.get("conviction_tier", "")), "")
    why, watch = reasons(row)

    def bar(label, val, hint):
        v = _num(val)
        w = 0 if v is None else max(0, min(100, v))
        shown = "n/a" if v is None else f"{v:.0f}"
        return (f'<div class="qs-bar" title="{e(hint)}"><span class="qs-bl">{e(label)}</span>'
                f'<span class="qs-track"><span class="qs-fill" style="width:{w:.0f}%"></span>'
                f'</span><span class="qs-bv">{shown}</span></div>')

    badges = ""
    if held:
        badges += '<span class="qs-badge qs-held">In your portfolio</span>'
    if tier:
        badges += f'<span class="qs-badge">{e(tier)}</span>'
    if sector:
        badges += f'<span class="qs-badge qs-muted">{e(str(sector))}</span>'
    f = _num(row.get("f_score"))
    roic, mom = _num(row.get("roic")), _num(row.get("momentum_12_1"))
    facts = " · ".join(x for x in (
        f"Health {int(f)}/9" if f is not None else "",
        f"Return on capital {roic:.0%}" if roic is not None else "",
        f"1-year price {mom:+.0%}" if mom is not None else "") if x)
    url = f"https://finance.yahoo.com/quote/{t}"
    return (
        '<div class="qs-card">'
        f'<div class="qs-head"><a class="qs-tk" href="{e(url)}" target="_blank">{e(t)}</a>'
        f'<span class="qs-name">{e(str(name))}</span></div>'
        f'<div class="qs-badges">{badges}</div>'
        f'<p class="qs-desc">{e(desc)}</p>'
        + bar("Cheapness", row.get("value_score"),
              "How cheap the stock is versus the rest of the universe (100 = cheapest)")
        + bar("Quality", row.get("quality_score"),
              "Profitability, financial health and stable margins (100 = best)")
        + bar("Price trend", row.get("momentum_score"),
              "Price change over the past year, skipping the last month (100 = strongest)")
        + ('<div class="qs-sec">Why it\'s on the list</div><ul>'
           + "".join(f"<li>{e(x)}</li>" for x in why) + "</ul>" if why else "")
        + ('<div class="qs-sec qs-warn">Worth knowing</div><ul>'
           + "".join(f"<li>{e(x)}</li>" for x in watch) + "</ul>" if watch else "")
        + f'<div class="qs-facts">{e(facts)}</div></div>')


CSS = f"""
<style>
.qs-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(320px,1fr));
           gap:16px; margin:8px 0 24px; }}
.qs-card {{ border:1px solid rgba(128,128,128,.25); border-radius:14px; padding:16px 18px;
           background:rgba(128,128,128,.06); }}
.qs-head {{ display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; }}
.qs-tk, .qs-tk:visited {{ font-size:1.35rem; font-weight:700; text-decoration:none !important;
          color:inherit !important; }}
.qs-tk:hover {{ color:{ACCENT} !important; }}
.qs-name {{ opacity:.75; font-size:.95rem; }}
.qs-badges {{ margin:6px 0 4px; display:flex; gap:6px; flex-wrap:wrap; }}
.qs-badge {{ font-size:.72rem; padding:2px 8px; border-radius:999px;
            border:1px solid rgba(128,128,128,.35); }}
.qs-held {{ background:{ACCENT}; border-color:{ACCENT}; color:#fff; font-weight:600; }}
.qs-muted {{ opacity:.8; }}
.qs-desc {{ font-size:.9rem; opacity:.85; margin:8px 0 12px; line-height:1.4; }}
.qs-bar {{ display:grid; grid-template-columns:86px 1fr 34px; align-items:center;
          gap:8px; font-size:.8rem; margin:4px 0; }}
.qs-bl {{ opacity:.8; }}
.qs-bv {{ text-align:right; font-variant-numeric:tabular-nums; }}
.qs-track {{ height:6px; border-radius:4px; background:rgba(128,128,128,.22); overflow:hidden; }}
.qs-fill {{ display:block; height:100%; border-radius:4px; background:{ACCENT}; }}
.qs-sec {{ font-size:.75rem; text-transform:uppercase; letter-spacing:.04em; opacity:.7;
          margin:12px 0 2px; }}
.qs-card ul {{ margin:0 0 0 18px; padding:0; font-size:.86rem; line-height:1.45; }}
.qs-facts {{ margin-top:10px; font-size:.75rem; opacity:.65; }}
.qs-steps {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:10px;
            margin:4px 0 8px; }}
.qs-step {{ border:1px solid rgba(128,128,128,.25); border-radius:12px; padding:10px 12px;
           font-size:.85rem; line-height:1.35; }}
.qs-step b {{ display:block; margin-bottom:2px; }}
.qs-step .n {{ color:{ACCENT}; font-weight:700; margin-right:4px; }}
</style>
"""

STEPS = [
    ("Health check", "Companies with weak finances (fewer than 5 of 9 health checks), "
                     "low returns or heavy debt are removed first."),
    ("Score", "Every remaining company gets scores for cheapness, quality, price trend "
              "and a few other signals."),
    ("Rank", "Two scoring methods rank the companies; picks are where they agree."),
    ("Risk limits", "The portfolio keeps any sector under 30%, any stock under 15%, and "
                    "only trades shares that are easy to buy and sell."),
]


def steps_html() -> str:
    e = html.escape
    return '<div class="qs-steps">' + "".join(
        f'<div class="qs-step"><b><span class="n">{i}</span>{e(t)}</b>{e(d)}</div>'
        for i, (t, d) in enumerate(STEPS, 1)) + "</div>"


def cards_html(rows: list[dict], profiles: dict, held: set[str]) -> str:
    return '<div class="qs-grid">' + "".join(
        card_html(r, profiles.get(str(r.get("ticker"))), str(r.get("ticker")) in held)
        for r in rows) + "</div>"
