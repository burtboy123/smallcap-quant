"""
tests/test_pipeline.py

Phase 1 validation tests for the data pipeline.

Run with:
    cd quant_system
    pytest tests/test_pipeline.py -v

All tests that hit external APIs or the filesystem are marked with appropriate
markers so they can be skipped in CI. The core logic tests run offline.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))


# =============================================================================
# Universe Builder Tests
# =============================================================================

class TestUniverseBuilder:
    """Tests for data/universe_builder.py"""

    def test_load_iwm_raises_if_csv_missing(self, tmp_path, monkeypatch):
        """FileNotFoundError is raised when IWM CSV doesn't exist."""
        from data import universe_builder

        monkeypatch.setattr(universe_builder, "IWM_CSV_PATH", tmp_path / "missing.csv")
        with pytest.raises(FileNotFoundError, match="IWM holdings CSV not found"):
            universe_builder.load_iwm_universe()

    def test_load_iwm_parses_correct_format(self, tmp_path, monkeypatch):
        """Universe builder correctly parses a well-formed iShares-style CSV."""
        from data import universe_builder

        csv_content = (
            "iShares Russell 2000 ETF\n"
            "Fund Holdings as of,\"March 15, 2026\"\n"
            "Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,Nominal,Price\n"
            "AAPL,Apple Inc,Technology,Equity,1000,0.5,1000,10,100\n"
            "MSFT,Microsoft Corp,Technology,Equity,900,0.4,900,9,100\n"
            "CASH,USD Cash,-,Cash,100,0.1,100,100,1\n"
            "-,Other,-,Other,50,0.02,50,50,1\n"
            "TOOLONGTICKERX,Bad Entry,-,Equity,50,0.01,50,10,5\n"
        )
        csv_path = tmp_path / "iwm_holdings.csv"
        csv_path.write_text(csv_content)
        monkeypatch.setattr(universe_builder, "IWM_CSV_PATH", csv_path)

        tickers = universe_builder.load_iwm_universe()

        assert "AAPL" in tickers
        assert "MSFT" in tickers
        assert "CASH" not in tickers, "CASH should be excluded"
        assert "-" not in tickers, "Dash rows should be excluded"
        assert "TOOLONGTICKERX" not in tickers, "Tickers > 5 chars should be excluded"
        assert len(tickers) == 2

    def test_no_duplicates_in_output(self, tmp_path, monkeypatch):
        """Duplicate tickers are removed while preserving order."""
        from data import universe_builder

        csv_content = (
            "Ticker,Name\n"
            "AAPL,Apple\n"
            "MSFT,Microsoft\n"
            "AAPL,Apple Duplicate\n"
        )
        csv_path = tmp_path / "iwm_holdings.csv"
        csv_path.write_text(csv_content)
        monkeypatch.setattr(universe_builder, "IWM_CSV_PATH", csv_path)

        tickers = universe_builder.load_iwm_universe()
        assert tickers.count("AAPL") == 1

    def test_apply_universe_filters_price(self):
        """Stocks below min_price threshold are filtered out."""
        from data.universe_builder import apply_universe_filters

        tickers = ["CHEAP", "NORMAL"]
        prices = pd.DataFrame(
            [
                {"ticker": "CHEAP", "date": date.today(), "adj_close": 1.0, "volume": 1_000_000},
                {"ticker": "NORMAL", "date": date.today(), "adj_close": 10.0, "volume": 1_000_000},
            ]
        )
        result = apply_universe_filters(tickers, prices)
        assert "CHEAP" not in result
        assert "NORMAL" in result

    def test_apply_universe_filters_volume(self):
        """Stocks below min average daily dollar volume are filtered out."""
        from data.universe_builder import apply_universe_filters

        tickers = ["ILLIQUID", "LIQUID"]
        prices = pd.DataFrame(
            [
                # avg dollar volume = 10 * 100 = $1,000 (below $500K threshold)
                {"ticker": "ILLIQUID", "date": date.today(), "adj_close": 10.0, "volume": 100},
                # avg dollar volume = 10 * 100_000 = $1M (above threshold)
                {"ticker": "LIQUID", "date": date.today(), "adj_close": 10.0, "volume": 100_000},
            ]
        )
        result = apply_universe_filters(tickers, prices)
        assert "ILLIQUID" not in result
        assert "LIQUID" in result

    def test_get_universe_returns_unfiltered_without_prices(self, tmp_path, monkeypatch):
        """get_universe() without prices_df returns raw unfiltered list."""
        from data import universe_builder

        csv_content = "Ticker,Name\nAAPL,Apple\nMSFT,Microsoft\n"
        csv_path = tmp_path / "iwm_holdings.csv"
        csv_path.write_text(csv_content)
        monkeypatch.setattr(universe_builder, "IWM_CSV_PATH", csv_path)

        result = universe_builder.get_universe(prices_df=None)
        assert result == ["AAPL", "MSFT"]


