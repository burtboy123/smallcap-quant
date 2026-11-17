"""
broker/book_guard.py — SINGLE RISK GUARD for every book this system writes.

Any book that will be tracked in a forward test OR sent to the paper account must
pass `assert_book_investable()`. It RAISES (BookRiskViolation, an AssertionError
subclass) rather than warning — consistent with the Kelly-cap assertion pattern.

Why this exists
---------------
The 2026Q4 forward test tracked a book that the system's own risk rules made
un-investable: Energy 46.81% against a 30% cap, and three names below the $5m/day
ADV floor (WEYS $0.90m, HBB $1.69m, ATNI $2.17m). After blocking those, the
remainder fell below the 10-name minimum, so the kill-switch gate blocked all 15 of
15 orders. A full quarter would have been spent measuring a book that could never be
held.

Two upstream gaps allowed it, and both are covered here:
  * `forward_test/build_q4_books.py` replicated only the SELECTION path and skipped
    production's downstream risk stage entirely.
  * Even production's stage would not have caught it: `enforce_sector_limits()` is a
    post-hoc trimmer capped at 3 removals per sector with no backfill, and
    `run_risk_check()` only REPORTS concentration in its `flags`. **Neither applies a
    liquidity floor at all** — the $5m/day rule lives in the pilot plan §4 and in the
    kill-switch, but was absent from book construction.

Thresholds are imported from broker.killswitch (which sources them from
config.settings) so there is exactly ONE definition and the guard cannot drift from
what the kill-switch enforces at the order boundary.

The guard fetches ADV itself when the caller does not supply it: a guard you can
bypass by omitting a column is not a guard.

PAPER ONLY context — this module places nothing and connects to nothing.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from broker.killswitch import (  # noqa: E402  — single source of truth
    MAX_POSITION_WEIGHT, MAX_SECTOR_WEIGHT, MIN_LIQUIDITY_ADV_USD, MIN_POSITIONS,
)


class BookRiskViolation(AssertionError):
    """A book breached a §4 hard limit. Raised, never warned."""


def load_adv(tickers: list, lookback: int = 60) -> dict:
    """Median daily dollar volume from local price history (no network)."""
    if not tickers:
        return {}
    conn = sqlite3.connect(f"file:{(ROOT/'quant_system.db').as_posix()}?mode=ro", uri=True)
    q = ",".join("?" * len(tickers))
    df = pd.read_sql(
        f"SELECT ticker, date, close, volume FROM prices WHERE ticker IN ({q})",
        conn, params=list(tickers))
    conn.close()
    out = {}
    for t, g in df.groupby("ticker"):
        dv = (g.sort_values("date").tail(lookback).eval("close * volume")).dropna()
        if len(dv):
            out[t] = float(dv.median())
    return out


def _weights(book: pd.DataFrame) -> pd.Series:
    for col in ("weight", "weight_equal", "target_weight"):
        if col in book.columns:
            w = pd.to_numeric(book[col], errors="coerce")
            if w.notna().any():
                return w.fillna(0.0)
    return pd.Series(1.0 / max(len(book), 1), index=book.index)   # equal-weight default


def check_book_risk(
    book: pd.DataFrame,
    *,
    adv_map: dict | None = None,
    sector_map: dict | None = None,
    min_positions: int = MIN_POSITIONS,
) -> list:
    """Return a list of violation strings. Empty list == investable."""
    v = []
    if book is None or book.empty:
        return [f"book is empty (minimum {min_positions} names)"]

    book = book.copy()
    if "sector" not in book.columns and sector_map:
        book["sector"] = book["ticker"].map(sector_map)
    if "adv_usd" not in book.columns:
        # `is None`, not `or`: an explicitly EMPTY map means "no ADV data" and must
        # produce "ADV unknown" violations, not a silent reload from the DB (same
        # convention as select_under_limits below).
        if adv_map is None:
            adv_map = load_adv(book["ticker"].tolist())
        book["adv_usd"] = book["ticker"].map(adv_map)

    # 1. minimum name count
    if len(book) < min_positions:
        v.append(f"only {len(book)} names — below the §4 minimum of {min_positions}")

    # 2. position cap
    w = _weights(book)
    over = book.loc[w > MAX_POSITION_WEIGHT + 1e-9, "ticker"].tolist()
    if over:
        v.append(f"position weight exceeds {MAX_POSITION_WEIGHT:.0%} cap: "
                 f"{[(t, f'{x:.2%}') for t, x in zip(over, w[w > MAX_POSITION_WEIGHT])]}")

    # 3. sector cap
    if "sector" in book.columns and book["sector"].notna().any():
        sw = book.assign(_w=w).groupby("sector")["_w"].sum()
        bad = sw[sw > MAX_SECTOR_WEIGHT + 1e-9]
        for sec, x in bad.items():
            v.append(f"sector '{sec}' at {x:.2%} exceeds the {MAX_SECTOR_WEIGHT:.0%} cap")
    else:
        v.append("sector data missing — cannot verify the sector cap")

    # 4. liquidity floor
    if book["adv_usd"].isna().any():
        missing = book.loc[book["adv_usd"].isna(), "ticker"].tolist()
        v.append(f"ADV unknown for {missing} — cannot verify the liquidity floor")
    illiquid = book[book["adv_usd"].notna() &
                    (book["adv_usd"] < MIN_LIQUIDITY_ADV_USD)]
    for r in illiquid.itertuples():
        v.append(f"{r.ticker} ADV ${r.adv_usd:,.0f}/day below the "
                 f"${MIN_LIQUIDITY_ADV_USD:,.0f}/day floor")
    return v


def assert_book_investable(
    book: pd.DataFrame,
    *,
    label: str = "book",
    adv_map: dict | None = None,
    sector_map: dict | None = None,
    min_positions: int = MIN_POSITIONS,
) -> pd.DataFrame:
    """Raise BookRiskViolation unless `book` satisfies every §4 hard limit.

    Call this before writing ANY forward-test book or sending ANY target book to the
    broker. Returns the book unchanged so it can be used inline.
    """
    v = check_book_risk(book, adv_map=adv_map, sector_map=sector_map,
                        min_positions=min_positions)
    if v:
        raise BookRiskViolation(
            f"UN-INVESTABLE BOOK ({label}) — {len(v)} violation(s):\n  - "
            + "\n  - ".join(v)
            + "\n\nThis book must not be tracked or traded. Rebuild it with the limits "
              "applied DURING selection (see forward_test/build_q4_books.py). "
              "Do NOT relax the limit to make the book fit."
        )
    return book


def select_under_limits(
    ranked: pd.DataFrame,
    n_target: int,
    *,
    adv_map: dict | None = None,
    min_positions: int = MIN_POSITIONS,
) -> pd.DataFrame:
    """Greedily take the best `n_target` names from `ranked` SUBJECT TO the limits.

    Limits are applied DURING selection — a name that would breach the sector cap or
    the liquidity floor is skipped and the next-best candidate is taken instead. This
    is the durable fix: post-hoc trimming (production's `enforce_sector_limits`) both
    shrinks the book and can still leave it over the cap, because it removes at most
    3 per sector and never backfills.

    `ranked` must be sorted best-first and carry ticker / composite_score / sector.
    """
    if adv_map is None:
        adv_map = load_adv(ranked["ticker"].tolist())

    def _greedy(size: int):
        # The cap must be evaluated against the size the book ACTUALLY reaches.
        # Deriving it from n_target is a bug: if the pool cannot fill n_target, the
        # shortfall raises every sector's share (4 of a 12-name book is 33%, not the
        # 26.7% that 4 of 15 would be). Caught by test_select_under_limits_respects_caps.
        cap = int(size * MAX_SECTOR_WEIGHT)
        picks, per_sector, skipped = [], {}, {"illiquid": [], "sector_full": []}
        for r in ranked.itertuples():
            adv = adv_map.get(r.ticker)
            if adv is None or adv < MIN_LIQUIDITY_ADV_USD:
                skipped["illiquid"].append(r.ticker)
                continue
            sec = getattr(r, "sector", None) or "Unknown"
            if per_sector.get(sec, 0) >= cap:
                skipped["sector_full"].append(r.ticker)
                continue
            picks.append({**r._asdict(), "adv_usd": adv, "sector": sec})
            per_sector[sec] = per_sector.get(sec, 0) + 1
            if len(picks) == size:
                break
        return picks, skipped

    # Converge: shrink the assumed size to what the pool actually supports, which
    # tightens the per-sector cap, and repeat until the size is stable.
    size, picks, skipped = n_target, [], {"illiquid": [], "sector_full": []}
    for _ in range(12):
        picks, skipped = _greedy(size)
        if len(picks) == size:
            break
        size = len(picks)
        if size < min_positions:
            break

    book = pd.DataFrame(picks).drop(columns=["Index"], errors="ignore")
    if len(book) < min_positions:
        raise BookRiskViolation(
            f"only {len(book)} names survive the §4 limits (need {min_positions}). "
            f"Widen the candidate pool — do NOT relax the limits. "
            f"skipped illiquid={len(skipped['illiquid'])}, "
            f"sector-full={len(skipped['sector_full'])}")
    book["weight_equal"] = round(1 / len(book), 4)
    book.attrs["skipped"] = skipped
    # This function must be incapable of returning a non-compliant book.
    assert_book_investable(book, label="select_under_limits output", adv_map=adv_map,
                           min_positions=min_positions)
    return book
