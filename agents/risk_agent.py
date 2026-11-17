"""
agents/risk_agent.py

Risk Agent — dynamic position sizing, correlation control,
drawdown monitoring, and stop-loss management.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import numpy as np
import pandas as pd
import yfinance as yf
from loguru import logger

from config.settings import DATABASE_URL

# ---------------------------------------------------------------------------
# Constants (mirrors PORTFOLIO in settings, extended with risk params)
# ---------------------------------------------------------------------------

MAX_POSITION_SIZE  = 0.10    # 10% max single position
MIN_POSITION_SIZE  = 0.03    # 3% min single position
MAX_SECTOR_EXPOSURE = 0.30   # 30% max in any sector
MAX_CORRELATION    = 0.75    # Max pairwise return correlation
STOP_LOSS_PCT      = -0.20   # -20% from entry triggers stop
MAX_POSITIONS      = 20
MIN_POSITIONS      = 10


def _db_path() -> str:
    return DATABASE_URL.replace("sqlite:///", "")


# ---------------------------------------------------------------------------
# Price fetch
# ---------------------------------------------------------------------------

def get_portfolio_prices(tickers: list[str], lookback_days: int = 252) -> pd.DataFrame:
    """Fetch Close price history for portfolio tickers."""
    end   = date.today()
    start = end - timedelta(days=lookback_days)
    prices: dict[str, pd.Series] = {}

    for ticker in tickers:
        try:
            df = yf.Ticker(ticker).history(
                start=start.isoformat(),
                end=end.isoformat(),
            )
            if not df.empty:
                prices[ticker] = df["Close"]
        except Exception as e:
            logger.warning(f"{ticker}: price fetch failed — {e}")

    return pd.DataFrame(prices).dropna(how="all")


# ---------------------------------------------------------------------------
# Correlation check
# ---------------------------------------------------------------------------

def calculate_correlation_matrix(prices_df: pd.DataFrame) -> pd.DataFrame:
    returns = prices_df.pct_change().dropna()
    return returns.corr()


def check_correlations(tickers: list[str], corr_matrix: pd.DataFrame) -> list[str]:
    """
    Find highly correlated pairs and return the lower-ranked ticker
    (assumes tickers list is ordered best-first).
    """
    remove: set[str] = set()
    seen: set[tuple] = set()

    for i, t1 in enumerate(tickers):
        for t2 in tickers[i + 1:]:
            if t1 not in corr_matrix.index or t2 not in corr_matrix.columns:
                continue
            corr = float(corr_matrix.loc[t1, t2])
            pair = tuple(sorted([t1, t2]))
            if abs(corr) > MAX_CORRELATION and pair not in seen:
                seen.add(pair)
                logger.warning(f"High correlation: {t1}/{t2} = {corr:.2f} — flagging {t2}")
                remove.add(t2)

    return list(remove)


# ---------------------------------------------------------------------------
# Position sizing — inverse volatility with conviction overlay
# ---------------------------------------------------------------------------

def calculate_position_sizes(
    ranked_stocks: pd.DataFrame,
    regime: dict,
    prices_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Inverse-volatility weighting with composite score conviction overlay.
    Higher conviction + lower vol = larger position.
    """
    ranked = ranked_stocks.copy()

    if prices_df.empty or "ticker" not in ranked.columns:
        ranked["weight"] = 1.0 / len(ranked)
        return ranked

    returns = prices_df.pct_change().dropna()
    raw_weights: dict[str, float] = {}

    for _, row in ranked.iterrows():
        ticker = row["ticker"]
        if ticker in returns.columns:
            vol = float(returns[ticker].std() * np.sqrt(252))
            vol = max(vol, 0.05)   # 5% vol floor
        else:
            vol = 0.25             # Default assumption

        conviction = float(row.get("composite_score", 70)) / 100.0
        raw_weights[ticker] = (1.0 / vol) * (0.7 + 0.3 * conviction)

    # Normalise → apply min/max → renormalise
    total = sum(raw_weights.values()) or 1.0
    for t in raw_weights:
        raw_weights[t] /= total

    for t in raw_weights:
        raw_weights[t] = min(MAX_POSITION_SIZE, max(MIN_POSITION_SIZE, raw_weights[t]))

    total = sum(raw_weights.values()) or 1.0
    for t in raw_weights:
        raw_weights[t] /= total

    ranked["weight"] = ranked["ticker"].map(raw_weights).fillna(1.0 / len(ranked))
    return ranked


