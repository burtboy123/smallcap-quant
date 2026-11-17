"""
agents/short_interest.py

Short Interest Signal.
High and rising short interest = bearish flag.
Low and declining short interest = bullish confirmation.
Available free via yfinance.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import yfinance as yf
from loguru import logger


def get_short_interest(ticker: str) -> dict:
    """
    Fetch short interest data for a single ticker.
    Returns short_ratio, short_pct_float, and MoM change direction.
    """
    try:
        info = yf.Ticker(ticker).info

        short_ratio      = info.get("shortRatio")         # Days to cover
        short_pct_float  = info.get("shortPercentOfFloat") # % of float shorted
        shares_short     = info.get("sharesShort")
        shares_short_prior = info.get("sharesShortPriorMonth")

        short_change = None
        if shares_short and shares_short_prior and shares_short_prior > 0:
            short_change = (shares_short - shares_short_prior) / shares_short_prior

        return {
            "ticker":          ticker,
            "short_ratio":     short_ratio,
            "short_pct_float": short_pct_float,
            "short_change":    short_change,
            "as_of":           str(date.today()),
        }
    except Exception as e:
        logger.warning(f"{ticker}: short interest fetch failed — {e}")
        return {
            "ticker": ticker,
            "short_ratio": None,
            "short_pct_float": None,
            "short_change": None,
        }


def calculate_short_interest_score(data: dict) -> float:
    """
    Convert short interest data to 0-100 score.

    Low short interest + declining = bullish = high score.
    High short interest + rising   = bearish = low score.

    Components:
      - % of float shorted (50% weight)
      - MoM change direction    (50% weight)
    """
    short_pct   = data.get("short_pct_float")
    short_change = data.get("short_change")

    # Level component
    if short_pct is not None:
        if short_pct < 0.03:    pct_score = 85.0   # <3%  very bullish
        elif short_pct < 0.07:  pct_score = 65.0   # 3-7% mildly bullish
        elif short_pct < 0.15:  pct_score = 45.0   # 7-15% neutral
        elif short_pct < 0.25:  pct_score = 25.0   # 15-25% bearish
        else:                   pct_score = 15.0   # >25% very bearish
    else:
        pct_score = 50.0

    # Direction component
    if short_change is not None:
        if short_change < -0.10:   change_score = 85.0   # Aggressively covering
        elif short_change < -0.05: change_score = 65.0   # Declining
        elif short_change < 0.05:  change_score = 50.0   # Stable
        elif short_change < 0.15:  change_score = 30.0   # Building
        else:                      change_score = 15.0   # Surging
    else:
        change_score = 50.0

    return round(pct_score * 0.5 + change_score * 0.5, 2)


def get_short_interest_batch(tickers: list[str]) -> pd.DataFrame:
    """Fetch short interest for a list of tickers. Returns DataFrame."""
    results = []
    for i, ticker in enumerate(tickers):
        if i % 50 == 0 and i > 0:
            logger.info(f"Short interest: {i}/{len(tickers)}")
        data = get_short_interest(ticker)
        data["short_interest_score"] = calculate_short_interest_score(data)
        results.append(data)
    return pd.DataFrame(results)
