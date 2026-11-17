"""
tests/test_today_page.py — the dashboard's Today page data (dashboard/today.py) and
the shared style (dashboard/ui.py). Temp files only.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dashboard import today as td  # noqa: E402
from dashboard import ui  # noqa: E402


def _state(tmp_path, failed=(), age_days=2, alerts=None, navs=()):
    s = tmp_path / "state"
    s.mkdir()
    ts = (datetime(2026, 10, 11) - pd.Timedelta(days=age_days)).isoformat()
    (s / "health_history.json").write_text(json.dumps([{
        "ts": ts, "statuses": {f"c{i}": "PASS" for i in range(11)}, "failed": list(failed)}]))
    (s / "alert_state.json").write_text(json.dumps(alerts or {}))
    (s / "nav_history.jsonl").write_text("".join(
        json.dumps({"ts": t, "account": a, "nav": n, "currency": "EUR"}) + "\n"
        for t, a, n in navs) + "not json\n")
    return s


def _ft(tmp_path, days=5, closes=None):
    d = tmp_path / "ft"
    d.mkdir()
    dates = pd.bdate_range("2026-09-30", periods=days)
    pd.DataFrame({"date": dates.strftime("%Y-%m-%d"),
                  "incumbent_index": 1.0, "incumbent_ret": 0.0, "incumbent_drawdown": 0.0,
                  "spy_index": 1.0, "spy_ret": 0.0}).to_csv(d / "equity_curves.csv", index=False)
    (d / "meta.json").write_text(json.dumps({"incumbent_baselines": {"A": 10.0, "B": 20.0}}))
    closes = closes or {"A": 12.0, "B": 15.0}
    pd.DataFrame([{"date": "2026-10-06", "ticker": t, "close": c} for t, c in closes.items()]
                 ).to_csv(d / "price_snapshots.csv", index=False)
    return d


def test_health_reads_the_last_run(tmp_path):
    h = td.health(_state(tmp_path, failed=["snapshot_fresh"]), now=datetime(2026, 10, 11))
    assert h["ran"] and h["n"] == 11 and h["failed"] == ["snapshot_fresh"]
    assert round(h["age_h"]) == 48
    assert td.health(tmp_path / "nowhere") == {"ran": False}


def test_nav_uses_the_latest_account_and_skips_torn_lines(tmp_path):
    s = _state(tmp_path, navs=[("2026-09-29T10:00", "OLD", 1e6), ("2026-10-01T10:00", "NEW", 9e5),
                               ("2026-10-06T10:00", "NEW", 9.5e5)])
    n = td.nav(s)
    assert list(n["account"].unique()) == ["NEW"] and list(n["nav"]) == [9e5, 9.5e5]


def test_holding_returns_are_since_baseline_and_sorted(tmp_path):
    r = td.holding_returns(_ft(tmp_path))
    assert list(r.index) == ["B", "A"] and round(r["A"], 2) == 0.2 and round(r["B"], 2) == -0.25


def test_freshness_counts_trading_days_behind(tmp_path):
    f = td.freshness(_ft(tmp_path, days=5), today=date(2026, 10, 9))   # last 2026-10-06
    assert f["latest"] == date(2026, 10, 6) and f["behind"] == 3


def test_next_monday():
    assert td.next_monday(date(2026, 10, 11)) == date(2026, 10, 12)    # Sunday
    assert td.next_monday(date(2026, 10, 12)) == date(2026, 10, 19)    # Monday -> next week


def test_summary_says_all_good_or_names_the_problems():
    good = {"ran": True, "n": 11, "failed": [], "age_h": 30}
    assert td.summary(good, {"behind": 0}, [], -0.01) == ("good", [])
    st, issues = td.summary({**good, "failed": ["x", "y"]}, {"behind": 3},
                            [{"severity": "CRITICAL"}], -0.13)
    assert st == "critical"
    assert issues == ["2 health check(s) failing", "Price data is 3 trading days behind",
                      "1 critical alert(s) open", "The paper account is -13.0% below its peak"]
    st, issues = td.summary({**good, "age_h": 10 * 24}, {"behind": 1}, [], None)
    assert st == "warning" and "last ran 10 days ago" in issues[0]
    assert td.summary({"ran": False}, {"behind": None}, [], None)[0] == "warning"


def test_status_never_relies_on_colour_alone():
    for state in ("good", "warning", "critical", "info"):
        html = ui.pill_html(state)
        colour, icon, word = ui.STATUS[state]
        assert icon in html and word in html


def test_text_is_escaped():
    assert "<b>" not in ui.status_card_html("<b>x</b>", "good", "<i>", "<s>")
    assert "&lt;b&gt;" in ui.header_html("<b>t</b>", "p")
    assert "<b>" not in ui.headline_html("critical", "Attention", "<b>x</b>")


class _Fig:
    """Records update_xaxes calls (CI installs no plotly)."""
    def __init__(self):
        self.x = {}

    def update_xaxes(self, **kw):
        self.x.update(kw)


def test_date_axis_never_shows_hours():
    fig = _Fig()
    ui.date_axis(fig, pd.bdate_range("2026-09-30", periods=3))
    assert fig.x["dtick"] == 86_400_000                   # whole days, not 6-hour steps
    assert "%H" not in fig.x["tickformat"]
    fig = _Fig()
    ui.date_axis(fig, pd.bdate_range("2024-01-01", periods=600))
    assert "dtick" not in fig.x and "%Y" in fig.x["tickformat"]
