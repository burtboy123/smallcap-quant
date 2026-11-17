"""
tests/test_min_positions_gate.py

The §4 ">= 10 names" shape check must count a TRIM as a trim, not an exit.

On 2026-10-01 the top-up run on DU1234567 (13 names held) produced 9 BUYs and 6
small trim SELLs. The gate subtracted all 6 trimmed names from the resulting book,
computed 13 + 9 - 6 = 9 < 10, and blocked every BUY. These tests reproduce that
exact book and pin the corrected rule. No broker, no audit file (audit_log=None).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from broker.killswitch import OrderGate, OrderIntent, PortfolioState  # noqa: E402

NAV = 1_126_000.0
HELD = {"MOV": 1134, "BVS": 5589, "CCSI": 1066, "CDE": 4288, "SENEA": 423,
        "SSRM": 2268, "TK": 2618, "MYE": 2472, "KODK": 3948, "AMN": 2121,
        "GPRE": 5009, "TALO": 4561, "SM": 2179}            # actual, 2026-10-01 20:18


def _pf(with_qty: bool = True) -> PortfolioState:
    pos = {t: {"entry_price": 20.0, "last_price": 20.0, **({"qty": q} if with_qty else {})}
           for t, q in HELD.items()}
    return PortfolioState(equity=NAV, high_water_mark=NAV, positions=pos)


def _buy(t, q=1000, px=20.0):
    return OrderIntent(t, "BUY", q, px, sector=t, adv_usd=10e6, target_weight=0.0667)


def _sell(t, q):
    return OrderIntent(t, "SELL", q, 20.0)


# the 2026-10-01 20:18 top-up, names and directions as printed by the dry run
TOP_UP = ([_buy(t) for t in ("REPX", "NUTX", "CCSI", "TK", "MOV", "KODK",
                             "SENEA", "GPRE", "TALO")]
          + [_sell("MYE", 46), _sell("BVS", 102), _sell("SSRM", 34),
             _sell("AMN", 30), _sell("SM", 16), _sell("CDE", 4)])


def gate() -> OrderGate:
    return OrderGate(None, audit_log=None)


def test_trims_do_not_count_as_exits():
    d = gate().authorize(TOP_UP, _pf(), book_value=NAV)
    assert not d.blocked, [r for _i, _s, r in d.blocked]
    assert len(d.approved) == len(TOP_UP)


def test_a_full_exit_still_removes_the_name():
    """Selling the whole position IS an exit, so a book shrinking below 10 is blocked."""
    sells = [_sell(t, q) for t, q in list(HELD.items())[:4]]       # 13 - 4 = 9 names
    d = gate().authorize(sells + [_buy("SM")], _pf(), book_value=NAV)
    assert any("below" in r for _i, _s, r in d.blocked)
    assert not [i for i in d.approved if i.action == "BUY"]


def test_unknown_quantity_is_still_treated_as_an_exit():
    """Without qty the gate cannot tell a trim from an exit: stay conservative."""
    d = gate().authorize(TOP_UP, _pf(with_qty=False), book_value=NAV)
    assert any("9 names" in r for _i, _s, r in d.blocked)


def test_a_book_genuinely_below_ten_is_still_blocked():
    d = gate().authorize([_buy(t) for t in ("A", "B", "C")],
                         PortfolioState(equity=NAV, high_water_mark=NAV), book_value=NAV)
    assert not d.approved
