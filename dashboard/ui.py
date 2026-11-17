"""
dashboard/ui.py — one look for every dashboard page.

Colours come from a validated palette: the dataviz validator passes them for
colour-blind separation, lightness, chroma and >= 3:1 contrast on BOTH Streamlit
backgrounds (dark #0e1117 and light #ffffff), so one set serves either theme. Status
always pairs a colour with an icon AND a word, so meaning never rests on colour alone.

Charts are built here, not on each page, so they share one quiet style: transparent
background, hairline grid, no toolbar, no animation; one hue for a single measure;
series colours in fixed slot order (never by rank, never cycled past three); values
in ink next to a coloured mark, never in the series colour; reference lines as solid
muted hairlines with a label, never red (red is reserved for problems).
"""

from __future__ import annotations

import html
import re

# Categorical slots 1-3, validated together all-pairs: blue, orange, aqua.
SERIES = ["#3987e5", "#d95926", "#199e70"]
GAIN, LOSS = "#3987e5", "#e66767"          # diverging pair: up / down (validated together)
# One colour per thing, on every chart. "comparison" and "hedged" never share a chart.
PORTFOLIO, COMPARISON, HEDGED, SP500 = SERIES[0], SERIES[1], SERIES[1], SERIES[2]
MUTED = "#898781"
GRID = "rgba(128,128,128,0.16)"
AXIS = "rgba(128,128,128,0.35)"

# state -> (colour, icon, word). Fixed status palette, never reused for series.
STATUS = {
    "good": ("#0ca30c", "✓", "OK"),
    "warning": ("#fab219", "!", "Check"),
    "critical": ("#d03b3b", "✕", "Problem"),
    "info": (MUTED, "•", "Info"),
}

CSS = """
<style>
.block-container { padding-top: 2.2rem; max-width: 1240px; }
.ui-head h1 { font-size: 2rem; font-weight: 700; margin: 0 0 .15rem; letter-spacing: -.01em; }
.ui-head p { opacity: .72; margin: 0 0 1.2rem; font-size: 1rem; max-width: 760px; }
.ui-sec { font-size: .78rem; text-transform: uppercase; letter-spacing: .06em; opacity: .65;
          margin: 1.6rem 0 .5rem; font-weight: 600; }
.ui-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
           gap: 14px; margin: .3rem 0 .6rem; }
.ui-card { border: 1px solid rgba(128,128,128,.22); border-radius: 14px; padding: 14px 16px;
           background: rgba(128,128,128,.05); }
.ui-card .lbl { font-size: .78rem; opacity: .7; margin-bottom: 6px; }
.ui-card .val { font-size: 1.45rem; font-weight: 650; line-height: 1.15; }
.ui-card .det { font-size: .82rem; opacity: .72; margin-top: 6px; line-height: 1.35; }
.ui-pill { display: inline-flex; align-items: center; gap: 6px; font-size: .78rem;
           font-weight: 600; padding: 2px 9px 2px 3px; border-radius: 999px;
           border: 1px solid rgba(128,128,128,.3); margin-bottom: 8px; }
.ui-pill .ic { width: 18px; height: 18px; border-radius: 999px; display: inline-grid;
               place-items: center; font-size: .72rem; color: #111; font-weight: 800; }
.ui-headline { font-size: 1.05rem; font-weight: 600; margin: .1rem 0 .9rem; line-height: 1.5; }
.ui-headline .ui-pill { margin: 0 8px 0 0; vertical-align: 1px; }
.ui-note { border-left: 3px solid rgba(128,128,128,.45); padding: 6px 12px; margin: .4rem 0 1rem;
           font-size: .9rem; line-height: 1.5; }
.ui-note .ui-pill { margin: 0 8px 0 0; vertical-align: 1px; }
.ui-note code { font-size: .82rem; }
.ui-row { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; padding: 7px 0;
          border-bottom: 1px solid rgba(128,128,128,.15); font-size: .92rem; }
.ui-row .ui-pill { margin: 0; }
.ui-row .det { opacity: .72; font-size: .86rem; }
.ui-kv { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
.ui-kv div { padding: 6px 0; }
.ui-kv .k { font-size: .78rem; opacity: .7; }
.ui-kv .v { font-size: 1.25rem; font-weight: 650; }
</style>
"""


def css() -> str:
    return CSS


def header_html(title: str, purpose: str) -> str:
    return (f'<div class="ui-head"><h1>{html.escape(title)}</h1>'
            f'<p>{inline_md(purpose)}</p></div>')


