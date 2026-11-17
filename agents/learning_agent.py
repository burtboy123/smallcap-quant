"""
agents/learning_agent.py

Learning Agent — the self-improvement engine.

Evaluates past portfolio picks against realized returns,
labels outcomes, stores them for model retraining, and
generates a learning report explaining what worked and why.

This is what separates a static quant system from one
that genuinely improves over time.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import DATABASE_URL

_DB_PATH = DATABASE_URL.replace("sqlite:///", "")
_MODEL_VERSION = "6C_regime_conditional"


# ---------------------------------------------------------------------------
# Return fetching
# ---------------------------------------------------------------------------

def fetch_realized_returns(
    tickers: list[str],
    entry_date: str,
    exit_date: str,
) -> dict[str, dict | None]:
    """
    Fetch realized return stats for each ticker between entry and exit dates.
    Returns dict ticker -> stats dict (or None if unavailable).
    """
    results: dict[str, dict | None] = {}
    start = (pd.Timestamp(entry_date) - timedelta(days=5)).isoformat()[:10]
    end   = (pd.Timestamp(exit_date)  + timedelta(days=5)).isoformat()[:10]
    entry_ts = pd.Timestamp(entry_date).tz_localize(None)
    exit_ts  = pd.Timestamp(exit_date).tz_localize(None)

    for ticker in tickers:
        try:
            df = yf.Ticker(ticker).history(start=start, end=end)
            if df.empty or len(df) < 2:
                results[ticker] = None
                continue

            # Normalize index to tz-naive
            idx = df.index.tz_localize(None) if df.index.tz is not None else df.index
            df = df.set_index(idx)

            entry_slice = df[df.index >= entry_ts]
            exit_slice  = df[df.index <= exit_ts]
            if entry_slice.empty or exit_slice.empty:
                results[ticker] = None
                continue

            entry_price = float(entry_slice["Close"].iloc[0])
            exit_price  = float(exit_slice["Close"].iloc[-1])
            total_return = (exit_price - entry_price) / entry_price

            period = df[(df.index >= entry_ts) & (df.index <= exit_ts)]["Close"]
            daily_rets = period.pct_change().dropna()

            if len(daily_rets) > 1:
                cumulative  = (1 + daily_rets).cumprod()
                rolling_max = cumulative.expanding().max()
                drawdowns   = (cumulative - rolling_max) / rolling_max
                max_drawdown = float(drawdowns.min())
                volatility   = float(daily_rets.std() * np.sqrt(252))
            else:
                max_drawdown = 0.0
                volatility   = 0.0

            results[ticker] = {
                "entry_price":    entry_price,
                "exit_price":     exit_price,
                "realized_return": total_return,
                "max_drawdown":   max_drawdown,
                "volatility":     volatility,
                "trading_days":   len(daily_rets),
            }
        except Exception as e:
            logger.warning(f"{ticker}: return calculation failed — {e}")
            results[ticker] = None

    return results


def fetch_spy_return(entry_date: str, exit_date: str) -> float:
    """SPY total return for the same holding period — used as benchmark."""
    try:
        start    = (pd.Timestamp(entry_date) - timedelta(days=5)).isoformat()[:10]
        end      = (pd.Timestamp(exit_date)  + timedelta(days=5)).isoformat()[:10]
        entry_ts = pd.Timestamp(entry_date).tz_localize(None)
        exit_ts  = pd.Timestamp(exit_date).tz_localize(None)

        spy = yf.Ticker("SPY").history(start=start, end=end)
        if spy.empty:
            return 0.0

        idx = spy.index.tz_localize(None) if spy.index.tz is not None else spy.index
        spy = spy.set_index(idx)

        ep = spy[spy.index >= entry_ts]
        xp = spy[spy.index <= exit_ts]
        if ep.empty or xp.empty:
            return 0.0

        return (float(xp["Close"].iloc[-1]) - float(ep["Close"].iloc[0])) / float(ep["Close"].iloc[0])
    except Exception as e:
        logger.warning(f"SPY return fetch failed — {e}")
        return 0.0


# ---------------------------------------------------------------------------
# Factor attribution
# ---------------------------------------------------------------------------

def analyse_factor_attribution(
    entry_scores: dict,
    realized_return: float,
    spy_return: float,
) -> dict:
    """
    Post-hoc analysis: which factors correctly predicted the outcome?

    "Predicted correctly" means:
      - High score (>65) AND stock beat SPY
      - Low score (<35) AND stock underperformed SPY
    """
    alpha    = realized_return - spy_return
    beat_spy = alpha > 0

    factor_map = {
        "value":            entry_scores.get("entry_value_score"),
        "quality":          entry_scores.get("entry_quality_score"),
        "momentum":         entry_scores.get("entry_momentum_score"),
        "sentiment":        entry_scores.get("entry_sentiment_score"),
        "short_interest":   entry_scores.get("entry_short_interest_score"),
        "insider":          entry_scores.get("entry_insider_score"),
        "earnings_surprise":entry_scores.get("entry_earnings_surprise_score"),
    }

    correct: list[str] = []
    failed:  list[str] = []

    for factor, score in factor_map.items():
        if score is None or score == 50:
            continue
        high = score > 65
        low  = score < 35
        if (high and beat_spy) or (low and not beat_spy):
            correct.append(factor)
        elif (high and not beat_spy) or (low and beat_spy):
            failed.append(factor)

    return {"factors_correct": correct, "factors_failed": failed,
            "alpha": alpha, "beat_spy": beat_spy}


# ---------------------------------------------------------------------------
# Main evaluation
# ---------------------------------------------------------------------------

def evaluate_past_portfolio(
    lookback_days: int = 90,
    min_hold_days: int = 60,
) -> pd.DataFrame:
    """
    Evaluate portfolio holdings that are at least min_hold_days old.
    Fetches realized returns, calculates alpha vs SPY, labels outcomes,
    stores them in portfolio_outcomes, and returns a DataFrame.
    """
    conn = sqlite3.connect(_DB_PATH)
    cutoff = (date.today() - timedelta(days=min_hold_days)).isoformat()

    past_dates = pd.read_sql(
        f"SELECT DISTINCT rebalance_date FROM portfolio_holdings "
        f"WHERE rebalance_date <= '{cutoff}' ORDER BY rebalance_date DESC LIMIT 6",
        conn,
    )

    if past_dates.empty:
        logger.warning("No mature portfolio entries found (need picks >= "
                       f"{min_hold_days} days old)")
        conn.close()
        return pd.DataFrame()

    all_outcomes: list[dict] = []

    for _, prow in past_dates.iterrows():
        entry_date = prow["rebalance_date"]
        entry_dt   = pd.Timestamp(entry_date)
        exit_dt    = min(entry_dt + timedelta(days=lookback_days),
                         pd.Timestamp(date.today()))
        exit_date  = exit_dt.strftime("%Y-%m-%d")
        hold_days  = (exit_dt - entry_dt).days

        if hold_days < min_hold_days:
            logger.info(f"Skipping {entry_date} — only {hold_days} days elapsed")
            continue

        already = pd.read_sql(
            f"SELECT COUNT(*) as n FROM portfolio_outcomes "
            f"WHERE entry_date = '{entry_date}'", conn
        ).iloc[0, 0]
        if already > 0:
            logger.info(f"{entry_date}: already evaluated — skipping")
            continue

        logger.info(f"Evaluating {entry_date} → {exit_date} ({hold_days}d)")

        # Load holdings + factor scores at entry date
        holdings = pd.read_sql(f"""
            SELECT ph.ticker, ph.composite_score, ph.weight,
                   ph.entry_price, ph.rationale,
                   fs.value_score    AS entry_value_score,
                   fs.quality_score  AS entry_quality_score,
                   fs.momentum_score AS entry_momentum_score,
                   fs.short_interest_score AS entry_short_interest_score,
                   fs.insider_score        AS entry_insider_score,
                   fs.earnings_surprise_score AS entry_earnings_surprise_score
            FROM portfolio_holdings ph
            LEFT JOIN factor_scores fs
                ON  ph.ticker = fs.ticker
                AND fs.calculation_date = ph.rebalance_date
            WHERE ph.rebalance_date = '{entry_date}'
        """, conn)

        if holdings.empty:
            continue

        tickers = holdings["ticker"].tolist()

        spy_return = fetch_spy_return(entry_date, exit_date)
        logger.info(f"SPY benchmark {entry_date}→{exit_date}: {spy_return:+.1%}")

        realized = fetch_realized_returns(tickers, entry_date, exit_date)

        valid_returns = [v["realized_return"] for v in realized.values() if v]
        if not valid_returns:
            logger.warning(f"{entry_date}: no valid return data")
            continue

        # Pre-compute quintile cuts outside the per-ticker loop
        def _quintile_rank(value: float, all_vals: list[float], n: int = 5) -> int | None:
            if len(all_vals) < n:
                return None
            try:
                bins = pd.qcut(all_vals, q=n, retbins=True, duplicates="drop")[1]
                label = pd.cut([value], bins=bins,
                               labels=list(range(1, len(bins))),
                               include_lowest=True)[0]
                return int(label) if not pd.isna(label) else None
            except Exception:
                return None

        alpha_values = [r - spy_return for r in valid_returns]
        outcomes_for_date: list[dict] = []

        for _, h in holdings.iterrows():
            ticker = h["ticker"]
            r      = realized.get(ticker)
            if r is None:
                logger.debug(f"{ticker}: no price data — skipping")
                continue

            ret   = r["realized_return"]
            alpha = ret - spy_return
            vol   = r["volatility"]
            risk_adj_alpha = alpha / vol if vol > 0.01 else alpha

            entry_scores = h.to_dict()
            attribution  = analyse_factor_attribution(entry_scores, ret, spy_return)

            outcome: dict = {
                "ticker":       ticker,
                "entry_date":   entry_date,
                "exit_date":    exit_date,
                "hold_days":    hold_days,
                "entry_price":  r["entry_price"],
                "exit_price":   r["exit_price"],
                "realized_return":       ret,
                "spy_return_same_period": spy_return,
                "alpha":                 alpha,
                "risk_adjusted_alpha":   risk_adj_alpha,
                "max_drawdown":          r["max_drawdown"],
                "volatility":            vol,
                "beat_spy":              bool(alpha > 0),
                "beat_spy_risk_adjusted": bool(risk_adj_alpha > 0),
                "return_quintile":       _quintile_rank(ret, valid_returns),
                "alpha_quintile":        _quintile_rank(alpha, alpha_values),
                "entry_composite_score": float(h.get("composite_score") or 0),
                "entry_ml_score":        float(h.get("composite_score") or 0),
                "entry_value_score":     h.get("entry_value_score"),
                "entry_quality_score":   h.get("entry_quality_score"),
                "entry_momentum_score":  h.get("entry_momentum_score"),
                "entry_sentiment_score": 50.0,
                "entry_short_interest_score": h.get("entry_short_interest_score"),
                "entry_insider_score":        h.get("entry_insider_score"),
                "entry_earnings_surprise_score": h.get("entry_earnings_surprise_score"),
                "entry_regime":          "UNKNOWN",
                "entry_kelly_weight":    float(h.get("weight") or 0),
                "entry_conviction_tier": str(h.get("rationale") or ""),
                "which_factors_predicted_correctly": json.dumps(attribution["factors_correct"]),
                "which_factors_failed":              json.dumps(attribution["factors_failed"]),
                "scoring_method": "ml_gradient_boosting",
                "model_version":  _MODEL_VERSION,
            }
            outcomes_for_date.append(outcome)

        if not outcomes_for_date:
            continue

        # Persist to DB
        insert_sql = """
            INSERT OR REPLACE INTO portfolio_outcomes
            (ticker, entry_date, exit_date, hold_days,
             entry_price, exit_price, realized_return,
             spy_return_same_period, alpha, risk_adjusted_alpha,
             max_drawdown, volatility, beat_spy, beat_spy_risk_adjusted,
             return_quintile, alpha_quintile,
             entry_composite_score, entry_ml_score,
             entry_value_score, entry_quality_score, entry_momentum_score,
             entry_sentiment_score, entry_short_interest_score,
             entry_insider_score, entry_earnings_surprise_score,
             entry_regime, entry_kelly_weight, entry_conviction_tier,
             which_factors_predicted_correctly, which_factors_failed,
             scoring_method, model_version)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """
        cols_ordered = [
            "ticker", "entry_date", "exit_date", "hold_days",
            "entry_price", "exit_price", "realized_return",
            "spy_return_same_period", "alpha", "risk_adjusted_alpha",
            "max_drawdown", "volatility", "beat_spy", "beat_spy_risk_adjusted",
            "return_quintile", "alpha_quintile",
            "entry_composite_score", "entry_ml_score",
            "entry_value_score", "entry_quality_score", "entry_momentum_score",
            "entry_sentiment_score", "entry_short_interest_score",
            "entry_insider_score", "entry_earnings_surprise_score",
            "entry_regime", "entry_kelly_weight", "entry_conviction_tier",
            "which_factors_predicted_correctly", "which_factors_failed",
            "scoring_method", "model_version",
        ]
        for o in outcomes_for_date:
            try:
                conn.execute(insert_sql, tuple(o[c] for c in cols_ordered))
            except Exception as e:
                logger.error(f"Save failed for {o['ticker']}: {e}")
        conn.commit()
        logger.info(f"Saved {len(outcomes_for_date)} outcomes for {entry_date}")
        all_outcomes.extend(outcomes_for_date)

    conn.close()
    return pd.DataFrame(all_outcomes) if all_outcomes else pd.DataFrame()


# ---------------------------------------------------------------------------
# Learning report
# ---------------------------------------------------------------------------

def generate_learning_report(outcomes_df: pd.DataFrame) -> dict:
    """
    Analyse evaluated outcomes to produce a structured learning report.
    Answers: win rate, factor accuracy, ML predictiveness, regime breakdown.
    """
    if outcomes_df.empty:
        return {"status": "no_outcomes",
                "message": "No outcomes to analyse"}

    report: dict = {}
    report["total_picks"]    = len(outcomes_df)
    report["win_rate"]       = float(outcomes_df["beat_spy"].mean())
    report["avg_alpha"]      = float(outcomes_df["alpha"].mean())
    report["avg_return"]     = float(outcomes_df["realized_return"].mean())
    report["avg_spy_return"] = float(outcomes_df["spy_return_same_period"].mean())

    best_idx  = outcomes_df["alpha"].idxmax()
    worst_idx = outcomes_df["alpha"].idxmin()
    report["best_pick"]  = outcomes_df.loc[best_idx,  ["ticker", "alpha", "entry_date"]].to_dict()
    report["worst_pick"] = outcomes_df.loc[worst_idx, ["ticker", "alpha", "entry_date"]].to_dict()

    # Factor attribution
    all_correct: list[str] = []
    all_failed:  list[str] = []
    for _, row in outcomes_df.iterrows():
        all_correct.extend(json.loads(row.get("which_factors_predicted_correctly") or "[]"))
        all_failed.extend(json.loads(row.get("which_factors_failed") or "[]"))

    correct_cnt = Counter(all_correct)
    failed_cnt  = Counter(all_failed)
    report["most_predictive_factors"]  = dict(correct_cnt.most_common(5))
    report["least_predictive_factors"] = dict(failed_cnt.most_common(5))

    factor_accuracy: dict[str, dict] = {}
    for f in set(all_correct + all_failed):
        c = correct_cnt.get(f, 0)
        fa = failed_cnt.get(f, 0)
        total = c + fa
        factor_accuracy[f] = {
            "accuracy": c / total,
            "correct": c, "failed": fa, "total": total,
        }
    report["factor_accuracy"] = dict(
        sorted(factor_accuracy.items(), key=lambda x: x[1]["accuracy"], reverse=True)
    )

    # ML score vs alpha correlation
    ml_corr = outcomes_df["entry_ml_score"].corr(outcomes_df["alpha"])
    report["ml_score_alpha_correlation"] = float(ml_corr) if not np.isnan(ml_corr) else 0.0

    # Regime performance
    if outcomes_df["entry_regime"].nunique() > 1:
        report["regime_performance"] = (
            outcomes_df.groupby("entry_regime")
            .agg(win_rate=("beat_spy", "mean"),
                 avg_alpha=("alpha", "mean"),
                 count=("ticker", "count"))
            .to_dict("index")
        )
    else:
        report["regime_performance"] = {}

    # Conviction analysis — high vs low ML score
    median_score = outcomes_df["entry_ml_score"].median()
    high = outcomes_df[outcomes_df["entry_ml_score"] >= median_score]
    low  = outcomes_df[outcomes_df["entry_ml_score"] <  median_score]
    report["high_conviction_win_rate"] = float(high["beat_spy"].mean()) if len(high) else None
    report["low_conviction_win_rate"]  = float(low["beat_spy"].mean())  if len(low)  else None
    hwr = report["high_conviction_win_rate"]
    lwr = report["low_conviction_win_rate"]
    report["conviction_edge"] = (hwr - lwr) if (hwr is not None and lwr is not None) else None

    # Recommendations
    recs: list[str] = []
    if report["win_rate"] < 0.55:
        recs.append("Win rate below 55% — consider tightening quality gates")
    worst_factors = [
        f for f, s in factor_accuracy.items() if s["accuracy"] < 0.40 and s["total"] >= 5
    ]
    if worst_factors:
        recs.append(f"Factors with <40% accuracy: {worst_factors} — consider reducing weight")
    best_factors = [
        f for f, s in factor_accuracy.items() if s["accuracy"] > 0.65 and s["total"] >= 5
    ]
    if best_factors:
        recs.append(f"Factors with >65% accuracy: {best_factors} — consider increasing weight")
    if report.get("conviction_edge") is not None and report["conviction_edge"] < 0.05:
        recs.append(
            "High conviction picks not outperforming — ML model needs more training data"
        )
    report["recommendations"] = recs
    return report


def print_learning_report(report: dict) -> None:
    """Print a formatted learning report to stdout (ASCII-safe for Windows)."""
    print("\n" + "=" * 60)
    print("SYSTEM LEARNING REPORT")
    print("=" * 60)

    if report.get("status") == "no_outcomes":
        print(report["message"])
        return

    print(f"\nOVERALL PERFORMANCE")
    print(f"  Total picks evaluated:   {report['total_picks']}")
    print(f"  Win rate vs SPY:         {report['win_rate']:.1%}")
    print(f"  Average alpha:           {report['avg_alpha']:+.2%}")
    print(f"  Average return:          {report['avg_return']:+.2%}")
    print(f"  SPY return (same period):{report['avg_spy_return']:+.2%}")

    best  = report.get("best_pick", {})
    worst = report.get("worst_pick", {})
    print(f"\n  Best pick:  {best.get('ticker')} "
          f"alpha={best.get('alpha', 0):+.1%} ({best.get('entry_date')})")
    print(f"  Worst pick: {worst.get('ticker')} "
          f"alpha={worst.get('alpha', 0):+.1%} ({worst.get('entry_date')})")

    print(f"\nFACTOR ACCURACY (what actually predicted returns)")
    fa = report.get("factor_accuracy", {})
    if fa:
        print(f"  {'Factor':<25} {'Accuracy':>10} {'Correct':>8} {'Failed':>8}")
        print(f"  {'-'*55}")
        for factor, stats in fa.items():
            acc = stats["accuracy"]
            bar = "#" * int(acc * 20)
            print(f"  {factor:<25} {acc:>9.1%} "
                  f"{stats['correct']:>8} {stats['failed']:>8}  {bar}")

    ml_corr = report.get("ml_score_alpha_correlation")
    if ml_corr is not None:
        print(f"\nML MODEL VALIDITY")
        print(f"  ML score vs alpha correlation: {ml_corr:+.3f}")
        if abs(ml_corr) > 0.10:
            print(f"  [STRONG] ML scores have meaningful predictive power")
        elif abs(ml_corr) > 0.05:
            print(f"  [WEAK]   ML scores have weak but positive signal")
        else:
            print(f"  [LOW]    ML scores not yet predictive — need more data")

    hwr = report.get("high_conviction_win_rate")
    lwr = report.get("low_conviction_win_rate")
    if hwr is not None:
        print(f"\nCONVICTION ANALYSIS")
        print(f"  High conviction win rate: {hwr:.1%}")
        print(f"  Low conviction win rate:  {lwr:.1%}")
        print(f"  Conviction edge:          {report['conviction_edge']:+.1%}")

    regime_perf = report.get("regime_performance", {})
    if regime_perf:
        print(f"\nREGIME PERFORMANCE")
        for regime, stats in regime_perf.items():
            print(f"  {regime:<25} "
                  f"win={stats['win_rate']:.1%}  "
                  f"alpha={stats['avg_alpha']:+.2%}  "
                  f"n={stats['count']}")

    recs = report.get("recommendations", [])
    if recs:
        print(f"\nSYSTEM RECOMMENDATIONS")
        for i, rec in enumerate(recs, 1):
            print(f"  {i}. {rec}")

    print("\n" + "=" * 60)
