"""
backtesting/portfolio.py

Portfolio construction from factor scores.

Strategy:
- Select top N stocks by composite_score
- Equal weighting (default, configured via PORTFOLIO["weight_scheme"])
- Apply per-stock position cap (max 8%)
- Apply sector concentration cap (max 30%)
- Renormalise weights to sum to 1.0

Buffer rule (activated when current_holdings is supplied):
- Only SELL a current holding if its universe percentile drops below sell_threshold_pct (default 55).
  No time-based forced rotation — holdings are held until they decay.
- Only BUY a new stock if its universe percentile is above buy_threshold_pct (default 75).
  No force-filling: if fewer than n_target stocks qualify, hold cash for remaining allocation.
"""

from __future__ import annotations

import pandas as pd
from loguru import logger

from config.settings import PORTFOLIO


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def construct_portfolio(
    scores: pd.DataFrame,
    sector_map: dict[str, str] | None = None,
    n_positions: int | None = None,
    current_holdings: set | None = None,
    sell_threshold_pct: float = 55.0,
    buy_threshold_pct: float = 75.0,
    **_kwargs,                          # absorb legacy entry_dates / rebalance_date / max_hold_months
) -> dict[str, float]:
    """
    Construct target portfolio weights from factor scores.

    Buffer mode (current_holdings supplied):
      - Keep holdings whose percentile >= sell_threshold_pct (default 55).
      - Add new positions only if percentile >= buy_threshold_pct (default 75).
      - Weights are 1/n_target each; unfilled slots stay as cash (no force-filling).

    Standard mode (no current_holdings):
      - Select top-N by composite_score, equal-weight, apply sector/position caps.
    """
    if scores.empty:
        logger.warning("construct_portfolio: empty scores DataFrame")
        return {}

    # Filter to ranked stocks only
    ranked = (
        scores
        .dropna(subset=["composite_score", "universe_rank"])
        .sort_values("universe_rank")
    )

    if ranked.empty:
        logger.warning("construct_portfolio: no tickers with valid composite scores")
        return {}

    n_target = n_positions or PORTFOLIO["target_positions"]

    # -----------------------------------------------------------------
    # Buffer rule — apply when current holdings are provided
    # -----------------------------------------------------------------
    use_buffer = current_holdings is not None

    if use_buffer and len(current_holdings) > 0:
        tickers = _apply_buffer_rule(
            ranked=ranked,
            current_holdings=current_holdings,
            n_target=n_target,
            sell_threshold_pct=sell_threshold_pct,
            buy_threshold_pct=buy_threshold_pct,
        )
    else:
        # First rebalance or buffer disabled — buy top-N above buy threshold
        if use_buffer:
            tickers = _initial_buffer_buy(ranked, n_target, buy_threshold_pct)
        else:
            tickers = ranked.head(n_target)["ticker"].tolist()

    if not tickers:
        logger.warning("construct_portfolio: no qualifying stocks")
        return {}

    if len(tickers) < PORTFOLIO["min_positions"]:
        logger.warning(
            f"construct_portfolio: only {len(tickers)} stocks qualify — "
            f"below minimum {PORTFOLIO['min_positions']}. Skipping rebalance."
        )
        return {}

    # ---- Weighting -------------------------------------------------------
    # Buffer mode: 1/n_target per position so unfilled slots stay as cash.
    # Standard mode: 1/n (fully invested, equal weight).
    weight_per_pos = 1.0 / n_target if use_buffer else 1.0 / len(tickers)
    weights: dict[str, float] = {t: weight_per_pos for t in tickers}

    # Caps only needed for standard mode (buffer mode already at 1/25 = 4% < 8% cap)
    if not use_buffer:
        if sector_map:
            weights = _apply_sector_caps(weights, sector_map, PORTFOLIO["max_sector_weight"])
        weights = _apply_position_caps(weights, PORTFOLIO["max_single_position"])

    logger.debug(
        f"Portfolio constructed: {len(weights)} positions, "
        f"invested={sum(weights.values()):.1%}, "
        f"weight_per_pos={weight_per_pos:.3f}"
    )

    return weights


# ---------------------------------------------------------------------------
# Buffer rule implementation
# ---------------------------------------------------------------------------

def _pct_lookup(ranked: pd.DataFrame) -> dict[str, float]:
    """Build {ticker: percentile} from a ranked scores DataFrame."""
    if "percentile" in ranked.columns:
        return ranked.set_index("ticker")["percentile"].to_dict()
    n = len(ranked)
    result = {}
    for _, row in ranked.iterrows():
        rank = row["universe_rank"]
        result[row["ticker"]] = (1 - (rank - 1) / n) * 100 if n > 0 else 50.0
    return result


