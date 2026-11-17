"""
broker/execution_store.py — durable execution history for the PAPER account.

Owns broker/execution_log/ and the canonical on-disk schema. Everything that needs
execution history (the dashboard, reports) reads THROUGH this module, so nothing has
to talk to IB — history survives Gateway restarts and is readable with Gateway closed.

WHY A STORE AND NOT ib.trades(): re-reading ib.trades() after an order completes
returns totalQuantity/filled = 0 for that order (IB clears the working quantity), so
a completed fill reads as "0 filled". That bug undercounted the 2026-09-29 run as
6 filled / 9 unfilled when the true figure was 9 / 3. Fills here are always derived
from EXECUTION RECORDS, never from a live order's working quantity.

Canonical trade row
-------------------
  run_id, timestamp, account, dry_run, book, nav_usd,
  ticker, side, ordered, filled, remaining, fill_rate,
  limit, original_limit, avg_fill, slippage_bps,
  status, reprices, children, engine, source

PAPER ONLY — this module connects to nothing.
"""
from __future__ import annotations

import glob
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
EXEC_LOG_DIR = ROOT / "broker" / "execution_log"
RECON_DIR = ROOT / "broker" / "reconciliation"
SCHEMA_VERSION = 1


def _slip_bps(avg, lim, side):
    if not avg or not lim:
        return None
    d = (avg - lim) if side == "BUY" else (lim - avg)
    return round(d / lim * 1e4, 1)


def write_run(trades: list, *, account: str, nav_usd: float | None = None,
              dry_run: bool = True, book: str | None = None,
              engine: str = "simple", config: dict | None = None,
              run_id: str | None = None, events: list | None = None,
              timestamp: str | None = None) -> Path:
    """Persist one execution run. `trades` are canonical rows (see module docstring).

    `timestamp` overrides the write time -- required when backfilling a historical run,
    so the dashboard orders runs by WHEN THEY TRADED, not when they were imported.
    """
    EXEC_LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now()
    rid = run_id or ts.strftime("%Y%m%d_%H%M%S")
    rec = {
        "schema_version": SCHEMA_VERSION,
        "run_id": rid,
        "timestamp": timestamp or ts.isoformat(timespec="seconds"),
        "account": account, "nav_usd": nav_usd, "dry_run": dry_run,
        "book": book, "engine": engine, "config": config or {},
        "trades": trades, "events": events or [],
    }
    p = EXEC_LOG_DIR / f"run_{rid}.json"
    p.write_text(json.dumps(rec, indent=2, default=str), encoding="utf-8")
    return p


def from_engine_summary(summary: dict, *, account: str, nav_usd: float,
                        dry_run: bool, book: str | None, config: dict) -> Path:
    """Persist an ExecutionEngine summary (child orders) at PARENT level + events."""
    ch = pd.DataFrame(summary.get("children", []))
    trades, events = [], []
    for c in summary.get("children", []):
        events.extend(c.get("events", []))
    if not ch.empty:
        for tk, g in ch.groupby("ticker"):
            ordered, filled = float(g.ordered.sum()), float(g.filled.sum())
            f = g[g.filled > 0]
            avg = float((f.avg_fill * f.filled).sum() / f.filled.sum()) if f.filled.sum() else 0.0
            lim = float(g.limit.mean())
            side = g.side.iloc[0]
            # The price the order was sized off. Cost vs it (positive = paid above /
            # sold below) is what the real-time-data decision needs; slippage vs the
            # limit is flattered by the buffer.
            ref = (float(g.ref_price.dropna().mean())
                   if "ref_price" in g and g.ref_price.notna().any() else None)
            sign = 1 if side == "BUY" else -1
            cost_ref = (round(sign * (avg - ref) / ref * 1e4, 1) if (avg and ref) else None)
            trades.append({
                "ref_price": ref, "cost_vs_ref_bps": cost_ref,
                "ticker": tk, "side": side, "ordered": ordered, "filled": filled,
                "remaining": ordered - filled,
                "fill_rate": (filled / ordered) if ordered else 0.0,
                "limit": lim, "original_limit": float(g.original_limit.mean()),
                "avg_fill": avg or None, "slippage_bps": _slip_bps(avg, lim, side),
                "status": ",".join(sorted(set(g.status))),
                "reprices": int(g.reprices.sum()), "children": int(len(g)),
            })
    return write_run(trades, account=account, nav_usd=nav_usd, dry_run=dry_run,
                     book=book, engine="execution_engine", config=config,
                     events=events)


