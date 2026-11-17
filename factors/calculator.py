"""
factors/calculator.py

Monthly factor calculation orchestrator.

Coordinates value, quality, and momentum factor engines for a given
rebalance date, applies filters, and produces the final ranked universe.

RULE 1 ENFORCEMENT: All fundamental data is queried with
load_fundamentals_as_of(as_of=rebalance_date), which enforces the 90-day
reporting lag. No lookahead possible.

RULE 3 (transaction costs): Not applied here — that is the backtesting engine's
responsibility.
"""

from __future__ import annotations

from datetime import date, timedelta
import pandas as pd
from loguru import logger

from config.settings import UNIVERSE
from data.storage import (
    get_session,
    load_prices,
    load_fundamentals_as_of,
    FactorScore,
    Fundamental,
    init_db,
)
from factors.value import calculate_value_factors
from factors.quality import calculate_quality_factors
from factors.momentum import calculate_momentum_factors
from factors.composite import calculate_composite_scores


# ---------------------------------------------------------------------------
# Universe filters
# ---------------------------------------------------------------------------

def apply_exclusion_filters(
    tickers: list[str],
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame,
    session=None,
) -> list[str]:
    """
    Apply EXCLUSION_FILTERS from settings before factor scoring.

    Filters:
    - Min price >= $3
    - Min avg daily dollar volume >= $500K
    - Exclude negative equity stocks
    - Exclude if no positive revenue
    - Exclude Financials and Utilities sectors (different accounting)

    Returns cleaned list of tickers that pass all filters.
    """
    if prices.empty or fundamentals.empty:
        return tickers

    # Sector exclusion — query stocks table when session is available
    excluded_sectors = set(UNIVERSE.get("exclude_sectors", []))
    sector_lookup: dict[str, str] = {}
    if session and excluded_sectors:
        from data.storage import Stock
        rows = (
            session.query(Stock.ticker, Stock.sector)
            .filter(Stock.ticker.in_(tickers), Stock.sector.isnot(None))
            .all()
        )
        sector_lookup = {r.ticker: r.sector for r in rows}

    # Price and volume filters
    recent_prices = (
        prices.sort_values("date")
        .groupby("ticker")
        .tail(60)  # Last ~3 months
    )
    price_stats = recent_prices.groupby("ticker").agg(
        avg_price=("adj_close", "mean"),
        avg_dollar_vol=("adj_close", lambda x: (x * recent_prices.loc[x.index, "volume"]).mean()),
    )

    # Fundamental filters
    fund_lookup = fundamentals.set_index("ticker") if "ticker" in fundamentals.columns else fundamentals

    passed = []
    removed = {"price": 0, "volume": 0, "neg_equity": 0, "no_revenue": 0, "sector": 0}

    for ticker in tickers:
        # Sector filter
        if excluded_sectors and ticker in sector_lookup:
            if sector_lookup[ticker] in excluded_sectors:
                removed["sector"] += 1
                continue

        # Price filter
        stats = price_stats.loc[ticker] if ticker in price_stats.index else None
        if stats is None:
            continue

        if stats["avg_price"] < UNIVERSE["min_price"]:
            removed["price"] += 1
            continue

        if stats["avg_dollar_vol"] < UNIVERSE["min_avg_daily_volume"]:
            removed["volume"] += 1
            continue

        # Fundamental filters
        if ticker in fund_lookup.index:
            row = fund_lookup.loc[ticker]

            # Negative equity
            if UNIVERSE["exclude_if_negative_equity"]:
                equity = row.get("total_equity")
                if not pd.isna(equity) and equity <= 0:
                    removed["neg_equity"] += 1
                    continue

            # No positive revenue
            if UNIVERSE["require_positive_revenue"]:
                revenue = row.get("revenue")
                if not pd.isna(revenue) and revenue <= 0:
                    removed["no_revenue"] += 1
                    continue

        passed.append(ticker)

    logger.debug(
        f"Exclusion filters: {len(passed)}/{len(tickers)} pass "
        f"(removed: price={removed['price']}, vol={removed['volume']}, "
        f"neg_equity={removed['neg_equity']}, no_rev={removed['no_revenue']}, "
        f"sector={removed['sector']})"
    )
    return passed


