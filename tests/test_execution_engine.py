"""
tests/test_execution_engine.py

The execution engine against the three defects seen in the first live run on
DU1234567 (2026-10-01 17:15). Offline: a fake broker runs the REAL OrderGate and
returns scripted fills, so nothing connects to IB and nothing is placed.

  1. Waves after the first were judged by the run-START portfolio, so on a fresh
     build from flat wave 2 (5 names) and late reprices (2 names) were blocked by
     the >=10-names check as if they were the whole book.
  2. After a reprice, intent.quantity holds only the remainder, but fills are
     cumulative: a partly filled child was marked FILLED as soon as its fills
     exceeded the remainder, and "ordered" was reported as that remainder
     (AMN: ordered 32, remaining -2089).
  3. The replacement was sized BEFORE the cancel landed, so fills arriving in
     between were ordered again (GPRE: 5 shares over plan).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from broker.execution import ExecutionEngine  # noqa: E402
from broker.killswitch import OrderGate, OrderIntent, PortfolioState  # noqa: E402

NAV = 1_131_760.0
CFG = {"quote_delay_minutes": 15, "buffer_vol_mult": 1.0, "buffer_min_bps": 15,
       "buffer_max_bps": 75, "fallback_sigma_daily_bps": 250,
       "reprice_interval_s": 0, "reprice_step_bps": 20, "reprice_max_attempts": 3,
       "reprice_total_drift_cap_bps": 60, "slice_max_participation": 0.20,
       "exec_horizon_minutes": 20, "slice_max_children": 6, "slice_interval_s": 0}


# ── fakes ───────────────────────────────────────────────────────────────────────

class _Exec:
    def __init__(self, shares, price):
        self.shares, self.price = shares, price


class _Fill:
    def __init__(self, shares, price):
        self.execution = _Exec(shares, price)


class FakeTrade:
    def __init__(self, intent, fill_now: int, fill_on_cancel: int = 0):
        self.order = intent
        self.qty = intent.quantity
        self.fills = [_Fill(fill_now, intent.limit_price)] if fill_now else []
        self.on_cancel = fill_on_cancel
        self.cancelled = False

    def filled(self):
        return sum(f.execution.shares for f in self.fills)

    def isDone(self):
        return self.cancelled or self.filled() >= self.qty


class FakeIB:
    def __init__(self):
        self.trades = []

    def sleep(self, _s):
        pass

    def waitOnUpdate(self, timeout=1.0):
        pass

    def cancelOrder(self, order):
        for t in self.trades:
            if t.order is order and not t.cancelled:
                if t.on_cancel:                        # fills that race the cancel
                    t.fills.append(_Fill(t.on_cancel, t.order.limit_price))
                t.cancelled = True


class FakeBroker:
    """submit_orders() with the real gate and scripted fills.

    script[ticker] = list of (fill_now, fill_on_cancel) per successive order placed
    for that ticker. Missing entries fill nothing.
    """

    def __init__(self, script: dict | None = None):
        self.gate = OrderGate(None, audit_log=None)
        self.ib = FakeIB()
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.placed_qty: dict = {}

    def submit_orders(self, intents, pf, execute=False, book_value=None):
        d = self.gate.authorize(intents, pf, book_value=book_value)
        res = {"halted": False, "approved": d.approved,
               "blocked": [(i, s.value, r) for i, s, r in d.blocked], "placed": []}
        if execute:
            for i in d.approved:
                now, late = (self.script.get(i.ticker) or [(0, 0)]).pop(0) \
                    if self.script.get(i.ticker) else (0, 0)
                t = FakeTrade(i, now, late)
                self.ib.trades.append(t)
                res["placed"].append(t)
                self.placed_qty.setdefault(i.ticker, []).append(i.quantity)
        return res


def _intent(t, q, px=20.0, side="BUY"):
    return OrderIntent(t, side, q, px, sector=t, adv_usd=50e6,
                       target_weight=0.0667 if side == "BUY" else None)


def _engine(br, intents, adv=None, dry_run=True):
    eng = ExecutionEngine(br, cfg=CFG, dry_run=dry_run)
    prices = {i.ticker: {"price": i.limit_price} for i in intents}
    adv = adv or {i.ticker: 50e6 for i in intents}
    eng.plan(intents, prices, adv, sigma={})
    return eng


def _flat():
    return PortfolioState(equity=NAV, high_water_mark=NAV, positions={})


# ── 1. later waves are judged against the projected book ────────────────────────

FIFTEEN = ["SM", "NUTX", "AMN", "BVS", "TALO", "TK", "MOV", "REPX", "MYE", "SSRM",
           "CCSI", "CDE", "GPRE", "SENEA", "KODK"]
THIN = {"TK", "MOV", "REPX", "CCSI", "KODK"}         # sliced into 2 on 2026-10-01


def test_fresh_build_second_wave_is_not_blocked():
    intents = [_intent(t, 3700, 20.0) for t in FIFTEEN]
    adv = {t: (6.5e6 if t in THIN else 50e6) for t in FIFTEEN}
    eng = _engine(FakeBroker(), intents, adv=adv)
    assert {c.parent for c in eng.children if c.seq == 2} == THIN
    s = eng.run(_flat(), NAV)
    blocked = [c for c in s["children"] if c["status"] == "BLOCKED"]
    assert not blocked, [(c["ticker"], c["seq"]) for c in blocked]


def test_projection_does_not_leak_into_the_callers_portfolio_state():
    pf = _flat()
    _engine(FakeBroker(), [_intent(t, 100) for t in FIFTEEN]).run(pf, NAV)
    assert pf.positions == {}


def test_a_genuinely_small_book_is_still_blocked():
    s = _engine(FakeBroker(), [_intent(t, 100) for t in ("A", "B", "C")]).run(_flat(), NAV)
    assert all(c["status"] == "BLOCKED" for c in s["children"])


# ── 2. fill accounting is against the PLANNED quantity ──────────────────────────

def _live_book(script):
    """AMN plus 9 other names (so the >=10 check is satisfied), executed for real."""
    others = [_intent(t, 100) for t in FIFTEEN if t != "AMN"][:9]
    br = FakeBroker({**script, **{i.ticker: [(100, 0)] for i in others}})
    eng = _engine(br, [_intent("AMN", 2121, 35.49)] + others, dry_run=False)
    return br, eng, eng.run(_flat(), NAV)


def test_partial_then_repriced_child_is_not_marked_filled_early():
    # order 1 fills 2089 of 2121; reprice #1 (32) fills nothing; reprice #2 fills 32
    br, _eng, s = _live_book({"AMN": [(2089, 0), (0, 0), (32, 0)]})
    amn = next(c for c in s["children"] if c["ticker"] == "AMN")
    assert br.placed_qty["AMN"] == [2121, 32, 32]
    assert amn["filled"] == 2121 and amn["status"] == "FILLED"


def test_summary_reports_planned_quantity_not_the_last_remainder():
    _br, _eng, s = _live_book({"AMN": [(2089, 0), (32, 0)]})
    amn = next(c for c in s["children"] if c["ticker"] == "AMN")
    assert amn["ordered"] == 2121 and amn["working_qty"] == 32
    assert amn["ref_price"] == pytest.approx(35.49)


def test_unfilled_child_gives_up_after_max_reprices_without_shrinking_to_one_share():
    br, _eng, s = _live_book({"AMN": [(1000, 0), (0, 0), (0, 0), (0, 0)]})
    # 2 or 3 reprices depending on how rounding meets the 60 bps drift cap; every
    # replacement must be the true remainder (the old code sent 1 share after #1)
    replacements = br.placed_qty["AMN"][1:]
    assert len(replacements) >= 2 and all(q == 1121 for q in replacements)
    amn = next(c for c in s["children"] if c["ticker"] == "AMN")
    assert amn["status"] in ("GAVE_UP", "DRIFT_CAPPED") and amn["filled"] == 1000


# ── 3. the replacement is sized after the cancel lands ──────────────────────────

def test_fills_racing_the_cancel_are_not_ordered_again():
    # GPRE-like: 4839 fill, 5 more land while cancelling, then the rest fills
    br, _eng, s = _live_book({"AMN": [(1800, 5), (316, 0)]})
    assert br.placed_qty["AMN"][1] == 2121 - 1805           # not 2121 - 1800
    amn = next(c for c in s["children"] if c["ticker"] == "AMN")
    assert amn["filled"] == 2121                             # not 2126


# ── reconciliation reads the planned quantity and the reference price ───────────

def test_reconciliation_uses_planned_quantity_and_costs_vs_reference(tmp_path,
                                                                     monkeypatch):
    import broker.ibkr_paper as bp
    monkeypatch.setattr(bp, "RECON_DIR", tmp_path)
    _br, _eng, s = _live_book({"AMN": [(2089, 0), (32, 0)]})

    class _Br:
        account = "DU_TEST"

        def positions(self):
            return pd.DataFrame(columns=["ticker", "position", "avg_cost"])

    rec = bp.reconcile_children(s, {}, NAV, _Br())
    amn = next(r for r in rec["orders"] if r["ticker"] == "AMN")
    assert amn["ordered"] == 2121 and amn["remaining"] == 0 and amn["fully_filled"]
    assert amn["cost_vs_ref_bps"] is not None and amn["cost_vs_ref_bps"] > 0
