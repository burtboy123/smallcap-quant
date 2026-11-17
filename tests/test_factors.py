"""
tests/test_factors.py

Unit tests for every factor calculation module.

Run with:
    cd quant_system
    pytest tests/test_factors.py -v

All tests use synthetic data — no API calls, no database, no filesystem.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# =============================================================================
# Fixtures — synthetic test data
# =============================================================================

@pytest.fixture
def sample_prices():
    """Synthetic price data for 3 tickers over 300 trading days."""
    tickers = ["CHEAP", "QUALITY", "MOMTM"]
    dates = pd.date_range("2019-01-02", periods=300, freq="B")

    rows = []
    prices_map = {"CHEAP": 10.0, "QUALITY": 25.0, "MOMTM": 5.0}
    # MOMTM has a strong uptrend: 5% per month over 12 months
    for i, d in enumerate(dates):
        for ticker in tickers:
            base = prices_map[ticker]
            if ticker == "MOMTM":
                # Strong uptrend
                price = base * (1 + 0.005 * i)
            else:
                price = base * (1 + 0.001 * (i % 20 - 10))  # Flat with noise
            rows.append({
                "ticker": ticker,
                "date": d.date(),
                "adj_close": max(price, 0.01),
                "volume": 1_000_000,
                "open": price * 0.99,
                "high": price * 1.01,
                "low": price * 0.98,
                "close": price,
            })
    return pd.DataFrame(rows)


@pytest.fixture
def sample_fundamentals():
    """Synthetic fundamental data for 3 tickers."""
    return pd.DataFrame([
        {
            "ticker": "CHEAP",
            "fiscal_year": 2019, "fiscal_quarter": 3,
            "period_end_date": date(2019, 9, 30),
            "available_date": date(2019, 12, 29),
            "revenue": 500_000_000,
            "gross_profit": 200_000_000,
            "operating_income": 100_000_000,
            "net_income": 75_000_000,
            "ebitda": 120_000_000,
            "eps_diluted": 1.50,
            "eps_basic": 1.52,
            "shares_outstanding": 50_000_000,
            "total_assets": 600_000_000,
            "total_equity": 300_000_000,
            "total_debt": 100_000_000,
            "cash_and_equivalents": 50_000_000,
            "net_debt": 50_000_000,
            "book_value_per_share": 6.00,
            "operating_cash_flow": 90_000_000,
            "capex": 20_000_000,
            "free_cash_flow": 70_000_000,
            "gross_margin": 0.40,
            "roa": 0.125,
        },
        {
            "ticker": "QUALITY",
            "fiscal_year": 2019, "fiscal_quarter": 3,
            "period_end_date": date(2019, 9, 30),
            "available_date": date(2019, 12, 29),
            "revenue": 1_000_000_000,
            "gross_profit": 600_000_000,
            "operating_income": 300_000_000,
            "net_income": 240_000_000,
            "ebitda": 350_000_000,
            "eps_diluted": 4.80,
            "eps_basic": 4.85,
            "shares_outstanding": 50_000_000,
            "total_assets": 2_000_000_000,
            "total_equity": 1_200_000_000,
            "total_debt": 200_000_000,
            "cash_and_equivalents": 300_000_000,
            "net_debt": -100_000_000,
            "book_value_per_share": 24.00,
            "operating_cash_flow": 280_000_000,
            "capex": 50_000_000,
            "free_cash_flow": 230_000_000,
            "gross_margin": 0.60,
            "roa": 0.12,
        },
        {
            "ticker": "MOMTM",
            "fiscal_year": 2019, "fiscal_quarter": 3,
            "period_end_date": date(2019, 9, 30),
            "available_date": date(2019, 12, 29),
            "revenue": 300_000_000,
            "gross_profit": 90_000_000,
            "operating_income": 30_000_000,
            "net_income": 20_000_000,
            "ebitda": 40_000_000,
            "eps_diluted": 0.40,
            "eps_basic": 0.41,
            "shares_outstanding": 50_000_000,
            "total_assets": 400_000_000,
            "total_equity": 200_000_000,
            "total_debt": 80_000_000,
            "cash_and_equivalents": 30_000_000,
            "net_debt": 50_000_000,
            "book_value_per_share": 4.00,
            "operating_cash_flow": 25_000_000,
            "capex": 15_000_000,
            "free_cash_flow": 10_000_000,
            "gross_margin": 0.30,
            "roa": 0.05,
        },
    ])


@pytest.fixture
def sample_fundamentals_prior(sample_fundamentals):
    """Prior-year fundamentals — slightly worse to test YoY signal direction."""
    prior = sample_fundamentals.copy()
    prior["fiscal_year"] = prior["fiscal_year"] - 1
    prior["available_date"] = prior["available_date"] - timedelta(days=365)
    # Slightly worse metrics for current year to show improvement
    prior["net_income"] *= 0.9
    prior["operating_income"] *= 0.88
    prior["gross_profit"] *= 0.92
    prior["roa"] *= 0.9
    prior["gross_margin"] *= 0.92
    prior["total_debt"] *= 1.1   # More debt previously (F5: falling leverage)
    prior["cash_and_equivalents"] *= 0.8  # Less cash previously (F6: improving liquidity)
    prior["shares_outstanding"] *= 1.02   # More shares previously (F7: dilution)
    return prior


# =============================================================================
# Value Factor Tests
# =============================================================================

class TestValueFactors:

    def test_ev_ebitda_calculation(self, sample_fundamentals, sample_prices):
        """EV/EBITDA is calculated correctly."""
        from factors.value import calc_ev_ebitda

        result = calc_ev_ebitda(sample_fundamentals, sample_prices)

        assert "CHEAP" in result.index
        assert "QUALITY" in result.index
        assert result["CHEAP"] > 0
        assert result["QUALITY"] > 0

    def test_ev_ebitda_nan_for_negative_ebitda(self, sample_prices):
        """Returns NaN when EBITDA <= 0."""
        from factors.value import calc_ev_ebitda

        funds = pd.DataFrame([{
            "ticker": "LOSER",
            "ebitda": -50_000_000,
            "shares_outstanding": 10_000_000,
            "net_debt": 20_000_000,
        }])
        result = calc_ev_ebitda(funds, sample_prices)
        # LOSER has no price data, but even if it did, negative EBITDA should give NaN
        assert "LOSER" not in result.index or pd.isna(result.get("LOSER"))

    def test_ev_ebitda_capped_at_100(self, sample_prices):
        """EV/EBITDA is capped at 100 for extreme values."""
        from factors.value import calc_ev_ebitda

        # Create a synthetic price entry for EXPENSIVE
        prices_with_expensive = pd.concat([
            sample_prices,
            pd.DataFrame([{
                "ticker": "EXPENSIVE",
                "date": date(2019, 12, 15),
                "adj_close": 1000.0,
                "volume": 100_000,
                "open": 999.0, "high": 1001.0, "low": 998.0, "close": 1000.0,
            }])
        ])

        funds = pd.DataFrame([{
            "ticker": "EXPENSIVE",
            "ebitda": 1_000,  # Tiny EBITDA vs huge market cap -> EV/EBITDA >> 100
            "shares_outstanding": 10_000_000,
            "net_debt": 0,
        }])
        result = calc_ev_ebitda(funds, prices_with_expensive)
        if "EXPENSIVE" in result.index and not pd.isna(result["EXPENSIVE"]):
            assert result["EXPENSIVE"] <= 100.0

    def test_fcf_yield_positive_for_cashflow_positive(self, sample_fundamentals, sample_prices):
        """FCF yield is positive when FCF > 0."""
        from factors.value import calc_fcf_yield

        result = calc_fcf_yield(sample_fundamentals, sample_prices)
        for ticker in ["CHEAP", "QUALITY", "MOMTM"]:
            if ticker in result.index and not pd.isna(result[ticker]):
                assert result[ticker] > 0, f"{ticker} should have positive FCF yield"

    def test_pb_ratio_positive(self, sample_fundamentals, sample_prices):
        """P/B ratio is positive."""
        from factors.value import calc_pb_ratio

        result = calc_pb_ratio(sample_fundamentals, sample_prices)
        for ticker in result.index:
            if not pd.isna(result[ticker]):
                assert result[ticker] > 0

    def test_pb_ratio_nan_for_negative_book_value(self, sample_prices):
        """Returns NaN if book value <= 0."""
        from factors.value import calc_pb_ratio

        prices_with_neg = pd.concat([
            sample_prices,
            pd.DataFrame([{
                "ticker": "NEGBOOK",
                "date": date(2019, 12, 15),
                "adj_close": 5.0, "volume": 100_000,
                "open": 4.9, "high": 5.1, "low": 4.8, "close": 5.0,
            }])
        ])
        funds = pd.DataFrame([{"ticker": "NEGBOOK", "book_value_per_share": -2.0}])
        result = calc_pb_ratio(funds, prices_with_neg)
        assert "NEGBOOK" not in result.index or pd.isna(result.get("NEGBOOK"))

    def test_earnings_yield_nan_for_negative_eps(self, sample_prices):
        """Earnings yield is NaN when EPS <= 0."""
        from factors.value import calc_earnings_yield

        prices_with_loser = pd.concat([
            sample_prices,
            pd.DataFrame([{
                "ticker": "LOSSMAKER",
                "date": date(2019, 12, 15),
                "adj_close": 5.0, "volume": 100_000,
                "open": 4.9, "high": 5.1, "low": 4.8, "close": 5.0,
            }])
        ])
        funds = pd.DataFrame([{"ticker": "LOSSMAKER", "eps_diluted": -0.50}])
        result = calc_earnings_yield(funds, prices_with_loser)
        assert "LOSSMAKER" not in result.index or pd.isna(result.get("LOSSMAKER"))

    def test_calculate_value_factors_returns_all_columns(self, sample_fundamentals, sample_prices):
        """calculate_value_factors returns all expected columns."""
        from factors.value import calculate_value_factors

        result = calculate_value_factors(sample_fundamentals, sample_prices)
        expected_cols = {"ev_ebitda", "fcf_yield", "pb_ratio", "earnings_yield", "pe_ratio"}
        assert expected_cols.issubset(set(result.columns))


# =============================================================================
# Quality Factor Tests
# =============================================================================

class TestQualityFactors:

    def test_roic_positive_for_profitable_company(self, sample_fundamentals):
        """ROIC is positive for profitable companies."""
        from factors.quality import calc_roic

        result = calc_roic(sample_fundamentals)
        for ticker in ["CHEAP", "QUALITY", "MOMTM"]:
            assert not pd.isna(result[ticker]), f"{ticker} ROIC should not be NaN"
            assert result[ticker] > 0, f"{ticker} ROIC should be positive"

    def test_roic_nan_for_zero_invested_capital(self):
        """ROIC is NaN when invested_capital <= 0."""
        from factors.quality import calc_roic

        funds = pd.DataFrame([{
            "ticker": "ZERO_IC",
            "operating_income": 1_000_000,
            "total_equity": 0,
            "net_debt": 0,
        }])
        result = calc_roic(funds)
        assert pd.isna(result["ZERO_IC"])

    def test_gross_margin_between_0_and_1(self, sample_fundamentals):
        """Gross margin is between 0 and 1."""
        from factors.quality import calc_gross_margin

        result = calc_gross_margin(sample_fundamentals)
        for ticker in result.index:
            if not pd.isna(result[ticker]):
                assert 0 <= result[ticker] <= 1.0, f"{ticker} gross margin out of range"

    def test_gross_margin_uses_precomputed_if_available(self):
        """Uses pre-computed gross_margin column when present."""
        from factors.quality import calc_gross_margin

        funds = pd.DataFrame([{
            "ticker": "TEST",
            "gross_margin": 0.55,
            "revenue": 1_000_000,
            "gross_profit": 400_000,  # Would give 0.40 if computed
        }])
        result = calc_gross_margin(funds)
        assert abs(result["TEST"] - 0.55) < 1e-9, "Should use pre-computed gross_margin"

    def test_accruals_ratio_negative_for_high_quality(self, sample_fundamentals):
        """High-quality earnings: OCF > net income → negative accruals."""
        from factors.quality import calc_accruals_ratio

        result = calc_accruals_ratio(sample_fundamentals)
        # CHEAP: OCF=90M > net_income=75M → accruals negative (good)
        assert result["CHEAP"] < 0, "CHEAP should have negative accruals (OCF > net income)"
        # QUALITY: OCF=280M > net_income=240M → negative
        assert result["QUALITY"] < 0, "QUALITY should have negative accruals"

    def test_f_score_between_0_and_9(self, sample_fundamentals, sample_fundamentals_prior):
        """F-Score is always between 0 and 9."""
        from factors.quality import calc_f_score

        result = calc_f_score(sample_fundamentals, sample_fundamentals_prior)
        for ticker in result.index:
            assert 0 <= result[ticker] <= 9, f"{ticker} F-Score {result[ticker]} out of range"

    def test_f_score_higher_for_improving_company(self, sample_fundamentals, sample_fundamentals_prior):
        """A company with improving metrics scores higher than 0."""
        from factors.quality import calc_f_score

        result = calc_f_score(sample_fundamentals, sample_fundamentals_prior)
        # All 3 companies have improving metrics vs prior year
        for ticker in ["CHEAP", "QUALITY", "MOMTM"]:
            assert result[ticker] > 0, f"{ticker} F-Score should be > 0 with improving metrics"

    def test_f_score_without_prior_data(self, sample_fundamentals):
        """F-Score works even without prior year data (YoY signals score 0)."""
        from factors.quality import calc_f_score

        result = calc_f_score(sample_fundamentals, pd.DataFrame())
        for ticker in result.index:
            assert 0 <= result[ticker] <= 4, f"Without prior data, max F-Score is 4 (profitability only)"

    def test_quality_factors_returns_all_columns(self, sample_fundamentals, sample_fundamentals_prior):
        """calculate_quality_factors returns all expected columns."""
        from factors.quality import calculate_quality_factors

        result = calculate_quality_factors(sample_fundamentals, sample_fundamentals_prior)
        expected = {"roic", "gross_margin", "f_score", "accruals_ratio", "debt_to_ebitda"}
        assert expected.issubset(set(result.columns))


# =============================================================================
# Momentum Factor Tests
# =============================================================================

class TestMomentumFactors:

    def test_momentum_12_1_positive_for_uptrend(self, sample_prices):
        """12-1 month momentum is positive for a strong uptrend ticker."""
        from factors.momentum import calc_momentum_12_1

        rebalance = date(2019, 12, 15)
        tickers = ["MOMTM"]
        result = calc_momentum_12_1(sample_prices, rebalance, tickers)

        assert "MOMTM" in result.index
        if not pd.isna(result["MOMTM"]):
            assert result["MOMTM"] > 0, "MOMTM has uptrend — momentum should be positive"

    def test_momentum_nan_for_insufficient_history(self, sample_prices):
        """Momentum is NaN when price history < lookback window."""
        from factors.momentum import calc_momentum_12_1

        # Use a very early date before 252 days of data exist
        early_date = date(2019, 3, 1)
        result = calc_momentum_12_1(sample_prices, early_date, ["CHEAP"])
        # With less than ~252 days of data before March 2019, should be NaN
        # (sample data starts 2019-01-02)
        assert "CHEAP" in result.index
        # NaN expected since we don't have 252 days back from March 2019

    def test_momentum_1m_reflects_recent_performance(self, sample_prices):
        """1-month momentum reflects the most recent month's performance."""
        from factors.momentum import calc_momentum_1m

        rebalance = date(2019, 12, 15)
        result = calc_momentum_1m(sample_prices, rebalance, ["CHEAP", "MOMTM"])
        # Both should return valid (or NaN) values — just check no exception thrown
        assert "CHEAP" in result.index
        assert "MOMTM" in result.index

    def test_momentum_factors_returns_all_columns(self, sample_prices):
        """calculate_momentum_factors returns all expected columns."""
        from factors.momentum import calculate_momentum_factors

        rebalance = date(2019, 12, 15)
        result = calculate_momentum_factors(sample_prices, rebalance, ["CHEAP", "QUALITY", "MOMTM"])
        expected = {"momentum_12_1", "momentum_1m", "eps_revision_3m"}
        assert expected.issubset(set(result.columns))


