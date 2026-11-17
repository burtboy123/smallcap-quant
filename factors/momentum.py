"""
factors/momentum.py

Momentum factor calculations.

Factors:
- 12-1 Month Momentum: price return from T-252 to T-21 trading days
  (skips most recent month to avoid short-term reversal contamination)
- 1-Month Momentum:    most recent month return (reversal signal)
- EPS Revision:        direction of EPS estimate changes (placeholder —
                       requires analyst estimate data not yet available)

RULE 1 ENFORCEMENT: Price data is market data, publicly available in real
time. No reporting lag is needed for price momentum. The caller still
enforces point-in-time discipline for any fundamental inputs.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

from config.settings import MOMENTUM


# ---------------------------------------------------------------------------
# Trading day lookback helpers
# ---------------------------------------------------------------------------

def _get_price_n_days_ago(
    prices_df: pd.DataFrame,
    ticker: str,
    reference_date: date,
    trading_days_back: int,
) -> Optional[float]:
    """
    Return the adjusted close price approximately N trading days before reference_date.

    We look for the closest available trading day within a ±5 day window of the
    target date (handles weekends, holidays, and data gaps).
    """
    ticker_prices = prices_df[prices_df["ticker"] == ticker].sort_values("date")

    if ticker_prices.empty:
        return None

    # All dates up to reference_date
    available = ticker_prices[ticker_prices["date"] <= reference_date]

    if len(available) < trading_days_back:
        return None

    # The Nth row from the end is approximately N trading days ago
    idx = len(available) - trading_days_back
    if idx < 0:
        return None

    price = available.iloc[idx]["adj_close"]
    return float(price) if not pd.isna(price) and price > 0 else None


# ---------------------------------------------------------------------------
# 12-1 Month Momentum
# ---------------------------------------------------------------------------

def calc_momentum_12_1(
    prices: pd.DataFrame,
    rebalance_date: date,
    tickers: list[str],
) -> pd.Series:
    """
    12-1 month price momentum.

    Return from approximately T-252 to T-21 trading days.
    Skip the most recent month to avoid short-term reversal effect.

    Higher = stronger momentum = better.
    Returns NaN if insufficient price history.
    """
    lookback = MOMENTUM["lookback_months"] * 21     # ~252 trading days
    skip = MOMENTUM["skip_months"] * 21             # ~21 trading days

    result = {}

    for ticker in tickers:
        price_12m = _get_price_n_days_ago(prices, ticker, rebalance_date, lookback)
        price_1m  = _get_price_n_days_ago(prices, ticker, rebalance_date, skip)

        if price_12m is None or price_1m is None or price_12m <= 0:
            result[ticker] = np.nan
            continue

        result[ticker] = (price_1m / price_12m) - 1.0

    return pd.Series(result, name="momentum_12_1")


# ---------------------------------------------------------------------------
# 1-Month Momentum (reversal signal — stored but not in composite)
# ---------------------------------------------------------------------------

def calc_momentum_1m(
    prices: pd.DataFrame,
    rebalance_date: date,
    tickers: list[str],
) -> pd.Series:
    """
    Most recent 1-month price return.

    Used as a reversal signal (NOT included in composite score by default).
    Stored in factor_scores table for research and risk management.
    """
    result = {}

    for ticker in tickers:
        price_now = _get_price_n_days_ago(prices, ticker, rebalance_date, 1)
        price_1m  = _get_price_n_days_ago(prices, ticker, rebalance_date, 21)

        if price_now is None or price_1m is None or price_1m <= 0:
            result[ticker] = np.nan
            continue

        result[ticker] = (price_now / price_1m) - 1.0

    return pd.Series(result, name="momentum_1m")


# ---------------------------------------------------------------------------
# EPS Revision (placeholder)
# ---------------------------------------------------------------------------

def calc_eps_revision(
    fundamentals_current: pd.DataFrame,
    fundamentals_prior_3m: pd.DataFrame | None = None,
) -> pd.Series:
    """
    EPS revision signal: direction of change in EPS over past 3 months.

    This uses REPORTED EPS (not analyst estimates) as a proxy.
    A better implementation would use analyst consensus estimates,
    which requires a paid data source (FactSet, Bloomberg, etc.).

    Proxy: compare current quarter EPS to same quarter prior year.
    Positive revision = EPS growing YoY.

    Returns values in range roughly [-1, +1]:
      +1 = EPS doubled or more YoY
       0 = flat
      -1 = EPS halved or worse YoY
    """
    if fundamentals_current.empty:
        return pd.Series(dtype=float, name="eps_revision_3m")

    prior_lookup = (
        {row["ticker"]: row for _, row in fundamentals_prior_3m.iterrows()}
        if fundamentals_prior_3m is not None and not fundamentals_prior_3m.empty
        else {}
    )

    result = {}
    for _, row in fundamentals_current.iterrows():
        ticker = row["ticker"]
        eps_cur = row.get("eps_diluted")
        prior = prior_lookup.get(ticker)

        if prior is None or pd.isna(eps_cur):
            result[ticker] = np.nan
            continue

        eps_prior = prior.get("eps_diluted")
        if pd.isna(eps_prior) or eps_prior == 0:
            result[ticker] = np.nan
            continue

        # Normalised change: clipped to [-1, 1] to prevent outlier distortion
        pct_change = (eps_cur - eps_prior) / abs(eps_prior)
        result[ticker] = float(np.clip(pct_change, -1.0, 1.0))

    return pd.Series(result, name="eps_revision_3m")


# ---------------------------------------------------------------------------
# Combined momentum factor calculation
# ---------------------------------------------------------------------------

def calculate_momentum_factors(
    prices: pd.DataFrame,
    rebalance_date: date,
    tickers: list[str],
    fundamentals_current: pd.DataFrame | None = None,
    fundamentals_prior_1y: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Calculate all momentum factors for a universe snapshot.

    Args:
        prices:                 Full price history for tickers.
        rebalance_date:         The date we are calculating factors for.
        tickers:                Tickers to calculate for.
        fundamentals_current:   Optional — for EPS revision signal.
        fundamentals_prior_1y:  Optional — prior year fundamentals for EPS revision.

    Returns:
        DataFrame indexed by ticker with columns:
            momentum_12_1, momentum_1m, eps_revision_3m
    """
    if not tickers:
        logger.warning("momentum: empty tickers list")
        return pd.DataFrame()

    if prices.empty:
        logger.warning("momentum: empty prices DataFrame")
        return pd.DataFrame()

    logger.debug(f"Calculating momentum factors for {len(tickers)} tickers as of {rebalance_date}")

    mom_12_1 = calc_momentum_12_1(prices, rebalance_date, tickers)
    mom_1m   = calc_momentum_1m(prices, rebalance_date, tickers)

    # EPS revision — optional, falls back to NaN if no fundamental data
    if fundamentals_current is not None and not fundamentals_current.empty:
        eps_rev = calc_eps_revision(fundamentals_current, fundamentals_prior_1y)
    else:
        eps_rev = pd.Series({t: np.nan for t in tickers}, name="eps_revision_3m")

    result = pd.DataFrame({
        "momentum_12_1":   mom_12_1,
        "momentum_1m":     mom_1m,
        "eps_revision_3m": eps_rev,
    })

    coverage = result["momentum_12_1"].notna().mean() * 100
    logger.debug(f"Momentum 12-1 coverage: {coverage:.1f}%")

    return result
