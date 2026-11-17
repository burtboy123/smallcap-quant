"""
backtesting/metrics.py

Performance analytics for backtest results.

All metrics are computed from monthly portfolio values (at rebalance dates).
Annualisation uses 12 months per year (monthly return series).

Key metrics:
- CAGR (Compound Annual Growth Rate)
- Sharpe Ratio (annualised, risk-free rate from settings or default 0%)
- Maximum Drawdown (peak-to-trough decline)
- Sortino Ratio (downside deviation only)
- Calmar Ratio (CAGR / max drawdown)
- Win Rate (% of months with positive net return)
- Monthly return statistics (mean, std, skew, kurtosis)
- Average portfolio turnover
- Estimated total cost drag
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd
from loguru import logger

from backtesting.engine import BacktestResult


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class PerformanceMetrics:
    # Period
    start_date: date
    end_date: date
    n_months: int

    # Returns
    total_return: float        # e.g. 0.85 = 85%
    cagr: float                # annualised compound return
    monthly_return_mean: float
    monthly_return_std: float
    monthly_return_skew: float
    monthly_return_kurt: float

    # Risk
    max_drawdown: float        # e.g. -0.35 = -35%
    volatility: float          # annualised monthly vol

    # Risk-adjusted
    sharpe_ratio: float        # annualised, vs risk_free_rate
    sortino_ratio: float       # annualised, downside std
    calmar_ratio: float        # CAGR / |max_drawdown|

    # Win/loss
    win_rate: float            # fraction of months positive
    avg_win: float             # mean positive monthly return
    avg_loss: float            # mean negative monthly return
    best_month: float
    worst_month: float

    # Portfolio activity
    avg_positions: float
    avg_turnover: float        # one-way monthly turnover
    total_cost_drag: float     # cumulative cost / initial capital

    # Gate pass/fail
    passes_sharpe_gate: bool
    passes_drawdown_gate: bool

    def passes_phase3_gate(self) -> bool:
        return self.passes_sharpe_gate and self.passes_drawdown_gate


# ---------------------------------------------------------------------------
# Main metrics calculation
# ---------------------------------------------------------------------------

def calculate_metrics(
    result: BacktestResult,
    risk_free_rate_annual: float = 0.04,
    min_sharpe: float = 0.6,
    max_drawdown_limit: float = 0.40,
) -> PerformanceMetrics:
    """
    Compute full performance metrics from a BacktestResult.

    Args:
        result:                Completed BacktestResult from run_backtest().
        risk_free_rate_annual: Annual risk-free rate (default 4%).
        min_sharpe:            Phase 3 gate threshold (default 0.6).
        max_drawdown_limit:    Phase 3 gate threshold (default 40%).

    Returns:
        PerformanceMetrics dataclass.
    """
    records = result.monthly_records
    if not records:
        logger.error("calculate_metrics: no monthly records")
        return _empty_metrics(result.start_date, result.end_date)

    # Build monthly return series
    values = np.array([r.portfolio_value for r in records])
    returns = np.diff(values) / values[:-1]  # month-over-month returns

    if len(returns) < 2:
        logger.warning("calculate_metrics: fewer than 2 monthly returns — metrics unreliable")

    # Dates
    dates = [r.date for r in records]
    start_dt = dates[0]
    end_dt = dates[-1]
    n_months = len(records)

    # Total return
    total_return = (values[-1] / result.initial_capital) - 1.0

    # CAGR
    years = n_months / 12.0
    cagr = (values[-1] / result.initial_capital) ** (1.0 / years) - 1.0 if years > 0 else 0.0

    # Monthly return statistics
    mean_ret = float(np.mean(returns)) if len(returns) > 0 else 0.0
    std_ret = float(np.std(returns, ddof=1)) if len(returns) > 1 else 0.0
    skew_ret = float(_safe_skew(returns))
    kurt_ret = float(_safe_kurt(returns))

    # Annualised volatility
    vol_annual = std_ret * np.sqrt(12)

    # Risk-free rate (monthly)
    rf_monthly = (1 + risk_free_rate_annual) ** (1/12) - 1

    # Sharpe ratio (annualised)
    excess_returns = returns - rf_monthly
    sharpe = (
        (np.mean(excess_returns) / np.std(excess_returns, ddof=1)) * np.sqrt(12)
        if len(excess_returns) > 1 and np.std(excess_returns, ddof=1) > 0
        else 0.0
    )

    # Sortino ratio (annualised, using downside deviation)
    downside = returns[returns < rf_monthly] - rf_monthly
    downside_std = float(np.sqrt(np.mean(downside ** 2))) if len(downside) > 0 else 0.0
    sortino = (
        (np.mean(excess_returns) / downside_std) * np.sqrt(12)
        if downside_std > 0
        else (np.inf if np.mean(excess_returns) > 0 else 0.0)
    )

    # Maximum drawdown
    cumulative = np.cumprod(1 + returns)
    cumulative = np.insert(cumulative, 0, 1.0)  # include starting point
    running_max = np.maximum.accumulate(cumulative)
    drawdowns = (cumulative - running_max) / running_max
    max_dd = float(np.min(drawdowns))

    # Calmar ratio
    calmar = abs(cagr / max_dd) if max_dd < 0 else np.inf

    # Win / loss stats
    positive = returns[returns > 0]
    negative = returns[returns < 0]
    win_rate = len(positive) / len(returns) if len(returns) > 0 else 0.0
    avg_win = float(np.mean(positive)) if len(positive) > 0 else 0.0
    avg_loss = float(np.mean(negative)) if len(negative) > 0 else 0.0
    best_month = float(np.max(returns)) if len(returns) > 0 else 0.0
    worst_month = float(np.min(returns)) if len(returns) > 0 else 0.0

    # Portfolio activity
    avg_positions = float(np.mean([r.n_positions for r in records]))
    avg_turnover = float(np.mean([r.turnover for r in records]))
    total_cost_drag = sum(r.cost_drag * r.portfolio_value for r in records) / result.initial_capital

    # Gate checks
    passes_sharpe = sharpe >= min_sharpe
    passes_drawdown = abs(max_dd) <= max_drawdown_limit

    return PerformanceMetrics(
        start_date=start_dt,
        end_date=end_dt,
        n_months=n_months,
        total_return=total_return,
        cagr=cagr,
        monthly_return_mean=mean_ret,
        monthly_return_std=std_ret,
        monthly_return_skew=skew_ret,
        monthly_return_kurt=kurt_ret,
        max_drawdown=max_dd,
        volatility=vol_annual,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        calmar_ratio=calmar,
        win_rate=win_rate,
        avg_win=avg_win,
        avg_loss=avg_loss,
        best_month=best_month,
        worst_month=worst_month,
        avg_positions=avg_positions,
        avg_turnover=avg_turnover,
        total_cost_drag=total_cost_drag,
        passes_sharpe_gate=passes_sharpe,
        passes_drawdown_gate=passes_drawdown,
    )


# ---------------------------------------------------------------------------
# Helper stats
# ---------------------------------------------------------------------------

def _safe_skew(arr: np.ndarray) -> float:
    if len(arr) < 3:
        return 0.0
    mean = np.mean(arr)
    std = np.std(arr, ddof=1)
    if std == 0:
        return 0.0
    return float(np.mean(((arr - mean) / std) ** 3))


def _safe_kurt(arr: np.ndarray) -> float:
    if len(arr) < 4:
        return 0.0
    mean = np.mean(arr)
    std = np.std(arr, ddof=1)
    if std == 0:
        return 0.0
    return float(np.mean(((arr - mean) / std) ** 4) - 3)


def _empty_metrics(start: date, end: date) -> PerformanceMetrics:
    return PerformanceMetrics(
        start_date=start, end_date=end, n_months=0,
        total_return=0.0, cagr=0.0,
        monthly_return_mean=0.0, monthly_return_std=0.0,
        monthly_return_skew=0.0, monthly_return_kurt=0.0,
        max_drawdown=0.0, volatility=0.0,
        sharpe_ratio=0.0, sortino_ratio=0.0, calmar_ratio=0.0,
        win_rate=0.0, avg_win=0.0, avg_loss=0.0,
        best_month=0.0, worst_month=0.0,
        avg_positions=0.0, avg_turnover=0.0, total_cost_drag=0.0,
        passes_sharpe_gate=False, passes_drawdown_gate=False,
    )


# ---------------------------------------------------------------------------
# Monthly summary builder
# ---------------------------------------------------------------------------

def build_monthly_summary(result: BacktestResult) -> pd.DataFrame:
    """
    Return a DataFrame with one row per rebalance month showing key metrics.
    """
    rows = []
    for rec in result.monthly_records:
        rows.append({
            "date": rec.date,
            "portfolio_value": rec.portfolio_value,
            "net_return": rec.net_return,
            "gross_return": rec.gross_return,
            "cost_drag": rec.cost_drag,
            "turnover": rec.turnover,
            "n_positions": rec.n_positions,
            "cash": rec.cash,
        })
    return pd.DataFrame(rows)


def calculate_sharpe(returns: pd.Series, risk_free: float = 0.0) -> float:
    """Standalone annualised Sharpe ratio from a daily returns series."""
    excess = returns - risk_free / 252
    std = excess.std()
    if std == 0:
        return 0.0
    return float((excess.mean() / std) * (252 ** 0.5))
