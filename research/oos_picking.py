"""
research/oos_picking.py — is the small-cap stock picking real? Out-of-sample years.

Implements docs/research/oos_picking_PREREG.md exactly: the universe study's `small`
contestant (incumbent-like picking in today's small caps, real past prices) on the years
before 2019-03, which no earlier study measured. One test: do the 15 picks out-compound
their own pool's equal-weight average, by enough and consistently enough?

Factor, pick, simulation and pool code is research/strategy_race.py's and
research/universe_study.py's, unchanged. READ-ONLY on every database; output goes only
to research/output/oos_picking/. Changes nothing in the live system.

    python -m research.oos_picking
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
from research import universe_study as us  # noqa: E402

OUT_DIR = ROOT / "research" / "output" / "oos_picking"

# ── pre-registered constants (docs/research/oos_picking_PREREG.md) ──────────────
OOS_END = pd.Timestamp("2019-02-28")       # last month before the earlier window
MIN_DOLLAR_VOL = 2e6
MIN_SCORED = 150
MIN_MONTHS = 36
EDGE_BAR, T_BAR = 0.02, 2.0
FINANCING = us.HEDGE_FINANCING


def _cagr(r) -> float:
    r = np.asarray(r, dtype=float)
    return float(np.prod(1 + r) ** (12 / len(r)) - 1) if len(r) else float("nan")


def verdict(picks: pd.Series, pool: pd.Series) -> dict:
    """CONFIRMED / CONSISTENT / NOT CONFIRMED / INSUFFICIENT DATA for the picks against
    their pool, by the pre-registered rule."""
    n = len(picks)
    if n < MIN_MONTHS:
        return {"verdict": "INSUFFICIENT DATA", "months": n, "edge": np.nan,
                "t": np.nan, "periods_ahead": 0}
    edge = _cagr(picks) - _cagr(pool)
    a = (picks - pool).to_numpy()
    sd = a.std(ddof=1)
    t = float(a.mean() / (sd / math.sqrt(n))) if sd > 0 else np.nan
    ahead = int(sum(_cagr(x) > _cagr(y) for x, y in
                    zip(np.array_split(picks.to_numpy(), 4), np.array_split(pool.to_numpy(), 4))))
    if edge >= EDGE_BAR and np.isfinite(t) and t >= T_BAR and ahead >= 3:
        v = "CONFIRMED"
    elif edge > 0 and ahead >= 3:
        v = "CONSISTENT"
    else:
        v = "NOT CONFIRMED"
    return {"verdict": v, "months": n, "edge": edge, "t": t, "periods_ahead": ahead}


@dataclass
class OosResult:
    monthly: pd.DataFrame                  # picks, pool, SPY, IWM, hedged (full run)
    start: pd.Timestamp | None
    coverage: pd.DataFrame
    oos: dict = field(default_factory=dict)
    later: dict = field(default_factory=dict)


def run(panel: pd.DataFrame, close: pd.DataFrame, adj: pd.DataFrame, vol: pd.DataFrame,
        members: dict, sectors: dict, bench: pd.DataFrame,
        end: pd.Timestamp | None = None) -> OosResult:
    """One continuous run from the first month with MIN_SCORED names to `end` (default:
    the latest month); the verdict uses the months through OOS_END only."""
    since = pd.Timestamp(f"{sr.FIRST_YEAR - 1}-01-01")
    close, adj, vol = (m.loc[since:].astype("float32") for m in (close, adj, vol))
    pf = sr.price_features(close, adj, vol)
    full = sr.with_prior_year(panel)
    small = full[full["ticker"].isin(set(members["iwm"]))]
    idx = adj.index[adj.index.year >= sr.FIRST_YEAR]
    if end is not None:
        idx = idx[idx <= end]
    dates = list(pd.Series(idx, index=idx).groupby(idx.to_period("M")).first())
    scores, cov = {}, []
    for k, d in enumerate(dates):
        if k % 24 == 0:
            print(f"  ... {d:%Y-%m} ({k + 1}/{len(dates)})", flush=True)
        scores[d] = sr.scores_on(d, small, pf, max_mcap=sr.MAX_MCAP,
                                 min_dollar_vol=MIN_DOLLAR_VOL)
        cov.append({"date": d, "scored": len(scores[d])})
    cov = pd.DataFrame(cov)
    hit = cov.loc[cov["scored"] >= MIN_SCORED, "date"]
    if hit.empty:
        return OosResult(pd.DataFrame(), None, cov)
    start = hit.iloc[0]
    ds = [d for d in dates if d >= start]
    if len(ds) < 2:
        return OosResult(pd.DataFrame(), start, cov)
    a, b = adj.ffill(), bench.ffill()
    sim, ew = us.run_pool(scores, ds, [sr._row(a, d) for d in ds], sectors)
    bp = [sr._row(b, d) for d in ds]
    m = pd.DataFrame({"picks": sim["net"], "pool": ew}, index=sim.index)
    for t in ("SPY", "IWM"):
        m[t] = [bp[i + 1][t] / bp[i][t] - 1 for i in range(len(ds) - 1)]
    m["hedged"] = us.hedged(m["SPY"], m["picks"], m["IWM"], FINANCING)
    res = OosResult(m, start, cov)
    o, later = m[m.index <= OOS_END], m[m.index > OOS_END]
    # a start after OOS_END leaves no out-of-sample month: INSUFFICIENT DATA, 0 months
    res.oos = {**verdict(o["picks"], o["pool"]), **(_describe(o) if len(o) else {})}
    res.later = {"months": len(later), **(_describe(later) if len(later) else {}),
                 "edge": (_cagr(later["picks"]) - _cagr(later["pool"])) if len(later) else np.nan}
    return res


def _describe(m: pd.DataFrame) -> dict:
    out = {"from": m.index[0], "to": m.index[-1]}
    for k in ("picks", "pool", "SPY", "IWM", "hedged"):
        st = sr.stats(m[k])
        out[k] = {"cagr": st["cagr"], "sharpe": st["sharpe"], "max_dd": st["max_dd"]}
    return out


# ── report ──────────────────────────────────────────────────────────────────────

def write_report(res: OosResult, out_dir: Path = OUT_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    res.monthly.to_csv(out_dir / "monthly_returns.csv")
    res.coverage.to_csv(out_dir / "coverage_by_date.csv", index=False)
    o, lt, p = res.oos, res.later, sr._pct
    yearly = res.coverage.assign(year=res.coverage["date"].dt.year) \
        .groupby("year")["scored"].mean().round(0).astype(int)
    lines = [
        "# Is the small-cap stock picking real? Out-of-sample result",
        "",
        "Pre-registered in `docs/research/oos_picking_PREREG.md`. Prices: research store "
        "(real past prices). Costs included.",
        "",
        f"## Verdict (years never measured before): **{o['verdict']}**",
        "",
    ]
    if o["verdict"] == "INSUFFICIENT DATA":
        lines += [f"Only {o['months']} months before 2019-03 had >= {MIN_SCORED} scored "
                  f"names; {MIN_MONTHS} are needed.", ""]
    else:
        lines += [
            f"Window {o['from']:%Y-%m} to {o['to']:%Y-%m} ({o['months']} months).",
            "",
            "| | value |",
            "|---|---|",
            f"| stock picking: picks CAGR − pool CAGR | **{p(o['edge'])}/yr** "
            f"(needs >= {p(EDGE_BAR)}) |",
            f"| t on monthly picks − pool | {o['t']:.2f} (needs >= {T_BAR:.1f}) |",
            f"| periods where the picks out-compounded the pool | {o['periods_ahead']} of 4 "
            f"(needs >= 3) |",
            "",
            "## The same window, in money (descriptive)",
            "",
            "| | CAGR | Sharpe | max DD |",
            "|---|---|---|---|",
            *[f"| {name} | {p(o[k]['cagr'])} | {o[k]['sharpe']:.2f} | {p(o[k]['max_dd'])} |"
              for k, name in (("picks", "your picks (15)"), ("pool", "the small-cap pool, EW"),
                              ("IWM", "IWM"), ("SPY", "S&P 500 (SPY)"),
                              ("hedged", "hedged: SPY + picks − IWM − 2%"))],
            "",
        ]
    if lt.get("months"):
        lines += [
            "## Same rules on 2019-03 onward (descriptive: compare the universe study's "
            "+5.0%)",
            "",
            f"{lt['months']} months; stock picking {p(lt['edge'])}/yr; hedged CAGR "
            f"{p(lt['hedged']['cagr'])} vs SPY {p(lt['SPY']['cagr'])}.",
            "",
        ]
    lines += ["## Scored names per year (average per month)", "",
              "| year | names |", "|---|---|",
              *[f"| {y} | {n} |" for y, n in yearly.items()], "",
              "## Limits (from the pre-registration)", "",
              "Survivorship grows the further back (today's small caps traced back); "
              "thinner SEC coverage early; a $2m dollar-volume floor and 150-name start "
              "instead of $5m / 300; 15-name portfolios are noisy; one history.", "",
              "No action taken."]
    path = out_dir / "REPORT.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="Pre-registered out-of-sample picking test.") \
        .parse_args(argv)
    from data import sec_xbrl
    from research import price_store
    for p, what, fix in ((price_store.DB_PATH, "research price store", "research.price_store"),
                         (sec_xbrl.DB_PATH, "SEC database", "data.sec_xbrl")):
        if not p.exists():
            print(f"FAILED: no {what} at {p} -- run: python -m {fix}")
            return 1
    print("loading prices (research store) and SEC fundamentals ...", flush=True)
    mats = price_store.load_matrices()
    members = {"iwm": mats["iwm"], "iwv": mats["iwv"]}
    if not members["iwm"]:
        print("FAILED: the price store has no IWM holdings -- run: python -m research.price_store")
        return 1
    try:
        close, adj, vol, bench = sr.market(sorted(set(members["iwm"])), load=lambda t: mats)
    except ValueError as e:
        print(f"FAILED: {e}")
        return 1
    panel = sec_xbrl.cached_panel()
    panel = panel[panel["ticker"].isin(set(adj.columns))]
    res = run(panel, close, adj, vol, members, mats["sectors"], bench)
    if res.monthly.empty:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        res.coverage.to_csv(OUT_DIR / "coverage_by_date.csv", index=False)
        print(f"FAILED: no month had {MIN_SCORED} scored names -- see "
              f"{OUT_DIR / 'coverage_by_date.csv'}")
        return 1
    path = write_report(res)
    o = res.oos
    if o["verdict"] == "INSUFFICIENT DATA":
        print(f"INSUFFICIENT DATA: {o['months']} months before 2019-03 (need {MIN_MONTHS})")
    else:
        print(f"{o['verdict']}: picking {o['edge']:+.1%}/yr vs the pool, t {o['t']:.2f}, "
              f"{o['periods_ahead']} of 4 periods ahead, {o['from']:%Y-%m} to {o['to']:%Y-%m}")
        print(f"  hedged {o['hedged']['cagr']:+.1%}/yr vs S&P 500 {o['SPY']['cagr']:+.1%}/yr "
              f"(Sharpe {o['hedged']['sharpe']:.2f} vs {o['SPY']['sharpe']:.2f})")
    print(f"report: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
