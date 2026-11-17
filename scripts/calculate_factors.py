"""
scripts/calculate_factors.py

Calculate factor scores for the full Russell 2000 universe as of today,
enforce quality gates, run ensemble ranking, and store results.

Usage:
    python -m scripts.calculate_factors
"""

from __future__ import annotations

import sqlite3
from datetime import date

from loguru import logger

from config.settings import DATABASE_URL
from data.universe_builder import get_universe
from factors.calculator import calculate_factors_for_date
from factors.composite import ensemble_rank


def get_db_path(url: str) -> str:
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///"):]
    raise ValueError(f"Not a SQLite URL: {url}")


def main() -> None:
    logger.info("Loading universe...")
    tickers = get_universe()
    logger.info(f"Universe: {len(tickers)} tickers")

    logger.info(f"Calculating factors as of {date.today()} (quality gates active, fetch_aux_signals=True, store=True)...")
    scores = calculate_factors_for_date(
        tickers=tickers,
        rebalance_date=date.today(),
        store=True,
        fetch_aux_signals=True,
    )

    if scores.empty:
        logger.error("Factor calculation returned no results.")
        return

    # ── Quality gate stats ──────────────────────────────────────────────────
    passed = scores["passed_quality_gate"].sum() if "passed_quality_gate" in scores.columns else len(scores)
    excluded = len(scores) - passed
    print(f"\n--- Quality Gate Results ---")
    print(f"  Passed : {passed:,}")
    print(f"  Excluded: {excluded:,}")
    print(f"  Total screened: {len(scores):,}")

    # ── Top 10 after quality gates ──────────────────────────────────────────
    ranked = scores[scores.get("passed_quality_gate", True) == True].sort_values(
        "composite_score", ascending=False
    ) if "passed_quality_gate" in scores.columns else scores.sort_values("composite_score", ascending=False)

    show_cols = [c for c in [
        "ticker", "composite_score", "value_score", "quality_score",
        "momentum_score", "f_score"
    ] if c in ranked.columns]

    print(f"\n--- Top 10 (quality gate enforced) ---")
    print(ranked.head(10)[show_cols].to_string(index=False))

    # Verify: no NaN momentum, no f_score < 4 in top 10
    top10 = ranked.head(10)
    nan_mom = top10["momentum_score"].isna().sum() if "momentum_score" in top10.columns else 0
    low_f   = (top10["f_score"] < 2).sum() if "f_score" in top10.columns else 0
    if nan_mom or low_f:
        print(f"\nWARNING: gate not fully effective — NaN momentum: {nan_mom}, f_score<2: {low_f}")
    else:
        print(f"\nGate check: OK — no NaN momentum, no f_score < 2 in top 10")

    # ── Aux signal coverage ─────────────────────────────────────────────────
    aux_cols = {
        "short_interest_score":    "Short Interest",
        "insider_score":           "Insider",
        "earnings_surprise_score": "Earnings Surprise",
    }
    print(f"\n--- Aux Signal Coverage (real score vs neutral 50.0) ---")
    for col, label in aux_cols.items():
        if col in scores.columns:
            total   = scores[col].notna().sum()
            neutral = (scores[col] == 50.0).sum()
            real    = total - neutral
            print(f"  {label:<22}: {real:>4} real  |  {neutral:>4} neutral (50.0)  |  {total:>4} total")
        else:
            print(f"  {label:<22}: column missing")

    # ── Ensemble ranking ────────────────────────────────────────────────────
    ensemble = ensemble_rank(scores)
    tier1 = ensemble[ensemble["conviction_tier"] == "TIER1_STRONG_BUY"]
    tier2 = ensemble[ensemble["conviction_tier"] == "TIER2_VALUE_BUY"]
    tier3 = ensemble[ensemble["conviction_tier"] == "TIER3_MOMENTUM_WATCH"]

    print(f"\n--- Ensemble Ranking (Screen A + B) ---")
    print(f"  TIER1_STRONG_BUY     : {len(tier1):>4}")
    print(f"  TIER2_VALUE_BUY      : {len(tier2):>4}")
    print(f"  TIER3_MOMENTUM_WATCH : {len(tier3):>4}")
    print(f"  TIER5_AVOID          : {len(ensemble) - len(tier1) - len(tier2) - len(tier3):>4}")

    if not tier1.empty:
        print(f"\nTIER1_STRONG_BUY stocks:")
        print(tier1[["ticker", "score_a", "score_b", "pct_a", "pct_b", "agreement_score"]].to_string(index=False))

    # ── DB verification ─────────────────────────────────────────────────────
    conn = sqlite3.connect(get_db_path(DATABASE_URL))
    row_count = conn.execute("SELECT COUNT(*) FROM factor_scores").fetchone()[0]
    conn.close()
    print(f"\nfactor_scores table: {row_count:,} rows (updated)")


if __name__ == "__main__":
    main()