def section_html(title: str) -> str:
    return f'<div class="ui-sec">{html.escape(title)}</div>'


def inline_md(text: str) -> str:
    """Escape, then allow the three inline marks the pages use: **bold**, *italic*,
    `code`. Nothing else is interpreted, so text can never inject HTML."""
    out = html.escape(text, quote=False)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"(?<![\w*])\*([^*\s][^*]*?)\*(?![\w*])", r"<i>\1</i>", out)
    return out


def note_html(text: str, state: str | None = None, word: str | None = None) -> str:
    """A calm notice. Neutral by default (replaces red boxes that are not errors); with
    a state it leads with the status pill (icon + word) and a matching edge."""
    if state is None:
        return f'<div class="ui-note">{inline_md(text)}</div>'
    colour = STATUS.get(state, STATUS["info"])[0]
    return (f'<div class="ui-note" style="border-left-color:{colour}">'
            f'{pill_html(state, word)}{inline_md(text)}</div>')


def pill_html(state: str, word: str | None = None) -> str:
    colour, icon, default = STATUS.get(state, STATUS["info"])
    return (f'<span class="ui-pill"><span class="ic" style="background:{colour}">{icon}'
            f'</span>{html.escape(word or default)}</span>')


def status_card_html(label: str, state: str, value: str, detail: str = "",
                     word: str | None = None) -> str:
    e = html.escape
    return (f'<div class="ui-card"><div class="lbl">{e(label)}</div>{pill_html(state, word)}'
            f'<div class="val">{e(value)}</div>'
            + (f'<div class="det">{e(detail)}</div>' if detail else "") + "</div>")


def headline_html(state: str, word: str, text: str) -> str:
    """One status sentence: pill (icon + word) followed by plain text."""
    return f'<div class="ui-headline">{pill_html(state, word)}{html.escape(text)}</div>'


def check_row_html(ok: bool, label: str, detail: str) -> str:
    """One pass/fail line: pill (icon + word), label, detail."""
    return (f'<div class="ui-row">{pill_html("good" if ok else "critical", "OK" if ok else "Fail")}'
            f'<b>{html.escape(label)}</b><span class="det">{html.escape(detail)}</span></div>')


def status_cell(status: str) -> str:
    """A table cell that never relies on colour: icon + word."""
    icon = {"PASS": "✓", "WARN": "!", "FAIL": "✕", "SKIP": "–"}.get(str(status).upper(), "•")
    return f"{icon} {status}"


def cards_html(cards: list[str]) -> str:
    return '<div class="ui-grid">' + "".join(cards) + "</div>"


def kv_html(pairs: list[tuple[str, str]]) -> str:
    e = html.escape
    return '<div class="ui-kv">' + "".join(
        f'<div><div class="k">{e(k)}</div><div class="v">{e(v)}</div></div>'
        for k, v in pairs) + "</div>"


def style_fig(fig, height: int = 320, y_pct: bool = False):
    """The one chart style: quiet, transparent, hairline grid, legend on top."""
    fig.update_layout(
        height=height, margin=dict(l=8, r=8, t=36, b=8),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(size=13), hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0,
                    title=None, bgcolor="rgba(0,0,0,0)"),
        hoverlabel=dict(font_size=12), transition_duration=0)
    fig.update_xaxes(showgrid=False, linecolor=AXIS, ticks="", title=None)
    fig.update_yaxes(gridcolor=GRID, zeroline=True, zerolinecolor=AXIS, title=None,
                     ticksuffix="%" if y_pct else "")
    return fig


def date_axis(fig, dates) -> None:
    """Dates only on the x-axis: no 00:00/06:00 ticks on short series, years on long."""
    d = list(dates)
    if not d:
        return
    span_days = (max(d) - min(d)).days
    if span_days <= 14:
        fig.update_xaxes(tickformat="%d %b", dtick=86_400_000)        # one tick per day
    elif span_days <= 400:
        fig.update_xaxes(tickformat="%d %b")
    else:
        fig.update_xaxes(tickformat="%b %Y")


def _rounded_bars_supported() -> bool:
    """Bar `cornerradius` arrived in plotly 5.19. On an older plotly (requirements.txt
    pinned 5.18 until 2026-11-10; it now pins the machine's 6.6.0) passing it raises
    ValueError and the whole chart fails. Round only when it works."""
    try:
        import plotly.graph_objects as go
        go.bar.Marker(cornerradius=4)
        return True
    except (ImportError, ValueError):
        return False


