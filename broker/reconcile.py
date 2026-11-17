"""
broker/reconcile.py — post-execution reconciliation against the PAPER account.

Re-reads live state (open orders, fills, positions) and compares it to the target
book: intended vs actual shares, fill rate, slippage vs limit, unfilled/partial, and
resulting position weights vs target weights.

  python -m broker.reconcile --book broker/compliant_book.csv
  python -m broker.reconcile --book broker/compliant_book.csv --cancel-open

--cancel-open cancels any still-working orders so the paper account is left in a
defined state rather than drifting unattended. Cancelling never places anything.

PAPER ONLY.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from broker.ibkr_paper import IBKRPaperBroker, RECON_DIR      # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", default=str(ROOT / "broker" / "compliant_book.csv"))
    ap.add_argument("--cancel-open", action="store_true",
                    help="cancel still-working orders (places nothing)")
    args = ap.parse_args(argv)

    book = pd.read_csv(args.book)

    with IBKRPaperBroker() as br:
        summ = br.account_summary()
        nav_usd = summ["_nav_usd"]
        trades = br.ib.trades()
        open_orders = [t for t in trades if not t.isDone()]

        rows = []
        for t in trades:
            o, c, st = t.order, t.contract, t.orderStatus
            # IB zeroes totalQuantity/filled on a COMPLETED order when trades() is
            # re-read, which made a fully-filled order look unfilled. Execution
            # records (trade.fills) persist, so they are the source of truth.
            fq = sum(float(f.execution.shares) for f in t.fills)
            notional = sum(float(f.execution.shares) * float(f.execution.price)
                           for f in t.fills)
            avg = (notional / fq) if fq else 0.0
            remaining = float(st.remaining or 0)
            ordered = float(o.totalQuantity or 0) or (fq + remaining)
            lim = float(o.lmtPrice)
            slip = (avg - lim) if (avg and o.action == "BUY") else None
            rows.append({
                "ticker": c.symbol, "side": o.action, "ordered": ordered,
                "filled": fq, "remaining": remaining,
                "fill_rate": fq / ordered if ordered else 0.0,
                "limit": lim, "avg_fill": avg or None,
                "slip_bps": round(slip / lim * 1e4, 1) if (slip is not None and lim) else None,
                "status": st.status,
            })
        r = pd.DataFrame(rows)

        if args.cancel_open and open_orders:
            print(f"cancelling {len(open_orders)} working orders...")
            for t in open_orders:
                br.ib.cancelOrder(t.order)
            br.ib.sleep(3)
            print("  cancelled.")

        pos = br.positions()
        print(f"\n=== FILL RECONCILIATION ({br.account}) ===")
        if r.empty:
            print("  (no orders this session)")
        else:
            print(f"  {'TICKER':<8}{'ORD':>7}{'FILL':>7}{'REM':>7}{'FILL%':>7}"
                  f"{'LIMIT':>9}{'AVGFILL':>9}{'SLIP(bp)':>10}  STATUS")
            for _, x in r.sort_values("ticker").iterrows():
                af = f"{x.avg_fill:9.2f}" if x.avg_fill else "      n/a"
                sb = f"{x.slip_bps:10.1f}" if x.slip_bps is not None else "       n/a"
                print(f"  {x.ticker:<8}{x.ordered:>7.0f}{x.filled:>7.0f}{x.remaining:>7.0f}"
                      f"{x.fill_rate*100:>6.0f}%{x.limit:>9.2f}{af}{sb}  {x.status}")
            full = int(((r.remaining == 0) & (r.filled > 0)).sum())
            part = int(((r.filled > 0) & (r.remaining > 0)).sum())
            none_ = int((r.filled == 0).sum())
            print(f"\n  fully filled {full}/{len(r)} | partial {part} | unfilled {none_}")
            print(f"  shares filled: {r.filled.sum():,.0f} / {r.ordered.sum():,.0f} "
                  f"({r.filled.sum()/r.ordered.sum()*100:.1f}%)")
            fl = r[r.slip_bps.notna()]
            if len(fl):
                print(f"  slippage vs limit: mean {fl.slip_bps.mean():+.1f} bps, "
                      f"best {fl.slip_bps.min():+.1f}, worst {fl.slip_bps.max():+.1f} "
                      f"(negative = better than limit)")

        # ── positions vs target ────────────────────────────────────────────
        print(f"\n=== POSITIONS vs TARGET BOOK ===")
        tgt = book.set_index("ticker")["weight_equal"].to_dict()
        held = pos.set_index("ticker")["position"].to_dict() if len(pos) else {}
        px = br.reference_prices(sorted(set(tgt) | set(held)))
        gross = sum(abs(q) * px.get(t, {}).get("price", 0) for t, q in held.items())
        print(f"  {'TICKER':<8}{'SHARES':>9}{'VALUE':>12}{'ACT.W':>8}{'TGT.W':>8}{'DIFF':>8}")
        recs = []
        for t in sorted(set(tgt) | set(held)):
            q = held.get(t, 0.0)
            val = q * px.get(t, {}).get("price", 0)
            aw = val / nav_usd if nav_usd else 0
            tw = tgt.get(t, 0.0)
            recs.append({"ticker": t, "shares": q, "value": val,
                         "actual_w": aw, "target_w": tw, "diff": aw - tw})
            print(f"  {t:<8}{q:>9,.0f}{val:>12,.0f}{aw*100:>7.2f}%{tw*100:>7.2f}%"
                  f"{(aw-tw)*100:>+7.2f}%")
        print(f"  {'TOTAL':<8}{'':>9}{gross:>12,.0f}{gross/nav_usd*100:>7.2f}%"
              f"{sum(tgt.values())*100:>7.2f}%")
        missing = [t for t in tgt if held.get(t, 0) == 0]
        if missing:
            print(f"\n  NOT HELD (target names with zero position): {missing}")

        RECON_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        (RECON_DIR / f"reconcile_{stamp}.json").write_text(json.dumps({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "account": br.account, "nav_usd": nav_usd,
            "base_currency": summ.get("_base_currency"), "fx": summ.get("_fx_to_usd"),
            "orders": rows, "positions": recs,
            "cancelled_open": bool(args.cancel_open and open_orders),
        }, indent=2, default=str), encoding="utf-8")
        print(f"\nwrote {RECON_DIR / f'reconcile_{stamp}.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
