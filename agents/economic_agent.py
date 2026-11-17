"""
Economic Agent — monitors interest rates, yield curve,
credit spreads, and leading economic indicators.
All data from FRED (free).
Feeds into macro regime detection.
"""

from __future__ import annotations

import time
from datetime import date, timedelta

import pandas as pd
from loguru import logger


# ---------------------------------------------------------------------------
# FRED fetch helpers
# ---------------------------------------------------------------------------

def get_fred_series(series_id: str, lookback_days: int = 365) -> pd.Series:
    """Fetch a single FRED data series (single attempt)."""
    try:
        from fredapi import Fred
        from config.settings import FRED_API_KEY
        if not FRED_API_KEY or FRED_API_KEY == "your_key_here":
            logger.warning("FRED_API_KEY not set — skipping FRED data")
            return pd.Series(dtype=float)

        fred = Fred(api_key=FRED_API_KEY)
        start = (date.today() - timedelta(days=lookback_days)).isoformat()
        data = fred.get_series(series_id, observation_start=start)
        return data.dropna()
    except Exception as e:
        logger.warning(f"FRED {series_id} fetch failed — {e}")
        return pd.Series(dtype=float)


def get_fred_series_with_retry(
    series_id: str,
    lookback_days: int = 365,
    max_retries: int = 3,
) -> pd.Series:
    """Fetch FRED series with exponential backoff on rate-limit failures."""
    for attempt in range(max_retries):
        result = get_fred_series(series_id, lookback_days)
        if not result.empty:
            return result
        if attempt < max_retries - 1:
            wait = 2 ** attempt  # 1s, 2s, 4s
            logger.warning(f"FRED {series_id} rate limited — retrying in {wait}s ({attempt+1}/{max_retries})")
            time.sleep(wait)
    logger.error(f"FRED {series_id} failed after {max_retries} retries")
    return pd.Series(dtype=float)


# ---------------------------------------------------------------------------
# Individual indicators
# ---------------------------------------------------------------------------

def get_yield_curve() -> dict:
    """
    10Y-2Y Treasury spread (T10Y2Y). Negative = inverted = recession warning.
    """
    spread = get_fred_series_with_retry("T10Y2Y", lookback_days=365)

    if spread.empty:
        return {
            "yield_curve_spread": None,
            "yield_curve_inverted": False,
            "yield_curve_trend": "unknown",
            "recession_warning": False,
            "spread_1m_change": None,
        }

    current = float(spread.iloc[-1])
    month_ago = float(spread.iloc[-21]) if len(spread) > 21 else current
    trend = "steepening" if current > month_ago else "flattening"

    # Sustained inversion (40+ of last 63 trading days) = strong recession warning
    recent_90d = spread.tail(63)
    sustained_inversion = (recent_90d < 0).sum() > 40

    return {
        "yield_curve_spread": round(current, 3),
        "yield_curve_inverted": current < 0,
        "yield_curve_trend": trend,
        "recession_warning": sustained_inversion,
        "spread_1m_change": round(current - month_ago, 3),
    }


def get_real_fed_funds_rate() -> dict:
    """
    Real Fed Funds Rate = Fed Funds Rate - CPI YoY.
    Positive and rising = restrictive = bearish small-caps.
    """
    fed_funds = get_fred_series_with_retry("FEDFUNDS", lookback_days=180)
    cpi_yoy   = get_fred_series_with_retry("CPIAUCSL", lookback_days=500)

    result: dict = {
        "fed_funds_rate": None,
        "cpi_yoy": None,
        "real_rate": None,
        "real_rate_restrictive": False,
        "real_rate_trend": "unknown",
    }

    if not fed_funds.empty:
        result["fed_funds_rate"] = round(float(fed_funds.iloc[-1]), 2)

    if not cpi_yoy.empty and len(cpi_yoy) > 12:
        latest   = float(cpi_yoy.iloc[-1])
        year_ago = float(cpi_yoy.iloc[-13])
        yoy = (latest - year_ago) / year_ago * 100
        result["cpi_yoy"] = round(yoy, 2)

    if result["fed_funds_rate"] is not None and result["cpi_yoy"] is not None:
        real = result["fed_funds_rate"] - result["cpi_yoy"]
        result["real_rate"] = round(real, 2)
        result["real_rate_restrictive"] = real > 1.0
        if len(fed_funds) > 3:
            old_real = float(fed_funds.iloc[-4]) - result["cpi_yoy"]
            result["real_rate_trend"] = "rising" if real > old_real else "falling"

    return result


