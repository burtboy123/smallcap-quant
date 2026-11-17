"""
tests/test_dashboard_style.py — the shared page look (dashboard/ui.py) and the Execution
page's defaults (dashboard/execution_view.py). No Streamlit needed.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dashboard import execution_view as ev  # noqa: E402
from dashboard import ui  # noqa: E402


def _runs(*dry):
    """run_summary() shape: newest first."""
    return pd.DataFrame({"run_id": [f"r{i}" for i in range(len(dry))],
                         "dry_run": list(dry)})


# ── Execution page ──────────────────────────────────────────────────────────────

def test_execution_page_opens_on_the_newest_run_that_traded():
    assert ev.default_run(_runs(True, False, False)) == 1      # skip the rehearsal
    assert ev.default_run(_runs(False, True)) == 0
    assert ev.default_run(_runs(True, True)) == 0              # nothing real: newest


def test_a_dry_run_is_called_a_rehearsal():
    dry = ev.run_label("2026-10-06 14:04", "DU1", "execution_engine", True, 0.0)
    real = ev.run_label("2026-10-01 16:40", "DU1", "execution_engine", False, 0.625)
    assert "Rehearsal" in dry and "filled" not in dry
    assert "62.5% filled" in real and "Rehearsal" not in real
    assert "100.0% filled" in ev.run_label("2026-10-01", "DU1", "e", False, 1.3)


def test_holdings_line_counts_only_open_positions():
    rec = {"timestamp": "2026-10-01T16:45:00",
           "positions_after": [{"ticker": "A", "position": 100},
                               {"ticker": "B", "position": 0},
                               {"ticker": "C", "position": 5}]}
    line = ev.holdings_line(rec)
    assert "holds 2 stocks" in line and "01 Oct 2026" in line
    assert ev.holdings_line({"positions_after": []}) is None
    assert ev.holdings_line({}) is None


# ── shared look ─────────────────────────────────────────────────────────────────

def test_inline_marks_render_and_html_never_does():
    out = ui.inline_md("**Bold** `code --x` *word* 5*3 <script>x</script>")
    assert "<b>Bold</b>" in out and "<code>code --x</code>" in out
    assert "<i>word</i>" in out and "5*3" in out
    assert "<script>" not in out and "&lt;script&gt;" in out


def test_notes_with_a_state_carry_icon_and_word():
    plain = ui.note_html("Nothing to see")
    assert "ui-pill" not in plain
    for state in ("good", "warning", "critical"):
        colour, icon, word = ui.STATUS[state]
        html = ui.note_html("x", state)
        assert icon in html and word in html and colour in html
    assert "Stale" in ui.note_html("x", "critical", "Stale")


def test_table_status_cells_and_check_rows_never_rely_on_colour():
    assert ui.status_cell("FAIL") == "✕ FAIL"
    assert ui.status_cell("PASS") == "✓ PASS"
    assert ui.status_cell("WARN") == "! WARN"
    row = ui.check_row_html(False, "DB <prices>", "0 rows")
    assert "✕" in row and "Fail" in row and "&lt;prices&gt;" in row
    assert "✓" in ui.check_row_html(True, "x", "y")


def test_every_page_uses_the_shared_header():
    """Each page opens with _head(title, purpose): one look, a plain explanation."""
    tree = ast.parse((ROOT / "dashboard" / "app.py").read_text(encoding="utf-8"))
    pages = [n for n in tree.body if isinstance(n, ast.FunctionDef)
             and n.name.startswith("_page_") and n.name != "_page_today"]
    assert len(pages) >= 11
    missing = [p.name for p in pages
               if not any(isinstance(c, ast.Call) and getattr(c.func, "id", "") == "_head"
                          for c in ast.walk(p))]
    assert not missing, f"pages without the shared header: {missing}"


def test_no_light_row_backgrounds_on_the_dark_theme():
    """Pale fills (#fde2e1, #ffe0e0, #fff4d6...) under white text were unreadable in
    dark mode; status is an icon + word column instead."""
    src = (ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
    assert "background-color: #f" not in src and "background-color: #e" not in src


def test_charts_never_pass_options_the_pinned_plotly_rejects(monkeypatch):
    """plotly before 5.19 has no bar cornerradius (requirements.txt pinned 5.18 until
    2026-11-10): passing it raised ValueError and blanked the Today and Stock Picks bar
    charts. Both branches are exercised here whatever plotly is installed."""
    monkeypatch.setattr(ui, "ROUNDED_BARS", False)
    assert ui.bar_marker("#3987e5") == {"color": "#3987e5"}
    monkeypatch.setattr(ui, "ROUNDED_BARS", True)
    assert ui.bar_marker("#3987e5")["cornerradius"] == 4
    for f in ("app.py", "today.py", "picks_view.py"):
        src = (ROOT / "dashboard" / f).read_text(encoding="utf-8")
        assert "cornerradius" not in src, f"{f}: use ui.bar_marker()"
