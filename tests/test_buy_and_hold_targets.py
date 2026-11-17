"""
tests/test_buy_and_hold_targets.py

A forward-test book is buy-and-hold, so a mid-quarter top-up must size to where the
book IS (entry weight moved by price), not back to the flat entry weight. Sized flat,
a top-up sells part of every winner and buys more of every loser -- a rebalance the
tracked test never does. broker/targets.py computes the weights; build_intents() uses
them; the weekly email measures drift against the same rule.

Pure functions, no broker: nothing here connects or places an order.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from broker.ibkr_paper import build_intents, size_book  # noqa: E402
from broker.targets import baselines_for, buy_and_hold_weights  # noqa: E402
from config.settings import ACTIVE_FT_PERIOD  # noqa: E402

NAMES = [f"N{i:02d}" for i in range(15)]
NAV0 = 1_000_000.0
BASE = 20.0


@pytest.fixture
def book():
    return pd.DataFrame({"ticker": NAMES, "weight_equal": 0.0667, "sector": "X",
                         "adv_usd": 50e6})


def _bought_at_baseline(book):
    """The account after the day-1 build: bought flat at the baseline prices."""
    flat = pd.DataFrame(columns=["ticker", "position"])
    prices = {t: {"price": BASE} for t in NAMES}
    buys = build_intents(book, flat, NAV0, prices, {})
    held = pd.DataFrame({"ticker": [i.ticker for i in buys],
                         "position": [i.quantity for i in buys]})
    cash = NAV0 - sum(i.quantity * BASE for i in buys)
    return held, cash


def _moved(held, cash, px):
    nav = cash + sum(q * px[t] for t, q in zip(held["ticker"], held["position"]))
    return nav, {t: {"price": p} for t, p in px.items()}


MOVES = {t: BASE for t in NAMES} | {"N00": 2 * BASE, "N01": 0.5 * BASE, "N02": 1.3 * BASE}


def test_weights_follow_price_and_keep_their_total(book):
    w, note = buy_and_hold_weights(book, {t: BASE for t in NAMES}, MOVES)
    assert w["N00"] == pytest.approx(4 * w["N01"])          # 2x vs 0.5x
    assert w["N02"] == pytest.approx(1.3 * w["N03"])
    assert w.sum() == pytest.approx(book["weight_equal"].sum())
    assert "buy-and-hold" in note


def test_a_fully_held_book_needs_no_trades_after_prices_move(book):
    held, cash = _bought_at_baseline(book)
    nav, prices = _moved(held, cash, MOVES)
    w, _ = buy_and_hold_weights(book, {t: BASE for t in NAMES},
                                {t: p["price"] for t, p in prices.items()})
    assert build_intents(book, held, nav, prices, {}, weights=w) == []


def test_flat_entry_weights_would_trim_winners_and_add_to_losers(book):
    """The behaviour being fixed, pinned so the contrast is explicit."""
    held, cash = _bought_at_baseline(book)
    nav, prices = _moved(held, cash, MOVES)
    by = {i.ticker: i.action for i in build_intents(book, held, nav, prices, {})}
    assert by["N00"] == "SELL" and by["N01"] == "BUY"


SMALL = {t: BASE for t in NAMES} | {"N00": 1.10 * BASE, "N01": 0.92 * BASE,
                                    "N02": 1.05 * BASE}


def _missing_n00(book, px):
    held, cash = _bought_at_baseline(book)
    gone = held[held["ticker"] == "N00"]["position"].iloc[0]
    held = held[held["ticker"] != "N00"]
    cash += gone * BASE                         # it never filled: cash stayed cash
    nav, prices = _moved(held, cash, px)
    w, _ = buy_and_hold_weights(book, {t: BASE for t in NAMES},
                                {t: p["price"] for t, p in prices.items()})
    return build_intents(book, held, nav, prices, {}, weights=w), w


def test_a_missing_name_is_bought_at_its_moved_weight_and_nothing_else(book):
    # Realistic: a top-up days or weeks in, names within about +-10%.
    intents, w = _missing_n00(book, SMALL)
    assert [(i.ticker, i.action) for i in intents] == [("N00", "BUY")]
    # Sized at its weight in the tracked book (up 10%), not at the flat 6.67%.
    assert intents[0].target_weight == pytest.approx(
        w["N00"] / book["weight_equal"].sum() * 0.995, rel=0.02)
    assert intents[0].target_weight > 0.0667 * 0.995 * 1.05


def test_a_missing_name_that_doubled_is_funded_by_trimming_the_rest(book):
    """Mirroring the tracked book exactly: if the never-bought name doubled, its
    weight in the book doubled while its cash did not, so the others are trimmed to
    fund it. Correct for a mirror, and visible in the dry run before anything trades."""
    intents, _ = _missing_n00(book, MOVES)
    by = {i.ticker: i.action for i in intents}
    assert by.pop("N00") == "BUY" and set(by.values()) == {"SELL"}


def test_no_baselines_means_entry_weights(book):
    w, note = buy_and_hold_weights(book, {}, MOVES)
    assert (w == book.set_index("ticker")["weight_equal"]).all()
    assert "entry weights" in note


def test_names_without_a_price_keep_their_entry_weight_and_are_named(book):
    px = {t: BASE for t in NAMES[1:]}
    w, note = buy_and_hold_weights(book, {t: BASE for t in NAMES}, px)
    assert w["N00"] == pytest.approx(w["N01"]) and "N00" in note


def test_baselines_come_from_the_matching_meta_key(tmp_path):
    (tmp_path / "meta.json").write_text(json.dumps({
        "incumbent_baselines": {"A": 1.0}, "challenger_baselines": {"B": 2.0}}))
    assert baselines_for(tmp_path / "incumbent_book.csv") == {"A": 1.0}
    assert baselines_for(tmp_path / "value_tilt_book.csv") == {"B": 2.0}
    assert baselines_for(tmp_path / "some_other_book.csv") == {}
    assert baselines_for(tmp_path / "nowhere" / "incumbent_book.csv") == {}


def test_the_active_book_has_a_baseline_for_every_name():
    book_p = ROOT / "forward_test" / ACTIVE_FT_PERIOD / "incumbent_book.csv"
    base = baselines_for(book_p)
    assert set(pd.read_csv(book_p)["ticker"]) <= set(base)


def test_size_book_uses_buy_and_hold_for_the_real_active_book():
    """The function main() sizes every run with, on the real Q4b book and baselines:
    a book held exactly at its buy-and-hold weights after a 2x move needs no trades."""
    book_p = ROOT / "forward_test" / ACTIVE_FT_PERIOD / "incumbent_book.csv"
    book = pd.read_csv(book_p)
    base = baselines_for(book_p)
    px = {t: base[t] for t in book["ticker"]}
    px[book["ticker"].iloc[0]] *= 2.0
    w, _ = buy_and_hold_weights(book, base, px)
    nav = 1_000_000.0
    from config.settings import PORTFOLIO
    scale = 1 / w.sum() if w.sum() > 1 else 1.0
    held = pd.DataFrame({"ticker": book["ticker"],
                         "position": [round(nav * (1 - PORTFOLIO["cash_reserve"])
                                            * w[t] * scale / px[t]) for t in book["ticker"]]})
    prices = {t: {"price": p} for t, p in px.items()}
    intents, basis = size_book(book_p, book, held, nav, prices, {})
    assert intents == [] and "buy-and-hold" in basis


def test_size_book_uses_entry_weights_for_a_fresh_book(tmp_path):
    book = pd.DataFrame({"ticker": ["A", "B"], "weight_equal": 0.5})
    p = tmp_path / "incumbent_book.csv"
    book.to_csv(p, index=False)
    flat = pd.DataFrame(columns=["ticker", "position"])
    intents, basis = size_book(p, book, flat, 10_000.0,
                               {"A": {"price": 10.0}, "B": {"price": 20.0}}, {})
    assert "entry weights" in basis
    assert {i.ticker: i.quantity * (10 if i.ticker == "A" else 20)
            for i in intents} == pytest.approx({"A": 4970, "B": 4960}, abs=20)
