"""
tests/test_snapshot_partial_bar.py — forward_test/snapshot.py never records a session
that has not closed.

The scheduled task also fires whenever the PC wakes after a missed 23:30 start, which on
2026-11-05 and 2026-11-09 was during the US session: yfinance returned the day's partial
bar and the snapshot wrote it as a close. The merge healed it on the next run, but the
risk-watch email and the printed equity curve in between were built on an intraday
print. The fix drops the open session before anything is written; these tests fail if
the drop is removed from main() or its clock comparison is flipped.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import forward_test.snapshot as snap  # noqa: E402

ET = ZoneInfo("America/New_York")
YDAY, TODAY = date(2026, 11, 9), date(2026, 11, 10)          # Monday, Tuesday


def _frame(days=(YDAY, TODAY)):
    rows = {YDAY: [10.0, 20.0, 100.0], TODAY: [11.0, 19.0, 101.0]}
    return pd.DataFrame([rows[d] for d in days], index=list(days),
                        columns=["AAA", "BBB", "SPY"])


def _at(h, m, day=TODAY):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=ET)


class TestDropOpenSession:
    def test_during_the_session_today_is_dropped(self):
        kept, dropped = snap.drop_open_session(_frame(), _at(11, 3))
        assert list(kept.index) == [YDAY] and dropped == [TODAY]

    def test_before_the_open_today_is_dropped_too(self):
        kept, dropped = snap.drop_open_session(_frame(), _at(7, 57))
        assert list(kept.index) == [YDAY] and dropped == [TODAY]

    def test_at_the_scheduled_time_today_is_kept(self):
        # 23:30 Finland is 16:30 New York (both on standard time in November)
        kept, dropped = snap.drop_open_session(_frame(), _at(16, 30))
        assert list(kept.index) == [YDAY, TODAY] and dropped == []

    def test_one_minute_before_the_close_settles_is_still_open(self):
        kept, dropped = snap.drop_open_session(_frame(), _at(16, 14))
        assert list(kept.index) == [YDAY] and dropped == [TODAY]

    def test_the_next_morning_yesterday_is_final(self):
        kept, dropped = snap.drop_open_session(_frame(), _at(7, 0, date(2026, 11, 11)))
        assert list(kept.index) == [YDAY, TODAY] and dropped == []


@pytest.fixture
def period(tmp_path, monkeypatch):
    """A private period directory; snapshot.py's module globals are pointed at it and
    yfinance is replaced by a fixed two-session frame."""
    d = tmp_path / "P"
    d.mkdir()
    pd.DataFrame({"ticker": ["AAA"]}).to_csv(d / "incumbent_book.csv", index=False)
    pd.DataFrame({"ticker": ["BBB"]}).to_csv(d / "value_tilt_book.csv", index=False)
    (d / "meta.json").write_text(json.dumps({"baselines_locked": False}), encoding="utf-8")
    monkeypatch.setattr(snap, "DIR", d)
    monkeypatch.setattr(snap, "SNAP", d / "price_snapshots.csv")
    monkeypatch.setattr(snap, "CURVE", d / "equity_curves.csv")
    monkeypatch.setattr(snap, "START", YDAY.isoformat())
    monkeypatch.setattr(snap, "CFG", {"start": YDAY.isoformat(), "spy_baseline": None,
                                      "baseline_mode": "first_close", "baseline_col": None})
    monkeypatch.setattr(snap, "SPY_BASELINE", None)
    monkeypatch.setattr(snap, "fetch", lambda tickers, start, end: _frame())
    return d


def test_main_during_the_session_records_only_closed_sessions(period, monkeypatch, capsys):
    monkeypatch.setattr(snap, "_now_et", lambda: _at(11, 3))
    snap.main()
    s = pd.read_csv(period / "price_snapshots.csv")
    assert sorted(s["date"].unique()) == [YDAY.isoformat()]
    assert list(pd.read_csv(period / "equity_curves.csv")["date"]) == [YDAY.isoformat()]
    out = capsys.readouterr().out
    assert TODAY.isoformat() in out and "not closed" in out
    assert f"as of {YDAY.isoformat()}" in out


def test_main_after_the_close_records_today(period, monkeypatch, capsys):
    monkeypatch.setattr(snap, "_now_et", lambda: _at(16, 30))
    snap.main()
    s = pd.read_csv(period / "price_snapshots.csv")
    assert sorted(s["date"].unique()) == [YDAY.isoformat(), TODAY.isoformat()]
    assert "not closed" not in capsys.readouterr().out


def test_the_open_day_is_recorded_by_the_next_run(period, monkeypatch):
    """Same file, two runs: intraday first, after the close second. The second run adds
    the day; nothing recorded by the first is lost."""
    monkeypatch.setattr(snap, "_now_et", lambda: _at(11, 3))
    snap.main()
    monkeypatch.setattr(snap, "_now_et", lambda: _at(16, 30))
    snap.main()
    s = pd.read_csv(period / "price_snapshots.csv")
    assert sorted(s["date"].unique()) == [YDAY.isoformat(), TODAY.isoformat()]
    assert len(s) == 6 and int(s["is_carried"].sum()) == 0


def test_first_day_during_the_session_is_not_started_not_a_crash(period, monkeypatch, capsys):
    monkeypatch.setattr(snap, "fetch", lambda *a: _frame((TODAY,)))
    monkeypatch.setattr(snap, "_now_et", lambda: _at(11, 3))
    assert snap.main() is None
    assert not (period / "price_snapshots.csv").exists()
    assert "NOT STARTED" in capsys.readouterr().out


def test_nothing_final_with_sessions_on_file_exits_nonzero(period, monkeypatch):
    """Sessions recorded, and the fetch has only the open day: that is an outage, and the
    scheduled task must not report success."""
    pd.DataFrame([("2026-11-06", "AAA", 9.0, 0)],
                 columns=["date", "ticker", "close", "is_carried"]
                 ).to_csv(period / "price_snapshots.csv", index=False)
    monkeypatch.setattr(snap, "fetch", lambda *a: _frame((TODAY,)))
    monkeypatch.setattr(snap, "_now_et", lambda: _at(11, 3))
    with pytest.raises(SystemExit) as e:
        snap.main()
    assert e.value.code == 1
