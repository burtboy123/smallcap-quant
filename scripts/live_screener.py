"""
scripts/live_screener.py

Real-time stock screener — ranks the universe right now and outputs actionable
BUY / HOLD / SELL signals with exact dollar amounts and position sizing.

Usage:
  python scripts/live_screener.py --portfolio 50000
  python scripts/live_screener.py --portfolio 50000 --update
  python scripts/live_screener.py --portfolio 50000 --current-holdings holdings.csv

Arguments:
  --portfolio VALUE         Total portfolio size in USD (required)
  --top N                   Number of positions to hold (default: 25)
  --update                  Fetch fresh prices from yfinance before ranking
  --current-holdings PATH   CSV with column 'ticker' to get HOLD/SELL signals

Sentiment is NOT a flag here. There used to be a --sentiment switch that called a
FinBERT news pipeline; that implementation is gone (agents/sentiment_agent.py is now
Claude earnings-transcript analysis), and the flag had been raising ImportError on
`analyze_batch_sentiment` for as long as it has been documented. Sentiment now reaches
the factor model one way only: `scripts/run_sentiment.py` writes the sentiment_scores
table, and factors.composite.calculate_composite_scores reads it automatically when no
scores are passed in. Populate that table before screening if you want the layer.
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import date, timedelta
from pathlib import Path

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
from loguru import logger

from config.settings import PORTFOLIO as PORT_CFG
from data.storage import get_session, load_prices, init_db
from data.universe_builder import get_universe as load_universe
from factors.calculator import calculate_factors_for_date


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_current_prices(tickers: list[str]) -> dict[str, float]:
    """
    Return the most recent adj_close for each ticker from the database.
    Falls back to NaN if no price available.
    """
    end   = date.today()
    start = end - timedelta(days=10)  # last 10 calendar days = ~7 trading days
    with get_session() as session:
        df = load_prices(tickers, start, end, session=session)
    if df.empty:
        return {}
    latest = (
        df.sort_values("date")
        .groupby("ticker")["adj_close"]
        .last()
    )
    return latest.to_dict()


def _position_sizing(
    portfolio_value: float,
    n_positions: int,
    prices: dict[str, float],
    max_single_pct: float = PORT_CFG["max_single_position"],
) -> dict[str, dict]:
    """
    Compute equal-weight position sizes.

    Returns {ticker: {"dollars": ..., "shares": ..., "price": ...}}
    """
    target_per_pos = portfolio_value / n_positions
    cap_per_pos    = portfolio_value * max_single_pct
    dollar_per_pos = min(target_per_pos, cap_per_pos)

    sizing = {}
    for ticker, price in prices.items():
        if price and price > 0:
            shares = math.floor(dollar_per_pos / price)
            actual_dollars = shares * price
        else:
            shares = 0
            actual_dollars = 0.0
        sizing[ticker] = {
            "dollars": actual_dollars,
            "shares":  shares,
            "price":   price or 0.0,
        }
    return sizing


def _signal(ticker: str, rank: int, top_n: int, held_tickers: set[str]) -> str:
    """BUY / HOLD / SELL based on rank thresholds."""
    if rank <= top_n:
        return "HOLD" if ticker in held_tickers else "BUY"
    elif rank <= top_n * 2:
        return "HOLD" if ticker in held_tickers else "WATCH"
    else:
        return "SELL" if ticker in held_tickers else "-"


def _hold_guidance(rank: int, top_n: int) -> str:
    if rank <= top_n:
        return f"Hold. Review monthly. Sell if rank drops below {top_n * 2}."
    elif rank <= top_n * 2:
        return "Watch — momentum fading. Sell after 3 months if rank stays here."
    else:
        return "Sell — outside top portfolio range."


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def _print_table(rows: list[dict], portfolio_value: float, top_n: int) -> None:
    """Print the ranked buy table to stdout."""
    header = (
        f"\n{'='*80}\n"
        f"  LIVE SCREENER  |  {date.today()}  |  Portfolio: ${portfolio_value:,.0f}  "
        f"|  Target positions: {top_n}\n"
        f"{'='*80}"
    )
    print(header)

    col_fmt = "{:>4}  {:<6}  {:>8}  {:>10}  {:>8}  {:>6}  {:<8}  {:<28}"
    print(col_fmt.format(
        "Rank", "Ticker", "Score", "Buy ($)", "Shares", "Price", "Signal", "Sector"
    ))
    print("-" * 80)

    for r in rows:
        signal = r["signal"]
        if signal == "BUY":
            marker = "** BUY **"
        elif signal == "HOLD":
            marker = "  HOLD   "
        elif signal == "SELL":
            marker = "!! SELL !!"
        elif signal == "WATCH":
            marker = "  watch  "
        else:
            marker = "   -     "

        print(col_fmt.format(
            r["rank"],
            r["ticker"],
            f"{r['composite_score']:.1f}",
            f"${r['dollars']:,.0f}" if r["dollars"] else "N/A",
            r["shares"] if r["shares"] else "-",
            f"${r['price']:.2f}" if r["price"] else "N/A",
            marker,
            (r["sector"] or "Unknown")[:28],
        ))

    print("=" * 80)

    # Per-position guidance for BUY signals
    buy_rows = [r for r in rows if r["signal"] == "BUY"]
    if buy_rows:
        print(f"\n  BUY ORDERS ({len(buy_rows)} positions):")
        for r in buy_rows:
            print(
                f"  [{r['ticker']}]  Buy ${r['dollars']:,.0f} "
                f"({r['shares']} shares @ ${r['price']:.2f})  |  "
                f"{_hold_guidance(r['rank'], top_n)}"
            )

    sell_rows = [r for r in rows if r["signal"] == "SELL"]
    if sell_rows:
        print(f"\n  SELL SIGNALS ({len(sell_rows)} positions):")
        for r in sell_rows:
            print(f"  [{r['ticker']}]  Rank {r['rank']} -- {_hold_guidance(r['rank'], top_n)}")

    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_screener(
    portfolio_value: float,
    top_n: int = 25,
    update_prices: bool = False,
    refresh_fundamentals: bool = False,
    current_holdings_path: str | None = None,
) -> None:

    init_db()

    # --- Optional: update prices ---
    if update_prices:
        from data.prices_updater import update_recent_prices
        all_tickers = load_universe()
        logger.info(f"Fetching latest prices for {len(all_tickers)} tickers...")
        update_recent_prices(all_tickers)

    # --- Optional: refresh TTM fundamentals for full universe ---
    if refresh_fundamentals:
        from data.fundamentals_yfinance import fetch_yfinance_fundamentals
        all_tickers = load_universe()
        logger.info(f"Refreshing TTM fundamentals for {len(all_tickers)} tickers (~20 min)...")
        fetch_yfinance_fundamentals(all_tickers, store=True)

    # --- Load universe ---
    tickers = load_universe()
    logger.info(f"Universe: {len(tickers)} tickers")

    # --- Run factor model as of today ---
    # No sentiment_scores argument on purpose: passing None lets
    # calculate_composite_scores read the sentiment_scores table itself (see the module
    # docstring). Passing an empty dict instead would suppress the DB read AND switch
    # the composite to the 4-factor weight set, which is not what "no sentiment" means.
    today = date.today()
    scores_df = calculate_factors_for_date(
        tickers,
        rebalance_date=today,
        store=False,
    )

    if scores_df.empty:
        print("ERROR: No factor scores computed.")
        print("  First time? Run: python scripts/bootstrap_data.py")
        print("  Already bootstrapped? Run with: --update --refresh-fundamentals")
        return

    # Reset index if ticker is in columns vs index
    if "ticker" not in scores_df.columns:
        scores_df = scores_df.reset_index()

    scores_df = scores_df.sort_values("universe_rank").reset_index(drop=True)

    # --- Current holdings ---
    held_tickers: set[str] = set()
    if current_holdings_path:
        try:
            h = pd.read_csv(current_holdings_path)
            held_tickers = set(h["ticker"].str.upper().tolist())
            logger.info(f"Loaded {len(held_tickers)} current holdings")
        except Exception as e:
            logger.warning(f"Could not load holdings file: {e}")

    # --- Get current prices ---
    ranked_tickers = scores_df["ticker"].tolist()
    current_prices = _get_current_prices(ranked_tickers)

    # --- Build display rows (top 2*N to show HOLD range too) ---
    display_cutoff = top_n * 2
    display_df = scores_df[scores_df["universe_rank"] <= display_cutoff].copy()

    # Position sizing only for top_n buy candidates
    buy_candidates = scores_df[scores_df["universe_rank"] <= top_n]["ticker"].tolist()
    buy_prices = {t: current_prices.get(t, 0.0) for t in buy_candidates}
    sizing = _position_sizing(portfolio_value, top_n, buy_prices)

    rows = []
    for _, row in display_df.iterrows():
        ticker  = row["ticker"]
        rank    = int(row["universe_rank"])
        price   = current_prices.get(ticker, 0.0)
        sz      = sizing.get(ticker, {"dollars": 0.0, "shares": 0, "price": price})
        signal  = _signal(ticker, rank, top_n, held_tickers)
        sector  = row.get("sector", "") or ""

        rows.append({
            "rank":            rank,
            "ticker":          ticker,
            "composite_score": row.get("composite_score", 0.0),
            "dollars":         sz["dollars"],
            "shares":          sz["shares"],
            "price":           price,
            "signal":          signal,
            "sector":          sector,
        })

    _print_table(rows, portfolio_value, top_n)

    # Summary stats
    total_invested = sum(r["dollars"] for r in rows if r["signal"] == "BUY")
    n_buy = sum(1 for r in rows if r["signal"] == "BUY")
    cash_remaining = portfolio_value - total_invested
    print(f"  Total to invest:  ${total_invested:,.0f}  ({n_buy} positions)")
    print(f"  Cash remaining:   ${cash_remaining:,.0f}")
    print(f"  Universe ranked:  {len(scores_df)} stocks\n")


def main():
    parser = argparse.ArgumentParser(
        description="Live stock screener — find undervalued stocks to buy today"
    )
    parser.add_argument("--portfolio", type=float, required=True,
                        help="Total portfolio value in USD")
    parser.add_argument("--top", type=int, default=25,
                        help="Number of positions to hold (default: 25)")
    parser.add_argument("--update", action="store_true",
                        help="Fetch latest prices from yfinance before ranking")
    parser.add_argument("--refresh-fundamentals", action="store_true",
                        help="Re-fetch TTM fundamentals from yfinance for full universe (~20 min)")
    parser.add_argument("--current-holdings", type=str, default=None,
                        help="Path to CSV with column 'ticker' for HOLD/SELL signals")
    args = parser.parse_args()

    run_screener(
        portfolio_value=args.portfolio,
        top_n=args.top,
        update_prices=args.update,
        refresh_fundamentals=args.refresh_fundamentals,
        current_holdings_path=args.current_holdings,
    )


if __name__ == "__main__":
    main()