# ---------------------------------------------------------------------------
# Kelly Criterion position sizing
# ---------------------------------------------------------------------------

def calculate_kelly_sizes(
    ranked_stocks: pd.DataFrame,
    prices_df: pd.DataFrame,
    ml_scores: pd.Series | None = None,
    max_position: float = 0.15,
    min_position: float = 0.02,
    kelly_fraction: float = 0.25,
) -> pd.DataFrame:
    """
    Kelly-optimal position sizes scaled by ML model confidence.

    Uses fractional Kelly (25% by default) — full Kelly is theoretically
    optimal but causes extreme volatility in practice. Quarter-Kelly is
    standard institutional practice.

    Formula: Kelly% = edge / variance
    Where:
      edge     = (avg_daily_return * conviction_multiplier) - risk_free_daily
      variance = daily return variance over lookback window
      conviction_multiplier derived from ML score: 0-100 -> 0.5-1.5x
    """
    from config.settings import KELLY

    risk_free_daily = KELLY["risk_free_rate"] / 252

    ranked = ranked_stocks.copy()
    if prices_df.empty or "ticker" not in ranked.columns:
        ranked["weight"]     = 1.0 / max(len(ranked), 1)
        ranked["weight_pct"] = (ranked["weight"] * 100).round(1)
        return ranked

    returns = prices_df.pct_change().dropna()
    kelly_weights: dict[str, float] = {}

    for _, row in ranked.iterrows():
        ticker = row["ticker"]

        # Historical return statistics
        if ticker in returns.columns and len(returns[ticker].dropna()) > 20:
            ticker_rets      = returns[ticker].dropna()
            avg_daily_return = float(ticker_rets.mean())
            variance         = float(ticker_rets.var())
        else:
            avg_daily_return = 0.0004   # ~10% annual assumption
            variance         = 0.0004   # ~32% annual vol assumption

        # ML score → conviction multiplier (score 100=1.5x, 50=1.0x, 0=0.5x)
        if ml_scores is not None and ticker in ml_scores.index:
            ml_score = float(ml_scores[ticker])
        else:
            ml_score = float(row.get("composite_score", 50))

        conviction_multiplier = 0.5 + (ml_score / 100.0)

        # Edge = adjusted expected return above risk-free
        edge = (avg_daily_return * conviction_multiplier) - risk_free_daily

        if variance <= 0 or edge <= 0:
            kelly_weights[ticker] = min_position
            continue

        # Fractional Kelly
        position_size = min(max_position, max(min_position,
                            (edge / variance) * kelly_fraction))
        kelly_weights[ticker] = position_size

    # Normalise to sum=1 subject to [min_position, max_position] via iterative
    # water-filling. The previous "normalise -> clamp -> renormalise" was buggy:
    # the final renormalise divided by the post-clamp total (<1), inflating
    # capped positions BACK ABOVE max_position (produced 17.3% > 15% cap). Here we
    # PIN any position that hits a bound and redistribute only the remaining budget
    # across the still-free positions, repeating until no position violates a bound.
    total = sum(kelly_weights.values()) or 1.0
    for t in kelly_weights:
        kelly_weights[t] /= total

    n = len(kelly_weights)
    if n * max_position < 1.0 - 1e-9:
        logger.warning(
            f"Kelly sizing: {n} positions * max {max_position:.0%} < 100% — cannot "
            f"reach full investment under the cap; weights will sum to {n*max_position:.0%}."
        )

    fixed: dict[str, float] = {}
    for _ in range(100):
        free = [t for t in kelly_weights if t not in fixed]
        if not free:
            break
        remaining = 1.0 - sum(fixed.values())
        free_sum = sum(kelly_weights[t] for t in free) or 1.0
        for t in free:
            kelly_weights[t] = kelly_weights[t] / free_sum * remaining
        newly: dict[str, float] = {}
        for t in free:
            if kelly_weights[t] > max_position + 1e-12:
                newly[t] = max_position
            elif kelly_weights[t] < min_position - 1e-12:
                newly[t] = min_position
        if not newly:
            break
        for t, v in newly.items():
            fixed[t] = v
            kelly_weights[t] = v
    for t, v in fixed.items():
        kelly_weights[t] = v

    # Guard: no position may exceed the cap (catches any future regression silently shipping).
    max_w = max(kelly_weights.values()) if kelly_weights else 0.0
    if max_w > max_position + 1e-6:
        raise AssertionError(
            f"Kelly sizing produced weight {max_w:.4f} > max_position {max_position:.4f} "
            f"after normalisation — cap not enforced."
        )

    ranked["weight"]     = ranked["ticker"].map(kelly_weights).fillna(min_position)
    ranked["weight_pct"] = (ranked["weight"] * 100).round(1)
    return ranked


