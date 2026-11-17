"""
broker/test_killswitch_in_path.py — KILL-SWITCHES WIRED INTO THE BROKER ORDER PATH.

Phase 3 proved the gate blocks in isolation. This proves it blocks *inside*
`IBKRPaperBroker.submit_orders()` — the only method that can place an order —
against a REAL connection to the paper Gateway.

Method: `ib.placeOrder` is monkeypatched with a recorder that raises if it is ever
reached. A test passes only if (a) the order was blocked/halted AND (b) the recorder
is empty. "Blocked" is therefore proven by the absence of a submission attempt, not
by trusting a return value.

PAPER ONLY. Connects to 4002; places nothing (execute=True is used deliberately so
the submission path is genuinely exercised — the gate is what stops it, not dry-run).
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from broker.ibkr_paper import IBKRPaperBroker, Q4_DIR          # noqa: E402
from broker.killswitch import (                                 # noqa: E402
    MAX_POSITION_WEIGHT, OrderGate, OrderIntent, PortfolioState, Switch,
)

SCRATCH = Path(ROOT) / "broker" / "_test_tmp"
# Deliberate trips write to a scratch audit log. They used to append to the REAL
# broker/killswitch_audit.log, so a proof run left HALT/BLOCK lines (e.g. 'SUNS
# BUY: ADV $900,000/day') in the record you read after a genuine halt.
TEST_AUDIT_LOG = SCRATCH / "audit.log"


class PlaceOrderRecorder:
    """Stands in for ib.placeOrder. Reaching it at all is a FAILURE."""

    def __init__(self):
        self.calls = []

    def __call__(self, contract, order):
        self.calls.append((contract.symbol, order.action, order.totalQuantity))
        raise AssertionError(
            f"KILL-SWITCH LEAK: placeOrder() was reached for "
            f"{contract.symbol} {order.action} {order.totalQuantity}")


def banner(n, title):
    print(f"\n{'='*74}\nTRIP TEST {n}: {title}\n{'='*74}")


def healthy_pf(nav: float) -> PortfolioState:
    return PortfolioState(equity=nav, high_water_mark=nav, positions={})


def run() -> int:
    failures = []
    SCRATCH.mkdir(parents=True, exist_ok=True)

    with IBKRPaperBroker() as br:
        summ = br.account_summary()
        nav = summ["_nav_usd"]
        print(f"connected: {br.account} (PAPER)   NAV ${nav:,.2f} USD")

        rec = PlaceOrderRecorder()
        br.ib.placeOrder = rec          # ← any submission attempt now explodes

        # ── TRIP 1: stale-snapshot halt (5.3) — portfolio-wide ──────────────
        banner(1, "STALE SNAPSHOT (5.3) — portfolio-wide halt")
        stale = SCRATCH / "stale_snapshot.csv"
        stale.write_text("date,ticker,close,is_carried\n2026-09-01,AAA,10.0,0\n",
                         encoding="utf-8")
        br.gate = OrderGate(snapshot_csv=stale, today=date(2026, 9, 29), audit_log=TEST_AUDIT_LOG)
        intents = [OrderIntent("INSW", "BUY", 10, 50.00, sector="Energy",
                               adv_usd=50e6, target_weight=0.01)]
        res = br.submit_orders(intents, healthy_pf(nav), execute=True, book_value=nav)
        ok = (res["halted"] and res["halt_switch"] == Switch.STALE_DATA.value
              and not rec.calls and not res["placed"])
        print(f"  halted      : {res['halted']} ({res['halt_switch']})")
        print(f"  detail      : {res['halt_detail']}")
        print(f"  placeOrder(): reached {len(rec.calls)} times  <- must be 0")
        print(f"  RESULT      : {'PASS — blocked before submit()' if ok else 'FAIL'}")
        if not ok:
            failures.append("5.3 stale snapshot")

        # ── TRIP 2: oversized position (4.x) — per-intent block ────────────
        banner(2, f"OVERSIZED POSITION (4.x) — {MAX_POSITION_WEIGHT:.0%} cap")
        br.gate = OrderGate(snapshot_csv=Q4_DIR / "price_snapshots.csv", audit_log=TEST_AUDIT_LOG)
        big_w = 0.20
        oversized = OrderIntent("INSW", "BUY", int(nav * big_w / 50), 50.00,
                                sector="Energy", adv_usd=50e6, target_weight=big_w)
        normal = OrderIntent("AMN", "BUY", 100, 25.00, sector="Healthcare",
                             adv_usd=40e6, target_weight=0.01)
        res = br.submit_orders([oversized, normal], healthy_pf(nav),
                               execute=True, book_value=nav)
        blocked = {i.ticker for i, _, _ in res["blocked"]}
        leaked = {c[0] for c in rec.calls}
        ok = ("INSW" in blocked and "INSW" not in leaked and not res["halted"])
        print(f"  blocked     : {[(i.ticker, s, r) for i, s, r in res['blocked']]}")
        print(f"  approved    : {[i.ticker for i in res['approved']]}")
        print(f"  placeOrder(): reached for {sorted(leaked) or 'nothing'}")
        print(f"  RESULT      : {'PASS — oversized never reached submit()' if ok else 'FAIL'}")
        if not ok:
            failures.append("4.x oversized position")

        # ── TRIP 3: max-drawdown halt (5.1) — portfolio-wide ───────────────
        banner(3, "MAX DRAWDOWN (5.1) — portfolio-wide halt at -15%")
        rec.calls.clear()
        drawn = PortfolioState(equity=nav * 0.84, high_water_mark=nav, positions={})
        res = br.submit_orders([normal], drawn, execute=True, book_value=nav)
        ok = (res["halted"] and res["halt_switch"] == Switch.MAX_DRAWDOWN.value
              and not rec.calls)
        print(f"  drawdown    : {drawn.drawdown:.2%}")
        print(f"  halted      : {res['halted']} ({res['halt_switch']})")
        print(f"  placeOrder(): reached {len(rec.calls)} times  <- must be 0")
        print(f"  RESULT      : {'PASS — blocked before submit()' if ok else 'FAIL'}")
        if not ok:
            failures.append("5.1 max drawdown")

        # ── TRIP 4: illiquid name (4.x) — the tail-audit case ──────────────
        banner(4, "LIQUIDITY FLOOR (4.x) — $0.9m/day ADV")
        rec.calls.clear()
        illiquid = OrderIntent("SUNS", "BUY", 100, 8.00, sector="Real Estate",
                               adv_usd=900_000, target_weight=0.01)
        res = br.submit_orders([illiquid], healthy_pf(nav), execute=True, book_value=nav)
        ok = (any(i.ticker == "SUNS" for i, _, _ in res["blocked"])
              and not rec.calls)
        print(f"  blocked     : {[(i.ticker, r) for i, s, r in res['blocked']]}")
        print(f"  placeOrder(): reached {len(rec.calls)} times  <- must be 0")
        print(f"  RESULT      : {'PASS — illiquid never reached submit()' if ok else 'FAIL'}")
        if not ok:
            failures.append("4.x liquidity floor")

    print(f"\n{'='*74}")
    if failures:
        print(f"FAILURES: {failures}")
        return 1
    print("ALL 4 IN-PATH TRIP TESTS PASSED — the gate blocks inside the broker's\n"
          "order path, proven by placeOrder() never being reached.")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
