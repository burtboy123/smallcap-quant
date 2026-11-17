"""
data/universe_builder.py

Universe construction from IWM (iShares Russell 2000 ETF) holdings CSV.

The IWM CSV is downloaded manually from iShares and stored at data/iwm_holdings.csv
It contains ~1,950 real Russell 2000 tickers — our entire stock universe.

SURVIVORSHIP BIAS: This is the CURRENT Russell 2000 composition. It excludes
companies that went bankrupt or were removed in prior years.

The size and DIRECTION of that bias were measured in 2026-09 from EDGAR exit
filings — see docs/survivorship_findings.md (canonical). Summary:
  - Net bias is ~±1%/yr with an UNCERTAIN SIGN, possibly mildly pessimistic.
  - Acquisitions outnumber bankruptcies ~6:1 (50.4% vs 8.6% of exits) and exit
    at a premium, roughly offsetting the bankruptcy losses.
  - Backtests are optimistic only for genuinely unprofitable names.
  - Caveat: the measured bankruptcy rate is a FLOOR (detection depends on 8-K
    item 1.03 filings).

The earlier note here — "historical backtests will therefore be slightly
optimistic" — asserted a one-directional inflation and is superseded by the
measurement above. Conservative performance expectations still apply, but on
SMALL-SAMPLE grounds rather than survivorship.

For live trading from today forward, this universe is perfectly valid.
"""

import pandas as pd
from loguru import logger
from config.settings import UNIVERSE, PROJECT_ROOT


IWM_CSV_PATH = PROJECT_ROOT / "data" / "iwm_holdings.csv"


def load_iwm_universe() -> list[str]:
    """
    Load Russell 2000 tickers from the IWM holdings CSV.

    The iShares CSV has metadata rows at the top before the actual holdings.
    We skip rows until we find the header row containing 'Ticker'.

    Returns list of clean ticker strings — typically 1,900-2,050 tickers.
    """
    if not IWM_CSV_PATH.exists():
        raise FileNotFoundError(
            f"IWM holdings CSV not found at {IWM_CSV_PATH}\n"
            f"Download it from: https://www.ishares.com/us/products/239710/IWM\n"
            f"Click 'Download' -> 'Holdings' and save to data/iwm_holdings.csv"
        )

    # iShares CSVs have variable-length metadata headers.
    # Scan as text to find the line index containing "Ticker", then re-read from there.
    with open(IWM_CSV_PATH, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    header_row = None
    for i, line in enumerate(lines):
        if "ticker" in line.lower():
            header_row = i
            break

    if header_row is None:
        raise ValueError(
            "Could not find 'Ticker' column in IWM CSV. "
            "Make sure you downloaded the Holdings file (not the Summary)."
        )

    # Re-read with correct header
    df = pd.read_csv(IWM_CSV_PATH, skiprows=header_row, dtype=str)
    df.columns = df.columns.str.strip()

    if "Ticker" not in df.columns:
        raise ValueError(f"Ticker column not found. Columns found: {df.columns.tolist()}")

    tickers = df["Ticker"].dropna().tolist()

    # Clean tickers:
    # - Strip whitespace
    # - Remove cash/money market positions (usually "-" or long strings)
    # - Keep only valid equity tickers (1-5 uppercase letters)
    # - Remove known non-equity entries
    NON_EQUITY = {"-", "CASH", "USD", "OTHER", ""}
    clean = []
    for t in tickers:
        t = str(t).strip().upper()
        if t in NON_EQUITY:
            continue
        if len(t) < 1 or len(t) > 5:
            continue
        # Valid ticker: letters only (some have dots for share classes — keep those)
        if not all(c.isalpha() or c == "." for c in t):
            continue
        clean.append(t)

    # Remove duplicates while preserving order
    seen = set()
    unique = []
    for t in clean:
        if t not in seen:
            seen.add(t)
            unique.append(t)

    logger.info(f"Universe loaded: {len(unique)} tickers from IWM holdings CSV")
    return unique


def apply_universe_filters(tickers: list[str], prices_df: pd.DataFrame) -> list[str]:
    """
    Apply liquidity and price filters to the raw IWM universe.
    Removes stocks that don't meet minimum trading requirements.

    Filters applied (from config/settings.py UNIVERSE block):
    - Minimum stock price >= $3.00 (avoid penny stocks)
    - Minimum average daily dollar volume >= $500,000 (ensure we can trade it)

    Returns filtered list of tickers.
    """
    if prices_df.empty:
        logger.warning("No price data provided — returning unfiltered universe")
        return tickers

    filtered = []
    removed_price = 0
    removed_volume = 0

    for ticker in tickers:
        ticker_data = prices_df[prices_df["ticker"] == ticker]

        if ticker_data.empty:
            continue

        # Use last 60 trading days for filter calculation
        recent = ticker_data.sort_values("date").tail(60)

        avg_price = recent["adj_close"].mean()
        avg_dollar_volume = (recent["adj_close"] * recent["volume"]).mean()

        if avg_price < UNIVERSE["min_price"]:
            removed_price += 1
            continue

        if avg_dollar_volume < UNIVERSE["min_avg_daily_volume"]:
            removed_volume += 1
            continue

        filtered.append(ticker)

    logger.info(
        f"Universe filtered: {len(filtered)} tickers remain "
        f"(removed {removed_price} for low price, {removed_volume} for low volume)"
    )
    return filtered


def get_universe(prices_df: pd.DataFrame = None) -> list[str]:
    """
    Main entry point. Returns the clean, filtered investment universe.

    If prices_df is provided, applies liquidity filters.
    If not provided, returns the raw IWM tickers (useful for initial data download).
    """
    raw_tickers = load_iwm_universe()

    if prices_df is not None and not prices_df.empty:
        return apply_universe_filters(raw_tickers, prices_df)

    return raw_tickers
