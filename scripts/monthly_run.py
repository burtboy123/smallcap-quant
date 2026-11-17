"""
scripts/monthly_run.py

Monthly automation — run on first trading day of each month.

Steps:
  1. Fetch prices (full history from 2005, upserted) + TTM fundamentals
     -- scripts.run_full_pipeline; must run before step 2 (prior-year rows)
  2. Recalculate factor scores
  3. Run full orchestrator (regime + risk + portfolio construction)
  4. Point at the IBKR paper dry run (the Alpaca order book was removed 2026-10-04)

Removed 2026-10-12, both no-ops for the scores: the Claude sentiment step (no API key
is used, so it scored nothing while reporting "no transcript"; the composite still
reads stored sentiment scores) and the ML retrain (the overlay is disabled,
ML_OVERLAY_ENABLED=False).

For a quick incremental price refresh instead, use `python -m data.prices_updater`.

Usage:
    python -m scripts.monthly_run
    python scripts/monthly_run.py
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

from loguru import logger

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def run_step(description: str, module: str) -> bool:
    """Run a Python module as a subprocess. Returns True on success."""
    logger.info(f"--- {description} ---")
    result = subprocess.run(
        [sys.executable, "-m", module],
        cwd=str(ROOT),
        capture_output=False,   # Stream output live
    )
    if result.returncode != 0:
        logger.error(f"FAILED: {description} (exit {result.returncode})")
        return False
    logger.success(f"Done: {description}")
    return True


def main() -> None:
    logger.info("=" * 60)
    logger.info(f"MONTHLY RUN — {date.today()}")
    logger.info("=" * 60)

    # ORDER IS A HARD DEPENDENCY, not a convenience:
    #   run_full_pipeline FETCHES fundamentals (incl. prior-year rows needed for the
    #   full 0-9 F-score) and MUST run before calculate_factors. If calculate runs
    #   first, prior-year rows are missing and f-scores truncate to 0-4.
    # This is enforced two ways: (1) the sequence below runs fetch before calculate
    # and aborts on any failure; (2) calculate_factors_for_date(require_prior_year=True)
    # raises if prior-year coverage is insufficient, so an out-of-order run fails loudly
    # rather than silently regressing.
    FETCH_STEP    = ("Update prices + fundamentals (fetch FIRST)", "scripts.run_full_pipeline")
    CALC_STEP     = ("Recalculate factor scores (needs prior-year rows)", "scripts.calculate_factors")
    steps = [
        FETCH_STEP,
        CALC_STEP,
        ("Orchestrate portfolio",           "agents.orchestrator"),
    ]
    assert steps.index(FETCH_STEP) < steps.index(CALC_STEP), \
        "fetch_yfinance_fundamentals must run before calculate_factors"

    for description, module in steps:
        success = run_step(description, module)
        if not success:
            logger.error(f"Monthly run aborted at: {description}")
            sys.exit(1)

    # Step 4 used to build an order book against the Alpaca account
    # (scripts/generate_orders.py). The Alpaca path was removed 2026-10-04; orders go
    # only through the IBKR PAPER path, with the kill-switches and health gate.
    logger.info("Orders: dry-run the IBKR paper path next -- see broker/RUNBOOK.md §4:")
    from config.settings import ACTIVE_FT_PERIOD
    logger.info(f"  python -m broker.ibkr_paper --book forward_test/{ACTIVE_FT_PERIOD}/"
                "incumbent_book.csv"
                " --execution-engine")

    logger.success(f"Monthly run complete — {date.today()}")


if __name__ == "__main__":
    main()
