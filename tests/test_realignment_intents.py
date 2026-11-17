"""
tests/test_realignment_intents.py

A rebalance from a held book to a different target must SELL the names that dropped
out. Fixture: the Phase-2 book (held in the retired paper account DUR222564) -> the
2026Q4b tracked book. build_intents() once iterated only the target book, so
a "rebalance" never exited anything (fixed in 4289eaa). This pins that fix against the
two real book files, so the dry run is not the first place a regression shows.

Pure function, no broker: nothing here connects or places an order.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from broker.ibkr_paper import build_intents  # noqa: E402

HELD_BOOK = ROOT / "broker" / "compliant_book.csv"                 # Phase-2, in the account
TARGET_BOOK = ROOT / "forward_test" / "2026Q4b" / "incumbent_book.csv"
NAV = 1_000_000.0
PRICE = 20.0


@pytest.fixture
def books():
    held = pd.read_csv(HELD_BOOK)
    target = pd.read_csv(TARGET_BOOK)
    shares = int(NAV * 0.0667 // PRICE)
    positions = pd.DataFrame({"ticker": held["ticker"], "position": shares})
    names = set(held["ticker"]) | set(target["ticker"])
    prices = {t: {"price": PRICE} for t in names}
    adv = dict(zip(target["ticker"], target["adv_usd"]))
    return target, positions, prices, adv, shares


def test_books_differ_by_the_expected_names():
    held = set(pd.read_csv(HELD_BOOK)["ticker"])
    target = set(pd.read_csv(TARGET_BOOK)["ticker"])
    assert held - target == {"ADAM", "MCS", "ZVRA"}          # to SELL
    assert target - held == {"CCSI", "CDE", "GPRE"}          # to BUY


def test_dropped_names_are_sold_in_full_and_first(books):
    target, positions, prices, adv, shares = books
    intents = build_intents(target, positions, NAV, prices, adv)
    exits = {i.ticker: i for i in intents if i.ticker in {"ADAM", "MCS", "ZVRA"}}
    assert set(exits) == {"ADAM", "MCS", "ZVRA"}
    assert all(i.action == "SELL" and i.quantity == shares for i in exits.values())
    assert {i.ticker for i in intents[:3]} == {"ADAM", "MCS", "ZVRA"}


def test_new_names_are_bought(books):
    target, positions, prices, adv, _ = books
    buys = {i.ticker for i in build_intents(target, positions, NAV, prices, adv)
            if i.action == "BUY"}
    assert {"CCSI", "CDE", "GPRE"} <= buys


def test_unfilled_names_are_bought_from_zero(books):
    """MOV/NUTX/SENEA hold 0 after 09-29's unfills: they must be bought, not skipped."""
    target, positions, prices, adv, shares = books
    positions.loc[positions["ticker"].isin(["MOV", "NUTX", "SENEA"]), "position"] = 0
    by = {i.ticker: i for i in build_intents(target, positions, NAV, prices, adv)}
    for t in ("MOV", "NUTX", "SENEA"):
        assert by[t].action == "BUY" and by[t].quantity == _target_shares(target)


def _target_shares(book) -> int:
    """Expected full position under the sizing rules (reserve, weight scaling)."""
    from config.settings import PORTFOLIO
    w = book["weight_equal"].astype(float)
    scale = 1 / w.sum() if w.sum() > 1 else 1.0
    return int(NAV * (1 - PORTFOLIO["cash_reserve"]) * w.iloc[0] * scale // PRICE)


def test_book_never_sizes_above_nav_after_the_reserve(books):
    """15 x 0.0667 = 100.05%: weights are scaled, and the reserve stays uninvested."""
    from config.settings import PORTFOLIO
    target, _p, prices, adv, _ = books
    flat = pd.DataFrame(columns=["ticker", "position"])
    spent = sum(i.quantity * PRICE for i in build_intents(target, flat, NAV, prices, adv))
    assert spent <= NAV * (1 - PORTFOLIO["cash_reserve"])
    assert spent > NAV * (1 - PORTFOLIO["cash_reserve"]) * 0.99


def test_small_adjustments_to_held_names_are_skipped(books):
    """The 2026-10-01 top-up sent six trims of $70-$1,400; they are now skipped."""
    target, positions, prices, adv, _ = books
    full = _target_shares(target)
    held = pd.DataFrame({"ticker": target["ticker"], "position": full + 10})  # $200 over
    assert build_intents(target, held, NAV, prices, adv) == []


def test_big_adjustment_new_position_and_exit_are_never_skipped(books):
    target, positions, prices, adv, _ = books
    full = _target_shares(target)
    held = pd.DataFrame({"ticker": list(target["ticker"][1:]) + ["OLD"],
                         "position": [full] * 13 + [full // 2] + [5]})
    prices = {**prices, "OLD": {"price": PRICE}}
    by = {i.ticker: i for i in build_intents(target, held, NAV, prices, adv)}
    first, half = target["ticker"].iloc[0], target["ticker"].iloc[-1]
    assert by[first].action == "BUY" and by[first].quantity == full        # new name
    assert by[half].action == "BUY" and by[half].quantity == full - full // 2  # big top-up
    assert by["OLD"].action == "SELL" and by["OLD"].quantity == 5         # tiny exit


def test_buy_target_weight_is_the_resulting_position_not_the_order(books):
    """The gate's 15% cap must judge the position after the order, not the order."""
    target, positions, prices, adv, _ = books
    full = _target_shares(target)
    held = pd.DataFrame({"ticker": target["ticker"], "position": full})
    held.loc[0, "position"] = full // 2
    buy = next(i for i in build_intents(target, held, NAV, prices, adv)
               if i.action == "BUY")
    assert buy.target_weight == pytest.approx(full * PRICE / NAV)


def test_sell_limits_are_below_and_buy_limits_above_reference(books):
    target, positions, prices, adv, _ = books
    for i in build_intents(target, positions, NAV, prices, adv):
        assert (i.limit_price < PRICE) if i.action == "SELL" else (i.limit_price > PRICE)


def test_held_name_without_a_price_is_not_silently_dropped_from_the_plan(books):
    """No price -> cannot exit. It is skipped WITH a warning; the others still exit."""
    target, positions, prices, adv, _ = books
    del prices["ZVRA"]
    exits = {i.ticker for i in build_intents(target, positions, NAV, prices, adv)
             if i.action == "SELL"}
    assert "ZVRA" not in exits and {"ADAM", "MCS"} <= exits


def test_fresh_build_from_a_flat_account_is_all_buys_under_the_caps(books):
    """DU1234567 starts flat: the first run is a fresh build, 15 BUYs and no SELLs."""
    target, _positions, prices, adv, _ = books
    flat = pd.DataFrame(columns=["ticker", "position"])
    intents = build_intents(target, flat, NAV, prices, adv)
    assert {i.ticker for i in intents} == set(target["ticker"])
    assert all(i.action == "BUY" for i in intents)
    assert all(i.target_weight <= 0.15 for i in intents)      # §4 position cap