def get_industrial_production() -> dict:
    """
    Industrial Production Index (INDPRO, 2017=100).
    YoY > 0 = expanding. Better data availability than ISM PMI on FRED.
    """
    indpro = get_fred_series_with_retry("INDPRO", lookback_days=400)

    if indpro.empty:
        return {"ism_pmi": None, "pmi_expanding": None, "pmi_trend": "unknown", "pmi_1m_change": None}

    current   = float(indpro.iloc[-1])
    month_ago = float(indpro.iloc[-2]) if len(indpro) > 1 else current
    # YoY: expanding when current > year-ago level
    year_ago  = float(indpro.iloc[-12]) if len(indpro) >= 12 else current
    yoy_pct   = (current - year_ago) / year_ago * 100

    return {
        "ism_pmi":      round(yoy_pct, 1),   # repurposed field: YoY % change
        "pmi_label":    "Industrial Production YoY%",
        "pmi_expanding": yoy_pct > 0,
        "pmi_trend": "improving" if current > month_ago else "deteriorating",
        "pmi_1m_change": round(current - month_ago, 2),
    }


def get_consumer_confidence() -> dict:
    """
    U-Mich Consumer Sentiment (UMCSENT) + inflation expectations (MICH).
    """
    sentiment    = get_fred_series_with_retry("UMCSENT", lookback_days=365)
    inflation_ex = get_fred_series_with_retry("MICH",    lookback_days=365)

    result: dict = {
        "consumer_sentiment": None,
        "sentiment_trend": "unknown",
        "inflation_expectations": None,
        "high_inflation_expectations": False,
    }

    if not sentiment.empty:
        current   = float(sentiment.iloc[-1])
        month_ago = float(sentiment.iloc[-2]) if len(sentiment) > 1 else current
        result["consumer_sentiment"] = round(current, 1)
        result["sentiment_trend"] = "improving" if current > month_ago else "deteriorating"

    if not inflation_ex.empty:
        exp = float(inflation_ex.iloc[-1])
        result["inflation_expectations"]      = round(exp, 1)
        result["high_inflation_expectations"] = exp > 4.0

    return result


def get_credit_spreads() -> dict:
    """
    HY spread (BAMLH0A0HYM2) and IG spread (BAMLC0A0CM).
    Widening = credit stress = bad for leveraged small-caps.
    """
    hy_spread = get_fred_series_with_retry("BAMLH0A0HYM2", lookback_days=365)
    ig_spread = get_fred_series_with_retry("BAMLC0A0CM",   lookback_days=365)

    result: dict = {
        "fred_hy_spread": None,
        "fred_ig_spread": None,
        "fred_credit_stress": False,
        "fred_spread_trend": "unknown",
        "fred_hy_spread_1m_change": None,
    }

    if not hy_spread.empty:
        current_hy   = float(hy_spread.iloc[-1])
        month_ago_hy = float(hy_spread.iloc[-21]) if len(hy_spread) > 21 else current_hy
        result["fred_hy_spread"]           = round(current_hy, 2)
        result["fred_credit_stress"]       = current_hy > 5.0
        result["fred_spread_trend"]        = "widening" if current_hy > month_ago_hy else "tightening"
        result["fred_hy_spread_1m_change"] = round(current_hy - month_ago_hy, 2)

    if not ig_spread.empty:
        result["fred_ig_spread"] = round(float(ig_spread.iloc[-1]), 2)

    return result


