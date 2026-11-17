"""
tests/test_charts.py — the shared chart builders in dashboard/ui.py, built with the real
plotly (CI installs the version requirements.txt pins, so an option that version
rejects fails here instead of on the trading machine's dashboard).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd
import plotly
import plotly.graph_objects as go

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dashboard import ui  # noqa: E402

DATES = pd.bdate_range("2026-09-30", periods=6)


def _pin(name: str, path: str) -> str | None:
    m = re.search(rf"^{name}==([\w.]+)", (ROOT / path).read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else None


def test_ci_tests_charts_against_the_pinned_plotly():
    """CI and the trading machine must run the same plotly, or a chart option can pass
    here and crash there (bar cornerradius under 5.18 did exactly that)."""
    pinned = _pin("plotly", "requirements.txt")
    assert pinned and _pin("plotly", "requirements-ci.txt") == pinned


def test_lines_take_slots_in_order_and_end_with_an_ink_label():
    fig = ui.line_fig(go, DATES, [("Yours", [0, 1, 2, 2, 3, 3.4]),
                                  ("Comparison", [0, .5, 1, 2, 2.5, 1.0]),
                                  ("S&P 500", [0, .2, .4, .6, .8, 0.9])])
    lines = [t for t in fig.data if t.showlegend is not False]
    assert [t.line.color for t in lines] == ui.SERIES          # fixed order, never by rank
    labels = {a.text: a for a in fig.layout.annotations}
    assert set(labels) == {"+3.4%", "+1.0%", "+0.9%"}
    assert all(a.font.color is None for a in labels.values())  # ink, not the series colour
    assert fig.layout.showlegend is True


def _calls(src: str, head: str) -> list[str]:
    """The full text of every call starting with `head` (balanced parentheses)."""
    out, i = [], src.find(head)
    while i >= 0:
        depth, j = 0, i + len(head) - 1
        while True:
            depth += {"(": 1, ")": -1}.get(src[j], 0)
            if depth == 0:
                break
            j += 1
        out.append(src[i:j + 1])
        i = src.find(head, j)
    return out


def test_a_line_keeps_its_things_colour_on_every_chart():
    fig = ui.line_fig(go, DATES, [("Yours", [0] * 6), ("S&P 500", [1] * 6)],
                      colors=[ui.PORTFOLIO, ui.SP500])
    assert [t.line.color for t in fig.data if t.showlegend is not False] == \
        [ui.PORTFOLIO, ui.SP500]
    for bad in (["#ff0000", ui.SP500], [ui.PORTFOLIO]):
        try:
            ui.line_fig(go, DATES, [("a", [0] * 6), ("b", [1] * 6)], colors=bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad} accepted")
    for f in ("app.py", "today.py"):               # every line chart pins its colours
        src = (ROOT / "dashboard" / f).read_text(encoding="utf-8")
        calls = _calls(src, "ui.line_fig(")
        assert calls and all("colors=[ui." in c for c in calls), f


def test_one_line_needs_no_legend_and_more_than_three_is_refused():
    fig = ui.line_fig(go, DATES, [("Value", [1, 2, 3, 4, 5, 6])], pct=False, fmt="{:,.0f}")
    assert fig.layout.showlegend is False
    try:
        ui.line_fig(go, DATES, [(str(i), [0] * 6) for i in range(4)])
    except ValueError:
        pass
    else:
        raise AssertionError("a fourth series must be refused, not given a cycled colour")


def test_end_labels_that_would_collide_are_moved_apart():
    # gap = 0.08 x 3 = 0.24; the upper label moves to 1.0 + 0.24: (1.24 - 1.02) / 0.24 x 15 px
    assert ui.end_label_shifts([1.0, 1.02], span=3.0) == [0, 14]
    assert ui.end_label_shifts([0.5, 2.5], span=3.0) == [0, 0]      # far apart: untouched
    assert ui.end_label_shifts([2.0], span=3.0) == [0]
    fig = ui.line_fig(go, DATES, [("A", [0, 1, 1, 1, 1, 0.9]), ("B", [0, 2, 2, 2, 2, 0.92])])
    shifts = sorted(a.yshift for a in fig.layout.annotations)
    assert shifts[0] == 0 and shifts[1] >= 10


def test_bars_one_hue_sorted_labelled_and_a_muted_reference_line():
    fig = ui.hbar_fig(go, ["Energy", "Tech", "Health"], [0.27, 0.13, 0.33], pct=True,
                      ref=0.30, ref_label="30% cap")
    bar = fig.data[0]
    assert list(bar.y) == ["Tech", "Energy", "Health"]          # largest on top
    assert set(bar.marker.color) == {ui.SERIES[0]}
    assert list(bar.text) == ["13%", "27%", "33%"]
    cap = [s for s in fig.layout.shapes]
    assert len(cap) == 1 and cap[0].line.color == ui.MUTED     # never red
    assert any(a.text == "30% cap" for a in fig.layout.annotations)
    assert fig.layout.height == 56 + 28 * 3                    # grows with the rows


def test_bar_labels_have_room_on_both_sides():
    """The -3.7% label on the longest red bar ran into the ticker names at phone width."""
    lo, hi = ui.bar_range([0.054, -0.037])
    assert lo < -0.037 - 0.02 and hi > 0.054 + 0.02
    assert ui.bar_range([0.2, 0.3])[0] == 0                    # nothing negative: start at 0
    assert ui.bar_range([0.2], ref=0.30)[1] > 0.30             # the cap line stays in view
    fig = ui.hbar_fig(go, ["A", "B"], [0.054, -0.037], pct=True)
    assert fig.layout.xaxis.range[0] < -0.037


def test_up_down_colours_follow_the_value():
    fig = ui.hbar_fig(go, ["A", "B"], [0.05, -0.02], pct=True,
                      colors=[ui.GAIN, ui.LOSS], text=["+5.0%", "-2.0%"])
    by_name = dict(zip(fig.data[0].y, fig.data[0].marker.color))
    assert by_name == {"A": ui.GAIN, "B": ui.LOSS}


def test_histogram_scatter_and_columns_build_on_the_pinned_plotly():
    ui.hist_fig(go, [0.1, -0.2, 0.05])
    ui.vbar_fig(go, ["2026-03", "2026-04"], [0.4, 0.6], pct=True, ref=0.5, ref_label="50%")
    sc = ui.scatter_fig(go, [("Beat", [1, 2], [0.1, 0.2], ui.GAIN),
                             ("Lagged", [3], [-0.1], ui.LOSS)],
                        hover_text=[["AA", "BB"], ["CC"]])
    assert [t.name for t in sc.data] == ["Beat", "Lagged"]     # legend names each colour
    assert plotly.__version__                                    # the import is real


def test_charts_use_the_shared_palette_only():
    """No page picks its own colours: one validated palette, one look."""
    for f in ("app.py", "today.py"):
        src = (ROOT / "dashboard" / f).read_text(encoding="utf-8")
        assert not re.findall(r"#[0-9A-Fa-f]{6}\b", src), f"{f}: colour outside ui.py"
        assert "plotly.express" not in src and '"white"' not in src
