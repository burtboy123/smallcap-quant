"""
factors/composite.py

Composite factor scoring: percentile ranking and weighted combination.

Scoring logic (from master prompt, exactly):
  Step 1: Percentile rank each raw factor within the universe (0-100).
          Lower raw value = better for "cheap" factors → invert rank.
  Step 2: Average within each category.
  Step 3: Weighted composite:
            value    35%
            quality  40%
            momentum 25%
  Step 4: Final universe rank (1 = best).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from loguru import logger

from config.settings import (
    FACTOR_WEIGHTS,
    FACTOR_WEIGHTS_BASE,
    FACTOR_WEIGHTS_SCREEN_A,
    FACTOR_WEIGHTS_SCREEN_B,
    QUALITY_GATES,
    ML_OVERLAY_ENABLED,
)

# Sentinel: True once the Claude sentiment agent is importable
try:
    import importlib
    importlib.util.find_spec("agents.sentiment_agent")
    import agents.sentiment_agent as _sa_mod
    SENTIMENT_AVAILABLE = hasattr(_sa_mod, "analyse_sentiment")
    del _sa_mod
except Exception:
    SENTIMENT_AVAILABLE = False

# ---------------------------------------------------------------------------
# Hard quality gates — stocks failing any gate are excluded from ranking
# ---------------------------------------------------------------------------

# Thresholds come from config.settings.QUALITY_GATES (single source of truth).
# f_score_min=5 on the corrected 0-9 Piotroski scale; quality_score floor retired.
MINIMUM_QUALITY_GATES = QUALITY_GATES


# ---------------------------------------------------------------------------
# Percentile ranking
# ---------------------------------------------------------------------------

def percentile_rank(series: pd.Series, ascending: bool = True) -> pd.Series:
    """
    Rank a series as percentile scores 0-100 within the non-NaN universe.

    ascending=True  → higher raw value = higher percentile (quality, momentum)
    ascending=False → lower raw value = higher percentile (value: low EV/EBITDA is good)

    NaN values remain NaN in the output.
    """
    if series.empty:
        return series.copy()

    valid_mask = series.notna()
    if valid_mask.sum() < 2:
        # Can't rank a single value — return 50 for the one valid value
        result = series.copy() * np.nan
        result[valid_mask] = 50.0
        return result

    ranked = series[valid_mask].rank(
        method="average",
        ascending=ascending,
        na_option="keep",
        pct=True,
    ) * 100.0

    result = series.copy() * np.nan
    result[valid_mask] = ranked
    return result


# ---------------------------------------------------------------------------
# Category score builders
# ---------------------------------------------------------------------------

def build_value_score(factors: pd.DataFrame) -> pd.Series:
    """
    Average of value factor percentile scores.

    Factors (lower raw = better, so ascending=False for rank):
      - ev_ebitda     (lower = cheaper)
      - pb_ratio      (lower = cheaper)

    Factors (higher raw = better, ascending=True):
      - fcf_yield     (higher = more cash generation)
      - earnings_yield (higher = cheaper earnings)

    Returns percentile score 0-100 per ticker.
    """
    components = {}

    if "ev_ebitda" in factors.columns:
        components["ev_ebitda_score"] = percentile_rank(factors["ev_ebitda"], ascending=False)

    if "pb_ratio" in factors.columns:
        components["pb_score"] = percentile_rank(factors["pb_ratio"], ascending=False)

    if "fcf_yield" in factors.columns:
        components["fcf_yield_score"] = percentile_rank(factors["fcf_yield"], ascending=True)

    if "earnings_yield" in factors.columns:
        components["earnings_yield_score"] = percentile_rank(factors["earnings_yield"], ascending=True)

    if not components:
        return pd.Series(np.nan, index=factors.index, name="value_score")

    component_df = pd.DataFrame(components, index=factors.index)
    # Average across available components per row (ignore NaN)
    value_score = component_df.mean(axis=1, skipna=True)
    return value_score.rename("value_score")


def build_quality_score(factors: pd.DataFrame) -> pd.Series:
    """
    Average of quality factor percentile scores.

    Factors (higher = better, ascending=True):
      - roic
      - gross_margin
      - f_score

    Factors (lower = better, ascending=False):
      - accruals_ratio   (lower = better earnings quality)
    """
    components = {}

    if "roic" in factors.columns:
        components["roic_score"] = percentile_rank(factors["roic"], ascending=True)

    if "gross_margin" in factors.columns:
        components["gm_score"] = percentile_rank(factors["gross_margin"], ascending=True)

    if "f_score" in factors.columns:
        components["fscore_score"] = percentile_rank(factors["f_score"].astype(float), ascending=True)

    if "accruals_ratio" in factors.columns:
        components["accruals_score"] = percentile_rank(factors["accruals_ratio"], ascending=False)

    if not components:
        return pd.Series(np.nan, index=factors.index, name="quality_score")

    component_df = pd.DataFrame(components, index=factors.index)
    quality_score = component_df.mean(axis=1, skipna=True)
    return quality_score.rename("quality_score")


def build_momentum_score(factors: pd.DataFrame) -> pd.Series:
    """
    Momentum score. Currently uses 12-1 month momentum only.

    Higher raw momentum = higher percentile = better.
    """
    components = {}

    if "momentum_12_1" in factors.columns:
        components["mom_score"] = percentile_rank(factors["momentum_12_1"], ascending=True)

    if "eps_revision_3m" in factors.columns and factors["eps_revision_3m"].notna().sum() > 10:
        # Only include EPS revision if we have enough data points
        components["eps_rev_score"] = percentile_rank(factors["eps_revision_3m"], ascending=True)

    if not components:
        return pd.Series(np.nan, index=factors.index, name="momentum_score")

    component_df = pd.DataFrame(components, index=factors.index)
    momentum_score = component_df.mean(axis=1, skipna=True)
    return momentum_score.rename("momentum_score")


# ---------------------------------------------------------------------------
# DB-backed sentiment loader
# ---------------------------------------------------------------------------

def add_sentiment_scores(scores_df: pd.DataFrame) -> pd.DataFrame:
    """
    Batch-query the sentiment_scores table and attach a sentiment_score column
    (0-100) to scores_df. Applies to ALL stocks in the universe.

    Stocks with no DB entry within the last 90 days receive 50.0 (neutral),
    so they participate in ranking without being penalised for missing data.

    Args:
        scores_df: DataFrame indexed by ticker (or with a 'ticker' column).

    Returns:
        Copy with 'sentiment_score' column added.
    """
    import sqlite3
    from datetime import date, timedelta
    from config.settings import DATABASE_URL

    df = scores_df.copy()

    # Determine whether ticker lives in a column or in the index.
    # Factor DataFrames are indexed by ticker strings but index.name may be None.
    ticker_is_column = "ticker" in df.columns

    db_path = DATABASE_URL.replace("sqlite:///", "")
    cutoff  = date.today() - timedelta(days=90)

    try:
        conn = sqlite3.connect(db_path)
        sentiment_data = pd.read_sql(
            """
            SELECT ticker, overall_sentiment,
                   management_confidence, guidance_direction
            FROM sentiment_scores
            WHERE available_date >= ?
            ORDER BY transcript_date DESC
            """,
            conn,
            params=[str(cutoff)],
        )
        conn.close()
    except Exception as e:
        logger.warning(f"add_sentiment_scores: DB read failed — {e}")
        df["sentiment_score"] = 50.0
        return df

    if sentiment_data.empty:
        df["sentiment_score"] = 50.0
        return df

    # Keep most recent row per ticker (query already ordered by transcript_date DESC)
    sentiment_data = sentiment_data.drop_duplicates("ticker", keep="first")

    def _calc(row) -> float:
        sent  = row["overall_sentiment"]      # -1 to 1
        conf  = row["management_confidence"]  #  0 to 1
        bonus = {
            "raised": 15, "maintained": 0,
            "lowered": -20, "mixed": -5, "none": 0,
        }.get(row["guidance_direction"] or "none", 0)
        sent_score = (sent + 1) / 2 * 100    # → 0-100
        conf_score = conf * 100               # → 0-100
        return min(100.0, max(0.0, sent_score * 0.5 + conf_score * 0.3 + bonus))

    sentiment_data["sentiment_score"] = sentiment_data.apply(_calc, axis=1)
    sent_lookup = sentiment_data.set_index("ticker")["sentiment_score"]

    if ticker_is_column:
        df = df.merge(
            sentiment_data[["ticker", "sentiment_score"]],
            on="ticker",
            how="left",
        )
    else:
        # Index contains ticker strings — map directly
        df["sentiment_score"] = df.index.map(sent_lookup)

    # Neutral fill for stocks with no recent sentiment data
    df["sentiment_score"] = df["sentiment_score"].fillna(50.0)

    n_real   = int(df["sentiment_score"].ne(50.0).sum())
    n_filled = int(df["sentiment_score"].eq(50.0).sum())
    logger.info(
        f"add_sentiment_scores: {n_real} tickers with real scores, "
        f"{n_filled} filled neutral (50.0)"
    )

    return df


# ---------------------------------------------------------------------------
# Sentiment score builder
# ---------------------------------------------------------------------------

def build_sentiment_score(
    sentiment_scores: dict[str, float],
    tickers: list[str],
) -> pd.Series:
    """
    Convert raw sentiment scores {ticker: -1..+1} into percentile ranks 0-100.

    Only tickers in `tickers` are included. Tickers missing from
    sentiment_scores are left as NaN (treated as missing data, not neutral).

    Args:
        sentiment_scores: {ticker: raw_score} from agents.sentiment_agent.extract_scores().
        tickers:          Full universe ticker list (sets the index).

    Returns:
        pd.Series indexed by ticker, values 0-100, name="sentiment_score".
    """
    raw = pd.Series(
        {t: sentiment_scores.get(t, float("nan")) for t in tickers},
        name="raw_sentiment",
    )
    # Higher raw score = more positive = better rank
    score = percentile_rank(raw, ascending=True)
    return score.rename("sentiment_score")


# ---------------------------------------------------------------------------
# Composite score
# ---------------------------------------------------------------------------

def build_composite_score(
    value_score: pd.Series,
    quality_score: pd.Series,
    momentum_score: pd.Series,
    weights: dict | None = None,
) -> pd.Series:
    """
    Weighted composite of the three category scores.

    Default weights: value 35%, quality 40%, momentum 25%.
    A stock must have at least 2 of 3 category scores to receive a composite.
    """
    if weights is None:
        weights = FACTOR_WEIGHTS

    df = pd.DataFrame({
        "value":    value_score,
        "quality":  quality_score,
        "momentum": momentum_score,
    })

    # Require at least 2 of 3 category scores to be non-NaN
    valid_count = df.notna().sum(axis=1)
    too_few = valid_count < 2

    # Weighted mean ignoring NaN: weight_i * score_i / sum(weights where score not NaN)
    w = pd.Series(weights)  # {'value': 0.35, 'quality': 0.40, 'momentum': 0.25}

    def _weighted_mean(row):
        available = row.dropna()
        if len(available) < 2:
            return np.nan
        total_weight = sum(w[k] for k in available.index if k in w)
        if total_weight == 0:
            return np.nan
        return sum(available[k] * w[k] for k in available.index if k in w) / total_weight

    composite = df.apply(_weighted_mean, axis=1)
    composite[too_few] = np.nan
    return composite.rename("composite_score")


def rank_universe(
    composite_score: pd.Series,
    scores_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Produce final universe ranking from composite scores.

    When `scores_df` is provided (with all 9 factor columns), attempts to use
    the trained ML model to re-score the universe before ranking.
    Falls back to the supplied weighted-average composite_score when the model
    is unavailable or has fewer than MIN_SAMPLES training points.

    Returns DataFrame with:
        composite_score, universe_rank, universe_size, percentile, scoring_method
    """
    # ── Try ML model ──────────────────────────────────────────────────────────
    scoring_method = "weighted_average"
    if ML_OVERLAY_ENABLED and scores_df is not None and not scores_df.empty:
        try:
            from models.factor_model import predict_scores
            ml_scores = predict_scores(scores_df)
            if ml_scores is not None:
                # Align to composite_score's index (only non-NaN positions)
                valid_idx     = composite_score.dropna().index
                aligned       = ml_scores.reindex(valid_idx)
                if aligned.notna().sum() > 0:
                    composite_score = composite_score.copy()
                    composite_score.update(aligned)
                    scoring_method = "ml_gradient_boosting"
                    logger.info("rank_universe: ML model scores applied")
        except Exception as e:
            logger.debug(f"rank_universe: ML scoring skipped — {e}")

    # ── Percentile rank ────────────────────────────────────────────────────────
    valid = composite_score.dropna()
    n     = len(valid)

    ranks       = valid.rank(method="min", ascending=False).astype(int)
    percentiles = (1 - (ranks - 1) / n) * 100

    result = pd.DataFrame({
        "composite_score": composite_score,
        "universe_rank":   ranks.reindex(composite_score.index),
        "universe_size":   n,
        "percentile":      percentiles.reindex(composite_score.index),
        "scoring_method":  scoring_method,
    })

    return result


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def calculate_composite_scores(
    value_factors: pd.DataFrame,
    quality_factors: pd.DataFrame,
    momentum_factors: pd.DataFrame,
    weights: dict | None = None,
    sentiment_scores: dict[str, float] | None = None,
    auxiliary_scores: dict[str, pd.Series] | None = None,
) -> pd.DataFrame:
    """
    Combine all factor DataFrames into final composite scores.

    Args:
        value_factors:    Output of calculate_value_factors().
        quality_factors:  Output of calculate_quality_factors().
        momentum_factors: Output of calculate_momentum_factors().
        weights:          Optional weight override (must cover all active factors).
        sentiment_scores: Optional {ticker: score} from sentiment_agent.extract_scores().
        auxiliary_scores: Optional {factor_name: pd.Series(0-100)} for extra signals
                          e.g. {"short_interest": Series, "insider": Series,
                                "earnings_surprise": Series}.
                          Series should be indexed by ticker.

    Returns:
        DataFrame indexed by ticker with all raw factors + scores + ranks.
    """
    # Merge all factor frames on index (ticker)
    all_factors = pd.concat(
        [f for f in [value_factors, quality_factors, momentum_factors] if not f.empty],
        axis=1,
    )

    if all_factors.empty:
        logger.warning("composite: all factor DataFrames are empty")
        return pd.DataFrame()

    # Build category scores
    value_score    = build_value_score(all_factors)
    quality_score  = build_quality_score(all_factors)
    momentum_score = build_momentum_score(all_factors)

    # Auto-fetch sentiment from DB when not explicitly provided
    if sentiment_scores is None and SENTIMENT_AVAILABLE:
        enriched = add_sentiment_scores(all_factors)
        if "sentiment_score" in enriched.columns:
            db_sent = enriched["sentiment_score"].dropna()
            if not db_sent.empty:
                sentiment_scores = db_sent.to_dict()
                logger.info(f"Auto-fetched sentiment from DB: {len(sentiment_scores)} tickers")

    # Build sentiment score Series (percentile-ranked)
    use_sentiment = sentiment_scores is not None and len(sentiment_scores) > 0
    sentiment_score: pd.Series | None = None
    if use_sentiment:
        tickers = all_factors.index.tolist()
        sentiment_score = build_sentiment_score(sentiment_scores, tickers)
        logger.info(f"Sentiment layer active: {sentiment_score.notna().sum()}/{len(tickers)} tickers scored")

    # Collect all extra factor scores; percentile-rank each so they're on 0-100 scale
    extra_factors: dict[str, pd.Series] = {}
    if use_sentiment and sentiment_score is not None:
        extra_factors["sentiment"] = sentiment_score

    if auxiliary_scores:
        for name, raw_series in auxiliary_scores.items():
            aligned = raw_series.reindex(all_factors.index)
            extra_factors[name] = percentile_rank(aligned, ascending=True)
        logger.info(f"Aux signals active: {list(auxiliary_scores.keys())}")

    # Determine weights and build composite
    has_extras = bool(extra_factors)
    if has_extras:
        effective_weights = weights or FACTOR_WEIGHTS
        composite_score = _build_weighted_composite(
            value_score, quality_score, momentum_score,
            extra_factors, effective_weights,
        )
    else:
        effective_weights = weights or FACTOR_WEIGHTS_BASE
        composite_score = build_composite_score(
            value_score, quality_score, momentum_score, effective_weights
        )

    # Apply hard quality gates — NaN-out composite for failing stocks
    composite_score, passed_gate = _apply_quality_gates(composite_score, all_factors)

    # Assemble feature matrix for ML model (before ranking)
    ml_input = all_factors.copy()
    ml_input["value_score"]    = value_score
    ml_input["quality_score"]  = quality_score
    ml_input["momentum_score"] = momentum_score
    for name, series in extra_factors.items():
        ml_input[f"{name}_score"] = series

    # Universe ranking — passes feature matrix so ML can re-score if available
    ranking = rank_universe(composite_score, scores_df=ml_input)

    # Assemble final output
    result = all_factors.copy()
    result["value_score"]    = value_score
    result["quality_score"]  = quality_score
    result["momentum_score"] = momentum_score
    for name, series in extra_factors.items():
        result[f"{name}_score"] = series
    result = result.join(ranking[["composite_score", "universe_rank", "universe_size", "percentile", "scoring_method"]])
    result["passed_quality_gate"] = passed_gate

    n_ranked   = int(result["universe_rank"].notna().sum())
    n_excluded = int((~passed_gate).sum())
    logger.info(
        f"Composite scores: {n_ranked} tickers ranked, {n_excluded} excluded "
        f"(top score: {result['composite_score'].max():.1f}, weights={effective_weights})"
    )

    return result


