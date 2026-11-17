"""
data/prices_updater.py

Incremental price update — fetches only the last N days for each ticker
and upserts into the database.  Used by the live screener to get today's prices
without re-downloading the full history.

Run it directly to refresh the whole universe:

    python data/prices_updater.py                      # 35-day lookback
    python data/prices_updater.py --lookback-days 45   # wider, to close a gap
    python data/prices_updater.py --tickers AMN KODK   # just these
    python data/prices_updater.py --dry-run            # show the plan, fetch nothing

Exit code 0 on rows upserted, 1 on none (so a scheduled caller can tell).

The CLI is deliberately the SAME path the pipeline uses -- get_universe() from
data.universe_builder, exactly as scripts/live_screener.py:38 and
scripts/run_full_pipeline.py:22 obtain it -- so running this by hand and running it
through the pipeline cannot drift apart.
"""

from __future__ import annotations

import sys
import time
from datetime import date, timedelta
from pathlib import Path

from loguru import logger

# Running `python data/prices_updater.py` puts data/ on sys.path, NOT the repo root,
# so the absolute imports below raise ModuleNotFoundError: No module named 'data'
# before any main() could run. Put the repo root first. (Harmless when imported as
# part of the package, which is how the pipeline reaches this module.)
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from data.prices import _fetch_batch, BATCH_SIZE, SLEEP_BETWEEN_BATCHES  # noqa: E402
from data.storage import get_session, upsert_prices  # noqa: E402


def update_recent_prices(tickers: list[str], lookback_days: int = 35) -> int:
    """
    Fetch the last `lookback_days` of prices for all tickers and upsert.

    Args:
        tickers:       List of ticker symbols to update.
        lookback_days: How many calendar days back to fetch (35 covers ~25 trading days).

    Returns:
        Total number of rows upserted.
    """
    if not tickers:
        return 0

    end_date   = date.today().isoformat()
    start_date = (date.today() - timedelta(days=lookback_days)).isoformat()

    logger.info(
        f"Updating prices for {len(tickers)} tickers "
        f"({start_date} to {end_date})"
    )

    total_rows = 0
    batches = [tickers[i:i + BATCH_SIZE] for i in range(0, len(tickers), BATCH_SIZE)]

    for idx, batch in enumerate(batches, 1):
        df = _fetch_batch(batch, start=start_date, end=end_date)
        if df.empty:
            logger.warning(f"Batch {idx}/{len(batches)}: no data returned")
        else:
            with get_session() as session:
                upsert_prices(session, df)
            total_rows += len(df)
            logger.debug(f"Batch {idx}/{len(batches)}: {len(df)} rows upserted")

        if idx < len(batches):
            time.sleep(SLEEP_BETWEEN_BATCHES)

    logger.info(f"Price update complete — {total_rows} rows upserted")
    return total_rows


# ---------------------------------------------------------------------------
# CLI
#
# This module had no entry point, so the command the health check told you to run
# ("python data/prices_updater.py") did nothing useful: without PYTHONPATH it died on
# ModuleNotFoundError, and with PYTHONPATH set it imported cleanly and exited having
# done nothing at all. A remediation command that appears to succeed while changing
# nothing is the same false-confidence bug as the hardcoded freshness checker and the
# %ERRORLEVEL% wrapper.
# ---------------------------------------------------------------------------

def _latest_recorded_date() -> str | None:
    """MAX(date) in the prices table, so the CLI can show what it achieved."""
    try:
        from sqlalchemy import create_engine, text

        from config.settings import DATABASE_URL
        with create_engine(DATABASE_URL).connect() as c:
            return c.execute(text("SELECT MAX(date) FROM prices")).scalar()
    except Exception as e:                                          # noqa: BLE001
        logger.warning(f"could not read MAX(date) from prices: {e}")
        return None


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="Incrementally refresh recent prices for the investment universe.",
        epilog="Default universe is get_universe() from data.universe_builder — the "
               "same source scripts/live_screener.py and scripts/run_full_pipeline.py "
               "use, so a manual run and a pipeline run cannot diverge.")
    ap.add_argument("--lookback-days", type=int, default=35,
                    help="calendar days back to fetch (default 35, ~25 trading days). "
                         "Widen it to close a bigger gap: 45 covers ~6 weeks.")
    ap.add_argument("--tickers", nargs="+", default=None, metavar="TICKER",
                    help="update only these tickers instead of the full universe")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be fetched and exit without touching the DB")
    args = ap.parse_args(argv)

    # Same source as the pipeline. Imported here rather than at module scope so the
    # library path (update_recent_prices) does not pull in the universe builder.
    from data.universe_builder import get_universe

    tickers = args.tickers or get_universe()
    start = (date.today() - timedelta(days=args.lookback_days)).isoformat()
    before = _latest_recorded_date()

    print(f"universe        : {len(tickers)} tickers"
          f"{' (explicit --tickers)' if args.tickers else ' (get_universe())'}")
    print(f"lookback        : {args.lookback_days} days  ({start} -> {date.today()})")
    print(f"latest recorded : {before}")

    if args.dry_run:
        n_batches = (len(tickers) + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"DRY RUN — would fetch {n_batches} batches of up to {BATCH_SIZE} "
              f"tickers. Nothing written.")
        return 0

    rows = update_recent_prices(tickers, lookback_days=args.lookback_days)
    after = _latest_recorded_date()
    print(f"\nrows upserted   : {rows:,}")
    print(f"latest recorded : {before} -> {after}")

    if not rows:
        # Genuinely a failure: yfinance returned nothing for every batch. Exit non-zero
        # so a caller (or a scheduled task) can tell, rather than reporting success.
        print("NO ROWS UPSERTED — every batch came back empty. Check connectivity.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
