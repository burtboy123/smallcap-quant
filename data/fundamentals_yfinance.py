"""
data/fundamentals_yfinance.py

Fetches trailing-12-month (TTM) fundamental data from yfinance for the full
Russell 2000 universe and stores it in the fundamentals table.

Why TTM instead of quarterly?
  yfinance .info returns the most recent TTM figures — perfect for live screening.
  We don't need historical quarters here; this is for ranking stocks TODAY.

Point-in-time treatment:
  available_date = today  (we just fetched it; it IS available right now)
  This is correct because the live screener asks "what is cheap TODAY", not 2021.
  The 90-day lag applies to backtesting only.

Coverage: typically ~1,400-1,600 of 1,925 Russell 2000 tickers return usable data.
The remainder are too small / delisted / have incomplete Yahoo Finance coverage.
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf
from loguru import logger

from config.settings import DATA_LAGS
from data.storage import get_session, upsert_fundamentals, upsert_stock, init_db


def _historical_available_date(period_end: date) -> date:
    """
    Point-in-time reporting lag for HISTORICAL statements.

    yfinance statements carry period-END dates only — no filing date. A statement
    is not actually public until filed (up to ~90 days for a 10-K). For backtest
    safety we treat a historical statement as known only period_end + 90 days
    (DATA_LAGS['fundamental_lag_days']). load_fundamentals_as_of then enforces it
    via `available_date <= as_of`. The LIVE TTM row is unaffected (it is genuinely
    available today, so its available_date stays = fetch_date).
    """
    return period_end + timedelta(days=DATA_LAGS["fundamental_lag_days"])


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DELAY_BETWEEN_TICKERS = 0.15  # seconds — polite to Yahoo Finance
LOG_EVERY_N = 50               # print progress every N tickers


# ---------------------------------------------------------------------------
# Statement extraction helpers (free multi-year balance sheet)
# ---------------------------------------------------------------------------

def _stmt_val(df, names: list[str], col: int):
    """
    Read a value from a yfinance statement DataFrame by the first matching row
    name at column index `col` (col 0 = most recent fiscal period). Returns
    float or None. yfinance statement columns ARE balance-sheet data — unlike
    .info, which does NOT contain totalAssets (the historical ingestion bug).
    """
    if df is None or getattr(df, "empty", True) or col >= df.shape[1]:
        return None
    idx = {str(i).lower(): i for i in df.index}
    for n in names:
        if n.lower() in idx:
            v = df.loc[idx[n.lower()]].iloc[col]
            return float(v) if v is not None and v == v else None  # v==v rejects NaN
    return None


def _statement_fields(bs, inc, cf, col: int) -> dict:
    """Pull the F-score-relevant fields for one fiscal period (column `col`)."""
    return {
        "total_assets":         _stmt_val(bs, ["Total Assets"], col),
        "total_debt":           _stmt_val(bs, ["Total Debt"], col),
        "cash":                 _stmt_val(bs, ["Cash And Cash Equivalents",
                                               "Cash Cash Equivalents And Short Term Investments"], col),
        "shares":               _stmt_val(bs, ["Ordinary Shares Number", "Share Issued"], col),
        "revenue":              _stmt_val(inc, ["Total Revenue"], col),
        "gross_profit":         _stmt_val(inc, ["Gross Profit"], col),
        "net_income":           _stmt_val(inc, ["Net Income", "Net Income Common Stockholders"], col),
        "operating_cash_flow":  _stmt_val(cf, ["Operating Cash Flow",
                                               "Cash Flow From Continuing Operating Activities"], col),
        "period_end":           (str(bs.columns[col])[:10] if (bs is not None and not bs.empty
                                                               and col < bs.shape[1]) else None),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _most_recent_quarter_end(today: date) -> tuple[date, int, int]:
    """
    Return (period_end_date, fiscal_year, fiscal_quarter) for the most recently
    completed quarter whose 90-day filing lag has already passed as of today.

    The storage layer enforces available_date = period_end + 90 days. To ensure
    the data is visible to the screener TODAY, we pick the quarter-end that is
    at least 90 days in the past.

    March 25, 2026: cutoff = Dec 25, 2025 -> Q3 2025 (Sep 30) [Sep+90=Dec 29 <= today]
    June 25, 2026:  cutoff = Mar 27, 2026 -> Q4 2025 (Dec 31) [Dec+90=Mar 31 <= today]
    """
    cutoff = today - timedelta(days=90)
    # Walk back through quarter-ends to find the most recent one <= cutoff
    quarter_ends = [
        (date(cutoff.year, 9, 30), cutoff.year, 3),
        (date(cutoff.year, 6, 30), cutoff.year, 2),
        (date(cutoff.year, 3, 31), cutoff.year, 1),
        (date(cutoff.year - 1, 12, 31), cutoff.year - 1, 4),
        (date(cutoff.year - 1, 9, 30), cutoff.year - 1, 3),
    ]
    for qend, fy, fq in quarter_ends:
        if qend <= cutoff:
            return qend, fy, fq
    # Fallback — should never reach here
    return date(today.year - 1, 9, 30), today.year - 1, 3


def _map_info(ticker: str, info: dict, period_end: date, fiscal_year: int,
              fiscal_quarter: int, fetch_date: date,
              bs_current: Optional[dict] = None) -> Optional[dict]:
    """
    Map a yfinance .info dict to the Fundamental ORM column set.
    Returns None if the ticker has no usable data (e.g. no price, no revenue).

    bs_current: balance-sheet fields from the most recent statement period
    (_statement_fields). When present, total_assets/total_debt/cash/shares are
    sourced from the STATEMENT (correct) rather than .info (which lacks them).
    """
    price = info.get("currentPrice") or info.get("regularMarketPrice") or 0.0
    market_cap = info.get("marketCap") or 0

    # Need at least a price to compute share-based metrics
    if price <= 0:
        return None

    # Shares outstanding: derived from market cap / price
    shares = int(market_cap / price) if market_cap > 0 else None

    # Revenue
    revenue = info.get("totalRevenue") or None

    # Gross profit: revenue × gross margin
    gross_margins = info.get("grossMargins") or 0.0
    gross_profit = revenue * gross_margins if (revenue and gross_margins) else None

    # Operating income: revenue × operating margin
    operating_margins = info.get("operatingMargins") or 0.0
    operating_income = revenue * operating_margins if (revenue and operating_margins) else None

    # Net income
    net_income = info.get("netIncomeToCommon") or None

    # EPS
    eps_diluted = info.get("trailingEps") or None

    # EBITDA
    ebitda = info.get("ebitda") or None

    # Balance sheet — sourced from the STATEMENT (.balance_sheet), not .info.
    # .info has no "totalAssets" key; the old info.get("totalAssets") NULLed it
    # for the whole universe and truncated the Piotroski F-score to a 0-4 ceiling.
    bs_current = bs_current or {}
    total_assets = bs_current.get("total_assets") or info.get("totalAssets") or None
    total_debt = bs_current.get("total_debt")
    if total_debt is None:
        total_debt = info.get("totalDebt") or 0.0
    cash = bs_current.get("cash")
    if cash is None:
        cash = info.get("totalCash") or 0.0
    # Prefer statement share count; fall back to market-cap-derived shares
    if bs_current.get("shares"):
        shares = int(bs_current["shares"])

    # Total equity: book value per share × shares
    book_value_per_share = info.get("bookValue") or None
    total_equity = (book_value_per_share * shares) if (book_value_per_share and shares) else None

    # Cash flow
    ocf = info.get("operatingCashflow") or None
    fcf = info.get("freeCashflow") or None

    # Margins and returns (pre-computed by Yahoo, no calculation needed)
    gross_margin   = info.get("grossMargins") or None
    operating_margin = info.get("operatingMargins") or None
    net_margin     = info.get("profitMargins") or None
    roa            = info.get("returnOnAssets") or None
    roe            = info.get("returnOnEquity") or None

    # Reject if no useful fundamental fields are present
    has_any_data = any(v is not None for v in [
        revenue, ebitda, net_income, total_assets, ocf, fcf
    ])
    if not has_any_data:
        return None

    return {
        "ticker":            ticker,
        "fiscal_year":       fiscal_year,
        "fiscal_quarter":    fiscal_quarter,
        "period_end_date":   period_end,
        "filing_date":       None,
        "available_date":    fetch_date,   # live fetch — available today
        "revenue":           revenue,
        "gross_profit":      gross_profit,
        "operating_income":  operating_income,
        "net_income":        net_income,
        "ebitda":            ebitda,
        "eps_diluted":       eps_diluted,
        "shares_outstanding": shares,
        "total_assets":      total_assets,
        "total_equity":      total_equity,
        "total_debt":        total_debt,
        "cash_and_equivalents": cash,
        "operating_cash_flow": ocf,
        "capex":             None,         # not available in .info
        "free_cash_flow":    fcf,
        "gross_margin":      gross_margin,
        "operating_margin":  operating_margin,
        "net_margin":        net_margin,
        "roa":               roa,
        "roe":               roe,
    }


def _prior_year_row(ticker: str, prior: dict) -> Optional[dict]:
    """
    Build a prior-fiscal-year Fundamental row from statement fields so the
    Piotroski F-score YoY signals (F3/F5/F6/F7/F8/F9) can be computed.
    Only the F-score-relevant fields are populated; the rest stay NULL.

    available_date = period_end + 90 days (_historical_available_date) so the
    historical row is point-in-time safe for backtests. The live TTM row keeps
    available_date = fetch_date (it is genuinely available today).
    """
    pe = prior.get("period_end")
    if not pe or prior.get("total_assets") is None:
        return None
    pe_date = date.fromisoformat(pe)
    return {
        "ticker":             ticker,
        "fiscal_year":        pe_date.year,
        "fiscal_quarter":     4,
        "period_end_date":    pe_date,
        "filing_date":        None,
        # +90d reporting lag — historical statement treated as known only after filing window
        "available_date":     _historical_available_date(pe_date),
        "revenue":            prior.get("revenue"),
        "gross_profit":       prior.get("gross_profit"),
        "operating_income":   None,
        "net_income":         prior.get("net_income"),
        "ebitda":             None,
        "eps_diluted":        None,
        "shares_outstanding": int(prior["shares"]) if prior.get("shares") else None,
        "total_assets":       prior.get("total_assets"),
        "total_equity":       None,
        "total_debt":         prior.get("total_debt") or 0.0,
        "cash_and_equivalents": prior.get("cash") or 0.0,
        "operating_cash_flow": prior.get("operating_cash_flow"),
        "capex":              None,
        "free_cash_flow":     None,
        "gross_margin":       None,
        "operating_margin":   None,
        "net_margin":         None,
        "roa":                None,
        "roe":                None,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_yfinance_fundamentals(
    tickers: list[str],
    store: bool = True,
) -> pd.DataFrame:
    """
    Fetch TTM fundamentals from yfinance for a list of tickers.

    Args:
        tickers: List of ticker symbols (e.g. the full Russell 2000 universe).
        store:   If True, upserts results into the fundamentals table.

    Returns:
        DataFrame of all successfully fetched rows.
    """
    if not tickers:
        return pd.DataFrame()

    fetch_date = date.today()
    period_end, fiscal_year, fiscal_quarter = _most_recent_quarter_end(fetch_date)

    logger.info(
        f"Fetching yfinance fundamentals for {len(tickers)} tickers "
        f"(TTM as of {fetch_date}, period_end={period_end})"
    )

    init_db()
    rows = []
    n_ok = 0
    n_fail = 0

    for i, ticker in enumerate(tickers, 1):
        try:
            t = yf.Ticker(ticker)
            info = t.info
            # Statements carry the balance sheet (.info does not) + multi-year history
            bs, inc, cf = t.balance_sheet, t.income_stmt, t.cashflow
            bs_current = _statement_fields(bs, inc, cf, 0) if bs is not None and not bs.empty else None

            row = _map_info(ticker, info, period_end, fiscal_year, fiscal_quarter,
                            fetch_date, bs_current=bs_current)
            if row:
                rows.append(row)
                n_ok += 1
                # Prior fiscal year (column 1) -> separate row for F-score YoY signals
                if bs is not None and not bs.empty and bs.shape[1] >= 2:
                    prior_row = _prior_year_row(ticker, _statement_fields(bs, inc, cf, 1))
                    if prior_row:
                        rows.append(prior_row)
            else:
                n_fail += 1
        except Exception as e:
            logger.debug(f"[{ticker}] fetch error: {e}")
            n_fail += 1

        if i % LOG_EVERY_N == 0:
            logger.info(
                f"Progress: {i}/{len(tickers)} | ok={n_ok} | skipped/failed={n_fail}"
            )

        time.sleep(DELAY_BETWEEN_TICKERS)

    if not rows:
        logger.warning("No fundamental data fetched.")
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    logger.info(
        f"Fetched fundamentals for {n_ok}/{len(tickers)} tickers "
        f"({n_fail} had no usable data)"
    )

    if store:
        session = get_session()
        try:
            # Ensure stock stubs exist
            for ticker in df["ticker"].unique():
                upsert_stock(session, ticker)
            session.commit()

            written = upsert_fundamentals(session, df)
            logger.info(f"Stored {written} fundamental rows")
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return df