ROUNDED_BARS = _rounded_bars_supported()


def bar_marker(color) -> dict:
    """Bar marker in the one style: rounded ends where the installed plotly allows."""
    return {"color": color, **({"cornerradius": 4} if ROUNDED_BARS else {})}


CHART_CONFIG = {"displayModeBar": False, "staticPlot": False}


# ── chart builders (pass the plotly.graph_objects module as `go`) ───────────────

def end_label_shifts(values: list[float], span: float, min_frac: float = 0.08,
                     px: int = 15) -> list[int]:
    """Upward pixel nudges so end labels of lines that finish close together do not
    sit on top of each other. Lines ending within `min_frac` of the y-span apart are
    stacked: the lower label stays, each one above moves up a line."""
    n = len(values)
    if n < 2 or not span:
        return [0] * n
    gap = min_frac * span
    shifts, prev = [0] * n, None
    for i in sorted(range(n), key=lambda k: values[k]):
        want = values[i] if prev is None else max(values[i], prev + gap)
        shifts[i] = round((want - values[i]) / gap * px)
        prev = want
    return shifts


def ref_line(fig, value: float, label: str, *, axis: str = "x") -> None:
    """A threshold (a cap, a 50% line, zero): solid muted hairline, labelled in ink."""
    if axis == "x":
        fig.add_vline(x=value, line_width=1, line_color=MUTED)
        fig.add_annotation(x=value, y=1, xref="x", yref="paper", text=label,
                           showarrow=False, xanchor="left", yanchor="bottom", xshift=4,
                           font=dict(size=11))
    else:
        fig.add_hline(y=value, line_width=1, line_color=MUTED)
        fig.add_annotation(x=1, y=value, xref="paper", yref="y", text=label,
                           showarrow=False, xanchor="right", yanchor="bottom",
                           font=dict(size=11))


def line_fig(go, x, lines: list[tuple[str, list]], *, height: int = 320,
             pct: bool = True, fmt: str = "{:+.1f}%", markers: bool = False,
             colors: list[str] | None = None):
    """Lines over time. `lines` = [(name, y values)], at most three (slots 1-3). Each
    line ends in a dot in its colour with its last value beside it in ink; a legend
    is shown for two or more lines (one line is named by its section title).
    `colors` pins each line to its thing's colour (PORTFOLIO, SP500, ...); only the
    validated slots are allowed."""
    if len(lines) > len(SERIES):
        raise ValueError(f"{len(lines)} lines: at most {len(SERIES)} (fold the rest or facet)")
    colors = colors or SERIES[:len(lines)]
    if len(colors) != len(lines) or not set(colors) <= set(SERIES):
        raise ValueError("colors must be one validated SERIES colour per line")
    x = list(x)
    ys = [list(y) for _, y in lines]
    allv = [v for y in ys for v in y]
    shifts = end_label_shifts([y[-1] for y in ys], (max(allv) - min(allv)) if allv else 0)
    hov = "%{y:+.2f}%" if pct else "%{y:,.0f}"
    fig = go.Figure()
    for i, ((name, _), y) in enumerate(zip(lines, ys)):
        c = colors[i]
        fig.add_trace(go.Scatter(
            x=x, y=y, name=name, mode="lines+markers" if markers else "lines",
            line=dict(color=c, width=2), marker=dict(size=8, color=c),
            hovertemplate=hov + "<extra>" + name + "</extra>"))
        fig.add_trace(go.Scatter(x=[x[-1]], y=[y[-1]], mode="markers", showlegend=False,
                                 marker=dict(size=8, color=c), hoverinfo="skip"))
        fig.add_annotation(x=x[-1], y=y[-1], text=fmt.format(y[-1]), showarrow=False,
                           xanchor="left", xshift=9, yshift=shifts[i], font=dict(size=12))
    style_fig(fig, height, y_pct=pct)
    date_axis(fig, x)
    fig.update_layout(showlegend=len(lines) > 1, margin=dict(r=76))
    return fig


