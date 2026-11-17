"""
factors/value.py

Value factor calculations.

All factors follow the spec in the master prompt exactly:
- EV/EBITDA    (lower = cheaper; NaN if EBITDA <= 0; capped at 100)
- FCF Yield    (higher = better; free_cash_flow / enterprise_value)
- P/B Ratio    (lower = cheaper; NaN if book value <= 0)
- Earnings Yield (higher = better = 1/PE; NaN if earnings <= 0)

RULE 1 ENFORCEMENT: All fundamental data passed here must already be
point-in-time filtered (available_date <= rebalance_date). This module
does not touch dates — the caller enforces the lag.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from loguru import logger


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EV_EBITDA_CAP = 100.0   # Cap extreme values to avoid distortion in ranking


# ---------------------------------------------------------------------------
# Individual factor calculations
# ---------------------------------------------------------------------------

def calc_ev_ebitda(
    fundamentals: pd.DataFrame,
    prices: pd.DataFrame,
) -> pd.Series:
    """
    Enterprise Value / EBITDA.

    EV = market_cap + net_debt
    market_cap = shares_outstanding * adj_close (latest price)

    Lower = cheaper = better (we invert for scoring later).
    Returns NaN for negative EBITDA companies (excluded from value ranking).
    Capped at EV_EBITDA_CAP to prevent extreme outliers from distorting ranks.
    """
    result = {}

    # Get latest price per ticker
    latest_prices = (
        prices.sort_values("date")
        .groupby("ticker")["adj_close"]
        .last()
    )

    for _, row in fundamentals.iterrows():
        ticker = row["ticker"]
        price = latest_prices.get(ticker)

        if price is None or pd.isna(price) or price <= 0:
            result[ticker] = np.nan
            continue

        shares = row.get("shares_outstanding")
        ebitda = row.get("ebitda")
        net_debt = row.get("net_debt", 0)

        if pd.isna(shares) or pd.isna(ebitda) or ebitda <= 0:
            result[ticker] = np.nan
            continue

        market_cap = shares * price
        enterprise_value = market_cap + (net_debt if not pd.isna(net_debt) else 0)

        if enterprise_value <= 0:
            result[ticker] = np.nan
            continue

        ev_ebitda = enterprise_value / ebitda
        result[ticker] = min(ev_ebitda, EV_EBITDA_CAP)

    return pd.Series(result, name="ev_ebitda")


def calc_fcf_yield(
    fundamentals: pd.DataFrame,
    prices: pd.DataFrame,
) -> pd.Series:
    """
    Free Cash Flow Yield = FCF / Enterprise Value.

    Higher = better. Returns NaN if EV <= 0.
    """
    result = {}

    latest_prices = (
        prices.sort_values("date")
        .groupby("ticker")["adj_close"]
        .last()
    )

    for _, row in fundamentals.iterrows():
        ticker = row["ticker"]
        price = latest_prices.get(ticker)

        if price is None or pd.isna(price) or price <= 0:
            result[ticker] = np.nan
            continue

        shares = row.get("shares_outstanding")
        fcf = row.get("free_cash_flow")
        net_debt = row.get("net_debt", 0)

        if pd.isna(shares) or pd.isna(fcf):
            result[ticker] = np.nan
            continue

        market_cap = shares * price
        enterprise_value = market_cap + (net_debt if not pd.isna(net_debt) else 0)

        if enterprise_value <= 0:
            result[ticker] = np.nan
            continue

        result[ticker] = fcf / enterprise_value

    return pd.Series(result, name="fcf_yield")


def calc_pb_ratio(
    fundamentals: pd.DataFrame,
    prices: pd.DataFrame,
) -> pd.Series:
    """
    Price-to-Book = price_per_share / book_value_per_share.

    Lower = cheaper = better.
    Returns NaN if book value is <= 0 (negative equity excluded per EXCLUSION_FILTERS).
    """
    result = {}

    latest_prices = (
        prices.sort_values("date")
        .groupby("ticker")["adj_close"]
        .last()
    )

    for _, row in fundamentals.iterrows():
        ticker = row["ticker"]
        price = latest_prices.get(ticker)

        if price is None or pd.isna(price) or price <= 0:
            result[ticker] = np.nan
            continue

        bvps = row.get("book_value_per_share")

        if pd.isna(bvps) or bvps <= 0:
            result[ticker] = np.nan
            continue

        result[ticker] = price / bvps

    return pd.Series(result, name="pb_ratio")


def calc_earnings_yield(
    fundamentals: pd.DataFrame,
    prices: pd.DataFrame,
) -> pd.Series:
    """
    Earnings Yield = EPS diluted / price = 1 / P/E ratio.

    Higher = better (cheap stock with strong earnings).
    Returns NaN if EPS is negative or zero.
    """
    result = {}

    latest_prices = (
        prices.sort_values("date")
        .groupby("ticker")["adj_close"]
        .last()
    )

    for _, row in fundamentals.iterrows():
        ticker = row["ticker"]
        price = latest_prices.get(ticker)

        if price is None or pd.isna(price) or price <= 0:
            result[ticker] = np.nan
            continue

        eps = row.get("eps_diluted")

        if pd.isna(eps) or eps <= 0:
            result[ticker] = np.nan
            continue

        result[ticker] = eps / price

    return pd.Series(result, name="earnings_yield")


def calc_pe_ratio(
    fundamentals: pd.DataFrame,
    prices: pd.DataFrame,
) -> pd.Series:
    """
    Price-to-Earnings = price / EPS diluted.

    Lower = cheaper = better (inverted for scoring).
    Returns NaN if EPS is negative.
    """
    result = {}

    latest_prices = (
        prices.sort_values("date")
        .groupby("ticker")["adj_close"]
        .last()
    )

    for _, row in fundamentals.iterrows():
        ticker = row["ticker"]
        price = latest_prices.get(ticker)

        if price is None or pd.isna(price) or price <= 0:
            result[ticker] = np.nan
            continue

        eps = row.get("eps_diluted")

        if pd.isna(eps) or eps <= 0:
            result[ticker] = np.nan
            continue

        result[ticker] = price / eps

    return pd.Series(result, name="pe_ratio")


# ---------------------------------------------------------------------------
# Combined value factor calculation
# ---------------------------------------------------------------------------

def calculate_value_factors(
    fundamentals: pd.DataFrame,
    prices: pd.DataFrame,
) -> pd.DataFrame:
    """
    Calculate all value factors for a universe snapshot.

    Args:
        fundamentals: Point-in-time fundamental data (one row per ticker,
                      already filtered to available_date <= rebalance_date).
        prices:       Price data covering the rebalance date window.

    Returns:
        DataFrame indexed by ticker with columns:
            ev_ebitda, fcf_yield, pb_ratio, earnings_yield, pe_ratio
    """
    if fundamentals.empty:
        logger.warning("value: empty fundamentals DataFrame passed")
        return pd.DataFrame()

    if prices.empty:
        logger.warning("value: empty prices DataFrame passed")
        return pd.DataFrame()

    tickers = fundamentals["ticker"].unique()
    logger.debug(f"Calculating value factors for {len(tickers)} tickers")

    ev_ebitda     = calc_ev_ebitda(fundamentals, prices)
    fcf_yield     = calc_fcf_yield(fundamentals, prices)
    pb_ratio      = calc_pb_ratio(fundamentals, prices)
    earnings_yield = calc_earnings_yield(fundamentals, prices)
    pe_ratio      = calc_pe_ratio(fundamentals, prices)

    result = pd.DataFrame({
        "ev_ebitda":      ev_ebitda,
        "fcf_yield":      fcf_yield,
        "pb_ratio":       pb_ratio,
        "earnings_yield": earnings_yield,
        "pe_ratio":       pe_ratio,
    })

    coverage = result.notna().mean() * 100
    logger.debug(f"Value factor coverage: {coverage.to_dict()}")

    return result
