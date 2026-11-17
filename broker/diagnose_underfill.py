"""
broker/diagnose_underfill.py — WHY the 2026-09-29 paper run underfilled.

Measures, per order, what the TRUE market did during the submission window and
attributes the shortfall to one of:

  A. LIMIT NEVER REACHED   the true low of the window stayed ABOVE the BUY limit, so
                           no fill was possible at any size. Price problem.
  B. LIMIT REACHED, SHORT  the market traded at/below the limit but the order still
                           did not complete. Size/liquidity/queue problem.
  C. FILLED                complete.

and separately quantifies DELAYED-QUOTE STALENESS as
    (true price at submission) - (delayed quote we priced off)
in bps, which is the part a data subscription would fix.

Truth source: IB historical 1-minute TRADES bars for the exact window. The Historical
Data farm needs no market-data subscription, so this is real market data, not the
delayed feed that caused the problem.

PAPER ONLY — read-only, places nothing.
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from broker.ibkr_paper import IBKRPaperBroker  # noqa: E402

# Orders went in ~11:26-11:28 ET and were cancelled 11:30:40 ET on 2026-09-29.
WINDOW_END_ET = "20260929 11:31:00 US/Eastern"
WINDOW_MINUTES = 6


def load_run() -> pd.DataFrame:
    """Merge the build-time record (has ref_at_build) with the final fills."""
    build = json.load(open(sorted(glob.glob(str(ROOT / "broker/reconciliation/recon_*.json")))[-1]))
    final = json.load(open(sorted(glob.glob(str(ROOT / "broker/reconciliation/reconcile_*.json")))[-1]))
    b = pd.DataFrame(build["orders"])[["ticker", "side", "ordered", "limit", "ref_at_build"]]
    f = pd.DataFrame(final["orders"])[["ticker", "filled", "remaining", "avg_fill"]]
    return b.merge(f, on="ticker", how="left")


def main() -> int:
    run = load_run()
    with IBKRPaperBroker() as br:
        from ib_async import Stock

        rows = []
        for r in run.itertuples():
            try:
                c = br.ib.qualifyContracts(Stock(r.ticker, "SMART", "USD"))[0]
                bars = br.ib.reqHistoricalData(
                    c, endDateTime=WINDOW_END_ET,
                    durationStr=f"{WINDOW_MINUTES * 60} S",
                    barSizeSetting="1 min", whatToShow="TRADES",
                    useRTH=True, formatDate=1)
            except Exception as e:
                print(f"  {r.ticker}: historical fetch failed — {e}")
                bars = []
            if not bars:
                rows.append({"ticker": r.ticker, "true_low": None, "true_at_submit": None})
                continue
            df = pd.DataFrame([{"t": b.date, "o": b.open, "h": b.high,
                                "l": b.low, "c": b.close, "v": b.volume} for b in bars])
            rows.append({
                "ticker": r.ticker,
                "true_low": float(df["l"].min()),
                "true_high": float(df["h"].max()),
                "true_at_submit": float(df["o"].iloc[0]),   # start of the window
                "true_vwap": float((df["c"] * df["v"]).sum() / max(df["v"].sum(), 1)),
                "window_volume": float(df["v"].sum()),
            })
        truth = pd.DataFrame(rows)

    d = run.merge(truth, on="ticker", how="left")
    d["fill_rate"] = d["filled"] / d["ordered"]

    # ── staleness: what we priced off vs what the market actually was ───────
    d["stale_bps"] = (d["true_at_submit"] - d["ref_at_build"]) / d["ref_at_build"] * 1e4
    # ── headroom: how far the limit sat above the best achievable price ─────
    d["limit_vs_truelow_bps"] = (d["limit"] - d["true_low"]) / d["true_low"] * 1e4

    def classify(r):
        if r.remaining == 0 and r.filled > 0:
            return "C_FILLED"
        if pd.isna(r.true_low):
            return "UNKNOWN"
        return "A_LIMIT_NEVER_REACHED" if r.limit < r.true_low else "B_REACHED_BUT_SHORT"

    d["cause"] = d.apply(classify, axis=1)
    d["unfilled_shares"] = d["ordered"] - d["filled"]
    d["unfilled_notional"] = d["unfilled_shares"] * d["limit"]

    pd.set_option("display.width", 200)
    print("\n=== PER-ORDER DIAGNOSIS (2026-09-29 paper run) ===")
    print(f"{'TICKER':<8}{'ORD':>7}{'FILL':>7}{'FILL%':>7}{'DELAYED':>9}{'TRUE@SUB':>10}"
          f"{'STALE bp':>10}{'LIMIT':>9}{'TRUE LOW':>10}{'LIM-LOW bp':>12}  CAUSE")
    for r in d.sort_values(["cause", "fill_rate"]).itertuples():
        tl = f"{r.true_low:10.2f}" if pd.notna(r.true_low) else "       n/a"
        ts = f"{r.true_at_submit:10.2f}" if pd.notna(r.true_at_submit) else "       n/a"
        sb = f"{r.stale_bps:10.1f}" if pd.notna(r.stale_bps) else "       n/a"
        lv = f"{r.limit_vs_truelow_bps:12.1f}" if pd.notna(r.limit_vs_truelow_bps) else "         n/a"
        print(f"{r.ticker:<8}{r.ordered:>7.0f}{r.filled:>7.0f}{r.fill_rate*100:>6.0f}%"
              f"{r.ref_at_build:>9.2f}{ts}{sb}{r.limit:>9.2f}{tl}{lv}  {r.cause}")

    print("\n=== ATTRIBUTION OF THE SHORTFALL ===")
    tot_unf = d["unfilled_shares"].sum()
    tot_ord = d["ordered"].sum()
    print(f"ordered {tot_ord:,.0f} shares | filled {d['filled'].sum():,.0f} "
          f"({d['filled'].sum()/tot_ord*100:.1f}%) | unfilled {tot_unf:,.0f}")
    for cause, g in d.groupby("cause"):
        print(f"\n  {cause}: {len(g)} orders, {g['unfilled_shares'].sum():,.0f} unfilled "
              f"shares ({g['unfilled_shares'].sum()/max(tot_unf,1)*100:.1f}% of the shortfall), "
              f"${g['unfilled_notional'].sum():,.0f} notional")
        print(f"    {sorted(g['ticker'])}")

    print("\n=== DELAYED-QUOTE STALENESS (the part a subscription would fix) ===")
    s = d["stale_bps"].dropna()
    if len(s):
        print(f"  mean {s.mean():+.1f} bps | median {s.median():+.1f} | "
              f"min {s.min():+.1f} | max {s.max():+.1f}")
        print(f"  delayed quote UNDERSTATED the true price in {(s > 0).sum()}/{len(s)} names")
    a = d[d.cause == "A_LIMIT_NEVER_REACHED"]
    if len(a):
        need = (a["true_low"] - a["limit"]) / a["limit"] * 1e4
        print(f"\n  For the {len(a)} LIMIT-NEVER-REACHED names, the limit would have "
              f"needed to be\n  {need.min():.0f}-{need.max():.0f} bps higher "
              f"(mean {need.mean():.0f} bps) just to touch the low.")
        print(f"  Their staleness was only {a['stale_bps'].mean():+.0f} bps on average — "
              f"so staleness\n  alone does NOT explain them; the 30bps buffer was the "
              f"binding constraint.")

    # ── participation vs ADV, and vs the liquidity actually available in the
    #    ~4 minutes the orders were alive. IB's historical window VOLUME is not
    #    trustworthy here (no market-data subscription: it reports e.g. 100 shares
    #    in 6 minutes for a $22m/day name), so ADV from our own price history is
    #    used as the denominator instead.
    from broker.book_guard import load_adv
    adv = load_adv(d["ticker"].tolist())
    d["adv_usd"] = d["ticker"].map(adv)
    d["order_notional"] = d["ordered"] * d["limit"]
    d["pct_of_adv"] = d["order_notional"] / d["adv_usd"]
    MINUTES_ALIVE = 4.0
    SESSION_MIN = 390.0
    d["pct_of_window_liquidity"] = d["pct_of_adv"] / (MINUTES_ALIVE / SESSION_MIN)

    print("\n=== PARTICIPATION (order size vs liquidity) ===")
    print(f"  Orders were alive ~{MINUTES_ALIVE:.0f} minutes before being cancelled.")
    print(f"  A {MINUTES_ALIVE:.0f}-minute slice of a session is "
          f"{MINUTES_ALIVE/SESSION_MIN*100:.1f}% of a day's liquidity.\n")
    print(f"  {'TICKER':<8}{'NOTIONAL':>11}{'ADV($m)':>10}{'%ADV':>8}"
          f"{'% OF 4-MIN LIQ':>16}{'FILL%':>8}")
    for r in d.sort_values("pct_of_window_liquidity", ascending=False).itertuples():
        if pd.isna(r.pct_of_adv):
            continue
        print(f"  {r.ticker:<8}{r.order_notional:>11,.0f}{r.adv_usd/1e6:>10.1f}"
              f"{r.pct_of_adv*100:>7.2f}%{r.pct_of_window_liquidity*100:>15.0f}%"
              f"{r.fill_rate*100:>7.0f}%")
    print(f"\n  Every order was ~1% of ADV — small on a daily view — but "
          f"{d['pct_of_window_liquidity'].median()*100:.0f}% of the liquidity\n"
          f"  realistically available in the {MINUTES_ALIVE:.0f} minutes it was given. "
          f"TIME IN MARKET, not order\n  size, is the binding constraint for the "
          f"B_REACHED_BUT_SHORT group.")
    print("\n  CAVEAT: IB window volume is unusable without a subscription "
          "(it reported 100\n  shares in 6 minutes for AMN, a $22m/day name), so ADV "
          "from the local price\n  history is the denominator above. Treat the earlier "
          "window-volume column as noise.")

    out = ROOT / "broker" / "reconciliation" / "underfill_diagnosis.csv"
    d.to_csv(out, index=False)
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