def _apply_buffer_rule(
    ranked: pd.DataFrame,
    current_holdings: set,
    n_target: int,
    sell_threshold_pct: float,
    buy_threshold_pct: float,
) -> list[str]:
    """
    Percentile-decay buffer rule — no time gate.

    SELL: drop holdings whose percentile < sell_threshold_pct.
    BUY:  add new positions only if percentile >= buy_threshold_pct.
    CASH: if fewer than n_target qualify, leave remaining slots as cash (no force-fill).

    Returns ticker list (may be shorter than n_target).
    """
    pct = _pct_lookup(ranked)

    # Step 1: keep holdings that have not decayed below the sell threshold
    keeps = [t for t in current_holdings if pct.get(t, 0.0) >= sell_threshold_pct]
    sells = len(current_holdings) - len(keeps)

    # Step 2: fill open slots with high-conviction new buys only
    slots = n_target - len(keeps)
    if slots > 0:
        new_buys = ranked[
            ~ranked["ticker"].isin(keeps) &
            (ranked["ticker"].map(lambda t: pct.get(t, 0.0)) >= buy_threshold_pct)
        ].head(slots)["ticker"].tolist()
    else:
        new_buys = []

    logger.debug(
        f"Buffer rule: kept={len(keeps)}, sold={sells}, "
        f"new_buys={len(new_buys)}, cash_slots={n_target - len(keeps) - len(new_buys)}"
    )
    return keeps + new_buys


def _initial_buffer_buy(
    ranked: pd.DataFrame,
    n_target: int,
    buy_threshold_pct: float,
) -> list[str]:
    """
    First rebalance when no holdings exist: buy everything above the buy threshold,
    up to n_target. Remainder stays as cash.
    """
    pct = _pct_lookup(ranked)
    candidates = ranked[
        ranked["ticker"].map(lambda t: pct.get(t, 0.0)) >= buy_threshold_pct
    ].head(n_target)
    return candidates["ticker"].tolist()


# ---------------------------------------------------------------------------
# Weight cap helpers
# ---------------------------------------------------------------------------

def _apply_position_caps(weights: dict[str, float], max_w: float) -> dict[str, float]:
    """
    Iteratively cap individual position weights and redistribute excess equally.

    Stops when all weights satisfy the cap (or after 50 iterations).
    """
    w = dict(weights)

    for _ in range(50):
        over = {t: v for t, v in w.items() if v > max_w + 1e-9}
        if not over:
            break

        # Cap the over-weight positions, collect excess
        excess = sum(v - max_w for v in over.values())
        for t in over:
            w[t] = max_w

        # Distribute excess to uncapped positions
        under = {t: v for t, v in w.items() if v < max_w - 1e-9}
        if not under:
            # All positions are at the cap — just normalise
            break

        per_ticker = excess / len(under)
        for t in under:
            w[t] = min(w[t] + per_ticker, max_w)

    # Final normalisation (floating point safety)
    total = sum(w.values())
    if total > 0:
        w = {t: v / total for t, v in w.items()}

    return w


def _apply_sector_caps(
    weights: dict[str, float],
    sector_map: dict[str, str],
    max_sector_w: float,
) -> dict[str, float]:
    """
    Cap sector concentration.  Excess weight is redistributed to other tickers
    in different sectors (equal share).

    Iterates until all sectors are below the cap or no further redistribution
    is possible.
    """
    w = dict(weights)

    for _ in range(50):
        # Compute sector totals
        sector_totals: dict[str, float] = {}
        for t, wt in w.items():
            sector = sector_map.get(t, "Unknown")
            sector_totals[sector] = sector_totals.get(sector, 0.0) + wt

        over_sectors = {s for s, sw in sector_totals.items() if sw > max_sector_w + 1e-9}
        if not over_sectors:
            break

        for sector in over_sectors:
            sector_tickers = [t for t in w if sector_map.get(t, "Unknown") == sector]
            other_tickers = [t for t in w if sector_map.get(t, "Unknown") != sector]

            if not other_tickers:
                break

            # Scale down sector tickers proportionally to hit the cap
            current_sw = sum(w[t] for t in sector_tickers)
            if current_sw <= 0:
                continue
            scale = max_sector_w / current_sw
            excess = sum(w[t] * (1 - scale) for t in sector_tickers)

            for t in sector_tickers:
                w[t] *= scale

            # Distribute excess to non-sector tickers
            per_other = excess / len(other_tickers)
            for t in other_tickers:
                w[t] += per_other

    # Renormalise
    total = sum(w.values())
    if total > 0:
        w = {t: v / total for t, v in w.items()}

    return w


# ---------------------------------------------------------------------------
# Portfolio diff / turnover helpers
# ---------------------------------------------------------------------------

def compute_turnover(
    old_weights: dict[str, float],
    new_weights: dict[str, float],
) -> float:
    """
    One-way portfolio turnover: sum of positive weight changes.

    Returns a value in [0, 1] representing the fraction of the portfolio
    that needs to be traded.
    """
    all_tickers = set(old_weights) | set(new_weights)
    buys = sum(
        max(new_weights.get(t, 0) - old_weights.get(t, 0), 0)
        for t in all_tickers
    )
    return buys


def get_trades(
    old_weights: dict[str, float],
    new_weights: dict[str, float],
) -> dict[str, float]:
    """
    Compute weight deltas for each ticker.

    Returns {ticker: delta_weight} — positive = buy, negative = sell.
    Excludes trivially small changes (< 0.1 bp).
    """
    all_tickers = set(old_weights) | set(new_weights)
    trades = {}
    for t in all_tickers:
        delta = new_weights.get(t, 0.0) - old_weights.get(t, 0.0)
        if abs(delta) > 1e-5:  # 0.001% threshold
            trades[t] = delta
    return trades