def _apply_quality_gates(
    composite_score: pd.Series,
    all_factors: pd.DataFrame,
) -> tuple[pd.Series, pd.Series]:
    """
    Zero out composite_score (set to NaN) for any stock that fails a
    hard quality gate. Returns (filtered_composite, passed_gate_bool_series).

    Gates (from config.settings.QUALITY_GATES):
      - f_score < 5               → exclude  (corrected 0-9 Piotroski scale)
      - momentum_12_1 is NaN      → exclude  (no price history)
      - debt_to_ebitda > 5.0      → exclude
      - roic < 0.02               → exclude
      (sector gate skipped — sector not present in factor DataFrames)
      (quality_score>=55 floor RETIRED 2026-06 — was an undocumented tightening)
    """
    passed = pd.Series(True, index=composite_score.index)

    # Gate 1: Piotroski F-Score — only exclude if value IS available and explicitly too low.
    # NaN f_score = data unavailable; do not penalise (f_score still contributes to quality_score ranking).
    if "f_score" in all_factors.columns:
        f = all_factors["f_score"]
        fail = f.notna() & (f < MINIMUM_QUALITY_GATES["f_score_min"])
        n = int(fail.sum())
        if n:
            logger.debug(f"Quality gate — f_score < {MINIMUM_QUALITY_GATES['f_score_min']}: {n} excluded")
        passed &= ~fail

    # Gate 2: NaN momentum — skip if momentum column present but NaN (no price history).
    if "momentum_12_1" in all_factors.columns:
        fail = all_factors["momentum_12_1"].isna()
        n = int(fail.sum())
        if n:
            logger.debug(f"Quality gate — momentum_12_1 is NaN: {n} excluded")
        passed &= ~fail

    # Gate 3: Debt / EBITDA — only exclude when value IS available and explicitly over limit.
    if "debt_to_ebitda" in all_factors.columns:
        d = all_factors["debt_to_ebitda"]
        fail = d.notna() & (d > MINIMUM_QUALITY_GATES["debt_to_ebitda_max"])
        n = int(fail.sum())
        if n:
            logger.debug(f"Quality gate — debt_to_ebitda > {MINIMUM_QUALITY_GATES['debt_to_ebitda_max']}: {n} excluded")
        passed &= ~fail

    # Gate 4: ROIC — only exclude when value IS available and explicitly negative / too low.
    if "roic" in all_factors.columns:
        r = all_factors["roic"]
        fail = r.notna() & (r < MINIMUM_QUALITY_GATES["min_roic"])
        n = int(fail.sum())
        if n:
            logger.debug(f"Quality gate — roic < {MINIMUM_QUALITY_GATES['min_roic']}: {n} excluded")
        passed &= ~fail

    # Gate 5 (quality_score >= 55 floor) RETIRED 2026-06: it was an undocumented
    # May-2026 tightening, and on the corrected 0-9 F-score the f_score>=5 gate is
    # the quality bar. Kept here as a no-op note for history.

    filtered = composite_score.copy()
    filtered[~passed] = np.nan
    return filtered, passed


