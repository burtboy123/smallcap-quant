"""
backtesting/costs.py

Transaction cost model for the quantitative backtest.

Cost components per RULE 3:
1. Bid-ask spread: 0.3% each way (0.3% on entry = half round-trip)
2. Commission: $0.005 per share, $1.00 minimum per trade
3. Market impact: 0.1% for orders > $50K notional

Total assumed round-trip cost: ~0.8% per position change.
"""

from __future__ import annotations

from config.settings import TRANSACTION_COSTS


def calc_trade_cost(
    shares: float,
    price: float,
    notional: float,
) -> float:
    """
    Calculate one-way transaction cost for a single trade.

    Args:
        shares:   Number of shares (positive, absolute value).
        price:    Execution price per share.
        notional: Absolute dollar value of the trade.

    Returns:
        Total cost in dollars (always positive).
    """
    tc = TRANSACTION_COSTS

    # 1. Bid-ask spread (one way — the other half is charged on exit)
    spread_cost = notional * tc["bid_ask_spread_pct"]

    # 2. Commission
    commission = max(abs(shares) * tc["commission_per_share"], tc["min_commission"])

    # 3. Market impact (only for large orders)
    impact_cost = 0.0
    if notional > tc["market_impact_threshold"]:
        impact_cost = notional * tc["market_impact_pct"]

    return spread_cost + commission + impact_cost


def calc_rebalance_costs(
    current_weights: dict[str, float],
    target_weights: dict[str, float],
    portfolio_value: float,
    prices: dict[str, float],
    min_trade_notional: float = 100.0,
) -> tuple[float, dict[str, float]]:
    """
    Calculate total transaction costs and trade map for a full rebalance.

    Args:
        current_weights:   {ticker: weight} of current portfolio (sum <= 1).
        target_weights:    {ticker: weight} of target portfolio (sum <= 1).
        portfolio_value:   Current total portfolio value in dollars.
        prices:            {ticker: price} current prices.
        min_trade_notional: Minimum trade size — smaller trades are skipped.

    Returns:
        (total_cost, trades) where:
            total_cost: total dollars paid in transaction costs
            trades:     {ticker: dollar_notional} signed (+ = buy, - = sell)
    """
    all_tickers = set(current_weights) | set(target_weights)

    total_cost = 0.0
    trades: dict[str, float] = {}

    for ticker in all_tickers:
        current_w = current_weights.get(ticker, 0.0)
        target_w = target_weights.get(ticker, 0.0)
        delta_w = target_w - current_w

        if abs(delta_w) < 1e-8:
            continue

        notional = abs(delta_w) * portfolio_value
        if notional < min_trade_notional:
            continue

        price = prices.get(ticker)
        if price is None or price <= 0:
            continue

        shares = notional / price
        cost = calc_trade_cost(shares, price, notional)
        total_cost += cost
        trades[ticker] = delta_w * portfolio_value  # signed dollar amount

    return total_cost, trades


def estimate_annual_cost_drag(
    monthly_turnover_pct: float,
) -> float:
    """
    Estimate annualised cost drag given average monthly turnover.

    Args:
        monthly_turnover_pct: Average % of portfolio traded each month (0-1).

    Returns:
        Annualised cost drag as a decimal (e.g. 0.048 = 4.8%).
    """
    tc = TRANSACTION_COSTS
    # Approximate one-way cost rate
    one_way_cost = (
        tc["bid_ask_spread_pct"]
        + tc["commission_per_share"] / 10.0   # assume $10 avg price
        + tc["market_impact_pct"] * 0.5       # assume 50% of trades exceed threshold
    )
    monthly_cost = monthly_turnover_pct * one_way_cost
    annual_cost = monthly_cost * 12
    return annual_cost