# ---------------------------------------------------------------------------
# Stop-loss check
# ---------------------------------------------------------------------------

def check_stop_losses(portfolio_holdings: pd.DataFrame) -> list[dict]:
    """
    Check each holding against its entry price stop-loss level.
    Returns list of dicts for triggered positions.
    """
    triggered: list[dict] = []

    for _, row in portfolio_holdings.iterrows():
        ticker      = row["ticker"]
        entry_price = float(row.get("entry_price") or 0)
        if entry_price <= 0:
            continue

        try:
            current = float(yf.Ticker(ticker).fast_info.last_price)
            pnl_pct = (current - entry_price) / entry_price

            if pnl_pct <= STOP_LOSS_PCT:
                logger.warning(
                    f"STOP LOSS: {ticker}  entry=${entry_price:.2f}  "
                    f"current=${current:.2f}  loss={pnl_pct:.1%}"
                )
                triggered.append({
                    "ticker":        ticker,
                    "entry_price":   entry_price,
                    "current_price": current,
                    "pnl_pct":       pnl_pct,
                })
        except Exception as e:
            logger.debug(f"{ticker}: stop-loss check failed — {e}")

    return triggered


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_risk_check(portfolio_holdings: pd.DataFrame, regime: dict) -> dict:
    """
    Run full risk assessment on current portfolio.
    Returns structured report with actionable flags.
    """
    tickers = portfolio_holdings["ticker"].tolist()
    if not tickers:
        return {"status": "ERROR", "message": "No portfolio holdings"}

    logger.info(f"Risk check: {len(tickers)} positions, regime={regime['regime']}")

    prices_df   = get_portfolio_prices(tickers)
    corr_matrix = calculate_correlation_matrix(prices_df) if not prices_df.empty else pd.DataFrame()
    high_corr   = check_correlations(tickers, corr_matrix) if not corr_matrix.empty else []
    stop_losses = check_stop_losses(portfolio_holdings)
    portfolio_holdings = calculate_position_sizes(portfolio_holdings, regime, prices_df)

    # Sector concentration via stocks table
    sector_exposure: dict[str, float] = {}
    try:
        conn = sqlite3.connect(_db_path())
        placeholders = ",".join(f"'{t}'" for t in tickers)
        sector_data  = pd.read_sql(
            f"SELECT ticker, sector FROM stocks WHERE ticker IN ({placeholders})",
            conn,
        )
        conn.close()

        if not sector_data.empty:
            merged = portfolio_holdings.merge(sector_data, on="ticker", how="left")
            sector_exposure = (
                merged.groupby("sector")["weight"]
                .sum()
                .dropna()
                .to_dict()
            )
    except Exception as e:
        logger.warning(f"Sector lookup failed — {e}")

    concentrated_sectors = {
        s: w for s, w in sector_exposure.items() if w > MAX_SECTOR_EXPOSURE
    }

    portfolio_return_1m = 0.0
    if not prices_df.empty and len(prices_df) > 21:
        portfolio_return_1m = float((prices_df.iloc[-1] / prices_df.iloc[-21] - 1).mean())

    flags: list[str] = []
    if stop_losses:
        flags.append(f"STOP LOSS: {[s['ticker'] for s in stop_losses]}")
    if high_corr:
        flags.append(f"HIGH CORRELATION: {high_corr}")
    if concentrated_sectors:
        flags.append(f"SECTOR CONCENTRATION: {concentrated_sectors}")

    return {
        "status":               "ALERT" if flags else "OK",
        "flags":                flags,
        "stop_losses":          stop_losses,
        "high_correlation":     high_corr,
        "sector_exposure":      sector_exposure,
        "concentrated_sectors": concentrated_sectors,
        "position_sizes":       portfolio_holdings[["ticker", "weight"]].to_dict("records"),
        "portfolio_return_1m":  portfolio_return_1m,
        "regime":               regime["regime"],
        "as_of":                str(date.today()),
    }


