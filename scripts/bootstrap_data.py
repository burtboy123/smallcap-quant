"""
scripts/bootstrap_data.py

One-time full data loader for the Russell 2000 universe.

Fetches:
  1. 2 years of price history for all ~1,925 tickers (needed for 12-month momentum)
  2. Current TTM fundamental data for all tickers (needed for value/quality factors)

Run this ONCE before using the live screener for the first time.
After that, use --update and --refresh-fundamentals on the live screener for incremental updates.

Estimated runtime: 30-45 minutes total
  - Prices:       ~10-15 min  (39 batches of 50 tickers via yfinance batch download)
  - Fundamentals: ~15-25 min  (~1,925 individual .info fetches at 0.15s each)

Usage:
  python scripts/bootstrap_data.py
  python scripts/bootstrap_data.py --prices-only
  python scripts/bootstrap_data.py --fundamentals-only
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from loguru import logger

from data.storage import init_db
from data.universe_builder import get_universe as load_universe


def run_bootstrap(prices: bool = True, fundamentals: bool = True) -> None:

    init_db()
    tickers = load_universe()
    logger.info(f"Universe: {len(tickers)} tickers")

    # ------------------------------------------------------------------
    # Step 1: Price history — 2 years
    # ------------------------------------------------------------------
    if prices:
        from data.prices import fetch_prices
        start_date = date.today() - timedelta(days=730)   # 2 years
        end_date   = date.today() - timedelta(days=1)     # never fetch today (market may be open)

        logger.info(
            f"[PRICES] Fetching {start_date} to {end_date} for {len(tickers)} tickers..."
        )
        df_prices = fetch_prices(tickers, start_date=start_date, end_date=end_date, store=True)

        if df_prices.empty:
            logger.error("[PRICES] No price data was fetched. Check yfinance connectivity.")
        else:
            n_tickers = df_prices["ticker"].nunique()
            logger.info(
                f"[PRICES] Done. {len(df_prices):,} rows for {n_tickers} tickers stored."
            )

    # ------------------------------------------------------------------
    # Step 2: Fundamental data — TTM snapshot for all tickers
    # ------------------------------------------------------------------
    if fundamentals:
        from data.fundamentals_yfinance import fetch_yfinance_fundamentals

        logger.info(
            f"[FUNDAMENTALS] Fetching TTM data for {len(tickers)} tickers "
            f"(this takes ~20 min)..."
        )
        df_fund = fetch_yfinance_fundamentals(tickers, store=True)

        if df_fund.empty:
            logger.error("[FUNDAMENTALS] No fundamental data was fetched.")
        else:
            logger.info(
                f"[FUNDAMENTALS] Done. {len(df_fund)} tickers with usable fundamentals."
            )

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("  BOOTSTRAP COMPLETE")
    print("=" * 60)
    print(f"  Universe:     {len(tickers)} tickers")
    if prices:
        print("  Prices:       stored (2yr history)")
    if fundamentals:
        print("  Fundamentals: stored (TTM snapshot)")
    print()
    print("  Next step:")
    print("    python scripts/live_screener.py --portfolio 50000 --top 5")
    print()
    print("  To refresh data weekly:")
    print("    python scripts/live_screener.py --portfolio 50000 --update --refresh-fundamentals --top 5")
    print("=" * 60 + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="One-time bootstrap: fetch 2yr prices + TTM fundamentals for all Russell 2000 tickers"
    )
    parser.add_argument("--prices-only", action="store_true",
                        help="Fetch prices only, skip fundamentals")
    parser.add_argument("--fundamentals-only", action="store_true",
                        help="Fetch fundamentals only, skip prices")
    args = parser.parse_args()

    run_prices = not args.fundamentals_only
    run_fundamentals = not args.prices_only

    run_bootstrap(prices=run_prices, fundamentals=run_fundamentals)


if __name__ == "__main__":
    main()