def hbar_fig(go, labels, values, *, colors=None, text=None, pct: bool = False,
             ref: float | None = None, ref_label: str = "", sort: bool = True,
             hover: str | None = None):
    """Horizontal bars: names stay readable in a narrow window. One hue for a single
    measure unless `colors` encodes a meaning (up/down, over a cap). Each bar is
    labelled with its value outside the bar end, in ink."""
    rows = list(zip(labels, values,
                    colors or [SERIES[0]] * len(values),
                    text or [f"{v:.0%}" if pct else f"{v:,.0f}" for v in values]))
    if sort:
        rows.sort(key=lambda r: r[1])                    # largest ends up on top
    lab, val, col, txt = (list(t) for t in zip(*rows)) if rows else ([], [], [], [])
    fig = go.Figure(go.Bar(
        x=val, y=lab, orientation="h", marker=bar_marker(col), text=txt,
        textposition="outside", cliponaxis=False,
        hovertemplate=hover or ("%{y}: %{x:.1%}<extra></extra>" if pct
                                else "%{y}: %{x:,}<extra></extra>")))
    style_fig(fig, 56 + 28 * len(rows))
    fig.update_layout(hovermode="closest", showlegend=False, bargap=0.3,
                      margin=dict(l=8, r=60, t=24 if ref is not None else 8, b=8))
    fig.update_xaxes(showgrid=True, gridcolor=GRID, zeroline=True, zerolinecolor=AXIS,
                     tickformat=".0%" if pct else None, range=bar_range(val, ref))
    fig.update_yaxes(showgrid=False, zeroline=False)
    if ref is not None:
        ref_line(fig, ref, ref_label, axis="x")
    return fig


def bar_range(values, ref: float | None = None, pad: float = 0.28) -> list[float] | None:
    """Axis range with room past each bar end, so a value label printed outside the
    longest bar (either side of zero) never runs into the names or off the chart."""
    vals = [v for v in values if v == v] + ([ref] if ref is not None else [])
    if not vals:
        return None
    lo, hi = min(0.0, *vals), max(0.0, *vals)
    room = pad * ((hi - lo) or 1.0)
    return [lo - room if lo < 0 else lo, hi + room if hi > 0 else hi]


def vbar_fig(go, x, values, *, pct: bool = False, ref: float | None = None,
             ref_label: str = "", height: int = 300):
    """Vertical bars for an ordered x (dates, cohorts): one hue, value on hover."""
    fig = go.Figure(go.Bar(
        x=list(x), y=list(values), marker=bar_marker(SERIES[0]),
        hovertemplate="%{x}: %{y:.0%}<extra></extra>" if pct
        else "%{x}: %{y:,}<extra></extra>"))
    style_fig(fig, height)
    fig.update_layout(hovermode="closest", showlegend=False, bargap=0.3)
    fig.update_yaxes(tickformat=".0%" if pct else None)
    if ref is not None:
        ref_line(fig, ref, ref_label, axis="y")
    return fig


def hist_fig(go, values, *, pct: bool = True, ref: float | None = 0.0,
             ref_label: str = "0", bins: int = 30, height: int = 300):
    """Distribution of one measure: one hue, a labelled reference line."""
    fig = go.Figure(go.Histogram(
        x=list(values), nbinsx=bins, marker=dict(color=SERIES[0]),
        hovertemplate=("%{x:.0%}" if pct else "%{x}") + ": %{y} picks<extra></extra>"))
    style_fig(fig, height)
    fig.update_layout(hovermode="closest", showlegend=False, bargap=0.08)
    fig.update_xaxes(tickformat=".0%" if pct else None)
    if ref is not None:
        ref_line(fig, ref, ref_label, axis="x")
    return fig


def scatter_fig(go, groups: list[tuple[str, list, list, str]], *, hover_text=None,
                x_pct: bool = False, y_pct: bool = True, height: int = 320):
    """Points in named groups [(name, x, y, colour)]. Legend names each group, so the
    colour is never the only cue; hover snaps to the nearest point."""
    fig = go.Figure()
    for k, (name, x, y, colour) in enumerate(groups):
        fig.add_trace(go.Scatter(
            x=list(x), y=list(y), name=name, mode="markers",
            marker=dict(size=9, color=colour, opacity=0.85),
            text=hover_text[k] if hover_text else None,
            hovertemplate=("%{text}: " if hover_text else "")
            + ("%{y:+.1%}" if y_pct else "%{y}") + "<extra>" + name + "</extra>"))
    style_fig(fig, height)
    fig.update_layout(hovermode="closest", showlegend=True)
    fig.update_xaxes(tickformat=".0%" if x_pct else None)
    fig.update_yaxes(tickformat="+.0%" if y_pct else None)
    return fig