def get_sector_rotation() -> dict:
    """
    3-month relative strength of small-cap sector ETFs vs IWM.
    """
    import yfinance as yf

    sector_etfs = {
        "Energy":          "IEO",
        "Technology":      "PSCT",
        "Healthcare":      "PSCH",
        "Industrials":     "PSCI",
        "Consumer_Disc":   "PSCD",
        "Consumer_Staples":"PSCC",
        "Financials":      "PSCF",
        "Materials":       "PSCM",
        "Utilities":       "PSCU",
        "Real_Estate":     "PSCE",
    }

    end   = date.today()
    start = end - timedelta(days=90)

    try:
        bench = yf.Ticker("IWM").history(start=start.isoformat(), end=end.isoformat())
        if bench.empty:
            return {"sector_leaders": [], "sector_laggards": [], "sector_returns": {}}
        bench_return = float(bench["Close"].iloc[-1] / bench["Close"].iloc[0] - 1)
    except Exception:
        return {"sector_leaders": [], "sector_laggards": [], "sector_returns": {}}

    sector_returns: dict = {}
    for sector, etf in sector_etfs.items():
        try:
            data = yf.Ticker(etf).history(start=start.isoformat(), end=end.isoformat())
            if not data.empty:
                ret = float(data["Close"].iloc[-1] / data["Close"].iloc[0] - 1)
                sector_returns[sector] = {
                    "return_3m":    round(ret, 4),
                    "vs_iwm":       round(ret - bench_return, 4),
                    "outperforming": ret > bench_return,
                }
        except Exception:
            pass

    leaders  = [s for s, d in sector_returns.items() if d["outperforming"]]
    laggards = [s for s, d in sector_returns.items() if not d["outperforming"]]

    return {
        "sector_returns":  sector_returns,
        "sector_leaders":  leaders,
        "sector_laggards": laggards,
        "iwm_return_3m":   round(bench_return, 4),
    }


# ---------------------------------------------------------------------------
# Per-ticker helpers (called from calculator.py)
# ---------------------------------------------------------------------------

def get_revenue_acceleration(ticker: str) -> dict:
    """
    Check if YoY revenue growth is accelerating vs prior quarter.
    Returns score-ready dict for use in factor calculator.
    """
    try:
        import yfinance as yf
        financials = yf.Ticker(ticker).quarterly_financials

        if financials is None or financials.empty:
            return {"revenue_accelerating": None, "revenue_growth_trend": "unknown"}

        if "Total Revenue" not in financials.index:
            return {"revenue_accelerating": None, "revenue_growth_trend": "unknown"}

        revenue = financials.loc["Total Revenue"].dropna()

        if len(revenue) < 6:
            return {"revenue_accelerating": None, "revenue_growth_trend": "unknown"}

        q1_growth = (float(revenue.iloc[0]) - float(revenue.iloc[4])) / abs(float(revenue.iloc[4]))
        q2_growth = (float(revenue.iloc[1]) - float(revenue.iloc[5])) / abs(float(revenue.iloc[5]))

        accelerating = q1_growth > q2_growth

        return {
            "revenue_accelerating":    accelerating,
            "revenue_growth_latest":   round(q1_growth, 4),
            "revenue_growth_prior":    round(q2_growth, 4),
            "revenue_growth_trend":    "accelerating" if accelerating else "decelerating",
        }
    except Exception as e:
        logger.debug(f"{ticker}: revenue acceleration failed — {e}")
        return {"revenue_accelerating": None, "revenue_growth_trend": "unknown"}


