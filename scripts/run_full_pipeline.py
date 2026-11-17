"""
scripts/run_full_pipeline.py

Full data pipeline for all 1,925 Russell 2000 tickers.
  1. Fetches price history from 2005-01-01 (upserts — safe to re-run)
  2. Refreshes TTM fundamentals via yfinance (upserts)
  3. Recalculates factor scores with quality gates
  4. Prints DB summary

Usage:
    python -m scripts.run_full_pipeline
"""

from __future__ import annotations

import sqlite3
from datetime import date

from loguru import logger

from config.settings import DATABASE_URL
from data.universe_builder import get_universe
from data.prices import fetch_prices
from data.fundamentals_yfinance import fetch_yfinance_fundamentals
from factors.calculator import calculate_factors_for_date
from factors.composite import ensemble_rank


def get_db_path(url: str) -> str:
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///"):]
    raise ValueError(f"Not a SQLite URL: {url}")


def db_summary(conn: sqlite3.Connection) -> str:
    tables = {
        "prices":        "SELECT COUNT(*), MIN(date), MAX(date) FROM prices",
        "fundamentals":  "SELECT COUNT(*), MIN(available_date), MAX(available_date) FROM fundamentals",
        "stocks":        "SELECT COUNT(*), NULL, NULL FROM stocks",
        "factor_scores": "SELECT COUNT(*), MIN(calculation_date), MAX(calculation_date) FROM factor_scores",
    }
    lines = ["\n--- DB Summary ---"]
    for table, q in tables.items():
        n, lo, hi = conn.execute(q).fetchone()
        span = f"{lo} to {hi}" if lo else "—"
        lines.append(f"  {table:<20} {int(n):>10,} rows   {span}")
    return "\n".join(lines)


def main() -> None:
    tickers = get_universe()
    logger.info(f"Starting full pipeline: {len(tickers)} tickers")

    db_path = get_db_path(DATABASE_URL)
    conn = sqlite3.connect(db_path)

    # Check existing price coverage
    row = conn.execute("SELECT MIN(date), COUNT(DISTINCT ticker) FROM prices").fetchone()
    existing_start, existing_tickers = row
    logger.info(f"Existing prices: {existing_tickers} tickers from {existing_start}")

    # ── Step 1: Prices from 2005 ──────────────────────────────────────────────
    target_start = date(2005, 1, 1)
    logger.info(f"Fetching prices from {target_start} (upserts — existing rows skipped)...")
    prices = fetch_prices(
        tickers,
        start_date=target_start,
        end_date=date.today(),
        store=True,
    )
    logger.info(f"Prices fetched: {len(prices):,} rows for {prices['ticker'].nunique()} tickers")

    # ── Step 2: Fundamentals via yfinance ─────────────────────────────────────
    logger.info("Refreshing TTM fundamentals via yfinance...")
    fetch_yfinance_fundamentals(tickers, store=True)

    # ── Step 3: Factor calculation ────────────────────────────────────────────
    logger.info("Calculating factor scores for full universe...")
    scores = calculate_factors_for_date(
        tickers=tickers,
        rebalance_date=date.today(),
        store=True,
    )

    if scores.empty:
        logger.error("Factor calculation returned no results.")
        conn.close()
        return

    passed  = int(scores.get("passed_quality_gate", scores.index).notna().sum()
                  if "passed_quality_gate" not in scores.columns
                  else scores["passed_quality_gate"].sum())
    excluded = len(scores) - passed

    ensemble = ensemble_rank(scores)
    tier1 = (ensemble["conviction_tier"] == "TIER1_STRONG_BUY").sum()

    # ── Final summary ─────────────────────────────────────────────────────────
    print(db_summary(conn))
    conn.close()

    print(f"\n--- Factor Score Summary ---")
    print(f"  Total screened      : {len(scores):,}")
    print(f"  Passed quality gates: {passed:,}")
    print(f"  Excluded            : {excluded:,}")
    print(f"  TIER1_STRONG_BUY    : {tier1}")

    ranked = scores[scores.get("passed_quality_gate", True) == True].sort_values(
        "composite_score", ascending=False
    ) if "passed_quality_gate" in scores.columns else scores.sort_values("composite_score", ascending=False)

    show = [c for c in ["ticker","composite_score","value_score","quality_score","momentum_score","f_score"]
            if c in ranked.columns]
    print(f"\nTop 10 after quality gates:")
    print(ranked.head(10)[show].to_string(index=False))


if __name__ == "__main__":
    main()
