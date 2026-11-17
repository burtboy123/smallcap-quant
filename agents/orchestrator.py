"""
agents/orchestrator.py

Monthly workflow orchestrator.
1. Detect macro regime
2. Load latest factor scores (regime-adjusted weights)
3. Run sentiment on candidates
4. Run risk check + position sizing
5. Save final portfolio to database

Usage:
    python -m agents.orchestrator
    python agents/orchestrator.py
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import yfinance as yf
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.macro_agent import get_current_regime, print_regime_report
from agents.risk_agent import (
    run_risk_check, print_risk_report,
    get_portfolio_prices, calculate_kelly_sizes,
)
from agents.sentiment_agent import analyse_portfolio_sentiment
from config.settings import DATABASE_URL, ML_OVERLAY_ENABLED
from data.storage import save_portfolio_holdings
from factors.composite import ensemble_rank
from models.factor_model import predict_scores_for_regime


def _db_path() -> str:
    return DATABASE_URL.replace("sqlite:///", "")


# ---------------------------------------------------------------------------
# Regime-aware sector bias
# ---------------------------------------------------------------------------

REGIME_SECTOR_BIAS: dict[str, dict] = {
    "INFLATION_SHOCK": {
        "overweight":  ["Energy", "Basic Materials", "Industrials"],
        "underweight": ["Consumer Cyclical", "Technology", "Real Estate"],
    },
    "RISK_ON_GROWTH": {
        "overweight":  ["Technology", "Consumer Cyclical", "Industrials", "Healthcare"],
        "underweight": ["Consumer Defensive", "Utilities"],
    },
    "RISK_OFF_DEFENSIVE": {
        "overweight":  ["Consumer Defensive", "Healthcare", "Utilities"],
        "underweight": ["Consumer Cyclical", "Energy", "Technology"],
    },
    "RECOVERY": {
        "overweight":  ["Industrials", "Basic Materials", "Consumer Cyclical", "Energy"],
        "underweight": ["Consumer Defensive", "Utilities"],
    },
    "NEUTRAL": {
        "overweight":  ["Technology", "Healthcare", "Industrials"],
        "underweight": ["Utilities", "Real Estate"],
    },
}


def apply_regime_sector_bias(
    scores_df: pd.DataFrame,
    sector_data: pd.DataFrame,
    regime: str,
    bias_strength: float = 5.0,
) -> pd.DataFrame:
    """
    Nudge composite scores by ±bias_strength based on regime-appropriate sector.
    Overweight sectors: +5 pts.  Underweight sectors: -5 pts.
    """
    if sector_data.empty or "ticker" not in scores_df.columns:
        return scores_df

    bias = REGIME_SECTOR_BIAS.get(regime, REGIME_SECTOR_BIAS["NEUTRAL"])
    if not bias["overweight"] and not bias["underweight"]:
        return scores_df

    merged = scores_df.merge(sector_data[["ticker", "sector"]], on="ticker", how="left")
    merged["sector"] = merged["sector"].fillna("")

    over_mask  = merged["sector"].isin(bias["overweight"])
    under_mask = merged["sector"].isin(bias["underweight"])
    merged.loc[over_mask,  "composite_score"] += bias_strength
    merged.loc[under_mask, "composite_score"] -= bias_strength

    if over_mask.any() or under_mask.any():
        logger.info(
            f"Regime sector bias ({regime}): "
            f"+{bias_strength} to {sorted(set(merged.loc[over_mask,'sector']))} | "
            f"-{bias_strength} to {sorted(set(merged.loc[under_mask,'sector']))}"
        )

    return merged.drop(columns=["sector"], errors="ignore")


# ---------------------------------------------------------------------------
# Sector concentration enforcement
# ---------------------------------------------------------------------------

def enforce_sector_limits(
    final_picks: pd.DataFrame,
    sector_data: pd.DataFrame,
    max_sector_pct: float = 0.30,
) -> pd.DataFrame:
    """
    Remove lowest-scoring positions from over-concentrated sectors.
    Never removes more than 3 positions per sector per rebalance.
    """
    if sector_data.empty or final_picks.empty:
        return final_picks

    merged = final_picks.merge(sector_data[["ticker", "sector"]], on="ticker", how="left")
    merged["sector"] = merged["sector"].fillna("Unknown")

    n       = len(merged)
    removed: list[str] = []

    for sector in merged["sector"].unique():
        sector_picks = merged[merged["sector"] == sector]
        sector_pct   = len(sector_picks) / n

        if sector_pct > max_sector_pct:
            max_allowed = max(1, int(n * max_sector_pct) - 1)
            to_remove   = min(len(sector_picks) - max_allowed, 3)

            # Drop lowest composite_score from this sector
            drop_tickers = (
                sector_picks.sort_values("composite_score", ascending=True)
                ["ticker"].head(to_remove).tolist()
            )
            removed.extend(drop_tickers)
            logger.warning(
                f"Sector trim: {sector} at {sector_pct:.0%} — "
                f"removing {drop_tickers} (limit {max_sector_pct:.0%})"
            )

    if removed:
        final_picks = final_picks[~final_picks["ticker"].isin(removed)].reset_index(drop=True)
        logger.info(f"Sector enforcement removed {len(removed)} positions: {removed}")

    return final_picks


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------

def run_monthly_workflow() -> dict:
    logger.info("=" * 60)
    logger.info(f"MONTHLY WORKFLOW — {date.today()}")
    logger.info("=" * 60)

    # STEP 1 — Macro regime
    logger.info("Step 1/5: Detecting macro regime...")
    regime = get_current_regime()
    current_regime = regime["regime"]
    print_regime_report(regime)

    # STEP 2 — Load factor scores (composite_score IS NOT NULL = passed quality gate)
    logger.info("Step 2/5: Loading factor scores...")
    conn = sqlite3.connect(_db_path())
    scores = pd.read_sql("""
        SELECT ticker, composite_score, value_score, quality_score,
               momentum_score, f_score, roic
        FROM factor_scores
        WHERE calculation_date = (SELECT MAX(calculation_date) FROM factor_scores)
          AND composite_score IS NOT NULL
    """, conn)
    sector_data = pd.read_sql(
        "SELECT ticker, sector FROM stocks WHERE sector IS NOT NULL", conn
    )
    conn.close()

    if scores.empty:
        logger.error("No factor scores — run scripts/calculate_factors.py first")
        return {}

    logger.info(f"Loaded {len(scores)} quality-passing stocks")

    # Apply regime-aware sector bias before ranking
    scores = apply_regime_sector_bias(scores, sector_data, regime=current_regime, bias_strength=5.0)

    # ensemble_rank expects passed_quality_gate column; all rows here already passed
    scores["passed_quality_gate"] = True

    ranked = ensemble_rank(scores)
    if ranked.empty:
        logger.error("ensemble_rank returned empty — check factor data")
        return {}

    # STEP 3 — Select candidates: TIER1 + TIER2, up to 25
    logger.info("Step 3/5: Selecting candidates...")
    tier1 = ranked[ranked["conviction_tier"] == "TIER1_STRONG_BUY"].head(20)
    tier2 = ranked[ranked["conviction_tier"] == "TIER2_VALUE_BUY"].head(10)
    candidates = pd.concat([tier1, tier2]).head(25)

    if len(candidates) < 10:
        logger.warning(f"Only {len(candidates)} TIER1/2 candidates — using top 15 regardless of tier")
        candidates = ranked.head(15)

    # Attach composite_score from factor scores (ensemble result has score_a/b, not composite_score)
    score_lookup = scores.set_index("ticker")["composite_score"].to_dict()
    candidates = candidates.copy()
    candidates["composite_score"] = candidates["ticker"].map(score_lookup).fillna(0.0)

    # Guard: correct any 0.0 or NaN composite scores using underlying factor data
    for idx, row in candidates.iterrows():
        if row["composite_score"] == 0.0 or pd.isna(row["composite_score"]):
            actual = scores.loc[scores["ticker"] == row["ticker"], "composite_score"]
            if not actual.empty and float(actual.iloc[0]) > 0:
                candidates.at[idx, "composite_score"] = float(actual.iloc[0])
                logger.warning(
                    f"{row['ticker']}: corrected 0.0 composite_score to {float(actual.iloc[0]):.1f}"
                )
            else:
                logger.warning(
                    f"{row['ticker']}: composite_score is 0.0 — excluding from portfolio"
                )

    # Drop tickers that still have 0.0 score after correction attempt
    zero_score = candidates[candidates["composite_score"] == 0.0]["ticker"].tolist()
    if zero_score:
        candidates = candidates[candidates["composite_score"] > 0].copy()
        logger.warning(f"Dropped {len(zero_score)} tickers with unresolvable 0.0 score: {zero_score}")

    # Minimum composite score threshold — remove low-conviction ensemble picks,
    # replace with next-best quality-passing candidates (non-Basic-Materials preference)
    MIN_COMPOSITE_SCORE = 30.0
    low_conv = candidates[candidates["composite_score"] < MIN_COMPOSITE_SCORE]["ticker"].tolist()
    if low_conv:
        logger.warning(f"Low conviction (score<{MIN_COMPOSITE_SCORE:.0f}): removing {low_conv}")
        candidates = candidates[candidates["composite_score"] >= MIN_COMPOSITE_SCORE].copy()

        already_in = set(candidates["ticker"].tolist())
        pool = ranked.copy()
        pool["composite_score"] = pool["ticker"].map(score_lookup).fillna(0.0)
        pool = pool[
            (~pool["ticker"].isin(already_in)) &
            (pool["composite_score"] >= MIN_COMPOSITE_SCORE)
        ]
        pool = pool.merge(sector_data[["ticker", "sector"]], on="ticker", how="left")
        pool["sector"] = pool["sector"].fillna("")
        # Prefer non-Basic-Materials replacements to avoid concentration
        non_bm = pool[pool["sector"] != "Basic Materials"]
        pool_filtered = non_bm if len(non_bm) >= len(low_conv) else pool
        additions = pool_filtered.head(len(low_conv)).drop(columns=["sector"], errors="ignore")
        for _, r in additions.iterrows():
            logger.info(
                f"Low-conviction replacement: added {r['ticker']} (score={r['composite_score']:.1f})"
            )
        candidates = pd.concat([candidates, additions], ignore_index=True)

    # Correlation-based deduplication: if two picks have correlation > 0.80
    # and are in the same sub-sector, keep the higher-scoring one
    # (Full corr check happens in risk_agent; this catches obvious pairs early)

    # STEP 4 — Sentiment filter
    logger.info("Step 4/5: Running sentiment on candidates...")
    tickers = candidates["ticker"].tolist()
    sentiment_results = analyse_portfolio_sentiment(tickers)

    negative = [
        t for t, r in sentiment_results.items()
        if r.get("overall_sentiment", 0) < -0.2
        or r.get("guidance_direction") == "lowered"
    ]
    if negative:
        logger.warning(f"Negative sentiment — removing: {negative}")
        candidates = candidates[~candidates["ticker"].isin(negative)]

    final_picks = candidates.head(15).copy()

    # STEP 5 — Risk check + position sizing
    logger.info("Step 5/5: Running risk check...")

    prices: dict[str, float] = {}
    for ticker in final_picks["ticker"]:
        try:
            prices[ticker] = float(yf.Ticker(ticker).fast_info.last_price)
        except Exception:
            prices[ticker] = 0.0

    final_picks["entry_price"] = final_picks["ticker"].map(prices)

    risk_report = run_risk_check(final_picks, regime)
    print_risk_report(risk_report)

    # Remove stop-loss triggered positions (unlikely for fresh picks, but safe)
    sl_tickers = {s["ticker"] for s in risk_report.get("stop_losses", [])}
    if sl_tickers:
        final_picks = final_picks[~final_picks["ticker"].isin(sl_tickers)]

    # Kelly position sizing. ML conviction overlay honours ML_OVERLAY_ENABLED:
    # when the overlay is disabled (current state), Kelly sizes on edge/variance
    # only — no frozen-model conviction tilt — so neither selection NOR sizing
    # imports the unproven model.
    ml_scores: pd.Series | None = None
    if ML_OVERLAY_ENABLED:
        try:
            conn = sqlite3.connect(_db_path())
            fs_df = pd.read_sql("""
                SELECT ticker, composite_score,
                       value_score, quality_score, momentum_score,
                       short_interest_score, insider_score,
                       earnings_surprise_score, proximity_52w, revenue_accel_score
                FROM factor_scores
                WHERE calculation_date = (SELECT MAX(calculation_date) FROM factor_scores)
                  AND composite_score IS NOT NULL
            """, conn)
            conn.close()
            if not fs_df.empty:
                fs_df["sentiment_score"] = 50.0
                ml_scores = predict_scores_for_regime(
                    fs_df.set_index("ticker"), current_regime
                )
        except Exception as e:
            logger.warning(f"Could not load ML scores for Kelly sizing — {e}")
    else:
        logger.info("ML overlay disabled — Kelly sizing without ML conviction tilt")

    prices_df = get_portfolio_prices(final_picks["ticker"].tolist())
    final_picks = calculate_kelly_sizes(final_picks, prices_df, ml_scores=ml_scores)

    # Enforce sector concentration limits after sizing
    final_picks = enforce_sector_limits(final_picks, sector_data, max_sector_pct=0.30)

    final_picks["rank_in_portfolio"] = range(1, len(final_picks) + 1)
    # conviction_tier is used as rationale by save_portfolio_holdings
    if "conviction_tier" not in final_picks.columns:
        final_picks["conviction_tier"] = "UNKNOWN"

    # Save
    save_portfolio_holdings(final_picks, date.today())
    logger.success(f"Saved {len(final_picks)} positions for {date.today()}")

    # Print summary with sector breakdown
    print("\nFINAL PORTFOLIO")
    print("=" * 70)
    print(f"Regime:    {regime['regime']} -- {regime['description']}")
    print(f"Positions: {len(final_picks)}")
    print()

    # Enrich with sector for display
    fp_display = final_picks.merge(sector_data[["ticker", "sector"]], on="ticker", how="left")
    fp_display["sector"] = fp_display["sector"].fillna("Unknown")

    for _, row in fp_display.iterrows():
        print(
            f"  {int(row['rank_in_portfolio']):2d}. {row['ticker']:<6}  "
            f"Score:{row['composite_score']:.1f}  "
            f"Weight:{row['weight']:.1%}  "
            f"Price:${row['entry_price']:.2f}  "
            f"Sector:{row['sector']}"
        )

    # Sector concentration summary
    sector_counts = fp_display.groupby("sector").size()
    print(f"\nSector breakdown ({len(final_picks)} positions):")
    for sec, cnt in sector_counts.sort_values(ascending=False).items():
        pct = cnt / len(final_picks)
        bar = "#" * int(pct * 20)
        flag = "  [WARN]" if pct > 0.30 else ""
        print(f"  {sec:<28} {cnt:2d} ({pct:.0%})  {bar}{flag}")

    logger.info("Monthly workflow complete")
    return {
        "regime":      regime,
        "portfolio":   final_picks,
        "risk_report": risk_report,
    }


if __name__ == "__main__":
    run_monthly_workflow()
