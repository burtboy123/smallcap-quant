"""
data/prices.py

Price data fetching and storage using yfinance.

Fetches adjusted OHLCV data and writes it to the prices table via data/storage.py.

Design notes:
- Always fetch adjusted prices (adj_close) to account for splits and dividends.
- Batch tickers to avoid hammering the yfinance API.
- Idempotent: re-running for the same date range simply overwrites existing rows.
- Never fetches future data — end date defaults to yesterday.
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf
from loguru import logger

from data.storage import get_session, upsert_prices, upsert_stock, init_db


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BATCH_SIZE = 50         # yfinance handles ~50 tickers per call efficiently
SLEEP_BETWEEN_BATCHES = 1.0  # seconds — be polite to the API
MAX_RETRIES = 3


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetch_batch(tickers: list[str], start: str, end: str) -> pd.DataFrame:
    """
    Fetch OHLCV data for a batch of tickers from yfinance.
    Returns a normalised DataFrame with columns:
        ticker, date, open, high, low, close, adj_close, volume

    Compatible with yfinance >= 1.0. In v1.x, yf.download() with auto_adjust=True
    returns adjusted OHLCV with MultiIndex columns: (PriceType, Ticker).
    """
    if not tickers:
        return pd.DataFrame()

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            raw = yf.download(
                tickers=tickers,
                start=start,
                end=end,
                auto_adjust=True,   # Returns adjusted prices directly as Close
                actions=False,
                progress=False,
            )
            break
        except Exception as e:
            logger.warning(f"yfinance batch attempt {attempt}/{MAX_RETRIES} failed: {e}")
            if attempt == MAX_RETRIES:
                logger.error(f"All retries exhausted for batch: {tickers[:5]}...")
                return pd.DataFrame()
            time.sleep(2 ** attempt)

    if raw is None or raw.empty:
        return pd.DataFrame()

    # yfinance 1.x always returns MultiIndex columns: (PriceType, Ticker)
    # PriceTypes: Open, High, Low, Close, Volume
    records = []

    # Normalise column index
    if isinstance(raw.columns, pd.MultiIndex):
        # Standard multi-ticker case: columns = (PriceType, Ticker)
        # Also handles single ticker when passed as list
        ticker_level = raw.columns.get_level_values(1)

        for ticker in ticker_level.unique():
            try:
                t_data = raw.xs(ticker, axis=1, level=1).copy()
            except KeyError:
                continue
            t_data.columns = [c.lower().replace(" ", "_") for c in t_data.columns]
            # With auto_adjust=True, 'close' is the adjusted close.
            # Always set adj_close = close so all tickers are consistent after concat.
            t_data["adj_close"] = t_data["close"]
            t_data["ticker"] = ticker
            t_data["date"] = t_data.index.date
            records.append(t_data)
    else:
        # Flat columns — single ticker edge case
        raw = raw.copy()
        raw.columns = [c.lower().replace(" ", "_") for c in raw.columns]
        raw["ticker"] = tickers[0]
        raw["date"] = raw.index.date
        records.append(raw)

    if not records:
        return pd.DataFrame()

    combined = pd.concat(records, ignore_index=True)

    required_cols = ["ticker", "date", "open", "high", "low", "close", "adj_close", "volume"]
    available = [c for c in required_cols if c in combined.columns]
    combined = combined[available].dropna(subset=["adj_close"])
    combined = combined[combined["adj_close"] > 0]

    return combined.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_prices(
    tickers: list[str],
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    store: bool = True,
) -> pd.DataFrame:
    """
    Fetch price history for a list of tickers and optionally store in database.

    Args:
        tickers:    List of ticker symbols.
        start_date: Start of fetch window. Defaults to 20 years ago.
        end_date:   End of fetch window. Defaults to yesterday (never today — market may not be closed).
        store:      If True, writes results to the prices table.

    Returns:
        DataFrame with all price rows fetched.
    """
    if start_date is None:
        start_date = date.today() - timedelta(days=365 * 20)
    if end_date is None:
        end_date = date.today() - timedelta(days=1)

    start_str = start_date.strftime("%Y-%m-%d")
    end_str = (end_date + timedelta(days=1)).strftime("%Y-%m-%d")  # yfinance end is exclusive

    logger.info(
        f"Fetching prices for {len(tickers)} tickers: {start_str} to {end_date.strftime('%Y-%m-%d')}"
    )

    all_frames = []
    batches = [tickers[i : i + BATCH_SIZE] for i in range(0, len(tickers), BATCH_SIZE)]

    for batch_idx, batch in enumerate(batches, 1):
        logger.debug(f"Batch {batch_idx}/{len(batches)}: {len(batch)} tickers")
        df = _fetch_batch(batch, start_str, end_str)
        if not df.empty:
            all_frames.append(df)

        if batch_idx < len(batches):
            time.sleep(SLEEP_BETWEEN_BATCHES)

    if not all_frames:
        logger.warning("No price data fetched.")
        return pd.DataFrame()

    combined = pd.concat(all_frames, ignore_index=True)
    logger.info(f"Fetched {len(combined):,} price rows for {combined['ticker'].nunique()} tickers")

    if store:
        _store_prices(combined)

    return combined


def _store_prices(prices_df: pd.DataFrame) -> None:
    """Write price data to database, creating stock stubs if needed."""
    init_db()
    session = get_session()
    try:
        # Ensure stock rows exist for all tickers
        for ticker in prices_df["ticker"].unique():
            upsert_stock(session, ticker)
        session.commit()

        written = upsert_prices(session, prices_df)
        logger.info(f"Stored {written:,} price rows")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def fetch_benchmark_prices(
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    store: bool = True,
) -> pd.DataFrame:
    """
    Fetch S&P 500 benchmark prices (SPY ETF).
    Called separately from the universe download to keep benchmark data clean.
    """
    logger.info("Fetching benchmark (SPY) prices")
    return fetch_prices(["SPY"], start_date=start_date, end_date=end_date, store=store)


def get_price_coverage(tickers: list[str]) -> pd.DataFrame:
    """
    For each ticker, report the date range and row count available in the database.
    Useful for validating that price data downloaded correctly.
    """
    from data.storage import get_session, Price
    from sqlalchemy import func

    session = get_session()
    try:
        rows = (
            session.query(
                Price.ticker,
                func.min(Price.date).label("earliest"),
                func.max(Price.date).label("latest"),
                func.count(Price.id).label("rows"),
            )
            .filter(Price.ticker.in_(tickers))
            .group_by(Price.ticker)
            .all()
        )
        return pd.DataFrame(
            [{"ticker": r.ticker, "earliest": r.earliest, "latest": r.latest, "rows": r.rows}
             for r in rows]
        )
    finally:
        session.close()


# Alias
fetch_universe_prices = fetch_prices