# ---------------------------------------------------------------------------
# Human-readable report
# ---------------------------------------------------------------------------

def print_risk_report(report: dict) -> None:
    print("\nRISK AGENT REPORT")
    print("=" * 50)
    print(f"Status:   {report['status']}")
    print(f"Regime:   {report['regime']}")
    print(f"As of:    {report['as_of']}")
    print(f"1M return:{report['portfolio_return_1m']:.1%}")

    if report["flags"]:
        print(f"\nALERTS:")
        for flag in report["flags"]:
            print(f"  WARNING  {flag}")

    if report["stop_losses"]:
        print(f"\nSTOP LOSSES TRIGGERED:")
        for sl in report["stop_losses"]:
            print(f"  {sl['ticker']}: {sl['pnl_pct']:.1%}  "
                  f"(entry ${sl['entry_price']:.2f} -> current ${sl['current_price']:.2f})")

    if report["sector_exposure"]:
        print(f"\nSector Exposure:")
        for sector, weight in sorted(
            report["sector_exposure"].items(), key=lambda x: x[1], reverse=True
        ):
            bar   = "#" * int(weight * 20)
            alert = " WARNING" if weight > MAX_SECTOR_EXPOSURE else ""
            print(f"  {sector:<28} {weight:.1%}  {bar}{alert}")

    n = len(report["position_sizes"])
    equal_w = 1.0 / n if n else 0.0
    print(f"\nPosition Sizes (Kelly vs equal-weight):")
    print(f"  {'Ticker':<6}  {'Kelly':>6}  {'Equal':>6}  {'Delta':>7}  Bar")
    for pos in sorted(report["position_sizes"], key=lambda x: x["weight"], reverse=True):
        kelly_w = pos["weight"]
        delta   = kelly_w - equal_w
        bar     = "#" * int(kelly_w * 100)
        sign    = "+" if delta >= 0 else ""
        print(f"  {pos['ticker']:<6}  {kelly_w:>5.1%}  {equal_w:>5.1%}  {sign}{delta:>5.1%}  {bar}")


if __name__ == "__main__":
    import sqlite3 as _sqlite3
    import pandas as _pd
    from agents.macro_agent import get_current_regime

    _conn = _sqlite3.connect(_db_path())
    _holdings = _pd.read_sql("""
        SELECT ticker, composite_score, weight, entry_price
        FROM portfolio_holdings
        WHERE rebalance_date = (SELECT MAX(rebalance_date) FROM portfolio_holdings)
    """, _conn)
    _conn.close()

    _regime = get_current_regime()
    _report = run_risk_check(_holdings, _regime)
    print_risk_report(_report)
