"""
factors/quality.py

Quality factor calculations.

Factors implemented exactly as specified in the master prompt:
- ROIC               (higher = better)
- Gross Margin       (higher = better)
- Piotroski F-Score  (0-9; higher = better financial health)
- Accruals Ratio     (lower = better earnings quality)
- Debt-to-EBITDA     (lower = safer; stored but not in composite by default)

RULE 1 ENFORCEMENT: Caller must pass point-in-time data only.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from loguru import logger


# ---------------------------------------------------------------------------
# ROIC
# ---------------------------------------------------------------------------

CORPORATE_TAX_RATE = 0.21  # Standard US corporate tax rate


def calc_roic(fundamentals: pd.DataFrame) -> pd.Series:
    """
    Return on Invested Capital.

    NOPAT = operating_income * (1 - 0.21)
    invested_capital = total_equity + net_debt
    ROIC = NOPAT / invested_capital

    Higher = better. Returns NaN if invested_capital <= 0.
    """
    result = {}
    for _, row in fundamentals.iterrows():
        ticker = row["ticker"]
        op_income = row.get("operating_income")
        total_equity = row.get("total_equity")
        net_debt = row.get("net_debt", 0)

        if pd.isna(op_income) or pd.isna(total_equity):
            result[ticker] = np.nan
            continue

        nopat = op_income * (1 - CORPORATE_TAX_RATE)
        invested_capital = total_equity + (net_debt if not pd.isna(net_debt) else 0)

        if invested_capital <= 0:
            result[ticker] = np.nan
            continue

        result[ticker] = nopat / invested_capital

    return pd.Series(result, name="roic")


# ---------------------------------------------------------------------------
# Gross Margin
# ---------------------------------------------------------------------------

def calc_gross_margin(fundamentals: pd.DataFrame) -> pd.Series:
    """
    Gross Margin = gross_profit / revenue.

    Higher = better pricing power. Returns NaN if revenue <= 0.
    Uses pre-computed gross_margin if available, else calculates from components.
    """
    result = {}
    for _, row in fundamentals.iterrows():
        ticker = row["ticker"]

        # Use pre-computed if available
        gm = row.get("gross_margin")
        if not pd.isna(gm):
            result[ticker] = float(gm)
            continue

        # Compute from components
        revenue = row.get("revenue")
        gross_profit = row.get("gross_profit")

        if pd.isna(revenue) or pd.isna(gross_profit) or revenue <= 0:
            result[ticker] = np.nan
            continue

        result[ticker] = gross_profit / revenue

    return pd.Series(result, name="gross_margin")


# ---------------------------------------------------------------------------
# Accruals Ratio
# ---------------------------------------------------------------------------

def calc_accruals_ratio(fundamentals: pd.DataFrame) -> pd.Series:
    """
    Accruals Ratio = (net_income - operating_cash_flow) / total_assets.

    More negative = better (cash earnings exceed reported earnings).
    Returns NaN if total_assets <= 0.
    """
    result = {}
    for _, row in fundamentals.iterrows():
        ticker = row["ticker"]
        net_income = row.get("net_income")
        ocf = row.get("operating_cash_flow")
        assets = row.get("total_assets")

        if pd.isna(net_income) or pd.isna(ocf) or pd.isna(assets) or assets <= 0:
            result[ticker] = np.nan
            continue

        result[ticker] = (net_income - ocf) / assets

    return pd.Series(result, name="accruals_ratio")


# ---------------------------------------------------------------------------
# Debt-to-EBITDA
# ---------------------------------------------------------------------------

def calc_debt_to_ebitda(fundamentals: pd.DataFrame) -> pd.Series:
    """
    Total Debt / EBITDA. Lower = safer balance sheet.
    Returns NaN if EBITDA <= 0.
    """
    result = {}
    for _, row in fundamentals.iterrows():
        ticker = row["ticker"]
        total_debt = row.get("total_debt", 0)
        ebitda = row.get("ebitda")

        if pd.isna(ebitda) or ebitda <= 0:
            result[ticker] = np.nan
            continue

        result[ticker] = (total_debt if not pd.isna(total_debt) else 0) / ebitda

    return pd.Series(result, name="debt_to_ebitda")


# ---------------------------------------------------------------------------
# Piotroski F-Score
# ---------------------------------------------------------------------------

def calc_f_score(
    fundamentals_current: pd.DataFrame,
    fundamentals_prior: pd.DataFrame,
) -> pd.Series:
    """
    Piotroski F-Score (0-9).

    Requires two periods of fundamental data:
    - fundamentals_current: most recent available quarter
    - fundamentals_prior:   same quarter one year prior (for YoY signals)

    9 binary signals:

    Profitability (4):
      F1: ROA > 0
      F2: Operating cash flow > 0
      F3: ROA improving YoY
      F4: OCF > net income (accruals check)

    Leverage (3):
      F5: Long-term debt ratio falling YoY
      F6: Current ratio improving YoY (proxied by quick assets if current ratio unavailable)
      F7: No new share issuance YoY

    Efficiency (2):
      F8: Gross margin improving YoY
      F9: Asset turnover improving YoY
    """
    # Build prior-year lookup: ticker -> prior row
    if fundamentals_prior.empty:
        prior_lookup = {}
    else:
        prior_lookup = {row["ticker"]: row for _, row in fundamentals_prior.iterrows()}

    result = {}

    for _, row in fundamentals_current.iterrows():
        ticker = row["ticker"]
        prior = prior_lookup.get(ticker)

        score = 0

        # ---- Profitability signals ----
        total_assets = row.get("total_assets")
        net_income = row.get("net_income")
        ocf = row.get("operating_cash_flow")

        roa = (net_income / total_assets
               if not pd.isna(net_income) and not pd.isna(total_assets) and total_assets > 0
               else None)

        # F1: Positive ROA
        if roa is not None and roa > 0:
            score += 1

        # F2: Positive operating cash flow
        if not pd.isna(ocf) and ocf > 0:
            score += 1

        # F3: Improving ROA (needs prior year)
        if prior is not None:
            prior_assets = prior.get("total_assets")
            prior_ni = prior.get("net_income")
            prior_roa = (prior_ni / prior_assets
                         if not pd.isna(prior_ni) and not pd.isna(prior_assets) and prior_assets > 0
                         else None)
            if roa is not None and prior_roa is not None and roa > prior_roa:
                score += 1

        # F4: OCF > net income (cash-based earnings quality)
        if not pd.isna(ocf) and not pd.isna(net_income) and ocf > net_income:
            score += 1

        # ---- Leverage signals ----
        total_debt = row.get("total_debt", 0)
        if pd.isna(total_debt):
            total_debt = 0

        # F5: Falling leverage (debt / assets ratio)
        if prior is not None and not pd.isna(total_assets) and total_assets > 0:
            cur_leverage = total_debt / total_assets
            prior_assets_val = prior.get("total_assets")
            prior_debt = prior.get("total_debt", 0)
            if pd.isna(prior_debt):
                prior_debt = 0
            if not pd.isna(prior_assets_val) and prior_assets_val > 0:
                prior_leverage = prior_debt / prior_assets_val
                if cur_leverage < prior_leverage:
                    score += 1

        # F6: Improving liquidity (use cash/assets as proxy if current ratio unavailable)
        cash = row.get("cash_and_equivalents", 0)
        if pd.isna(cash):
            cash = 0
        if prior is not None and not pd.isna(total_assets) and total_assets > 0:
            cur_liquidity = cash / total_assets
            prior_assets_val = prior.get("total_assets")
            prior_cash = prior.get("cash_and_equivalents", 0)
            if pd.isna(prior_cash):
                prior_cash = 0
            if not pd.isna(prior_assets_val) and prior_assets_val > 0:
                prior_liquidity = prior_cash / prior_assets_val
                if cur_liquidity > prior_liquidity:
                    score += 1

        # F7: No dilution (shares outstanding not increased)
        shares = row.get("shares_outstanding")
        if prior is not None and not pd.isna(shares):
            prior_shares = prior.get("shares_outstanding")
            if not pd.isna(prior_shares) and shares <= prior_shares:
                score += 1

        # ---- Efficiency signals ----
        revenue = row.get("revenue")
        gross_profit = row.get("gross_profit")

        gm = (gross_profit / revenue
              if not pd.isna(gross_profit) and not pd.isna(revenue) and revenue > 0
              else None)

        # F8: Improving gross margin
        if prior is not None and gm is not None:
            prior_rev = prior.get("revenue")
            prior_gp = prior.get("gross_profit")
            prior_gm = (prior_gp / prior_rev
                        if not pd.isna(prior_gp) and not pd.isna(prior_rev) and prior_rev > 0
                        else None)
            if prior_gm is not None and gm > prior_gm:
                score += 1

        # F9: Improving asset turnover
        at = (revenue / total_assets
              if not pd.isna(revenue) and not pd.isna(total_assets) and total_assets > 0
              else None)
        if prior is not None and at is not None:
            prior_assets_val = prior.get("total_assets")
            prior_rev = prior.get("revenue")
            prior_at = (prior_rev / prior_assets_val
                        if not pd.isna(prior_rev) and not pd.isna(prior_assets_val) and prior_assets_val > 0
                        else None)
            if prior_at is not None and at > prior_at:
                score += 1

        result[ticker] = score

    return pd.Series(result, name="f_score")


# ---------------------------------------------------------------------------
# Combined quality factor calculation
# ---------------------------------------------------------------------------

def calculate_quality_factors(
    fundamentals_current: pd.DataFrame,
    fundamentals_prior: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Calculate all quality factors for a universe snapshot.

    Args:
        fundamentals_current: Most recent available fundamental data per ticker
                              (point-in-time filtered by caller).
        fundamentals_prior:   Same data for ~1 year prior (for F-Score YoY signals).
                              If None, YoY-dependent signals score 0.

    Returns:
        DataFrame indexed by ticker with columns:
            roic, gross_margin, f_score, accruals_ratio, debt_to_ebitda
    """
    if fundamentals_current.empty:
        logger.warning("quality: empty fundamentals DataFrame passed")
        return pd.DataFrame()

    tickers = fundamentals_current["ticker"].unique()
    logger.debug(f"Calculating quality factors for {len(tickers)} tickers")

    prior = fundamentals_prior if fundamentals_prior is not None else pd.DataFrame()

    roic          = calc_roic(fundamentals_current)
    gross_margin  = calc_gross_margin(fundamentals_current)
    f_score       = calc_f_score(fundamentals_current, prior)
    accruals      = calc_accruals_ratio(fundamentals_current)
    debt_ebitda   = calc_debt_to_ebitda(fundamentals_current)

    result = pd.DataFrame({
        "roic":           roic,
        "gross_margin":   gross_margin,
        "f_score":        f_score,
        "accruals_ratio": accruals,
        "debt_to_ebitda": debt_ebitda,
    })

    coverage = result.notna().mean() * 100
    logger.debug(f"Quality factor coverage: {coverage.to_dict()}")

    return result


# Aliases for consistency with system prompt specification
calculate_piotroski_fscore = calc_f_score
