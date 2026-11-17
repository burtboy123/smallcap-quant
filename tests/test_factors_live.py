"""
tests/test_factors_live.py -- a weighted factor that is the same for every stock is
reported, not hidden.

Found 2026-11-17: sentiment (11%) and revenue acceleration (5%) were 50 for every stock
(sentiment's 90-day window held no transcript; yfinance gives 5 quarters of revenue and
the factor needs 6). Every row was non-null, so nothing looked wrong.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from factors.health import constant_factors  # noqa: E402
from monitoring import checks as ck  # noqa: E402

W = {"value": 0.5, "sentiment": 0.3, "revenue_accel": 0.2, "unused": 0.0}


def test_constant_and_missing_factors_are_reported():
    df = pd.DataFrame({"value_score": [10, 50, 90], "sentiment_score": [50, 50, 50]})
    assert constant_factors(df, W) == [("sentiment", 0.3), ("revenue_accel", 0.2)]


def test_varying_factors_pass_and_zero_weights_are_ignored():
    df = pd.DataFrame({"value_score": [1, 2, 3], "sentiment_score": [4, 5, 6],
                       "revenue_accel_score": [25, 75, 50], "unused_score": [1, 1, 1]})
    assert constant_factors(df, W) == []


def test_raw_column_name_is_used_when_there_is_no_score_column():
    df = pd.DataFrame({"proximity_52w": [10, 20]})
    assert constant_factors(df, {"proximity_52w": 0.05}) == []


# ── the health check, on a real (temporary) database ─────────────────────────────

FACTORS = {"value": "value_score", "quality": "quality_score", "momentum": "momentum_score",
           "short_interest": "short_interest_score", "insider": "insider_score",
           "earnings_surprise": "earnings_surprise_score", "proximity_52w": "proximity_52w",
           "revenue_accel": "revenue_accel_score"}


def _db(tmp_path, monkeypatch, *, accel_varies: bool, sentiment_age_days: int | None):
    db = tmp_path / "q.db"
    rows = []
    for i, t in enumerate(["AAA", "BBB", "CCC", "DDD"]):
        r = {"ticker": t, "calculation_date": "2026-11-01"}
        r.update({c: 10.0 + 20 * i for c in FACTORS.values()})
        r["revenue_accel_score"] = 25.0 + 25 * (i % 3) if accel_varies else 50.0
        rows.append(r)
    with sqlite3.connect(db) as c:
        pd.DataFrame(rows).to_sql("factor_scores", c, index=False)
        s = pd.DataFrame(columns=["ticker", "overall_sentiment", "management_confidence",
                                  "guidance_direction", "transcript_date", "available_date"])
        if sentiment_age_days is not None:
            d = str(date.today() - timedelta(days=sentiment_age_days))
            s = pd.DataFrame({"ticker": ["AAA", "BBB", "CCC", "DDD"],
                              "overall_sentiment": [-0.5, 0.0, 0.4, 0.9],
                              "management_confidence": [0.2, 0.5, 0.6, 0.9],
                              "guidance_direction": ["lowered", "none", "maintained", "raised"],
                              "transcript_date": d, "available_date": d})
        s.to_sql("sentiment_scores", c, index=False)
    url = f"sqlite:///{db}"
    import config.settings as cs                  # both readers import it at call time
    monkeypatch.setattr(cs, "DATABASE_URL", url)


def test_check_reports_the_two_found_on_2026_11_17(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch, accel_varies=False, sentiment_age_days=150)
    r = ck.check_factors_live()
    assert r.status == ck.WARN and not r.failed
    assert set(r.metrics["constant"]) == {"sentiment", "revenue_accel"}
    assert "84%" in r.detail


def test_recent_sentiment_counts_as_live(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch, accel_varies=False, sentiment_age_days=10)
    r = ck.check_factors_live()
    assert set(r.metrics["constant"]) == {"revenue_accel"}


def test_all_factors_varying_passes(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch, accel_varies=True, sentiment_age_days=10)
    assert ck.check_factors_live().status == ck.PASS


def test_check_is_in_the_health_run():
    import inspect
    assert "check_factors_live" in inspect.getsource(ck.run_all)
