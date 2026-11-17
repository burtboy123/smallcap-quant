"""
backtesting/engine.py

Monthly rebalancing backtest engine.

Algorithm:
  1. Generate first-trading-day rebalance dates for [start_date, end_date].
  2. Load all price data for the universe once upfront.
  3. On each rebalance date:
     a. Mark portfolio to market.
     b. Calculate factor scores (uses point-in-time DB query — Rule 1 enforced).
     c. Construct target portfolio (top 25 equal-weight, sector/position capped).
     d. Compute trades (current vs target weights).
     e. Apply transaction costs (deducted from cash).
     f. Update cash & share positions.
     g. Record snapshot.
  4. Generate daily equity curve by interpolating positions between rebalances.
  5. Return BacktestResult with equity curve, trade log, and monthly summary.

RULE 3 ENFORCEMENT: All P&L calculations deduct transaction costs computed
by backtesting/costs.py before recording any return.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
import pandas as pd
from loguru import logger

from data.storage import get_session, load_prices
from factors.calculator import calculate_factors_for_date, _get_first_trading_days
from backtesting.costs import calc_rebalance_costs
from backtesting.portfolio import construct_portfolio, compute_turnover


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------

@dataclass
class RebalanceRecord:
    date: date
    portfolio_value: float
    cash: float
    n_positions: int
    gross_return: float          # return since last rebalance, before costs
    cost_drag: float             # transaction costs / portfolio_value
    net_return: float            # return since last rebalance, after costs
    turnover: float              # one-way turnover fraction
    holdings: dict[str, float]   # {ticker: weight}


@dataclass
class BacktestResult:
    equity_curve: pd.DataFrame           # date, portfolio_value, daily_return
    monthly_records: list[RebalanceRecord]
    trade_log: pd.DataFrame              # date, ticker, direction, notional, cost
    initial_capital: float
    start_date: date
    end_date: date
    universe_size: int
    n_rebalances: int = field(init=False)

    def __post_init__(self):
        self.n_rebalances = len(self.monthly_records)


# ---------------------------------------------------------------------------
# Price lookup helpers
# ---------------------------------------------------------------------------

def _build_price_matrix(
    prices_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Pivot prices to a (date × ticker) matrix of adj_close prices.
    Forward-fill missing values (non-trading days / data gaps).
    """
    if prices_df.empty:
        return pd.DataFrame()

    matrix = (
        prices_df
        .pivot_table(index="date", columns="ticker", values="adj_close", aggfunc="last")
        .sort_index()
    )
    # Forward-fill up to 5 days for weekends / holidays
    matrix = matrix.ffill(limit=5)
    return matrix


def _get_prices_on_date(
    price_matrix: pd.DataFrame,
    target_date: date,
) -> dict[str, float]:
    """
    Return {ticker: adj_close} for the closest available trading day
    on or before target_date.
    """
    if price_matrix.empty:
        return {}

    # Find closest date <= target_date
    available = price_matrix.index[price_matrix.index <= target_date]
    if len(available) == 0:
        return {}

    closest = available[-1]
    row = price_matrix.loc[closest].dropna()
    return row.to_dict()


# ---------------------------------------------------------------------------
# Main backtest runner
# ---------------------------------------------------------------------------