def _build_weighted_composite(
    value_score: pd.Series,
    quality_score: pd.Series,
    momentum_score: pd.Series,
    extra_factors: dict[str, pd.Series],
    weights: dict,
) -> pd.Series:
    """
    Weighted composite including any number of extra factor scores.
    Requires at least 2 of the 3 core factors (value/quality/momentum).
    Extra factors (sentiment, short_interest, insider, etc.) are optional bonuses.
    """
    df = pd.DataFrame({
        "value":    value_score,
        "quality":  quality_score,
        "momentum": momentum_score,
        **extra_factors,
    })

    w = pd.Series(weights)

    def _weighted_mean(row):
        available = row.dropna()
        core_available = [k for k in ["value", "quality", "momentum"] if k in available.index]
        if len(core_available) < 2:
            return np.nan
        total_weight = sum(w.get(k, 0) for k in available.index)
        if total_weight == 0:
            return np.nan
        return sum(available[k] * w.get(k, 0) for k in available.index) / total_weight

    return df.apply(_weighted_mean, axis=1).rename("composite_score")


# ---------------------------------------------------------------------------
# Ensemble ranking
# ---------------------------------------------------------------------------

def ensemble_rank(scores_df: pd.DataFrame) -> pd.DataFrame:
    """
    Run two screens (A = value/quality, B = momentum) and combine into
    conviction tiers.

    Args:
        scores_df: Output of calculate_composite_scores() — must contain
                   value_score, quality_score, momentum_score columns and
                   be indexed by ticker.

    Returns:
        DataFrame sorted by agreement_score descending with columns:
            ticker, score_a, score_b, pct_a, pct_b,
            agreement_score, conviction_tier
    """
    if scores_df.empty:
        return pd.DataFrame()

    # Ensure ticker is a column
    df = scores_df.copy()
    if "ticker" not in df.columns and df.index.name == "ticker":
        df = df.reset_index()

    # Rebuild composite scores for each screen using only quality-gate-passed rows
    gate_mask = df.get("passed_quality_gate", pd.Series(True, index=df.index))
    passed = df[gate_mask].copy()

    if passed.empty:
        logger.warning("ensemble_rank: no stocks passed quality gate")
        return pd.DataFrame()

    passed = passed.set_index("ticker") if "ticker" in passed.columns else passed

    def _composite_for_weights(w: dict) -> pd.Series:
        v = passed.get("value_score",    pd.Series(np.nan, index=passed.index))
        q = passed.get("quality_score",  pd.Series(np.nan, index=passed.index))
        m = passed.get("momentum_score", pd.Series(np.nan, index=passed.index))
        return build_composite_score(v, q, m, weights=w)

    score_a = _composite_for_weights(FACTOR_WEIGHTS_SCREEN_A)
    score_b = _composite_for_weights(FACTOR_WEIGHTS_SCREEN_B)

    pct_a = score_a.rank(pct=True, na_option="bottom") * 100
    pct_b = score_b.rank(pct=True, na_option="bottom") * 100

    result = pd.DataFrame({
        "ticker":          passed.index,
        "score_a":         score_a.values,
        "score_b":         score_b.values,
        "pct_a":           pct_a.values,
        "pct_b":           pct_b.values,
        "agreement_score": ((pct_a + pct_b) / 2).values,
    })

    def _tier(row) -> str:
        a_top = row["pct_a"] >= 80
        b_top = row["pct_b"] >= 80
        a_mid = row["pct_a"] >= 60
        b_mid = row["pct_b"] >= 60
        if a_top and b_top:
            return "TIER1_STRONG_BUY"
        if a_top and b_mid:
            return "TIER2_VALUE_BUY"
        if b_top and a_mid:
            return "TIER3_MOMENTUM_WATCH"
        if a_mid or b_mid:
            return "TIER4_WATCHLIST"
        return "TIER5_AVOID"

    result["conviction_tier"] = result.apply(_tier, axis=1)
    return result.sort_values("agreement_score", ascending=False).reset_index(drop=True)
