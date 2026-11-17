"""
factors/health.py -- which weighted factors actually vary across the universe.

A factor that is the same number for every stock adds the same amount to every
composite: it changes no rank, so its weight is weight in name only. Two were found
that way on 2026-11-17, both looking healthy (100% non-null):

  sentiment      11%  no transcript newer than 90 days (the fetch step was removed
                      2026-10-12); every stock 50 -- at the 2026Q4b build as well
  revenue_accel   5%  yfinance returns 5 quarters of revenue, the factor needs 6;
                      every stock 50 since it was added

Used by factors/calculator.py (a loud log line at every calculation) and by the
health check (factors_live). Changing the weights is a strategy decision, not this
module's: it only reports.
"""
from __future__ import annotations

import pandas as pd


def factor_column(scores: pd.DataFrame, factor: str) -> str | None:
    """The column holding a factor's 0-100 score: '<factor>_score', else '<factor>'."""
    for col in (f"{factor}_score", factor):
        if col in scores.columns:
            return col
    return None


def constant_factors(scores: pd.DataFrame, weights: dict) -> list[tuple[str, float]]:
    """[(factor, weight)] for every factor with a non-zero weight whose score takes at
    most one value across `scores` (a missing column counts: it varies nothing)."""
    out = []
    for factor, w in weights.items():
        if not w:
            continue
        col = factor_column(scores, factor)
        if col is None or scores[col].dropna().nunique() <= 1:
            out.append((factor, float(w)))
    return out


def describe(dead: list[tuple[str, float]]) -> str:
    return ", ".join(f"{f} {w:.0%}" for f, w in dead)
