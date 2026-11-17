"""
broker/targets.py — what weight each name in a forward-test book should have TODAY.

A forward-test book is bought at its entry weights on the baseline date and never
rebalanced: the tracked index is mean(close / baseline). Its weights therefore move
with prices. Sizing the paper account to the flat entry weight later in the quarter
would sell part of every winner and buy more of every loser -- a rebalance the test
never does, pulling the account away from the record it is meant to mirror.

    weight_i  =  w0_i * (price_i / baseline_i)  /  sum_j(...)  *  sum(w0)

The total stays sum(w0), so cash reserve and weight scaling downstream are unchanged.
A name with no baseline or no price keeps its entry weight (no move), and the note
says which. With no baselines at all (a freshly built book, before its first close)
the entry weights are returned unchanged -- which is exactly right on day 1.

Pure functions: nothing here connects to IB or places an order.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def baselines_for(book_path: str | Path) -> dict:
    """The recorded baselines for this book file in today's share units (restated for
    any split in corporate_actions.json), or {} if there are none. The broker's prices
    are post-split, so a raw locked baseline would size a split name at 1/ratio of its
    target and sell it down (forward_test/baselines.py)."""
    from forward_test.baselines import for_book
    return for_book(book_path)


def buy_and_hold_weights(book: pd.DataFrame, baselines: dict,
                         prices: dict | pd.Series) -> tuple[pd.Series, str]:
    """(weights indexed by ticker, one-line description of the basis)."""
    w0 = book.set_index("ticker")["weight_equal"].astype(float)
    base = pd.Series(baselines, dtype=float).reindex(w0.index)
    if base.isna().all():
        return w0, "entry weights (no baselines recorded for this book)"
    px = pd.Series(prices, dtype=float).reindex(w0.index)
    rel = px / base
    gaps = sorted(rel.index[rel.isna()])
    w = w0 * rel.fillna(1.0)
    w = w / w.sum() * w0.sum()
    note = "buy-and-hold weights (entry weight moved by price since the baseline)"
    if gaps:
        note += f"; no price move for {', '.join(gaps)}"
    return w, note
