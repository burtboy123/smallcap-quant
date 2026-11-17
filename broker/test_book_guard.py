"""
broker/test_book_guard.py — proof the risk guard FIRES.

Includes the real regression case: the retired 2026Q4 book, which was tracked for 7
days despite being un-investable. The guard must reject it.

Run:  python -m pytest broker/test_book_guard.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from broker.book_guard import (  # noqa: E402
    BookRiskViolation, assert_book_investable, check_book_risk, select_under_limits,
)
from broker.killswitch import (  # noqa: E402
    MAX_SECTOR_WEIGHT,
)

RETIRED_Q4 = ROOT / "forward_test" / "2026Q4_retired" / "incumbent_book.csv"
LIVE_Q4 = ROOT / "forward_test" / "2026Q4" / "incumbent_book.csv"


def _book(n=12, sector="Healthcare", adv=20e6, extra=None):
    rows = [{"ticker": f"T{i}", "sector": sector, "adv_usd": adv} for i in range(n)]
    if extra:
        rows += extra
    b = pd.DataFrame(rows)
    b["weight_equal"] = 1 / len(b)
    return b


# ── the real regression: the book that shipped ──────────────────────────────
def test_guard_rejects_the_actual_retired_q4_book():
    """THE REGRESSION CASE. This book was tracked live for 7 days."""
    path = RETIRED_Q4 if RETIRED_Q4.exists() else LIVE_Q4
    if not path.exists():
        pytest.skip("Q4 book not present")
    # Integration test: the illiquid-name verdicts come from REAL ADV in the prices
    # table. Without that DB (a fresh clone, CI, a cloud session) there is nothing
    # to judge against -- skip, as for a missing book file.
    try:
        from broker.book_guard import load_adv
        load_adv(["SPY"])
    except Exception as e:                                          # noqa: BLE001
        pytest.skip(f"prices DB unavailable: {type(e).__name__}")
    book = pd.read_csv(path)
    with pytest.raises(BookRiskViolation) as exc:
        assert_book_investable(book, label="2026Q4 incumbent")
    msg = str(exc.value)
    assert "Energy" in msg, "must flag the Energy sector breach"
    assert any(t in msg for t in ("WEYS", "HBB", "ATNI")), "must flag illiquid names"
    print("\n" + msg)


def test_guard_accepts_the_compliant_book():
    p = ROOT / "broker" / "compliant_book.csv"
    if not p.exists():
        pytest.skip("compliant book not built yet")
    assert_book_investable(pd.read_csv(p), label="compliant")


# ── each limit, individually ────────────────────────────────────────────────
def test_sector_cap_fires():
    b = _book(n=12, sector="Energy")          # 100% one sector
    v = check_book_risk(b)
    assert any("Energy" in x and "cap" in x for x in v), v


def test_liquidity_floor_fires():
    b = _book(n=11, extra=[{"ticker": "ILLQ", "sector": "Utilities",
                            "adv_usd": 900_000}])
    b["weight_equal"] = 1 / len(b)
    v = check_book_risk(b)
    assert any("ILLQ" in x and "floor" in x for x in v), v


def test_min_positions_fires():
    b = _book(n=5)
    v = check_book_risk(b)
    assert any("minimum" in x for x in v), v


def test_position_cap_fires():
    b = _book(n=12)
    b["weight_equal"] = [0.25] + [0.75 / 11] * 11     # one 25% position
    v = check_book_risk(b)
    assert any("position weight" in x for x in v), v


def test_missing_adv_is_a_violation_not_a_pass():
    """A guard you can bypass by omitting data is not a guard."""
    b = _book(n=12)
    b = b.drop(columns=["adv_usd"])
    b["ticker"] = [f"NOSUCH{i}" for i in range(len(b))]
    v = check_book_risk(b, adv_map={})          # no ADV data at all; no DB needed
    assert any("ADV unknown" in x for x in v), v


def test_missing_sector_is_a_violation_not_a_pass():
    b = _book(n=12).drop(columns=["sector"])
    v = check_book_risk(b)
    assert any("sector data missing" in x for x in v), v


def test_clean_book_passes():
    b = pd.concat([_book(4, "Healthcare"), _book(4, "Energy"),
                   _book(4, "Industrials"), _book(3, "Utilities")], ignore_index=True)
    b["ticker"] = [f"T{i}" for i in range(len(b))]
    b["weight_equal"] = 1 / len(b)
    assert check_book_risk(b) == []


# ── selection under limits ──────────────────────────────────────────────────
def test_select_under_limits_respects_caps():
    """Needs >=4 sectors: equal-weighting across 3 sectors forces >=33.3% each,
    so no 30%-compliant book exists — see the dedicated test below."""
    ranked = pd.DataFrame([
        {"ticker": f"{p}{i}", "composite_score": base - i, "sector": sec}
        for p, sec, base in [("E", "Energy", 100), ("H", "Healthcare", 90),
                             ("I", "Industrials", 80), ("U", "Utilities", 70),
                             ("M", "Basic Materials", 60)]
        for i in range(10)
    ]).sort_values("composite_score", ascending=False)
    adv = {t: 50e6 for t in ranked.ticker}
    book = select_under_limits(ranked, 15, adv_map=adv)
    w = book.groupby("sector").size() / len(book)
    assert (w <= MAX_SECTOR_WEIGHT + 1e-9).all(), w.to_dict()
    assert len(book) == 15
    assert_book_investable(book, label="greedy", adv_map=adv)


def test_three_sectors_cannot_satisfy_a_30pct_cap():
    """Equal weight over 3 sectors means >=33.3% in one — mathematically
    impossible under a 30% cap. The guard must RAISE, not silently emit 33%."""
    ranked = pd.DataFrame([
        {"ticker": f"{p}{i}", "composite_score": base - i, "sector": sec}
        for p, sec, base in [("E", "Energy", 100), ("H", "Healthcare", 90),
                             ("I", "Industrials", 80)]
        for i in range(10)
    ])
    with pytest.raises(BookRiskViolation, match="do NOT relax"):
        select_under_limits(ranked, 15, adv_map={t: 50e6 for t in ranked.ticker})


def test_select_under_limits_raises_rather_than_relaxing():
    """If the pool cannot fill the book under the limits, RAISE — never relax."""
    ranked = pd.DataFrame([
        {"ticker": f"E{i}", "composite_score": 100 - i, "sector": "Energy"}
        for i in range(30)
    ])
    with pytest.raises(BookRiskViolation, match="do NOT relax"):
        select_under_limits(ranked, 15, adv_map={t: 50e6 for t in ranked.ticker})


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call(
        [sys.executable, "-m", "pytest", __file__, "-v", "--no-header",
         "-p", "no:cacheprovider", "-s"]))
