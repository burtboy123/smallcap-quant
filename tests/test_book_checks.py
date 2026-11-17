"""
tests/test_book_checks.py -- the two health checks added 2026-11-17.

baselines_match: the health check stayed 11/11 green while a split made the book read
-12.9% instead of -7.5%. It must FAIL (CRITICAL: the broker's targets were wrong too)
whenever a locked baseline disagrees with the stored series, and pass once restated.

book_inputs_fresh: the universe file was from March and the newest fundamentals quarter
2026-03-31, with no check saying so. It must stay quiet (WARN, no email) while the next
book is far off and FAIL once the book is due within BOOK_DUE_DAYS.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from forward_test import baselines as bl  # noqa: E402
from monitoring import checks as ck  # noqa: E402

D1 = "2026-11-12"


@pytest.fixture
def period(tmp_path, monkeypatch):
    d = tmp_path / "P"
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({
        "baselines_locked": True, "baseline_date": D1, "spy_baseline": 500.0,
        "incumbent_baselines": {"AAA": 100.0}, "challenger_baselines": {"BBB": 10.0}}),
        encoding="utf-8")
    pd.DataFrame([(D1, "AAA", 20.0, 0), (D1, "BBB", 10.0, 0), (D1, "SPY", 500.0, 0)],
                 columns=["date", "ticker", "close", "is_carried"]
                 ).to_csv(d / "price_snapshots.csv", index=False)
    monkeypatch.setattr(ck, "SNAP", d / "price_snapshots.csv")
    return d


# ── baselines_match ─────────────────────────────────────────────────────────────

def test_unrecorded_split_fails_critical(period):
    r = ck.check_baselines_match()
    assert r.status == ck.FAIL and r.severity == ck.CRITICAL
    assert r.metrics["mismatched"] == {"AAA": 0.2}
    assert r.runbook.startswith("python forward_test/snapshot.py --period ")


def test_recorded_split_passes(period):
    bl.append_splits(period, [{"ticker": "AAA", "date": "2026-11-16", "ratio": 5.0}])
    r = ck.check_baselines_match()
    assert r.status == ck.PASS and r.metrics["splits_recorded"] == 1


def test_baseline_date_missing_from_snapshot_fails(period):
    pd.DataFrame([("2026-11-13", "AAA", 20.0, 0)],
                 columns=["date", "ticker", "close", "is_carried"]
                 ).to_csv(period / "price_snapshots.csv", index=False)
    assert ck.check_baselines_match().status == ck.FAIL


def test_not_locked_is_skipped(period):
    (period / "meta.json").write_text(json.dumps({"baselines_locked": False}), encoding="utf-8")
    assert ck.check_baselines_match().status == ck.SKIP


def test_active_period_baselines_match_today():
    """The committed data itself: the active period's baselines agree with its prices."""
    r = ck.check_baselines_match()
    assert r.status in (ck.PASS, ck.SKIP), r.summary


# ── book_inputs_fresh ───────────────────────────────────────────────────────────

def _plan(d: Path, due: str):
    (d / "QUARTER_END_PLAN.md").write_text(
        f"| Evaluation date (2026-09-14) | **{due}** (Tue) | x |\n", encoding="utf-8")


STALE = {"universe": 244, "fundamentals": 231, "factor_scores": 57}
FRESH = {"universe": 30, "fundamentals": 140, "factor_scores": 20}


def test_stale_inputs_far_from_the_book_only_warn(period, monkeypatch):
    _plan(period, "2026-12-29")
    monkeypatch.setattr(ck, "_book_input_ages", lambda today: STALE)
    r = ck.check_book_inputs(date(2026, 11, 17))
    assert r.status == ck.WARN and not r.failed
    assert "universe 244d" in r.summary and "factor_scores" not in r.summary


def test_stale_inputs_with_the_book_due_fail_without_blocking(period, monkeypatch):
    _plan(period, "2026-12-29")
    monkeypatch.setattr(ck, "_book_input_ages", lambda today: STALE)
    r = ck.check_book_inputs(date(2026, 12, 8))                      # 21 days left
    assert r.status == ck.FAIL and r.severity == ck.WARNING         # never blocks the broker
    assert r.runbook == "python -m scripts.monthly_run"


def test_fresh_inputs_pass_even_when_due(period, monkeypatch):
    _plan(period, "2026-12-29")
    monkeypatch.setattr(ck, "_book_input_ages", lambda today: FRESH)
    assert ck.check_book_inputs(date(2026, 12, 20)).status == ck.PASS


def test_missing_input_counts_as_stale(period, monkeypatch):
    _plan(period, "2026-12-29")
    monkeypatch.setattr(ck, "_book_input_ages", lambda today: dict(FRESH, universe=None))
    r = ck.check_book_inputs(date(2026, 11, 17))
    assert r.status == ck.WARN and "universe missing" in r.summary


def test_ages_are_read_from_the_database_and_the_file(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE fundamentals (period_end_date TEXT)")
        c.execute("CREATE TABLE factor_scores (calculation_date TEXT)")
        c.executemany("INSERT INTO fundamentals VALUES (?)", [("2026-03-31",), ("2025-12-31",)])
        c.execute("INSERT INTO factor_scores VALUES ('2026-09-21')")
    csv = tmp_path / "iwm.csv"
    csv.write_text("x", encoding="utf-8")
    t = time.mktime(date(2026, 3, 18).timetuple())
    os.utime(csv, (t, t))
    import config.settings as cs
    monkeypatch.setattr(cs, "DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setattr(ck, "UNIVERSE_CSV", csv)
    ages = ck._book_input_ages(date(2026, 11, 17))
    assert ages == {"universe": 244, "fundamentals": 231, "factor_scores": 57}


def test_both_checks_are_in_the_health_run():
    import inspect
    src = inspect.getsource(ck.run_all)
    assert "check_baselines_match" in src and "check_book_inputs" in src