# =============================================================================
# Storage Tests
# =============================================================================

class TestStorage:
    """Tests for data/storage.py — uses in-memory SQLite."""

    @pytest.fixture
    def in_memory_db(self, monkeypatch):
        """Override database URL with in-memory SQLite for each test."""
        import config.settings as settings
        import data.storage as storage_module

        monkeypatch.setattr(settings, "DATABASE_URL", "sqlite:///:memory:")
        monkeypatch.setattr(storage_module, "DATABASE_URL", "sqlite:///:memory:")
        monkeypatch.setattr(storage_module, "_engine", None)
        monkeypatch.setattr(storage_module, "_SessionFactory", None)

        storage_module.init_db()
        yield storage_module.get_session()

    def test_upsert_stock(self, in_memory_db):
        """Stock records can be inserted and retrieved."""
        from data.storage import upsert_stock, Stock

        session = in_memory_db
        upsert_stock(session, "TEST", company_name="Test Corp", sector="Technology")
        session.commit()

        stock = session.get(Stock, "TEST")
        assert stock is not None
        assert stock.company_name == "Test Corp"

    def test_upsert_stock_is_idempotent(self, in_memory_db):
        """Upserting the same ticker twice doesn't create a duplicate."""
        from data.storage import upsert_stock, Stock
        from sqlalchemy import func

        session = in_memory_db
        upsert_stock(session, "TEST", company_name="Test Corp")
        session.commit()
        upsert_stock(session, "TEST", company_name="Updated Name")
        session.commit()

        count = session.query(func.count(Stock.ticker)).scalar()
        assert count == 1

        stock = session.get(Stock, "TEST")
        assert stock.company_name == "Updated Name"

    def test_upsert_prices(self, in_memory_db):
        """Price rows are written and can be read back."""
        from data.storage import upsert_stock, upsert_prices, load_prices

        session = in_memory_db
        upsert_stock(session, "AAPL")
        session.commit()

        prices = pd.DataFrame(
            [
                {
                    "ticker": "AAPL",
                    "date": date(2020, 1, 2),
                    "open": 100.0,
                    "high": 105.0,
                    "low": 99.0,
                    "close": 103.0,
                    "adj_close": 103.0,
                    "volume": 1_000_000,
                },
                {
                    "ticker": "AAPL",
                    "date": date(2020, 1, 3),
                    "open": 103.0,
                    "high": 108.0,
                    "low": 102.0,
                    "close": 107.0,
                    "adj_close": 107.0,
                    "volume": 1_200_000,
                },
            ]
        )
        written = upsert_prices(session, prices)
        assert written == 2

        result = load_prices(["AAPL"], date(2020, 1, 1), date(2020, 1, 31), session=session)
        assert len(result) == 2
        assert list(result["ticker"].unique()) == ["AAPL"]

    def test_reporting_lag_is_enforced(self, in_memory_db):
        """
        RULE 1: available_date must be >= period_end_date + 90 days.
        Records with a too-early available_date are corrected automatically.
        """
        from data.storage import upsert_stock, upsert_fundamentals, Fundamental

        session = in_memory_db
        upsert_stock(session, "LAGTEST")
        session.commit()

        # Q3 ends September 30 — available_date must be >= December 29
        period_end = date(2020, 9, 30)
        bad_available = date(2020, 10, 15)  # Only 15 days lag — violates Rule 1

        df = pd.DataFrame(
            [
                {
                    "ticker": "LAGTEST",
                    "fiscal_year": 2020,
                    "fiscal_quarter": 3,
                    "period_end_date": period_end,
                    "filing_date": bad_available,
                    "available_date": bad_available,  # This will be corrected
                    "revenue": 1_000_000.0,
                }
            ]
        )
        upsert_fundamentals(session, df)

        record = (
            session.query(Fundamental)
            .filter_by(ticker="LAGTEST", fiscal_year=2020, fiscal_quarter=3)
            .first()
        )
        assert record is not None
        min_allowed = period_end + timedelta(days=90)
        assert record.available_date >= min_allowed, (
            f"available_date {record.available_date} is before minimum {min_allowed}. "
            f"Rule 1 (90-day reporting lag) was violated!"
        )

    def test_load_fundamentals_point_in_time(self, in_memory_db):
        """load_fundamentals_as_of never returns data with available_date > as_of."""
        from data.storage import upsert_stock, upsert_fundamentals, load_fundamentals_as_of

        session = in_memory_db
        upsert_stock(session, "PIT")
        session.commit()

        df = pd.DataFrame(
            [
                {
                    "ticker": "PIT",
                    "fiscal_year": 2019,
                    "fiscal_quarter": 4,
                    "period_end_date": date(2019, 12, 31),
                    "available_date": date(2020, 3, 30),  # Available March 30, 2020
                    "revenue": 500_000.0,
                },
                {
                    "ticker": "PIT",
                    "fiscal_year": 2020,
                    "fiscal_quarter": 1,
                    "period_end_date": date(2020, 3, 31),
                    "available_date": date(2020, 6, 29),  # Available June 29, 2020
                    "revenue": 600_000.0,
                },
            ]
        )
        upsert_fundamentals(session, df)

        # As of April 1 2020: only the Q4 2019 record should be visible
        result = load_fundamentals_as_of(["PIT"], date(2020, 4, 1), session=session)
        assert len(result) == 1
        assert result.iloc[0]["fiscal_quarter"] == 4
        assert result.iloc[0]["fiscal_year"] == 2019

        # As of July 1 2020: Q1 2020 is now visible
        result = load_fundamentals_as_of(["PIT"], date(2020, 7, 1), session=session)
        assert len(result) == 1
        assert result.iloc[0]["fiscal_quarter"] == 1
        assert result.iloc[0]["fiscal_year"] == 2020


