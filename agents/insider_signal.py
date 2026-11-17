"""
agents/insider_signal.py

Insider Buying Signal.
Open-market purchases by executives = strongest alignment signal.
Clusters of buying within 90 days predict 4-8% outperformance.
Data sourced via yfinance (SEC Form 4 filings).
"""

from __future__ import annotations

import time

import pandas as pd
import yfinance as yf
from loguru import logger


def get_insider_signal_yfinance(ticker: str) -> dict:
    """
    Get insider transaction data from yfinance.
    Classifies transactions into buys/sells over the last 90 days.
    """
    empty = {
        "ticker":             ticker,
        "insider_buy_count":  0,
        "insider_sell_count": 0,
        "net_insider_shares": 0,
        "insider_buy_value":  0,
        "has_recent_cluster": False,
    }

    try:
        insider_df = yf.Ticker(ticker).insider_transactions

        if insider_df is None or insider_df.empty:
            return empty

        cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=90)

        # yfinance may return a DatetimeIndex or a RangeIndex
        if not isinstance(insider_df.index, pd.DatetimeIndex):
            # Try to find a date column and use it
            date_col = next(
                (c for c in insider_df.columns
                 if "date" in c.lower() or "startdate" in c.lower()),
                None,
            )
            if date_col is None:
                return empty
            insider_df = insider_df.copy()
            insider_df.index = pd.to_datetime(insider_df[date_col], utc=True)
        else:
            insider_df = insider_df.copy()
            idx = insider_df.index
            if idx.tz is None:
                insider_df.index = idx.tz_localize("UTC")
            else:
                insider_df.index = idx.tz_convert("UTC")

        recent = insider_df[insider_df.index >= cutoff]
        if recent.empty:
            return empty

        # Classify via "Text" column if available, else via Shares sign
        if "Text" in recent.columns:
            buy_mask  = recent["Text"].str.contains(r"Purchase|Buy",  case=False, na=False)
            sell_mask = recent["Text"].str.contains(r"Sale|Sell", case=False, na=False)
            buys  = recent[buy_mask]
            sells = recent[sell_mask]
        elif "Shares" in recent.columns:
            buys  = recent[recent["Shares"] > 0]
            sells = recent[recent["Shares"] < 0]
        else:
            return empty

        buy_count  = len(buys)
        sell_count = len(sells)

        buy_shares  = float(buys["Shares"].sum())  if not buys.empty  and "Shares" in buys.columns  else 0.0
        sell_shares = abs(float(sells["Shares"].sum())) if not sells.empty and "Shares" in sells.columns else 0.0
        buy_value   = float(buys["Value"].sum())   if not buys.empty  and "Value"  in buys.columns  else 0.0

        return {
            "ticker":             ticker,
            "insider_buy_count":  buy_count,
            "insider_sell_count": sell_count,
            "net_insider_shares": buy_shares - sell_shares,
            "insider_buy_value":  buy_value,
            "has_recent_cluster": buy_count >= 2,
        }

    except Exception as e:
        logger.debug(f"{ticker}: yfinance insider failed — {e}")
        return empty


def calculate_insider_score(data: dict) -> float:
    """
    Convert insider transaction data to 0-100 score.

    Cluster of open-market buys = high score.
    Heavy selling / no activity = 50 (neutral).
    """
    buy_count   = data.get("insider_buy_count",  0)
    sell_count  = data.get("insider_sell_count", 0)
    net_shares  = data.get("net_insider_shares", 0)
    has_cluster = data.get("has_recent_cluster", False)

    total = buy_count + sell_count
    if total == 0:
        return 50.0   # No activity = neutral

    buy_ratio = buy_count / total

    if   buy_ratio >= 0.8 and has_cluster: score = 90.0
    elif buy_ratio >= 0.7:                 score = 75.0
    elif buy_ratio >= 0.5:                 score = 60.0
    elif buy_ratio >= 0.3:                 score = 35.0
    else:                                  score = 20.0

    if has_cluster:
        score = min(100.0, score + 10.0)

    if net_shares < 0:
        score = max(10.0, score - 15.0)

    return round(score, 2)


def get_insider_batch(tickers: list[str]) -> pd.DataFrame:
    """Fetch insider signals for a list of tickers. Returns DataFrame."""
    results = []
    for i, ticker in enumerate(tickers):
        if i % 50 == 0 and i > 0:
            logger.info(f"Insider signal: {i}/{len(tickers)}")
        data = get_insider_signal_yfinance(ticker)
        data["insider_score"] = calculate_insider_score(data)
        results.append(data)
        time.sleep(0.1)
    return pd.DataFrame(results)