# ---------------------------------------------------------------------------
# F-score truncation guards
# ---------------------------------------------------------------------------
# The Piotroski F-score needs a prior-year fundamental row for its YoY signals.
# Without it the score silently truncates to a 0-4 ceiling (the pre-fix bug,
# commit 92661d4). These thresholds let calculate_factors_for_date refuse to
# compute, rather than regress, when prior-year data is missing.

PRIOR_YEAR_COVERAGE_MIN = 0.80   # >=80% of the screened universe must have a YoY row
FSCORE_MEAN_FLOOR       = 4.0    # corrected universe mean is ~5.0; below this = suspect


# ---------------------------------------------------------------------------
# Prior-year fundamentals helper
# ---------------------------------------------------------------------------

FLOW_FIELDS = ("revenue", "gross_profit", "operating_income", "net_income", "ebitda",
               "operating_cash_flow", "capex", "free_cash_flow")


def twelve_month_prior(prior: pd.DataFrame, history: pd.DataFrame) -> tuple[pd.DataFrame, int, int]:
    """Make every prior-year row cover 12 months, like this year's rows do.

    The fundamentals table mixes SimFin QUARTERLY rows (filing_date set) with yfinance
    12-MONTH rows (filing_date NULL). This year's row is almost always 12-month; when the
    prior-year row is a SimFin quarter, the F-score's YoY ROA and asset-turnover signals
    compared a quarter with a year (129 of 1,869 tickers on 2026-09-21; see
    docs/research/fundamentals_mix_FINDINGS.md). Here a quarterly prior row's flow fields
    become the sum of that quarter and the three before it (consecutive, from `history`,
    which must already be point-in-time). Balance-sheet fields are unchanged. A row
    without 4 consecutive quarters is left as it was, so coverage never drops.

    Returns (rows, n_converted, n_left_quarterly).
    """
    if prior.empty or "filing_date" not in prior:
        return prior, 0, 0
    out = prior.copy()
    q = history[history["filing_date"].notna()].copy() if len(history) else history
    if len(q):
        q["qidx"] = q["fiscal_year"].astype(int) * 4 + q["fiscal_quarter"].astype(int)
    converted = left = 0
    for i, r in out[out["filing_date"].notna()].iterrows():
        idx = int(r["fiscal_year"]) * 4 + int(r["fiscal_quarter"])
        g = (q[(q["ticker"] == r["ticker"]) & q["qidx"].between(idx - 3, idx)]
             .drop_duplicates("qidx", keep="last") if len(q) else q)
        if len(g) != 4:
            left += 1
            continue
        for col in FLOW_FIELDS:
            if col in out.columns and col in g.columns:
                vals = pd.to_numeric(g[col], errors="coerce")
                out.at[i, col] = float(vals.sum()) if vals.notna().all() else float("nan")
        converted += 1
    return out, converted, left


def _load_prior_year_fundamentals(
    tickers: list[str],
    as_of: date,
    session,
) -> pd.DataFrame:
    """Load fundamentals as of ~1 year ago for YoY F-Score signals, on a 12-month basis."""
    prior_date = as_of - timedelta(days=365)
    try:
        prior = load_fundamentals_as_of(tickers, prior_date, session=session)
    except Exception as e:
        logger.warning(f"Could not load prior-year fundamentals: {e}")
        return pd.DataFrame()
    if prior.empty or "filing_date" not in prior or prior["filing_date"].isna().all():
        return prior
    try:
        quarterly = prior.loc[prior["filing_date"].notna(), "ticker"].unique().tolist()
        rows = (session.query(Fundamental)
                .filter(Fundamental.ticker.in_(quarterly),
                        Fundamental.available_date <= prior_date,     # point in time
                        Fundamental.filing_date.isnot(None))
                .all())
        cols = ["ticker", "fiscal_year", "fiscal_quarter", "filing_date", *FLOW_FIELDS]
        history = pd.DataFrame([{c: (float(getattr(r, c)) if c in FLOW_FIELDS and
                                     getattr(r, c) is not None else getattr(r, c))
                                 for c in cols} for r in rows])
        prior, n_conv, n_left = twelve_month_prior(prior, history)
        logger.info(f"Prior-year rows: {n_conv} quarterly rows converted to 12 months; "
                    f"{n_left} left quarterly (no 4 consecutive quarters)")
    except Exception as e:                                             # noqa: BLE001
        logger.warning(f"Prior-year 12-month conversion skipped: {e}")
    return prior


# ---------------------------------------------------------------------------
# Main calculation entry point
# ---------------------------------------------------------------------------