def backfill_from_reconciliation() -> list:
    """One-time import of pre-store runs from broker/reconciliation/.

    The 2026-09-29 baseline predates this store. Its build-time record (recon_*.json,
    which carries ref_at_build) is merged with the final fills (reconcile_*.json) so
    the dashboard has the real baseline to compare against.
    """
    written = []
    builds = sorted(glob.glob(str(RECON_DIR / "recon_*.json")))
    finals = sorted(glob.glob(str(RECON_DIR / "reconcile_*.json")))
    if not builds or not finals:
        return written
    build = json.load(open(builds[-1]))
    final = json.load(open(finals[-1]))
    b = pd.DataFrame(build["orders"])
    f = pd.DataFrame(final["orders"])[["ticker", "filled", "remaining", "avg_fill", "status"]]
    m = b[["ticker", "side", "ordered", "limit"]].merge(f, on="ticker", how="left")

    trades = []
    for r in m.itertuples():
        avg = float(r.avg_fill) if pd.notna(r.avg_fill) else 0.0
        trades.append({
            "ticker": r.ticker, "side": r.side, "ordered": float(r.ordered),
            "filled": float(r.filled), "remaining": float(r.remaining),
            "fill_rate": float(r.filled) / float(r.ordered) if r.ordered else 0.0,
            "limit": float(r.limit), "original_limit": float(r.limit),
            "avg_fill": avg or None, "slippage_bps": _slip_bps(avg, float(r.limit), r.side),
            "status": r.status, "reprices": 0, "children": 1,
        })
    rid = "20260929_182912_baseline"
    if (EXEC_LOG_DIR / f"run_{rid}.json").exists():
        return written
    p = write_run(trades, account=final["account"], nav_usd=final.get("nav_usd"),
                  dry_run=False, book="broker/compliant_book.csv",
                  engine="simple_flat_30bps", run_id=rid,
                  timestamp=final.get("timestamp"),
                  config={"note": "pre-store baseline: flat 30bps buffer, no slicing, "
                                  "no repricing, cancelled after ~4 minutes"})
    written.append(p)
    return written


def load_runs() -> list:
    """All persisted runs, newest first."""
    out = []
    for p in sorted(glob.glob(str(EXEC_LOG_DIR / "run_*.json")), reverse=True):
        try:
            out.append(json.loads(Path(p).read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def load_trades() -> pd.DataFrame:
    """Tidy trade-level frame across every run (the dashboard's source)."""
    rows = []
    for r in load_runs():
        for t in r.get("trades", []):
            rows.append({
                "run_id": r["run_id"], "timestamp": r["timestamp"],
                "account": r["account"], "dry_run": r.get("dry_run", True),
                "book": r.get("book"), "engine": r.get("engine", "simple"),
                "nav_usd": r.get("nav_usd"), **t,
            })
    df = pd.DataFrame(rows)
    if not df.empty:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


def load_events() -> pd.DataFrame:
    rows = []
    for r in load_runs():
        for e in r.get("events", []):
            rows.append({"run_id": r["run_id"], **e})
    return pd.DataFrame(rows)


def run_summary() -> pd.DataFrame:
    """One row per run: fill rate, deployed, slippage, reprices."""
    df = load_trades()
    if df.empty:
        return df
    g = df.groupby(["run_id", "timestamp", "engine", "dry_run", "account",
                    "nav_usd", "book"], dropna=False)
    s = g.apply(lambda x: pd.Series({
        "orders": len(x),
        "fully_filled": int(((x.remaining == 0) & (x.filled > 0)).sum()),
        "partial": int(((x.filled > 0) & (x.remaining > 0)).sum()),
        "unfilled": int((x.filled == 0).sum()),
        "shares_ordered": x.ordered.sum(),
        "shares_filled": x.filled.sum(),
        "share_fill_rate": x.filled.sum() / x.ordered.sum() if x.ordered.sum() else 0.0,
        "filled_notional": (x.filled * x.avg_fill.fillna(0)).sum(),
        "mean_slippage_bps": x.slippage_bps.dropna().mean(),
        "reprices": x.reprices.sum(),
        "children": x.children.sum(),
    }), include_groups=False).reset_index()
    return s.sort_values("timestamp", ascending=False)


if __name__ == "__main__":
    w = backfill_from_reconciliation()
    print(f"backfilled {len(w)} run(s): {[p.name for p in w]}")
    print(f"\nruns on disk: {len(load_runs())}")
    print(run_summary().to_string(index=False))