def run_backtest(
    tickers: list[str],
    start_date: date,
    end_date: date,
    initial_capital: float = 10_000_000.0,
    price_lookback_days: int = 400,
    sector_map: dict[str, str] | None = None,
    store_factor_scores: bool = False,
    use_buffer_rule: bool = True,
    sell_threshold_pct: float = 55.0,
    buy_threshold_pct: float = 75.0,
    is_in_sample: bool = False,
) -> BacktestResult:
    """
    Run a monthly-rebalance backtest over [start_date, end_date].

    Args:
        tickers:             Universe of tickers to evaluate each month.
        start_date:          First rebalance date (inclusive).
        end_date:            Last rebalance date (inclusive).
        initial_capital:     Starting portfolio value in dollars.
        price_lookback_days: Days of price history loaded per rebalance (for momentum).
        sector_map:          Optional {ticker: sector} for sector concentration caps.
        store_factor_scores: Whether to persist factor scores to DB.

    Returns:
        BacktestResult with equity curve, monthly records, and trade log.
    """
    logger.info(
        f"Starting backtest: {len(tickers)} tickers, "
        f"{start_date} → {end_date}, capital=${initial_capital:,.0f}"
    )

    # --- Load all price data upfront ---
    price_start = start_date - timedelta(days=price_lookback_days + 30)
    session = get_session()
    try:
        all_prices = load_prices(tickers, price_start, end_date, session=session)
    finally:
        session.close()

    if all_prices.empty:
        logger.error("No price data available — backtest aborted")
        return _empty_result(initial_capital, start_date, end_date, len(tickers))

    price_matrix = _build_price_matrix(all_prices)
    logger.info(
        f"Price matrix: {len(price_matrix)} days × {len(price_matrix.columns)} tickers"
    )

    # --- Generate rebalance dates ---
    rebalance_dates = _get_first_trading_days(start_date, end_date)
    if not rebalance_dates:
        logger.error("No rebalance dates generated")
        return _empty_result(initial_capital, start_date, end_date, len(tickers))

    logger.info(f"Rebalance dates: {len(rebalance_dates)} months")

    # --- State ---
    cash = initial_capital
    positions: dict[str, float] = {}   # {ticker: shares}
    current_weights: dict[str, float] = {}

    monthly_records: list[RebalanceRecord] = []
    trade_rows: list[dict] = []
    equity_snapshots: list[dict] = []

    prev_portfolio_value = initial_capital

    # --- Main rebalance loop ---
    for i, rd in enumerate(rebalance_dates):
        prices_today = _get_prices_on_date(price_matrix, rd)

        if not prices_today:
            logger.warning(f"[{rd}] No prices available — skipping rebalance")
            continue

        # Mark portfolio to market
        invested_value = sum(
            shares * prices_today.get(t, 0.0)
            for t, shares in positions.items()
            if t in prices_today
        )
        portfolio_value = cash + invested_value

        gross_return = (portfolio_value / prev_portfolio_value) - 1.0 if prev_portfolio_value > 0 else 0.0

        # Calculate factor scores for this rebalance date
        try:
            scores = calculate_factors_for_date(
                tickers, rd,
                price_lookback_days=price_lookback_days,
                store=store_factor_scores,
            )
        except Exception as e:
            logger.error(f"[{rd}] Factor calculation failed: {e}")
            equity_snapshots.append({"date": rd, "portfolio_value": portfolio_value})
            prev_portfolio_value = portfolio_value
            continue

        if scores.empty:
            logger.warning(f"[{rd}] No factor scores — holding current portfolio")
            equity_snapshots.append({"date": rd, "portfolio_value": portfolio_value})
            prev_portfolio_value = portfolio_value
            continue

        # Construct target portfolio (buffer rule reduces churn)
        target_weights = construct_portfolio(
            scores,
            sector_map=sector_map,
            current_holdings=set(positions.keys()) if use_buffer_rule else None,
            sell_threshold_pct=sell_threshold_pct,
            buy_threshold_pct=buy_threshold_pct,
        )

        if not target_weights:
            logger.warning(f"[{rd}] Portfolio construction returned empty — holding current")
            equity_snapshots.append({"date": rd, "portfolio_value": portfolio_value})
            prev_portfolio_value = portfolio_value
            continue

        # Compute transaction costs
        total_cost, trades = calc_rebalance_costs(
            current_weights,
            target_weights,
            portfolio_value,
            prices_today,
        )

        turnover = compute_turnover(current_weights, target_weights)
        cost_drag = total_cost / portfolio_value if portfolio_value > 0 else 0.0

        # Execute trades: update positions and cash
        for ticker, dollar_trade in trades.items():
            price = prices_today.get(ticker)
            if price is None or price <= 0:
                continue
            shares_delta = dollar_trade / price
            positions[ticker] = positions.get(ticker, 0.0) + shares_delta
            cash -= dollar_trade  # cash decreases on buy, increases on sell

            trade_rows.append({
                "date": rd,
                "ticker": ticker,
                "direction": "BUY" if dollar_trade > 0 else "SELL",
                "notional": abs(dollar_trade),
                "price": price,
                "shares": abs(shares_delta),
                "cost": total_cost * (abs(dollar_trade) / sum(abs(v) for v in trades.values()))
                        if trades else 0.0,
            })

        # Deduct transaction costs from cash
        cash -= total_cost

        # Remove near-zero positions (< $100 notional)
        positions = {
            t: s for t, s in positions.items()
            if abs(s) * prices_today.get(t, 0.0) >= 100.0
        }

        # Recompute actual portfolio value after costs
        invested_value = sum(
            shares * prices_today.get(t, 0.0)
            for t, shares in positions.items()
            if t in prices_today
        )
        portfolio_value_after = cash + invested_value
        net_return = (portfolio_value_after / prev_portfolio_value) - 1.0 if prev_portfolio_value > 0 else 0.0

        # Update current weights for next iteration
        if portfolio_value_after > 0:
            current_weights = {
                t: (s * prices_today.get(t, 0.0)) / portfolio_value_after
                for t, s in positions.items()
                if t in prices_today
            }
        else:
            current_weights = {}

        monthly_records.append(RebalanceRecord(
            date=rd,
            portfolio_value=portfolio_value_after,
            cash=cash,
            n_positions=len(positions),
            gross_return=gross_return,
            cost_drag=cost_drag,
            net_return=net_return,
            turnover=turnover,
            holdings=dict(target_weights),
        ))

        equity_snapshots.append({"date": rd, "portfolio_value": portfolio_value_after})
        prev_portfolio_value = portfolio_value_after

        logger.info(
            f"[{i+1}/{len(rebalance_dates)}] {rd}: "
            f"value=${portfolio_value_after:,.0f}, "
            f"net_ret={net_return:.2%}, "
            f"cost_drag={cost_drag:.3%}, "
            f"positions={len(positions)}, "
            f"turnover={turnover:.1%}"
        )

    # --- Build equity curve DataFrame ---
    equity_df = pd.DataFrame(equity_snapshots)
    if not equity_df.empty:
        equity_df["date"] = pd.to_datetime(equity_df["date"])
        equity_df = equity_df.sort_values("date").set_index("date")
        equity_df["daily_return"] = equity_df["portfolio_value"].pct_change()

    # --- Build trade log DataFrame ---
    trade_df = pd.DataFrame(trade_rows) if trade_rows else pd.DataFrame(
        columns=["date", "ticker", "direction", "notional", "price", "shares", "cost"]
    )

    logger.info(
        f"Backtest complete: {len(monthly_records)} months, "
        f"final value=${monthly_records[-1].portfolio_value:,.0f}" if monthly_records else "Backtest complete: 0 months"
    )

    result = BacktestResult(
        equity_curve=equity_df,
        monthly_records=monthly_records,
        trade_log=trade_df,
        initial_capital=initial_capital,
        start_date=start_date,
        end_date=end_date,
        universe_size=len(tickers),
    )

    # Persist summary to DB
    if monthly_records:
        try:
            from backtesting.metrics import calculate_metrics
            from data.storage import save_backtest_result
            m = calculate_metrics(result)
            save_backtest_result({
                "strategy_name": "main_strategy",
                "start_date": start_date,
                "end_date": end_date,
                "is_in_sample": is_in_sample,
                "total_return": m.total_return,
                "annualized_return": m.cagr,
                "benchmark_return": None,
                "alpha": None,
                "sharpe_ratio": m.sharpe_ratio,
                "sortino_ratio": m.sortino_ratio,
                "max_drawdown": m.max_drawdown,
                "total_transaction_costs": m.total_cost_drag * initial_capital,
                "number_of_trades": len(trade_df),
            })
        except Exception as e:
            logger.warning(f"Could not save backtest result to DB: {e}")

    return result


def _empty_result(capital, start, end, n_tickers) -> BacktestResult:
    return BacktestResult(
        equity_curve=pd.DataFrame(columns=["portfolio_value", "daily_return"]),
        monthly_records=[],
        trade_log=pd.DataFrame(columns=["date", "ticker", "direction", "notional", "price", "shares", "cost"]),
        initial_capital=capital,
        start_date=start,
        end_date=end,
        universe_size=n_tickers,
    )
