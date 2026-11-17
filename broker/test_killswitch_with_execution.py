"""
broker/test_killswitch_with_execution.py — kill-switches STILL block when the
execution engine (slicing + repricing) is active.

The concern this answers: slicing and repricing create NEW orders after the initial
authorization, so they are exactly where a gate could be bypassed. It cannot be here,
because ExecutionEngine never calls ib.placeOrder — every wave and every reprice goes
through IBKRPaperBroker.submit_orders(), which runs the gate internally.

Proof method, as before: ib.placeOrder is monkeypatched with a recorder that RAISES
if reached. A test passes only if the order was blocked AND the recorder is empty.
dry_run=False is used deliberately so the real submission path executes — the gate is
what stops it, not dry-run mode.

PAPER ONLY. Connects to 4002; places nothing.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from loguru import logger                                                 # noqa: E402
from broker.execution import ExecutionEngine, daily_sigma_bps, plan_slices  # noqa: E402
from broker.ibkr_paper import IBKRPaperBroker                        # noqa: E402
from broker.killswitch import (                                              # noqa: E402
    MAX_POSITION_WEIGHT, OrderGate, OrderIntent, PortfolioState, Switch,
)

SCRATCH = ROOT / "broker" / "_test_tmp"
# Deliberate trips write to a scratch audit log. They used to append to the REAL
# broker/killswitch_audit.log, so a proof run left HALT/BLOCK lines (e.g. 'SUNS
# BUY: ADV $900,000/day') in the record you read after a genuine halt.
TEST_AUDIT_LOG = SCRATCH / "audit.log"

# Fallback NAV denominator when the account's own USD-equivalent NAV cannot be
# read (market-data outage). Round number, close to the account's ~$1m, so the
# weights in the trips below stay the ones the trips were written to test.
SYNTHETIC_NAV = 1_000_000.0


class PlaceOrderRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, contract, order):
        self.calls.append((contract.symbol, order.action, order.totalQuantity))
        raise AssertionError(
            f"KILL-SWITCH LEAK via execution engine: placeOrder() reached for "
            f"{contract.symbol} {order.action} {order.totalQuantity}")


def fresh_snapshot() -> Path:
    """A snapshot dated to the most recent trading day, so the 5.3 staleness switch
    PASSES. Without this it fires first and masks whichever switch is under test --
    portfolio-wide halts are evaluated before per-intent limits by design."""
    from broker.killswitch import _most_recent_trading_day
    d = _most_recent_trading_day(date.today())
    p = SCRATCH / "fresh.csv"
    p.write_text("date,ticker,close,is_carried\n"
                 + f"{d.isoformat()},AAA,10.0,0\n", encoding="utf-8")
    return p


def banner(n, t):
    print(f"\n{'='*76}\nTRIP {n} (execution engine ACTIVE): {t}\n{'='*76}")


FAST = {**__import__("config.settings", fromlist=["EXECUTION"]).EXECUTION,
        "reprice_interval_s": 2, "reprice_max_attempts": 1, "slice_interval_s": 0}


def run() -> int:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    failures = []

    with IBKRPaperBroker() as br:
        # NAV here is only a denominator for the weight/drawdown arithmetic, so it
        # must NOT depend on a live FX quote. Deriving it from account_summary()
        # made this proof unrunnable during a market-data outage (IB 10197/162 on
        # EURUSD), which is exactly backwards: a safety proof has to be runnable at
        # any hour, with or without a data line. Real NAV when available, a fixed
        # synthetic NAV otherwise, and the run says which it used.
        try:
            nav = br.account_summary()["_nav_usd"]
            nav_src = "account (USD-equivalent)"
        except Exception as e:
            nav = SYNTHETIC_NAV
            nav_src = f"SYNTHETIC (account NAV unavailable: {type(e).__name__})"
            logger.warning(f"using synthetic NAV ${nav:,.0f} — {e}")
        print(f"connected {br.account} (PAPER)  NAV ${nav:,.2f}  [{nav_src}]")
        rec = PlaceOrderRecorder()
        br.ib.placeOrder = rec

        # Thin name + big order => genuinely sliced, so we test the multi-wave path.
        thin = OrderIntent("KODK", "BUY", 7900, 9.57, sector="Industrials",
                           adv_usd=6.5e6, target_weight=0.0667)
        prices = {"KODK": {"price": 9.55}, "MOV": {"price": 33.5}}
        adv = {"KODK": 6.5e6, "MOV": 6.7e6}
        sigma = daily_sigma_bps(["KODK", "MOV"])
        n_sl = plan_slices(7900 * 9.55, 6.5e6)
        print(f"slicing check: KODK ${7900*9.55:,.0f} vs ADV $6.5m -> {n_sl} children "
              f"(multi-wave path {'ACTIVE' if n_sl > 1 else 'NOT active'})")

        # ── TRIP 1: stale snapshot halts EVERY wave ────────────────────────
        banner(1, "STALE SNAPSHOT (5.3) — must halt the sliced order")
        stale = SCRATCH / "stale.csv"
        stale.write_text("date,ticker,close,is_carried\n2026-09-01,AAA,10.0,0\n",
                         encoding="utf-8")
        br.gate = OrderGate(snapshot_csv=stale, today=date(2026, 9, 29), audit_log=TEST_AUDIT_LOG)
        eng = ExecutionEngine(br, cfg=FAST, dry_run=False)
        eng.plan([thin], prices, adv, sigma)
        summary = eng.run(PortfolioState(equity=nav, high_water_mark=nav, positions={}),
                          nav, wave_pause_s=0)
        ok = (summary["halted"] is not None
              and summary["halted"]["switch"] == Switch.STALE_DATA.value
              and not rec.calls
              and all(c["status"] == "HALTED" for c in summary["children"]))
        print(f"  children planned : {len(summary['children'])}")
        print(f"  halted           : {summary['halted']}")
        print(f"  placeOrder()     : reached {len(rec.calls)} times  <- must be 0")
        print(f"  child statuses   : {sorted({c['status'] for c in summary['children']})}")
        print(f"  RESULT           : {'PASS' if ok else 'FAIL'}")
        failures += [] if ok else ["5.3 stale (sliced)"]

        # ── TRIP 2: oversized position blocked on every child ──────────────
        banner(2, f"OVERSIZED POSITION (4.x) — {MAX_POSITION_WEIGHT:.0%} cap, sliced")
        rec.calls.clear()
        br.gate = OrderGate(snapshot_csv=fresh_snapshot(), audit_log=TEST_AUDIT_LOG)
        # Each CHILD carries target_weight = parent/n, so make the parent large
        # enough that even a child breaches the cap.
        # 50% parent. With the fix, EVERY child carries 0.50 (the resulting position
        # weight), so slicing cannot smuggle it past the 15% cap.
        huge = OrderIntent("KODK", "BUY", 60000, 9.57, sector="Industrials",
                           adv_usd=6.5e6, target_weight=0.50)
        eng2 = ExecutionEngine(br, cfg=FAST, dry_run=False)
        kids = eng2.plan([huge], prices, adv, sigma)
        s2 = eng2.run(PortfolioState(equity=nav, high_water_mark=nav, positions={}),
                      nav, wave_pause_s=0)
        statuses = {c["status"] for c in s2["children"]}
        ok2 = (not rec.calls and "BLOCKED" in statuses)
        print(f"  children planned : {len(kids)}; each carries the PARENT weight "
              f"{kids[0].intent.target_weight:.0%} vs the {MAX_POSITION_WEIGHT:.0%} cap")
        print(f"  child statuses   : {sorted(statuses)}")
        print(f"  placeOrder()     : reached {len(rec.calls)} times  <- must be 0")
        print(f"  RESULT           : {'PASS' if ok2 else 'FAIL'}")
        failures += [] if ok2 else ["4.x oversized (sliced)"]

        # ── TRIP 3: drawdown halt during a sliced order ────────────────────
        banner(3, "MAX DRAWDOWN (5.1) — halts the sliced order")
        rec.calls.clear()
        br.gate = OrderGate(snapshot_csv=fresh_snapshot(), audit_log=TEST_AUDIT_LOG)
        drawn = PortfolioState(equity=nav * 0.84, high_water_mark=nav, positions={})
        eng3 = ExecutionEngine(br, cfg=FAST, dry_run=False)
        eng3.plan([thin], prices, adv, sigma)
        s3 = eng3.run(drawn, nav, wave_pause_s=0)
        ok3 = (s3["halted"] and s3["halted"]["switch"] == Switch.MAX_DRAWDOWN.value
               and not rec.calls)
        print(f"  drawdown         : {drawn.drawdown:.2%}")
        print(f"  halted           : {s3['halted']['switch'] if s3['halted'] else None}")
        print(f"  placeOrder()     : reached {len(rec.calls)} times  <- must be 0")
        print(f"  RESULT           : {'PASS' if ok3 else 'FAIL'}")
        failures += [] if ok3 else ["5.1 drawdown (sliced)"]

        # ── TRIP 4: illiquid name blocked before any slice is sent ─────────
        banner(4, "LIQUIDITY FLOOR (4.x) — $0.9m/day ADV, sliced")
        rec.calls.clear()
        br.gate = OrderGate(snapshot_csv=fresh_snapshot(), audit_log=TEST_AUDIT_LOG)
        illq = OrderIntent("SUNS", "BUY", 9000, 8.00, sector="Real Estate",
                           adv_usd=900_000, target_weight=0.0667)
        eng4 = ExecutionEngine(br, cfg=FAST, dry_run=False)
        eng4.plan([illq], {"SUNS": {"price": 8.0}}, {"SUNS": 900_000},
                  daily_sigma_bps(["SUNS"]))
        s4 = eng4.run(PortfolioState(equity=nav, high_water_mark=nav, positions={}),
                      nav, wave_pause_s=0)
        st4 = {c["status"] for c in s4["children"]}
        ok4 = (not rec.calls and "BLOCKED" in st4)
        print(f"  children planned : {len(s4['children'])}")
        print(f"  child statuses   : {sorted(st4)}")
        print(f"  placeOrder()     : reached {len(rec.calls)} times  <- must be 0")
        print(f"  RESULT           : {'PASS' if ok4 else 'FAIL'}")
        failures += [] if ok4 else ["4.x liquidity (sliced)"]

    print(f"\n{'='*76}")
    if failures:
        print(f"FAILURES: {failures}")
        return 1
    print("ALL 4 PASS — slicing and repricing route every child through the gate;\n"
          "placeOrder() was never reached.")
    print("=" * 76)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