def calculate_factors_for_date(
    tickers: list[str],
    rebalance_date: date,
    price_lookback_days: int = 400,
    store: bool = True,
    sentiment_scores: dict[str, float] | None = None,
    fetch_aux_signals: bool = False,
    require_prior_year: bool = True,
) -> pd.DataFrame:
    """
    Calculate all factor scores for a universe of tickers as of rebalance_date.

    This is the single entry point for the factor engine. It:
    1. Loads point-in-time fundamental data (available_date <= rebalance_date)
    2. Loads price data covering the momentum lookback window
    3. Applies exclusion filters
    4. Calculates value, quality, and momentum factors
    5. Builds the composite score and ranks the universe
    6. Optionally stores results to the factor_scores table

    Args:
        tickers:             Full universe of tickers to evaluate.
        rebalance_date:      The calculation date. No data after this date is used.
        price_lookback_days: How many days of price history to load (default 400 covers
                             the 12-month momentum window with buffer).
        store:               If True, write results to factor_scores table.
        sentiment_scores:    Optional {ticker: score} from sentiment_agent.extract_scores().
                             When provided, activates the 4-factor model (sentiment 15%).

    Returns:
        DataFrame indexed by ticker with all factor scores and rankings.
        Returns empty DataFrame if insufficient data.
    """
    logger.info(f"Calculating factors for {len(tickers)} tickers as of {rebalance_date}")

    session = get_session()
    try:
        # Step 1: Load point-in-time fundamentals (Rule 1 enforced)
        fundamentals = load_fundamentals_as_of(tickers, rebalance_date, session=session)

        if fundamentals.empty:
            logger.warning(f"No fundamental data available as of {rebalance_date}")
            return pd.DataFrame()

        available_tickers = fundamentals["ticker"].tolist()
        logger.debug(f"Fundamentals available for {len(available_tickers)}/{len(tickers)} tickers")

        # Step 2: Load price data
        price_start = rebalance_date - timedelta(days=price_lookback_days)
        prices = load_prices(available_tickers, price_start, rebalance_date, session=session)

        if prices.empty:
            logger.warning("No price data available for factor calculation")
            return pd.DataFrame()

        # Step 3: Apply exclusion filters (sector lookup requires session)
        filtered_tickers = apply_exclusion_filters(available_tickers, prices, fundamentals, session=session)
        if not filtered_tickers:
            logger.warning("All tickers filtered out — check EXCLUSION_FILTERS thresholds")
            return pd.DataFrame()

        fundamentals = fundamentals[fundamentals["ticker"].isin(filtered_tickers)]

        # Step 4: Load prior-year fundamentals for F-Score YoY signals
        prior_fundamentals = _load_prior_year_fundamentals(filtered_tickers, rebalance_date, session)

        # GUARD: prior-year rows are REQUIRED for the full 0-9 Piotroski f-score.
        # Without them the f-score silently truncates to 0-4 (the pre-fix bug). Refuse
        # to compute rather than regress. Pass require_prior_year=False only for a
        # deliberate run that knowingly accepts truncated scores (e.g. an early-period
        # backtest with no free YoY history).
        if require_prior_year:
            if prior_fundamentals.empty or "total_assets" not in prior_fundamentals.columns:
                prior_cov = 0.0
            else:
                prior_with_yoy = prior_fundamentals.loc[
                    prior_fundamentals["total_assets"].notna(), "ticker"
                ].nunique()
                prior_cov = prior_with_yoy / max(1, len(filtered_tickers))
            if prior_cov < PRIOR_YEAR_COVERAGE_MIN:
                raise RuntimeError(
                    f"prior-year fundamentals missing — run fetch_yfinance_fundamentals first; "
                    f"refusing to compute truncated f-scores "
                    f"(only {prior_cov:.0%} of {len(filtered_tickers)} screened tickers have a "
                    f"prior-year row with total_assets; need >= {PRIOR_YEAR_COVERAGE_MIN:.0%}). "
                    f"To override deliberately, call with require_prior_year=False."
                )

        # Step 5: Calculate factors
        value_factors = calculate_value_factors(fundamentals, prices)
        quality_factors = calculate_quality_factors(fundamentals, prior_fundamentals)

        # Sanity floor (non-fatal): corrected universe mean f-score is ~5.0. A mean
        # below FSCORE_MEAN_FLOOR suggests truncation from incomplete YoY inputs.
        if "f_score" in quality_factors.columns and len(quality_factors):
            mean_f = float(quality_factors["f_score"].mean())
            if mean_f < FSCORE_MEAN_FLOOR:
                logger.warning(
                    f"[F-SCORE TRUNCATION?] universe mean f_score={mean_f:.2f} < floor "
                    f"{FSCORE_MEAN_FLOOR} — f-scores may be truncated to 0-4 "
                    f"(prior-year fundamentals incomplete?). Investigate before trusting the gate."
                )
        momentum_factors = calculate_momentum_factors(
            prices,
            rebalance_date,
            filtered_tickers,
            fundamentals_current=fundamentals,
            fundamentals_prior_1y=prior_fundamentals,
        )

        # Step 6 (optional): Fetch auxiliary signals — skipped during backtest
        auxiliary_scores: dict[str, pd.Series] | None = None
        aux_raw_data: dict[str, pd.DataFrame] = {}
        if fetch_aux_signals:
            from agents.short_interest import get_short_interest_batch
            from agents.insider_signal import get_insider_batch
            from agents.earnings_surprise import get_earnings_surprise_batch

            logger.info("Fetching short interest signals...")
            si_df = get_short_interest_batch(filtered_tickers)
            aux_raw_data["short_interest"] = si_df

            logger.info("Fetching insider signals...")
            ins_df = get_insider_batch(filtered_tickers)
            aux_raw_data["insider"] = ins_df

            logger.info("Fetching earnings surprise signals...")
            es_df = get_earnings_surprise_batch(filtered_tickers)
            aux_raw_data["earnings_surprise"] = es_df

            # 52-week proximity and revenue acceleration
            logger.info("Calculating 52-week proximity and revenue acceleration scores...")
            from agents.economic_agent import get_52week_proximity, get_revenue_acceleration
            proximity_scores: dict[str, float] = {}
            revenue_accel: dict[str, float]    = {}
            for t in filtered_tickers:
                proximity_scores[t] = get_52week_proximity(t)
                rev = get_revenue_acceleration(t)
                if rev.get("revenue_accelerating") is True:
                    revenue_accel[t] = 75.0
                elif rev.get("revenue_accelerating") is False:
                    revenue_accel[t] = 25.0
                else:
                    revenue_accel[t] = 50.0

            # Build ticker-indexed Series for each signal score
            auxiliary_scores = {
                "short_interest":    pd.Series(
                    si_df.set_index("ticker")["short_interest_score"].fillna(50.0)
                ),
                "insider":           pd.Series(
                    ins_df.set_index("ticker")["insider_score"].fillna(50.0)
                ),
                "earnings_surprise": pd.Series(
                    es_df.set_index("ticker")["earnings_surprise_score"].fillna(50.0)
                ),
                "proximity_52w":  pd.Series(proximity_scores),
                "revenue_accel":  pd.Series(revenue_accel),
            }
            logger.info(
                f"Aux signals fetched: short_interest={len(si_df)}, "
                f"insider={len(ins_df)}, earnings_surprise={len(es_df)}, "
                f"proximity_52w={len(proximity_scores)}, revenue_accel={len(revenue_accel)}"
            )

        # Step 7: Build composite scores and rank
        scores = calculate_composite_scores(
            value_factors, quality_factors, momentum_factors,
            sentiment_scores=sentiment_scores,
            auxiliary_scores=auxiliary_scores,
        )

        if scores.empty:
            logger.warning("Composite score calculation returned empty results")
            return pd.DataFrame()

        scores.index.name = "ticker"
        scores = scores.reset_index()
        scores["calculation_date"] = rebalance_date

        # Merge raw aux signal data (ratio/count columns) so they persist to DB
        if fetch_aux_signals and aux_raw_data:
            si_raw = aux_raw_data.get("short_interest")
            if si_raw is not None:
                merge_cols = [c for c in ["ticker", "short_ratio", "short_change"]
                              if c in si_raw.columns]
                rename_map = {"short_ratio": "short_interest_ratio",
                              "short_change": "short_interest_change"}
                scores = scores.merge(
                    si_raw[merge_cols].rename(columns=rename_map),
                    on="ticker", how="left",
                )

            ins_raw = aux_raw_data.get("insider")
            if ins_raw is not None:
                merge_cols = [c for c in ["ticker", "insider_buy_count", "insider_sell_count"]
                              if c in ins_raw.columns]
                scores = scores.merge(ins_raw[merge_cols], on="ticker", how="left")

            es_raw = aux_raw_data.get("earnings_surprise")
            if es_raw is not None:
                merge_cols = [c for c in ["ticker", "beats_last_4q", "avg_surprise_pct"]
                              if c in es_raw.columns]
                scores = scores.merge(es_raw[merge_cols], on="ticker", how="left")

            # 52-week proximity and revenue accel — stored directly as scored columns
            scores["proximity_52w"]       = scores["ticker"].map(proximity_scores).fillna(50.0)
            scores["revenue_accel_score"] = scores["ticker"].map(revenue_accel).fillna(50.0)

        logger.info(
            f"Factor calculation complete: {scores['universe_rank'].notna().sum()} tickers ranked"
        )
        # A weighted factor that is the same for every stock ranks nothing; say so at
        # every calculation instead of letting it look healthy (factors/health.py).
        from config.settings import FACTOR_WEIGHTS, FACTOR_WEIGHTS_BASE
        from factors.health import constant_factors, describe
        _aux = any(f"{f}_score" in scores.columns for f in ("short_interest", "insider"))
        _dead = constant_factors(scores, FACTOR_WEIGHTS if _aux else FACTOR_WEIGHTS_BASE)
        if _dead:
            logger.warning(
                f"CONSTANT FACTORS (same value for every stock, so they change no rank): "
                f"{describe(_dead)} of the composite weight. See factors/health.py."
            )

        # Step 7: Store results
        if store:
            _store_factor_scores(scores, session)

        return scores

    finally:
        session.close()


