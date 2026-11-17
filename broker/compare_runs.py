"""
broker/compare_runs.py — before/after execution quality, straight from the store.

Compares the 2026-09-29 baseline (flat 30bps buffer, no slicing, no repricing,
cancelled after ~4 minutes) against the newest execution-engine run.

  python -m broker.compare_runs

Reports fill rate, % deployed, slippage, reprices and slices, and states plainly
whether execution improved, was a wash, or got worse. A wash is reported as a wash.

PAPER ONLY — reads broker/execution_log/, connects to nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path



ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import broker.execution_store as xs  # noqa: E402

BASELINE_ID = "20260929_182912_baseline"
# The stated baseline, for a cross-check that the store is not drifting.
BASELINE_EXPECTED = {"fully_filled": 9, "orders": 15, "share_fill_rate": 0.821,
                     "mean_slippage_bps": -21.6}


def main() -> int:
    runs = xs.run_summary()
    if runs.empty:
        print("no runs in broker/execution_log/ — run a paper cycle first.")
        return 1

    base = runs[runs.run_id == BASELINE_ID]
    if base.empty:
        print(f"baseline {BASELINE_ID} not found. Import it with:\n"
              f"  python -m broker.execution_store")
        return 1
    base = base.iloc[0]

    # The baseline is the yardstick for every "did execution improve?" claim, so
    # verify the store still reproduces the figures measured on the day. If this
    # drifts, the comparison below is meaningless and must not be trusted.
    drift = [f"{k}: store {float(base[k]):.3f} vs measured {v}"
             for k, v in BASELINE_EXPECTED.items()
             if abs(float(base[k]) - v) > (0.05 if isinstance(v, float) else 0)]
    if drift:
        print("BASELINE DRIFT — refusing to compare against a baseline that no "
              "longer\nmatches what was measured on 2026-09-29:")
        for d in drift:
            print(f"  {d}")
        return 1
    print(f"baseline cross-check OK: {int(base.fully_filled)}/{int(base.orders)} "
          f"filled, {base.share_fill_rate*100:.1f}% of shares, "
          f"{base.mean_slippage_bps:+.1f} bps — matches the measured run.\n")

    engine = runs[(runs.engine == "execution_engine") & (~runs.dry_run)]
    if engine.empty:
        print("=== BASELINE (2026-09-29) ===")
        _show(base)
        print("\n=== NO EXECUTION-ENGINE RUN YET ===")
        print("  The improved execution has NOT been validated on paper.")
        print("  It requires a running Gateway AND an open US session")
        print("  (16:30-23:00 Finland / 09:30-16:00 ET), because orders are")
        print("  outsideRth=False and simply do not fill when the market is shut.")
        print("\n  Run:")
        from config.settings import ACTIVE_FT_PERIOD
        print(f"    python -m broker.ibkr_paper --book forward_test/{ACTIVE_FT_PERIOD}/"
              "incumbent_book.csv \\\n        --execution-engine --paper-execute")
        print("    python -m broker.compare_runs")
        return 0

    new = engine.sort_values("timestamp").iloc[-1]
    print("=== EXECUTION QUALITY: BASELINE vs EXECUTION ENGINE ===\n")
    rows = [
        ("run", base.run_id, new.run_id),
        ("engine", base.engine, new.engine),
        ("orders", f"{int(base.orders)}", f"{int(new.orders)}"),
        ("fully filled", f"{int(base.fully_filled)}/{int(base.orders)}",
         f"{int(new.fully_filled)}/{int(new.orders)}"),
        ("partial", f"{int(base.partial)}", f"{int(new.partial)}"),
        ("unfilled", f"{int(base.unfilled)}", f"{int(new.unfilled)}"),
        ("shares filled", f"{base.share_fill_rate*100:.1f}%",
         f"{new.share_fill_rate*100:.1f}%"),
        ("book deployed",
         f"{base.filled_notional/base.nav_usd*100:.2f}%" if base.nav_usd else "n/a",
         f"{new.filled_notional/new.nav_usd*100:.2f}%" if new.nav_usd else "n/a"),
        ("mean slippage (bps)", f"{base.mean_slippage_bps:+.1f}",
         f"{new.mean_slippage_bps:+.1f}"),
        ("reprices", f"{int(base.reprices)}", f"{int(new.reprices)}"),
        ("child orders", f"{int(base.children)}", f"{int(new.children)}"),
    ]
    print(f"  {'METRIC':<22}{'BASELINE':>16}{'ENGINE':>16}")
    for k, a, b in rows:
        print(f"  {k:<22}{str(a):>16}{str(b):>16}")

    d_fill = (new.share_fill_rate - base.share_fill_rate) * 100
    d_dep = ((new.filled_notional / new.nav_usd - base.filled_notional / base.nav_usd) * 100
             if (new.nav_usd and base.nav_usd) else float("nan"))
    d_slip = new.mean_slippage_bps - base.mean_slippage_bps
    print(f"\n  delta shares filled : {d_fill:+.1f} pp")
    print(f"  delta deployed      : {d_dep:+.2f} pp")
    print(f"  delta slippage      : {d_slip:+.1f} bps "
          f"({'worse' if d_slip > 0 else 'better'} vs limit)")

    # Verdict thresholds stated up front so the call is not made post hoc.
    MATERIAL_PP = 5.0
    print("\n=== VERDICT ===")
    if d_fill >= MATERIAL_PP:
        print(f"  IMPROVED — share fill rate up {d_fill:+.1f} pp "
              f"(threshold {MATERIAL_PP:+.0f} pp).")
    elif d_fill <= -MATERIAL_PP:
        print(f"  WORSE — share fill rate down {d_fill:+.1f} pp. The changes did not "
              f"help; do not keep them on this evidence.")
    else:
        print(f"  A WASH — share fill rate moved only {d_fill:+.1f} pp, inside the "
              f"{MATERIAL_PP:.0f} pp materiality band.\n"
              f"  This is NOT an improvement and must not be presented as one.")
    print("\n  CAVEAT: one run each, different order sets, and IB's paper engine "
          "simulates\n  fills rather than queueing in a real book. Directional only.")

    # Per-name detail on the three that failed at baseline.
    t = xs.load_trades()
    watch = ["MOV", "NUTX", "SENEA"]
    b_t = t[(t.run_id == base.run_id) & (t.ticker.isin(watch))]
    n_t = t[(t.run_id == new.run_id) & (t.ticker.isin(watch))]
    if not b_t.empty and not n_t.empty:
        print("\n=== THE THREE THAT FAILED AT BASELINE (0% filled) ===")
        print(f"  {'TICKER':<8}{'BASE FILL%':>12}{'ENGINE FILL%':>14}{'RPX':>5}{'SLICES':>8}")
        for tk in watch:
            b = b_t[b_t.ticker == tk]
            n = n_t[n_t.ticker == tk]
            if b.empty or n.empty:
                continue
            print(f"  {tk:<8}{b.iloc[0].fill_rate*100:>11.0f}%"
                  f"{n.iloc[0].fill_rate*100:>13.0f}%"
                  f"{int(n.iloc[0].reprices):>5}{int(n.iloc[0].children):>8}")
    return 0


def _show(r):
    print(f"  orders {int(r.orders)} | filled {int(r.fully_filled)} | "
          f"partial {int(r.partial)} | unfilled {int(r.unfilled)}")
    print(f"  shares {r.share_fill_rate*100:.1f}% | deployed "
          f"{r.filled_notional/r.nav_usd*100:.2f}% | slippage "
          f"{r.mean_slippage_bps:+.1f} bps")


if __name__ == "__main__":
    raise SystemExit(main())