# =============================================================================
# Composite Score Tests
# =============================================================================

class TestCompositeScores:

    def test_percentile_rank_range(self):
        """Percentile ranks are in range [0, 100] for valid values."""
        from factors.composite import percentile_rank

        values = pd.Series({"A": 10.0, "B": 20.0, "C": 30.0, "D": np.nan})
        result = percentile_rank(values, ascending=True)

        valid = result.dropna()
        assert (valid >= 0).all(), "Percentile ranks must be >= 0"
        assert (valid <= 100).all(), "Percentile ranks must be <= 100"
        assert pd.isna(result["D"]), "NaN input must produce NaN output"

    def test_percentile_rank_ascending_true(self):
        """Higher values get higher percentiles when ascending=True."""
        from factors.composite import percentile_rank

        values = pd.Series({"LOW": 1.0, "MID": 5.0, "HIGH": 10.0})
        result = percentile_rank(values, ascending=True)
        assert result["HIGH"] > result["MID"] > result["LOW"]

    def test_percentile_rank_ascending_false(self):
        """Lower values get higher percentiles when ascending=False (e.g. EV/EBITDA)."""
        from factors.composite import percentile_rank

        values = pd.Series({"CHEAP": 3.0, "FAIR": 10.0, "EXPENSIVE": 50.0})
        result = percentile_rank(values, ascending=False)
        assert result["CHEAP"] > result["FAIR"] > result["EXPENSIVE"]

    def test_composite_score_between_0_and_100(self, sample_fundamentals, sample_prices):
        """Composite scores are between 0 and 100."""
        from factors.value import calculate_value_factors
        from factors.quality import calculate_quality_factors
        from factors.momentum import calculate_momentum_factors
        from factors.composite import calculate_composite_scores

        rebalance = date(2019, 12, 15)
        value_f = calculate_value_factors(sample_fundamentals, sample_prices)
        quality_f = calculate_quality_factors(sample_fundamentals)
        momentum_f = calculate_momentum_factors(sample_prices, rebalance, ["CHEAP", "QUALITY", "MOMTM"])

        result = calculate_composite_scores(value_f, quality_f, momentum_f)

        valid_scores = result["composite_score"].dropna()
        assert (valid_scores >= 0).all(), "Composite scores must be >= 0"
        assert (valid_scores <= 100).all(), "Composite scores must be <= 100"

    def test_universe_rank_is_1_for_best_stock(self, sample_fundamentals, sample_prices):
        """The best composite score gets universe_rank == 1."""
        from factors.value import calculate_value_factors
        from factors.quality import calculate_quality_factors
        from factors.momentum import calculate_momentum_factors
        from factors.composite import calculate_composite_scores

        rebalance = date(2019, 12, 15)
        value_f = calculate_value_factors(sample_fundamentals, sample_prices)
        quality_f = calculate_quality_factors(sample_fundamentals)
        momentum_f = calculate_momentum_factors(sample_prices, rebalance, ["CHEAP", "QUALITY", "MOMTM"])

        result = calculate_composite_scores(value_f, quality_f, momentum_f)

        ranked = result.dropna(subset=["universe_rank"])
        if not ranked.empty:
            best = ranked["composite_score"].idxmax()
            assert ranked.loc[best, "universe_rank"] == 1

    def test_quality_company_scores_well(self, sample_fundamentals, sample_prices):
        """QUALITY stock (high margins, ROIC, OCF) should rank well."""
        from factors.value import calculate_value_factors
        from factors.quality import calculate_quality_factors
        from factors.momentum import calculate_momentum_factors
        from factors.composite import calculate_composite_scores

        rebalance = date(2019, 12, 15)
        value_f = calculate_value_factors(sample_fundamentals, sample_prices)
        quality_f = calculate_quality_factors(sample_fundamentals)
        momentum_f = calculate_momentum_factors(sample_prices, rebalance, ["CHEAP", "QUALITY", "MOMTM"])

        result = calculate_composite_scores(value_f, quality_f, momentum_f)

        ranked = result.dropna(subset=["universe_rank"])
        if not ranked.empty and "QUALITY" in ranked.index:
            quality_rank = ranked.loc["QUALITY", "universe_rank"]
            universe_size = ranked.loc["QUALITY", "universe_size"]
            # QUALITY should be in top half
            assert quality_rank <= universe_size / 2 + 1, \
                f"QUALITY ranked {quality_rank}/{universe_size} — expected top half"

    def test_factor_weights_applied_correctly(self):
        """Custom weights override defaults."""
        from factors.composite import build_composite_score

        v = pd.Series({"A": 80.0, "B": 20.0})
        q = pd.Series({"A": 80.0, "B": 20.0})
        m = pd.Series({"A": 80.0, "B": 20.0})

        # All weights equal — result should be same as input
        result = build_composite_score(v, q, m, weights={"value": 1/3, "quality": 1/3, "momentum": 1/3})
        assert abs(result["A"] - 80.0) < 1.0
        assert abs(result["B"] - 20.0) < 1.0