def get_52week_proximity(ticker: str) -> float:
    """
    Position within 52-week high/low range as 0-100 score.
    Near 52-week high with good fundamentals = institutional accumulation.
    """
    try:
        import yfinance as yf
        info     = yf.Ticker(ticker).info
        current  = info.get("currentPrice") or info.get("regularMarketPrice", 0)
        high_52w = info.get("fiftyTwoWeekHigh", 0)
        low_52w  = info.get("fiftyTwoWeekLow", 0)

        if not current or not high_52w or not low_52w or high_52w == low_52w:
            return 50.0

        position = (current - low_52w) / (high_52w - low_52w)
        return round(min(max(position * 100, 0), 100), 1)
    except Exception:
        return 50.0


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def get_full_economic_snapshot() -> dict:
    """
    Returns complete economic picture. Called by macro agent before regime detection.
    Gracefully returns neutral defaults for any series that fail to fetch.
    """
    logger.info("Fetching economic indicators from FRED + sector ETFs...")

    yield_curve = get_yield_curve()
    real_rates  = get_real_fed_funds_rate()
    pmi         = get_industrial_production()
    consumer    = get_consumer_confidence()
    credit      = get_credit_spreads()
    sectors     = get_sector_rotation()

    snapshot = {
        **yield_curve,
        **real_rates,
        **pmi,
        **consumer,
        **credit,
        "sector_leaders":  sectors.get("sector_leaders", []),
        "sector_laggards": sectors.get("sector_laggards", []),
        "sector_returns":  sectors.get("sector_returns", {}),
        "iwm_return_3m":   sectors.get("iwm_return_3m"),
        "as_of": str(date.today()),
    }

    logger.info(
        f"Yield curve: {yield_curve['yield_curve_spread']} "
        f"({'INVERTED' if yield_curve['yield_curve_inverted'] else 'normal'})"
    )
    logger.info(
        f"Real rate: {real_rates['real_rate']} "
        f"({'restrictive' if real_rates['real_rate_restrictive'] else 'accommodative'})"
    )
    logger.info(
        f"PMI: {pmi['ism_pmi']} "
        f"({'expanding' if pmi['pmi_expanding'] else 'contracting' if pmi['pmi_expanding'] is False else 'N/A'})"
    )
    logger.info(
        f"HY spread (FRED): {credit['fred_hy_spread']} "
        f"({'stress' if credit['fred_credit_stress'] else 'normal'})"
    )

    return snapshot


def print_economic_report(snapshot: dict) -> None:
    print("\nECONOMIC SNAPSHOT")
    print("=" * 55)
    print(f"Date: {snapshot['as_of']}")

    print(f"\nINTEREST RATES")
    print(f"  Fed Funds Rate:         {snapshot.get('fed_funds_rate', 'N/A')}%")
    print(f"  CPI YoY:                {snapshot.get('cpi_yoy', 'N/A')}%")
    print(f"  Real Rate:              {snapshot.get('real_rate', 'N/A')}%")
    print(f"  Real Rate Restrictive:  {snapshot.get('real_rate_restrictive')}")
    print(f"  Real Rate Trend:        {snapshot.get('real_rate_trend')}")

    print(f"\nYIELD CURVE")
    print(f"  10Y-2Y Spread:          {snapshot.get('yield_curve_spread', 'N/A')}%")
    print(f"  Inverted:               {snapshot.get('yield_curve_inverted')}")
    print(f"  Trend:                  {snapshot.get('yield_curve_trend')}")
    print(f"  Recession Warning:      {snapshot.get('recession_warning')}")

    print(f"\nECONOMIC ACTIVITY")
    print(f"  ISM PMI:                {snapshot.get('ism_pmi', 'N/A')}")
    print(f"  PMI Expanding:          {snapshot.get('pmi_expanding')}")
    print(f"  PMI Trend:              {snapshot.get('pmi_trend')}")
    print(f"  Consumer Sentiment:     {snapshot.get('consumer_sentiment', 'N/A')}")
    print(f"  Sentiment Trend:        {snapshot.get('sentiment_trend')}")
    print(f"  Inflation Expectations: {snapshot.get('inflation_expectations', 'N/A')}%")

    print(f"\nCREDIT (FRED)")
    print(f"  HY Spread:              {snapshot.get('fred_hy_spread', 'N/A')}%")
    print(f"  IG Spread:              {snapshot.get('fred_ig_spread', 'N/A')}%")
    print(f"  Credit Stress:          {snapshot.get('fred_credit_stress')}")
    print(f"  Spread Trend:           {snapshot.get('fred_spread_trend')}")

    print(f"\nSECTOR ROTATION (vs IWM 3M, IWM={snapshot.get('iwm_return_3m', 0):.1%})")
    print(f"  Leaders:  {snapshot.get('sector_leaders', [])}")
    print(f"  Laggards: {snapshot.get('sector_laggards', [])}")
