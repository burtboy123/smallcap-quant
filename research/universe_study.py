"""
research/universe_study.py — the pre-registered universe study (wider pools vs SPY).

Implements docs/research/universe_study_PREREG.md exactly: the strategy race's
incumbent-like rules applied to three pools (small caps, all caps, the 1,000 largest),
plus 70% SPY + 30% small-cap picks, and SPY + small-cap picks − IWM (picking without
the small-cap exposure). Verdicts on compounded returns (CAGR) vs SPY.

Factor, pick and simulation code is research/strategy_race.py's, unchanged; prices come
from research/price_store.py (real past prices). READ-ONLY on every database; output
goes only to research/output/universe_study/. Changes nothing in the live system.

    python -m research.universe_study
    python -m research.universe_study --end 2026-09-30
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research import strategy_race as sr  # noqa: E402

OUT_DIR = ROOT / "research" / "output" / "universe_study"

# ── pre-registered constants (docs/research/universe_study_PREREG.md) ───────────
WEIGHTS, GATE = sr.CONTESTANTS["incumbent_like"]
LARGEST = 1000
POOLS = {                                   # name -> (member list, max market cap, largest N)
    "small": ("iwm", sr.MAX_MCAP, None),
    "wide": ("iwv", math.inf, None),
    "large": ("iwv", math.inf, LARGEST),
}
CORE_SPY, SLEEVE_COST, SPY_COST = 0.70, sr.COST_PER_SIDE, 0.0005
HEDGE_FINANCING = 0.02                      # per year: margin spread + IWM borrow
NAMES = ["small", "wide", "large", "core70", "hedged"]


# ── pools ───────────────────────────────────────────────────────────────────────

def pool_scores(d, panels: dict, pf: dict, pools: dict | None = None) -> dict:
    pools = pools or POOLS
    return {name: sr.scores_on(d, panels[name], pf, max_mcap=mx, largest=n)
            for name, (_, mx, n) in pools.items()}


def run_pool(scores: dict, dates: list, prices: list, sectors: dict) -> tuple[pd.DataFrame,
                                                                              pd.Series]:
    """(simulate() frame for the pool's 15 picks, pool equal-weight gross returns), on
    `dates`; scores[d] = that pool's scores on date d."""
    targets, ew, held = [], [], set()
    for d in dates:
        s = scores[d]
        ch = sr.pick(s, WEIGHTS, GATE, sectors, held) if len(s) else []
        held = set(ch)
        targets.append({t: 1 / sr.N_HOLD for t in ch})
        ew.append({t: 1 / len(s) for t in s.index} if len(s) else {})
    months = pd.DatetimeIndex(dates[:-1])
    sim = sr.simulate(targets, prices).set_index(months)
    return sim, pd.Series(sr.simulate(ew, prices)["gross"].to_numpy(), index=months)


def core_satellite(spy: pd.Series, sleeve: pd.Series, w: float = CORE_SPY) -> pd.Series:
    """w in SPY, 1-w in the sleeve (its returns already net of its own costs),
    rebalanced back to w every month; rebalancing trades cost SLEEVE_COST / SPY_COST."""
    out = []
    for rs, rl in zip(spy.to_numpy(), sleeve.to_numpy()):
        r = w * rs + (1 - w) * rl
        drift = abs(w * (1 + rs) / (1 + r) - w) if 1 + r > 0 else 0.0
        out.append((1 + r) * (1 - drift * (SLEEVE_COST + SPY_COST)) - 1)
    return pd.Series(out, index=spy.index)


def hedged(spy: pd.Series, sleeve: pd.Series, iwm: pd.Series,
           financing: float = HEDGE_FINANCING) -> pd.Series:
    """SPY + (sleeve − IWM) − financing: the sleeve's picking without its small-cap
    exposure. 200% long, 100% short."""
    return spy + sleeve - iwm - financing / 12


# ── verdict (on CAGR) ───────────────────────────────────────────────────────────

def _cagr(r) -> float:
    r = np.asarray(r, dtype=float)
    return float(np.prod(1 + r) ** (12 / len(r)) - 1)


def verdict(r: pd.Series, spy: pd.Series) -> dict:
    a = sr.active(r, spy)
    st, sb = sr.stats(r), sr.stats(spy)
    folds = [_cagr(x) > _cagr(y) for x, y in
             zip(np.array_split(r.to_numpy(), sr.N_FOLDS),
                 np.array_split(spy.to_numpy(), sr.N_FOLDS))]
    better = st["cagr"] > sb["cagr"]
    n_folds = int(sum(folds))
    if (better and n_folds >= 3 and np.isfinite(a["t"]) and a["t"] >= sr.T_BAR
            and st["max_dd"] >= sb["max_dd"] - sr.MAX_DD_SLACK):
        v = "BEATS"
    elif better and n_folds >= 3:
        v = "PROMISING"
    else:
        v = "DOES NOT BEAT"
    return {"verdict": v, "cagr_gap": st["cagr"] - sb["cagr"], "t": a["t"],
            "folds_cagr_ahead": n_folds}


# ── the study ───────────────────────────────────────────────────────────────────

@dataclass
class StudyResult:
    monthly: pd.DataFrame                       # contestants, pool EWs, SPY, IWM
    start: pd.Timestamp | None
    coverage: pd.DataFrame
    table: pd.DataFrame = field(default_factory=pd.DataFrame)
    long: pd.DataFrame = field(default_factory=pd.DataFrame)   # descriptive


def study(panel: pd.DataFrame, close: pd.DataFrame, adj: pd.DataFrame, vol: pd.DataFrame,
          members: dict, sectors: dict, bench: pd.DataFrame,
          end: pd.Timestamp | None = None) -> StudyResult:
    since = pd.Timestamp(f"{sr.FIRST_YEAR - 1}-01-01")
    close, adj, vol = (m.loc[since:].astype("float32") for m in (close, adj, vol))
    pf = sr.price_features(close, adj, vol)
    full = sr.with_prior_year(panel)
    panels = {n: full[full["ticker"].isin(set(members[src]))]
              for n, (src, _, _) in POOLS.items()}
    idx = adj.index[adj.index.year >= sr.FIRST_YEAR]
    if end is not None:
        idx = idx[idx <= end]
    dates = list(pd.Series(idx, index=idx).groupby(idx.to_period("M")).first())
    scores = {n: {} for n in POOLS}
    cov = []
    for k, d in enumerate(dates):
        if k % 24 == 0:
            print(f"  ... {d:%Y-%m} ({k + 1}/{len(dates)})", flush=True)
        s = pool_scores(d, panels, pf)
        for n in POOLS:
            scores[n][d] = s[n]
        cov.append({"date": d, **{n: len(s[n]) for n in POOLS}})
    cov = pd.DataFrame(cov)

    def first(cond) -> pd.Timestamp | None:
        hit = cov.loc[cond, "date"]
        return hit.iloc[0] if len(hit) else None

    start = first((cov["small"] >= sr.MIN_SCORED) & (cov["large"] >= sr.MIN_SCORED))
    if start is None:
        return StudyResult(pd.DataFrame(), None, cov)
    a, b = adj.ffill(), bench.ffill()

    def window(d0):
        ds = [d for d in dates if d >= d0]
        return ds, [sr._row(a, d) for d in ds], [sr._row(b, d) for d in ds]

    ds, prices, bp = window(start)
    months = pd.DatetimeIndex(ds[:-1])
    m = pd.DataFrame(index=months)
    for t in ("SPY", "IWM"):
        m[t] = [bp[i + 1][t] / bp[i][t] - 1 for i in range(len(ds) - 1)]
    for n in POOLS:
        sim, ew = run_pool(scores[n], ds, prices, sectors)
        m[n], m[f"{n}_ew"] = sim["net"], ew
    m["core70"] = core_satellite(m["SPY"], m["small"])
    m["hedged"] = hedged(m["SPY"], m["small"], m["IWM"])
    res = StudyResult(m, start, cov)
    res.table = summarize(m)
    res.long = long_window(scores, dates, cov, a, b, sectors)
    return res


def summarize(m: pd.DataFrame) -> pd.DataFrame:
    spy = sr.stats(m["SPY"])
    rows = []
    for n in NAMES:
        st, v = sr.stats(m[n]), verdict(m[n], m["SPY"])
        row = {"contestant": n, **st, **v,
               "growth_10k": float(10_000 * (1 + m[n]).prod()),
               "spy_growth_10k": float(10_000 * (1 + m["SPY"]).prod())}
        if n in POOLS:
            row["part_pool"] = sr.stats(m[f"{n}_ew"])["cagr"] - spy["cagr"]
            row["part_picking"] = st["cagr"] - sr.stats(m[f"{n}_ew"])["cagr"]
        rows.append(row)
    return pd.DataFrame(rows).set_index("contestant")


def long_window(scores: dict, dates: list, cov: pd.DataFrame, a: pd.DataFrame,
                b: pd.DataFrame, sectors: dict) -> pd.DataFrame:
    """Descriptive only: `large` and `wide` from their own first month with enough
    scored names (a longer history than the verdict window)."""
    rows = []
    for n in ("large", "wide"):
        hit = cov.loc[cov[n] >= sr.MIN_SCORED, "date"]
        if hit.empty:
            continue
        ds = [d for d in dates if d >= hit.iloc[0]]
        if len(ds) < 13:
            continue
        sim, ew = run_pool(scores[n], ds, [sr._row(a, d) for d in ds], sectors)
        bp = [sr._row(b, d) for d in ds]
        spy = pd.Series([bp[i + 1]["SPY"] / bp[i]["SPY"] - 1 for i in range(len(ds) - 1)],
                        index=sim.index)
        rows.append({"pool": n, "from": ds[0], "months": len(sim),
                     "cagr": sr.stats(sim["net"])["cagr"],
                     "spy_cagr": sr.stats(spy)["cagr"],
                     "pool_ew_cagr": sr.stats(ew)["cagr"],
                     "max_dd": sr.stats(sim["net"])["max_dd"],
                     "spy_max_dd": sr.stats(spy)["max_dd"]})
    return pd.DataFrame(rows)


# ── report ──────────────────────────────────────────────────────────────────────

def write_report(res: StudyResult, out_dir: Path = OUT_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    res.monthly.to_csv(out_dir / "monthly_returns.csv")
    res.table.to_csv(out_dir / "summary.csv")
    res.coverage.to_csv(out_dir / "coverage_by_date.csv", index=False)
    t, m, p = res.table, res.monthly, sr._pct
    spy = sr.stats(m["SPY"])
    lines = [
        "# Universe study — result",
        "",
        f"Pre-registered in `docs/research/universe_study_PREREG.md`. Prices: research "
        f"store (real past prices). Window {m.index[0]:%Y-%m} to {m.index[-1]:%Y-%m} "
        f"({len(m)} months; first month with >= {sr.MIN_SCORED} scored names in both the "
        f"small and the large pool). Costs included. Verdicts on compounded returns.",
        "",
        "## Verdicts vs the S&P 500",
        "",
        "| Contestant | verdict | CAGR vs SPY /yr | t | folds ahead (of 4) |",
        "|---|---|---|---|---|",
        *[f"| {n} | {r.verdict} | {p(r.cagr_gap)} | {r.t:.2f} | {r.folds_cagr_ahead} |"
          for n, r in t.iterrows()],
        "",
        "## Returns and risk",
        "",
        "| | CAGR | vol | Sharpe | max DD | worst month | $10,000 became |",
        "|---|---|---|---|---|---|---|",
        *[f"| {n} | {p(r.cagr)} | {r.vol:.1%} | {r.sharpe:.2f} | {p(r.max_dd)} | "
          f"{p(r.worst_month)} | ${r.growth_10k:,.0f} |" for n, r in t.iterrows()],
        f"| SPY | {p(spy['cagr'])} | {spy['vol']:.1%} | {spy['sharpe']:.2f} | "
        f"{p(spy['max_dd'])} | {p(spy['worst_month'])} | "
        f"${t['spy_growth_10k'].iloc[0]:,.0f} |",
        "",
        "`hedged` holds 100% SPY + 100% small-cap picks − 100% IWM (leverage, a short "
        f"position, {HEDGE_FINANCING:.0%}/yr financing deducted).",
        "",
        "## The pools: where each gap to SPY comes from (CAGR, adds up)",
        "",
        "| | gap to SPY | = the pool itself (pool EW − SPY) | + stock picking (− pool EW) |",
        "|---|---|---|---|",
        *[f"| {n} | {p(t.loc[n, 'cagr_gap'])} | {p(t.loc[n, 'part_pool'])} | "
          f"{p(t.loc[n, 'part_picking'])} |" for n in POOLS],
        "",
    ]
    if len(res.long):
        lines += [
            "## Longer history (descriptive only, no verdict)",
            "",
            "| pool | from | months | CAGR | SPY CAGR | pool EW CAGR | max DD | SPY max DD |",
            "|---|---|---|---|---|---|---|---|",
            *[f"| {r.pool} | {r['from']:%Y-%m} | {r.months} | {p(r.cagr)} | "
              f"{p(r.spy_cagr)} | {p(r.pool_ew_cagr)} | {p(r.max_dd)} | "
              f"{p(r.spy_max_dd)} |" for _, r in res.long.iterrows()],
            "",
        ]
    lines += ["## Limits (from the pre-registration)", "",
              "Current-constituent survivorship (larger for the large pool: today's large "
              "caps include yesterday's small-cap winners); window set by SEC coverage; "
              "today's sectors; the financing cost is an estimate; one history.", "",
              "No action taken."]
    path = out_dir / "REPORT.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Pre-registered universe study vs SPY.")
    ap.add_argument("--end", default=None, help="last date used (YYYY-MM-DD)")
    args = ap.parse_args(argv)
    from data import sec_xbrl
    from research import price_store
    if not price_store.DB_PATH.exists():
        print(f"FAILED: no research price store at {price_store.DB_PATH} -- run: "
              f"python -m research.price_store")
        return 1
    if not sec_xbrl.DB_PATH.exists():
        print(f"FAILED: no SEC database at {sec_xbrl.DB_PATH} -- run: "
              f"python -m research.price_store")
        return 1
    print("loading prices (research store) and SEC fundamentals ...", flush=True)
    mats = price_store.load_matrices()
    members = {"iwm": mats["iwm"], "iwv": mats["iwv"]}
    if not members["iwv"] or not members["iwm"]:
        print("FAILED: the price store has no IWV/IWM holdings -- run: "
              "python -m research.price_store")
        return 1
    try:
        close, adj, vol, bench = sr.market(sorted(set(members["iwv"]) | set(members["iwm"])),
                                           load=lambda t: mats)
    except ValueError as e:
        print(f"FAILED: {e}")
        return 1
    panel = sec_xbrl.cached_panel()
    panel = panel[panel["ticker"].isin(set(adj.columns))]
    print(f"{adj.shape[1]} tickers with prices, {panel['ticker'].nunique()} with SEC "
          f"fundamentals", flush=True)
    res = study(panel, close, adj, vol, members, mats["sectors"], bench,
                pd.Timestamp(args.end) if args.end else None)
    if res.monthly.empty:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        res.coverage.to_csv(OUT_DIR / "coverage_by_date.csv", index=False)
        print(f"FAILED: no month had {sr.MIN_SCORED} scored names in both the small and "
              f"the large pool -- see {OUT_DIR / 'coverage_by_date.csv'}")
        return 1
    path = write_report(res)
    for n, r in res.table.iterrows():
        print(f"{n:<8} {r.verdict:<14} CAGR {r.cagr:+.1%}  vs SPY {r.cagr_gap:+.1%}/yr  "
              f"max DD {r.max_dd:+.0%}")
    print(f"report: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
