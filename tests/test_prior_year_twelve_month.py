"""
tests/test_prior_year_twelve_month.py — the F-score's prior-year row must cover 12
months, like this year's row (factors/calculator.twelve_month_prior).

A SimFin QUARTERLY prior-year row compared with a 12-month current row made ROA and
asset turnover look ~4x better year over year (129 of 1,869 tickers on 2026-09-21).
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from factors.calculator import twelve_month_prior  # noqa: E402
from factors.quality import calculate_quality_factors  # noqa: E402


def _q(t, fy, fq, ni, rev, filed=True, ta=1000.0):
    return {"ticker": t, "fiscal_year": fy, "fiscal_quarter": fq,
            "filing_date": date(fy, 12, 1) if filed else None,
            "net_income": ni, "revenue": rev, "gross_profit": rev / 2,
            "operating_income": ni, "ebitda": ni, "operating_cash_flow": ni,
            "capex": 1.0, "free_cash_flow": ni - 1, "total_assets": ta,
            "total_equity": 500.0, "total_debt": 100.0, "shares_outstanding": 100,
            "current_assets": None, "current_liabilities": None}


def test_quarterly_prior_row_becomes_twelve_months():
    history = pd.DataFrame([_q("A", 2025, q, 10.0, 100.0) for q in (1, 2, 3, 4)])
    prior = pd.DataFrame([_q("A", 2025, 4, 10.0, 100.0)])
    out, conv, left = twelve_month_prior(prior, history)
    assert (conv, left) == (1, 0)
    r = out.iloc[0]
    assert r["net_income"] == 40 and r["revenue"] == 400 and r["gross_profit"] == 200
    assert r["total_assets"] == 1000                       # balance sheet untouched


def test_twelve_month_rows_are_left_alone():
    prior = pd.DataFrame([_q("B", 2025, 4, 40.0, 400.0, filed=False)])
    out, conv, left = twelve_month_prior(prior, pd.DataFrame())
    assert (conv, left) == (0, 0) and out.iloc[0]["net_income"] == 40


def test_without_four_consecutive_quarters_the_row_is_kept_not_dropped():
    history = pd.DataFrame([_q("A", 2025, q, 10.0, 100.0) for q in (1, 3, 4)])  # Q2 missing
    prior = pd.DataFrame([_q("A", 2025, 4, 10.0, 100.0)])
    out, conv, left = twelve_month_prior(prior, history)
    assert (conv, left) == (0, 1) and len(out) == 1 and out.iloc[0]["net_income"] == 10


def test_fscore_no_longer_rewards_a_quarter_vs_year_comparison():
    """Flat business: NI 40/yr both years. Compared with the raw quarterly prior row,
    ROA and turnover look 4x better and earn F3/F9; on a 12-month basis they do not."""
    current = pd.DataFrame([_q("A", 2026, 4, 40.0, 400.0, filed=False)])
    history = pd.DataFrame([_q("A", 2025, q, 10.0, 100.0) for q in (1, 2, 3, 4)])
    raw_prior = pd.DataFrame([_q("A", 2025, 4, 10.0, 100.0)])
    fixed_prior, _, _ = twelve_month_prior(raw_prior, history)
    f_raw = calculate_quality_factors(current, raw_prior).loc["A", "f_score"]
    f_fix = calculate_quality_factors(current, fixed_prior).loc["A", "f_score"]
    assert f_raw - f_fix >= 2                              # F3 (ROA) and F9 (turnover)


def test_loader_converts_using_only_point_in_time_quarters(tmp_path):
    """The real loader against a temporary SQLite DB: the prior-year SimFin row becomes
    12 months, and a quarter that only became available AFTER the prior date is not
    used (no look-ahead)."""
    from datetime import timedelta
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from data.storage import Base, Fundamental
    from factors.calculator import _load_prior_year_fundamentals
    eng = create_engine(f"sqlite:///{tmp_path / 'f.db'}")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    as_of = date(2026, 9, 21)
    prior_date = as_of - timedelta(days=365)                       # 2025-09-21

    def add(fy, fq, ni, avail, filed=True):
        pe = date(fy, fq * 3, 28)
        s.add(Fundamental(ticker="A", fiscal_year=fy, fiscal_quarter=fq,
                          period_end_date=pe, filing_date=pe if filed else None,
                          available_date=avail, net_income=ni, revenue=100,
                          gross_profit=50, operating_cash_flow=ni, total_assets=1000))
    add(2024, 3, 10, date(2024, 12, 26)); add(2024, 4, 10, date(2025, 3, 28))
    add(2025, 1, 10, date(2025, 6, 26)); add(2025, 2, 10, date(2025, 9, 1))
    add(2025, 3, 999, date(2025, 12, 27))                          # available later
    s.commit()
    out = _load_prior_year_fundamentals(["A"], as_of, s)
    r = out.iloc[0]
    assert (r["fiscal_year"], r["fiscal_quarter"]) == (2025, 2)
    assert r["net_income"] == 40 and r["revenue"] == 400            # 4 quarters, not 999
    s.close()


def test_loader_never_sums_a_quarter_filed_after_the_prior_date(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from data.storage import Base, Fundamental
    from factors.calculator import _load_prior_year_fundamentals
    eng = create_engine(f"sqlite:///{tmp_path / 'g.db'}")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()

    def add(fy, fq, ni, avail):
        pe = date(fy, fq * 3, 28)
        s.add(Fundamental(ticker="A", fiscal_year=fy, fiscal_quarter=fq, period_end_date=pe,
                          filing_date=pe, available_date=avail, net_income=ni, revenue=100,
                          gross_profit=50, operating_cash_flow=ni, total_assets=1000))
    add(2024, 3, 500, date(2025, 10, 1))      # in the window, but filed AFTER 2025-09-21
    add(2024, 4, 10, date(2025, 3, 28)); add(2025, 1, 10, date(2025, 6, 26))
    add(2025, 2, 10, date(2025, 9, 1))
    s.commit()
    r = _load_prior_year_fundamentals(["A"], date(2026, 9, 21), s).iloc[0]
    assert r["net_income"] == 10              # left quarterly; the late 500 never summed in
    s.close()
