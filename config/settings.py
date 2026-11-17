"""
config/settings.py

Central configuration for the quant system.
All constants, thresholds, and parameters live here.
Never hardcode these values in other modules — always import from here.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
DATA_DIR = PROJECT_ROOT / "data"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"
DATABASE_DIR = PROJECT_ROOT / "database"

load_dotenv(PROJECT_ROOT / ".env")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{PROJECT_ROOT}/quant_system.db")

# ---------------------------------------------------------------------------
# API Keys
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
FINNHUB_API_KEY   = os.getenv("FINNHUB_API_KEY", "")
FRED_API_KEY      = os.getenv("FRED_API_KEY", "")

# ---------------------------------------------------------------------------
# Sentiment Agent
# ---------------------------------------------------------------------------

SENTIMENT = {
    "weight_in_composite": 0.15,
    "model": "claude-haiku-4-5-20251001",  # Cheapest Claude model
    "max_transcript_chars": 15000,          # First 15K chars only
    "cache_days": 90,                       # Re-use score for 90 days
}

# ---------------------------------------------------------------------------
# Universe / Stock Filters
# ---------------------------------------------------------------------------

UNIVERSE = {
    "min_market_cap": 50_000_000,       # $50M minimum — avoid nano-caps
    "max_market_cap": 10_000_000_000,   # $10B maximum — stay in small/mid
    "min_avg_daily_volume": 500_000,    # $500K average daily dollar volume
    "min_price": 3.0,                   # $3 minimum price — avoid penny stocks
    "max_debt_to_equity": 5.0,          # Extreme leverage exclusion
    "require_positive_revenue": True,
    "min_history_years": 2,
    "exclude_sectors": ["Financial Services", "Utilities"],
    "exclude_if_negative_equity": True,
}

# ---------------------------------------------------------------------------
# Backtesting Periods
# ---------------------------------------------------------------------------

BACKTEST = {
    "in_sample_start": "2005-01-01",
    "in_sample_end": "2018-12-31",
    "out_of_sample_start": "2019-01-01",
    "out_of_sample_end": "2024-12-31",
    "live_start": "2025-01-01",
}

# ---------------------------------------------------------------------------
# Transaction Costs
# ---------------------------------------------------------------------------

TRANSACTION_COSTS = {
    "bid_ask_spread_pct": 0.003,        # 0.3% each way for small caps
    "commission_per_share": 0.005,      # $0.005 per share
    "min_commission": 1.00,             # $1 minimum per trade
    "market_impact_pct": 0.001,         # 0.1% for positions > $50K
    "market_impact_threshold": 50_000,  # Dollar threshold for market impact
}

# Total assumed round-trip cost: ~0.8% per position change

# ---------------------------------------------------------------------------
# Portfolio Construction
# ---------------------------------------------------------------------------

PORTFOLIO = {
    "target_positions": 25,             # Target number of holdings
    "max_positions": 30,                # Hard maximum
    "min_positions": 10,                # Hard minimum (don't run if fewer stocks pass)
    "rebalance_day": "first",           # "first" = first trading day of month
    "weight_scheme": "equal",           # equal weighting
    "max_sector_weight": 0.30,          # No more than 30% in any one sector
    "max_single_position": 0.08,        # No more than 8% in any one stock
    # Order sizing (broker/ibkr_paper.py build_intents), added 2026-10-04:
    "cash_reserve": 0.005,              # keep 0.5% of NAV uninvested: BUY limits sit up
                                        # to 75 bps above the reference, and the book's
                                        # rounded weights (15 x 0.0667) summed to 100.05%
    "min_trade_weight": 0.0025,         # skip ADJUSTMENTS smaller than 0.25% of NAV
                                        # (~$2.8k). On 2026-10-01 a top-up sent six trims
                                        # of $70-$1,400 that only churned the account.
                                        # Exits and new positions are never skipped.
}

# ---------------------------------------------------------------------------
# Factor Weights (must sum to 1.0)
# ---------------------------------------------------------------------------

FACTOR_WEIGHTS = {
    "value":              0.22,   # Core value (EV/EBITDA, P/B, FCF yield)
    "quality":            0.27,   # Piotroski, ROIC, gross margin
    "momentum":           0.16,   # 12-1 month price momentum
    "sentiment":          0.11,   # Claude transcripts. CONSTANT since 2026-09 (no transcript
                                  # in the 90-day window): ranks nothing. factors/health.py
    "short_interest":     0.07,   # Short % of float + MoM change
    "insider":            0.04,   # Open-market insider buy clusters
    "earnings_surprise":  0.03,   # Beat rate over last 4 quarters
    "proximity_52w":      0.05,   # Position in 52-week range
    "revenue_accel":      0.05,   # Revenue growth acceleration. CONSTANT: needs 6 quarters,
                                  # yfinance gives 5. Ranks nothing. factors/health.py
}

# 4-factor weights used when aux signals are unavailable (e.g. backtesting)
FACTOR_WEIGHTS_BASE = {
    "value":     0.30,
    "quality":   0.35,
    "momentum":  0.20,
    "sentiment": 0.15,
}

FACTOR_WEIGHTS_SCREEN_A = {   # Value-Quality tilt (default)
    "value": 0.35,
    "quality": 0.40,
    "momentum": 0.25,
}

FACTOR_WEIGHTS_SCREEN_B = {   # Momentum tilt
    "value": 0.15,
    "quality": 0.25,
    "momentum": 0.60,
}

# Legacy alias — kept for any callers that reference it directly
FACTOR_WEIGHTS_WITH_SENTIMENT = FACTOR_WEIGHTS_BASE

# ---------------------------------------------------------------------------
# Quality Gates (single source of truth — never hardcode in other modules)
# ---------------------------------------------------------------------------
# Recalibrated 2026-06 after fixing the .info->.balance_sheet ingestion bug that
# had truncated the Piotroski F-score to a 0-4 ceiling. On the corrected 0-9
# scale, f_score>=5 is the canonical Piotroski "financially healthy" line and the
# completeness-aware convergence point. The previously-enforced quality_score>=55
# floor was an undocumented May-2026 tightening and has been RETIRED.

QUALITY_GATES = {
    "f_score_min":        5,       # INCUMBENT construction (restored 2026-09 after the
                                   # 2026Q3 forward test chose KEEP INCUMBENT). Corrected
                                   # 0-9 Piotroski health line. This is the gate the winning
                                   # book was selected under.
                                   # NOTE: the f>=3 demotion (bb4a5c4) was reverted on the
                                   # FORWARD-TEST result, not because the walk-forward study
                                   # behind f>=3 was wrong. That study still stands on its own
                                   # terms (f>=5 halved in-gate value IC +0.124 -> +0.047);
                                   # it was simply outranked by a live head-to-head in which
                                   # the f>=5 book won and the value thesis went negative.
                                   # See forward_test/2026Q3_parallel/EVALUATION_RESULT.md,
                                   # docs/survivorship_findings.md (unprofitability carries a
                                   # ~14x bankruptcy gradient vs ~2-4x for cheapness),
                                   # docs/regime_rules_validation.md lineage + sandbox studies.
    "min_roic":           0.02,    # ROIC > 2%
    "debt_to_ebitda_max": 5.0,     # exclude highly leveraged
    # Sector exclusion is UNIVERSE["exclude_sectors"] (yfinance's spelling, "Financial
    # Services"), applied in factors/calculator.py. An unused copy here said
    # "Financials", which matches no stock, and was removed 2026-11-17.
    # "quality_score_min" intentionally removed (retired undocumented floor)
    # momentum_12_1 NaN -> excluded is structural (no price history), enforced in code
}

# ---------------------------------------------------------------------------
# ML Overlay
# ---------------------------------------------------------------------------
# DISABLED (2026-06). The gradient-boosting overlay was the WORST of three
# walk-forward configurations (leakage-free OOS rank IC +0.015, vs +0.047 gated
# value-tilt and +0.124 full-universe value-tilt) and sign-unstable across folds.
# Set False so rank_universe() uses the weighted-average composite directly.
# The trained .pkl files are RETAINED (model frozen, not deleted) so this can be
# re-enabled if a future leakage-free retrain demonstrates real OOS skill.
ML_OVERLAY_ENABLED = False

# ---------------------------------------------------------------------------
# Data / Reporting Lag
# ---------------------------------------------------------------------------

DATA_LAGS = {
    # RULE 1: Fundamental data only available 90 days after quarter end.
    # available_date = MAX(filing_date, period_end_date + 90 days)
    "fundamental_lag_days": 90,
}

# ---------------------------------------------------------------------------
# Momentum Calculation Windows
# ---------------------------------------------------------------------------

MOMENTUM = {
    "lookback_months": 12,              # Total lookback
    "skip_months": 1,                   # Skip most recent month (reversal effect)
    # Effective window = 12-1 = 11 months of returns
}

# ---------------------------------------------------------------------------
# Validation Thresholds
# ---------------------------------------------------------------------------

VALIDATION = {
    # Phase 3 gate: out-of-sample must exceed these to proceed to Phase 4
    "min_out_of_sample_sharpe": 0.6,
    "max_out_of_sample_drawdown": 0.40,
    # Phase 4 gate: sentiment layer only kept if Sharpe improves by this much
    "sentiment_min_sharpe_improvement": 0.1,
}

# ---------------------------------------------------------------------------
# Kelly Position Sizing
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Execution (PAPER broker path) — limit placement, repricing, slicing
# ---------------------------------------------------------------------------
# Calibrated from the measured 2026-09-29 paper run (see
# broker/reconciliation/underfill_diagnosis.csv and the commit that produced it):
# 9/15 filled, 82.1% of shares, book 69.88% deployed. Attribution of the shortfall:
#   43.3%  limit never reached  -> the flat 30bps buffer could not bridge a delayed
#                                  quote that understated the true price by 42-62bps
#   56.7%  limit reached but short -> orders were cancelled after ~4 minutes, which is
#                                  ~1% of a session's liquidity; the median order was
#                                  ~66% of the liquidity available in that time
# So the buffer is sized to the quote-delay horizon, and orders are given time and
# sliced rather than fired once and cancelled.
EXECUTION = {
    # ── limit placement ────────────────────────────────────────────────────
    # The account has no real-time data, so quotes are ~15 min delayed. The buffer
    # is sized to cover a 1-sigma adverse move over exactly that delay horizon:
    #   buffer_bps = clamp(vol_mult * sigma_daily_bps * sqrt(delay_min/390), min, max)
    # DEFAULT vol_mult = 1.0 -> one standard deviation of the move that can have
    # happened while the quote was going stale. Principled rather than a round number,
    # and it adapts per name: a quiet name gets a tight limit, a volatile one gets
    # more room, instead of one flat 30bps for everything.
    "quote_delay_minutes":      15,
    "buffer_vol_mult":          1.0,
    "buffer_min_bps":           15,     # floor: never price inside the spread
    "buffer_max_bps":           75,     # HARD CEILING — a limit can never become a
                                        # de facto market order, however volatile
    "fallback_sigma_daily_bps": 250,    # used only if price history is unavailable

    # ── repricing ──────────────────────────────────────────────────────────
    # If still unfilled after the interval, cancel and re-submit higher — bounded in
    # BOTH attempts and cumulative drift, so a chase can never run away. Never
    # converts to a market order.
    "reprice_interval_s":       90,
    "reprice_max_attempts":     3,
    "reprice_step_bps":         20,
    "reprice_total_drift_cap_bps": 60,  # hard cap on cumulative drift from the
                                        # ORIGINAL limit across all reprices

    # ── slicing (TWAP) ─────────────────────────────────────────────────────
    # Slice when the order would consume more than `slice_max_participation` of the
    # liquidity realistically available over the execution horizon, estimated as
    #   ADV * (exec_horizon_minutes / 390)
    # DEFAULT 20%: on 2026-09-29 the median order was ~66% of the 4-minute liquidity
    # and the worst were >100%, which is precisely what starved the fills.
    "exec_horizon_minutes":     20,
    "slice_max_participation":  0.20,
    "slice_max_children":       6,
    "slice_interval_s":         120,
}

KELLY = {
    "fraction":      0.25,    # Quarter Kelly — standard institutional practice
    "max_position":  0.15,    # 15% hard cap per position
    "min_position":  0.02,    # 2% floor per position
    "lookback_days": 252,     # 1-year volatility window
    "risk_free_rate": 0.0364, # Current Fed funds rate (annualised)
}

# ---------------------------------------------------------------------------
# Active forward test
# ---------------------------------------------------------------------------
# The ONE place the active forward-test period is named. forward_test/snapshot.py's
# default, the broker's kill-switch snapshot (5.3), the dashboard and the printed
# --book hints all read it. Two places cannot: forward_test/run_snapshot.bat (batch
# cannot import Python) and the per-quarter builder build_q4_books.py (frozen with its
# quarter). tests/test_silent_failure_rules.py fails until the .bat agrees and until
# no other code names a period. A period mismatch hid nine silent days once.
ACTIVE_FT_PERIOD = "2026Q4b"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_DIR = OUTPUTS_DIR / "logs"