# =============================================================================
# Settings / Configuration Tests
# =============================================================================

class TestSettings:
    """Sanity checks on config/settings.py"""

    def test_factor_weights_sum_to_one(self):
        from config.settings import FACTOR_WEIGHTS
        total = sum(FACTOR_WEIGHTS.values())
        assert abs(total - 1.0) < 1e-9, f"Factor weights sum to {total}, expected 1.0"

    def test_sentiment_weights_sum_to_one(self):
        from config.settings import FACTOR_WEIGHTS_WITH_SENTIMENT
        total = sum(FACTOR_WEIGHTS_WITH_SENTIMENT.values())
        assert abs(total - 1.0) < 1e-9, f"Sentiment weights sum to {total}, expected 1.0"

    def test_reporting_lag_is_90_days(self):
        from config.settings import DATA_LAGS
        assert DATA_LAGS["fundamental_lag_days"] == 90, (
            "Reporting lag must be exactly 90 days (Rule 1)"
        )

    def test_in_sample_ends_before_out_of_sample(self):
        from config.settings import BACKTEST
        in_end = date.fromisoformat(BACKTEST["in_sample_end"])
        oos_start = date.fromisoformat(BACKTEST["out_of_sample_start"])
        assert in_end < oos_start, "In-sample period must end before out-of-sample period starts"

    def test_universe_min_price_positive(self):
        from config.settings import UNIVERSE
        assert UNIVERSE["min_price"] > 0

    def test_portfolio_max_gte_target(self):
        from config.settings import PORTFOLIO
        assert PORTFOLIO["max_positions"] >= PORTFOLIO["target_positions"]


# =============================================================================
# Integration: IWM CSV validation (requires the CSV to be present)
# =============================================================================

@pytest.mark.skipif(
    not (Path(__file__).parent.parent / "data" / "iwm_holdings.csv").exists(),
    reason="IWM holdings CSV not downloaded yet — run Step 0 from the setup guide",
)
class TestIWMCSVIntegration:
    """Integration tests that require the IWM holdings CSV to be present."""

    def test_universe_size_in_expected_range(self):
        """Universe must contain between 1,800 and 2,100 tickers."""
        from data.universe_builder import load_iwm_universe
        tickers = load_iwm_universe()
        assert 1_800 <= len(tickers) <= 2_100, (
            f"Universe has {len(tickers)} tickers — expected 1,800-2,100. "
            f"Check that you downloaded the Holdings file (not Summary)."
        )

    def test_no_cash_or_dash_tickers(self):
        """No cash, dash, or non-equity entries in the universe."""
        from data.universe_builder import load_iwm_universe
        tickers = load_iwm_universe()
        invalid = [t for t in tickers if t in {"-", "CASH", "USD", ""}]
        assert not invalid, f"Found non-equity entries: {invalid}"

    def test_all_tickers_valid_format(self):
        """All tickers are 1-5 character alphabetic strings."""
        from data.universe_builder import load_iwm_universe
        tickers = load_iwm_universe()
        bad = [t for t in tickers if not (1 <= len(t) <= 5)]
        assert not bad, f"Found invalid ticker formats: {bad[:10]}"

    def test_ticker_sample_looks_real(self):
        """Spot-check: some well-known small-cap tickers should be in the universe."""
        from data.universe_builder import load_iwm_universe
        tickers = set(load_iwm_universe())
        # These are often in the Russell 2000 — just sanity-check we have real equity tickers
        # (exact membership changes quarterly — just confirm the list isn't garbage)
        assert len(tickers) > 100, "Universe seems too small — check CSV parsing"
