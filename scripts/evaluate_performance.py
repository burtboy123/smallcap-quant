"""
scripts/evaluate_performance.py

Standalone evaluation script — run at any time to evaluate
past portfolio picks against realized returns.

Usage:
    python scripts/evaluate_performance.py
    python scripts/evaluate_performance.py --min-days 30
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.learning_agent import (
    evaluate_past_portfolio,
    generate_learning_report,
    print_learning_report,
)
from loguru import logger

min_hold_days = 30
if "--min-days" in sys.argv:
    try:
        min_hold_days = int(sys.argv[sys.argv.index("--min-days") + 1])
    except (IndexError, ValueError):
        pass

logger.info(f"Evaluating past portfolio picks (min hold: {min_hold_days} days)...")

outcomes = evaluate_past_portfolio(lookback_days=90, min_hold_days=min_hold_days)

if outcomes.empty:
    print(f"\nNo mature picks to evaluate yet.")
    print(f"Need picks that are {min_hold_days}+ days old in the database.")
    print(f"Portfolio holdings found in portfolio_holdings table will be")
    print(f"evaluated automatically once they have aged sufficiently.")
    sys.exit(0)

report = generate_learning_report(outcomes)
print_learning_report(report)

print(f"\nOutcomes saved to portfolio_outcomes table.")
print(f"Run scripts/train_ml_model.py to retrain models with new data.")