def _store_factor_scores(scores_df: pd.DataFrame, session) -> None:
    """Write factor scores to the factor_scores table."""
    init_db()

    score_cols = [c.key for c in FactorScore.__table__.columns if c.key != "id"]

    count = 0
    for _, row in scores_df.iterrows():
        ticker = row["ticker"]
        calc_date = row["calculation_date"]

        existing = (
            session.query(FactorScore)
            .filter_by(ticker=ticker, calculation_date=calc_date)
            .first()
        )

        row_dict = {k: (None if pd.isna(v) else v)
                    for k, v in row.items() if k in score_cols}

        if existing:
            for k, v in row_dict.items():
                setattr(existing, k, v)
        else:
            session.add(FactorScore(**{k: v for k, v in row_dict.items() if v is not None or k in ("ticker", "calculation_date")}))
        count += 1

    session.commit()
    logger.info(f"Stored {count} factor score rows for {row['calculation_date']}")


# ---------------------------------------------------------------------------
# Batch calculation over multiple months
# ---------------------------------------------------------------------------

def calculate_factors_monthly(
    tickers: list[str],
    start_date: date,
    end_date: date,
    store: bool = True,
) -> dict[date, pd.DataFrame]:
    """
    Run factor calculation for every first trading day of the month
    between start_date and end_date.

    Returns dict mapping rebalance_date -> scores DataFrame.
    """
    results = {}

    # Generate first-of-month dates
    rebalance_dates = _get_first_trading_days(start_date, end_date)

    logger.info(
        f"Running monthly factor calculation from {start_date} to {end_date}: "
        f"{len(rebalance_dates)} rebalance dates"
    )

    for i, rd in enumerate(rebalance_dates, 1):
        logger.info(f"[{i}/{len(rebalance_dates)}] Calculating factors for {rd}")
        try:
            scores = calculate_factors_for_date(tickers, rd, store=store)
            if not scores.empty:
                results[rd] = scores
        except Exception as e:
            logger.error(f"Factor calculation failed for {rd}: {e}")
            continue

    logger.info(f"Monthly calculation complete: {len(results)}/{len(rebalance_dates)} months succeeded")
    return results


def _get_first_trading_days(start: date, end: date) -> list[date]:
    """
    Generate approximate first trading days of each month between start and end.

    Returns the 1st of each month, shifted forward to Monday if it falls on a weekend.
    The backtesting engine will reconcile against actual trading days from price data.
    """
    dates = []
    current = date(start.year, start.month, 1)

    while current <= end:
        # Shift to Monday if weekend
        weekday = current.weekday()
        if weekday == 5:    # Saturday
            current += timedelta(days=2)
        elif weekday == 6:  # Sunday
            current += timedelta(days=1)

        if current <= end:
            dates.append(current)

        # Move to first of next month
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)

    return dates


# Alias
calculate_all_factors = calculate_factors_for_date
