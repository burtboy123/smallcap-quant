"""
tests/test_risk_watch.py — the early-warning risk watch and the NAV high-water mark
that makes the 5.1 drawdown kill-switch reachable again.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from broker import nav_history as nh                                   # noqa: E402
from broker.killswitch import (KillSwitchTripped, OrderGate,            # noqa: E402
                               PortfolioState, Switch)
from monitoring import risk_watch as rw                                # noqa: E402

# ── NAV history / high-water mark ───────────────────────────────────────────────


def test_hwm_is_the_highest_recorded_nav_for_that_account(tmp_path):
    p = tmp_path / "nav.jsonl"
    for nav in (1_000_000, 1_020_000, 990_000):
        nh.record("DU1", nav, "EUR", path=p)
    nh.record("DU2", 5_000_000, "EUR", path=p)                     # other account ignored
    hw = nh.high_water_mark("DU1", 969_000, path=p)
    assert hw["hwm"] == 1_020_000 and hw["drawdown"] == pytest.approx(-0.05)


def test_failed_reads_and_torn_lines_are_ignored(tmp_path):
    p = tmp_path / "nav.jsonl"
    nh.record("DU1", 0, "EUR", path=p)                             # failed read: skipped
    p.write_text('{"account": "DU1", "nav": 1000.0, "ts": "x", "currency": "EUR"}\n{bro')
    assert nh.high_water_mark("DU1", 900.0, path=p)["drawdown"] == pytest.approx(-0.10)


def test_drawdown_halt_fires_with_the_recorded_hwm():
    """The bug: HWM = current NAV made 5.1 unreachable. With the recorded mark it trips."""
    nav_usd, dd = 960_000.0, -0.16                                 # 16% below the peak
    pf = PortfolioState(equity=nav_usd, high_water_mark=nav_usd / (1 + dd))
    with pytest.raises(KillSwitchTripped) as e:
        OrderGate(None, audit_log=None).authorize([], pf, book_value=nav_usd)
    assert e.value.switch == Switch.MAX_DRAWDOWN


def test_old_construction_could_never_trip():
    pf = PortfolioState(equity=500_000.0, high_water_mark=500_000.0)  # HWM = NAV
    OrderGate(None, audit_log=None).authorize([], pf, book_value=500_000.0)  # no raise


# ── risk watch ──────────────────────────────────────────────────────────────────

@pytest.fixture
def period(tmp_path, monkeypatch):
    d = tmp_path / "2026Q4b"
    d.mkdir()
    (d / "meta.json").write_text(json.dumps(
        {"incumbent_baselines": {"AAA": 10.0, "BBB": 10.0, "CCC": 10.0}}))
    pd.DataFrame({"ticker": ["AAA", "BBB", "CCC"],
                  "sector": ["Energy", "Energy", "Healthcare"],
                  "weight_equal": 1 / 3}).to_csv(d / "incumbent_book.csv", index=False)
    snap = d / "price_snapshots.csv"
    monkeypatch.setattr(rw.ck, "SNAP", snap)
    recon = tmp_path / "recon"
    recon.mkdir()
    import monitoring.weekly_report as wr
    monkeypatch.setattr(wr, "RECON_DIR", recon)
    monkeypatch.setattr(nh, "NAV_LOG", tmp_path / "nav.jsonl")
    # the health-task watch reads the real Task Scheduler; its own tests are below
    monkeypatch.setattr(rw, "_health_task_ran", lambda *a, **k: [])
    # the price cross-check calls Finnhub; its own tests are tests/test_price_crosscheck.py
    monkeypatch.setattr(rw, "_price_crosscheck", lambda *a, **k: [])
    return d, snap, recon


def _closes(snap, **px):
    pd.DataFrame({"date": "2026-10-05", "ticker": list(px), "close": list(px.values()),
                  "is_carried": 0}).to_csv(snap, index=False)


def _curve(d, values):
    pd.DataFrame({"date": [f"2026-10-{i + 1:02d}" for i in range(len(values))],
                  "incumbent_index": values}).to_csv(d / "equity_curves.csv", index=False)


def test_all_clear_sends_nothing(period):
    d, snap, _ = period
    _closes(snap, AAA=10.5, BBB=10.2, CCC=9.9)
    _curve(d, [1.0, 1.02, 1.03])
    assert rw.evaluate(d) == []


@pytest.mark.parametrize("last,sev", [(0.91, "WARNING"), (0.87, "CRITICAL")])
def test_book_drawdown_warns_then_goes_critical(period, last, sev):
    d, snap, _ = period
    _closes(snap, AAA=10, BBB=10, CCC=10)
    _curve(d, [1.0, 1.0, last])
    (a,) = [x for x in rw.evaluate(d) if x.key == "risk:book_drawdown"]
    assert a.severity == sev


def test_single_name_drop(period):
    d, snap, _ = period
    _closes(snap, AAA=7.0, BBB=5.5, CCC=10.0)                 # -30% and -45%
    _curve(d, [1.0])
    got = {a.key: a.severity for a in rw.evaluate(d)}
    assert got == {"risk:name:AAA": "WARNING", "risk:name:BBB": "CRITICAL"}


def test_sector_drift_toward_the_cap(period):
    d, snap, recon = period
    _closes(snap, AAA=10, BBB=10, CCC=10)
    _curve(d, [1.0])
    (recon / "recon_20261005_170000.json").write_text(json.dumps({
        "account": "DU1", "positions_after": [
            {"ticker": "AAA", "position": 2000, "avg_cost": 10},
            {"ticker": "BBB", "position": 2000, "avg_cost": 10},
            {"ticker": "CCC", "position": 6000, "avg_cost": 10}]}))
    keys = {a.key for a in rw.evaluate(d)}
    assert "risk:sector:Energy" in keys and "risk:sector:Healthcare" in keys  # 40%, 60%


def test_account_drawdown_from_recorded_navs(period):
    d, snap, _ = period
    _closes(snap, AAA=10, BBB=10, CCC=10)
    _curve(d, [1.0])
    nh.record("DU1", 1_000_000, "EUR")
    nh.record("DU1", 900_000, "EUR")
    (a,) = [x for x in rw.evaluate(d, account="DU1") if x.key == "risk:account_drawdown"]
    assert a.severity == "WARNING" and "-10.0%" in a.title


def test_a_broken_check_is_reported_not_swallowed(period, monkeypatch):
    d, snap, _ = period
    monkeypatch.setattr(rw, "_names", lambda d: 1 / 0)
    _curve(d, [1.0])
    _closes(snap, AAA=10, BBB=10, CCC=10)
    assert [a.key for a in rw.evaluate(d)] == ["risk:watch_error"]


def test_snapshot_wrapper_still_returns_the_snapshots_exit_code():
    lines = [l.strip() for l in (ROOT / "forward_test" / "run_snapshot.bat")
             .read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.strip().upper().startswith("REM")]
    py = next(i for i, l in enumerate(lines) if "snapshot.py" in l)
    assert lines[py + 1] == "set RC=%ERRORLEVEL%"
    assert lines[-1] == "endlocal & exit /b %RC%"
    assert any("monitoring.risk_watch" in l for l in lines[py + 1:])


# ── dead-man's switch for the Monday health check (runs from the nightly job) ──────

from datetime import datetime as _dt  # noqa: E402

NOW = _dt(2026, 11, 26, 23, 30)                 # a Thursday night


def _info(last_run, next_run="2026-11-30T15:30:00+02:00", result=0):
    return {"state": "Ready", "last_run": last_run, "last_result": result,
            "next_run": next_run}


def test_health_task_that_ran_this_week_is_silent():
    assert rw._health_task_ran(NOW, _info("2026-11-23T15:30:05.0000000+02:00")) == []


def test_health_task_that_has_not_run_for_ten_days_alerts():
    (a,) = rw._health_task_ran(NOW, _info("2026-11-16T15:30:05+02:00"))
    assert a.key == "risk:health_task_stale" and "10 days" in a.title
    assert a.runbook == "python -m monitoring.health_check -v --weekly-report"


def test_newly_registered_task_due_soon_is_silent():
    """Re-registration resets the history: never run, next run in 4 days."""
    info = _info("1999-11-30T00:00:00.0000000+02:00", result=267011)
    assert rw._health_task_ran(NOW, info) == []


def test_never_run_and_not_due_soon_alerts():
    info = _info("1999-11-30T00:00:00+02:00", next_run="2027-01-04T15:30:00+02:00",
                 result=267011)
    assert len(rw._health_task_ran(NOW, info)) == 1


def test_unregistered_health_task_alerts():
    (a,) = rw._health_task_ran(NOW, {})
    assert "not registered" in a.title


def test_watch_includes_the_health_task_check():
    import inspect
    assert "_health_task_ran" in inspect.getsource(rw.evaluate)
