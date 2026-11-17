"""
forward_test/evaluate.py — compute the forward test's verdict inputs exactly as
<period>/EVAL_CRITERIA.md defines them, and say which decision clauses fire.

    python forward_test/evaluate.py                       # active period, INTERIM reading
    python forward_test/evaluate.py --as-of 2026-11-13    # interim as of a date
    python forward_test/evaluate.py --final               # the verdict inputs (needs data
                                                          #   through the evaluation date)
    python forward_test/evaluate.py --write               # also save the report

The window, baselines and evaluation date come from the period's own files, never from
arguments typed on the day (QUARTER_END_PLAN.md §1): first-close baselines and the SPY
baseline from meta.json, the evaluation date from QUARTER_END_PLAN.md, entry value scores
from entry_scores.csv (frozen at the start of the window).

Metric definitions (EVAL_CRITERIA.md §1), all over sessions <= the as-of date:
  total return       equal-weight mean of close / baseline - 1 (carried-forward cells hold
                     the last traded price, i.e. a delisting counts as a realized loss)
  alpha vs SPY       book return - SPY return
  relative spread    Challenger - Incumbent
  max drawdown       worst peak-to-trough of the equal-weight daily equity curve
  hit rate           % of names up; % of names beating SPY
  value consistency  Spearman rank IC: entry value_score vs realized return, across the
                     combined (unique) names of both books

Decision clauses (§2) are evaluated mechanically. Clause (a)5 -- "tail acceptable given
the size of the win" -- is a judgement, so this script never marks it satisfied: an ADOPT
can only come from a human reading the numbers, never from the script alone (the plan's
rule: what cannot be computed as defined counts as NOT satisfied, failing toward KEEP).

INTERIM readings are not the verdict. The criteria judge one window, on one date.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

NOISE_BAND = 0.03          # ±3.0 pp  (§2 a1 / b1 / b4 / c1)
DD_MARGIN = 0.05           # +5 pp deeper drawdown (§2 a3 / b3)
DD_FLOOR = -0.25           # hard breach (§2 a3 / b3)
ALPHA_FLOOR = -0.03        # Challenger alpha vs SPY >= -3 pp (§2 a4)


# ── inputs ──────────────────────────────────────────────────────────────────────

def evaluation_date(period_dir: Path) -> date | None:
    """The evaluation date fixed in QUARTER_END_PLAN.md §1 (never an argument)."""
    plan = period_dir / "QUARTER_END_PLAN.md"
    if not plan.exists():
        return None
    m = re.search(r"Evaluation date[^|]*\|\s*\*\*(\d{4}-\d{2}-\d{2})\*\*",
                  plan.read_text(encoding="utf-8"))
    return date.fromisoformat(m.group(1)) if m else None


@dataclass
class Inputs:
    period: str
    closes: pd.DataFrame            # index=date, columns=tickers (+ SPY)
    inc_base: dict
    ch_base: dict
    spy_base: float
    entry: pd.DataFrame | None      # ticker -> value_score
    eval_date: date | None
    carried: int = 0


def load(period_dir: Path, as_of: date) -> Inputs:
    meta = json.loads((period_dir / "meta.json").read_text(encoding="utf-8"))
    if not meta.get("baselines_locked"):
        raise ValueError("baselines are not locked yet -- no session recorded")
    snap = pd.read_csv(period_dir / "price_snapshots.csv")
    snap = snap[pd.to_datetime(snap["date"]).dt.date <= as_of]
    if snap.empty:
        raise ValueError(f"no sessions on or before {as_of}")
    closes = snap.pivot(index="date", columns="ticker", values="close").sort_index()
    entry = None
    ep = period_dir / "entry_scores.csv"
    if ep.exists():
        e = pd.read_csv(ep)
        entry = e.drop_duplicates("ticker").set_index("ticker")[["value_score"]]
    # baselines in today's share units: the closes are split-restated by yfinance, so a
    # raw locked baseline would read a split as a loss (forward_test/baselines.py)
    from forward_test.baselines import current_baselines
    cb = current_baselines(period_dir)
    return Inputs(period=period_dir.name, closes=closes,
                  inc_base=cb["incumbent"], ch_base=cb["challenger"],
                  spy_base=float(cb["spy"]), entry=entry,
                  eval_date=evaluation_date(period_dir),
                  carried=int(snap.get("is_carried", pd.Series(dtype=int)).sum()))


# ── metrics ─────────────────────────────────────────────────────────────────────

@dataclass
class BookStats:
    name: str
    ret: float
    alpha: float
    max_dd: float
    hit_up: float
    hit_spy: float
    per_name: pd.Series = field(repr=False)        # ticker -> return


def book_stats(name: str, closes: pd.DataFrame, base: dict, spy_ret: float) -> BookStats:
    tickers = [t for t in base if t in closes.columns]
    rel = closes[tickers].ffill().div(pd.Series(base)[tickers])      # price / baseline
    curve = rel.mean(axis=1)                                          # equal weight
    dd = (curve / curve.cummax() - 1).min()
    per = rel.iloc[-1] - 1
    ret = float(per.mean())
    return BookStats(name, ret, ret - spy_ret, float(min(dd, 0.0)),
                     float((per > 0).mean()), float((per > spy_ret).mean()), per)


def spearman(x: pd.Series, y: pd.Series) -> tuple[float, float | None, int]:
    d = pd.concat([x, y], axis=1).dropna()
    n = len(d)
    if n < 3 or d.iloc[:, 0].nunique() < 2 or d.iloc[:, 1].nunique() < 2:
        return float("nan"), None, n          # no variation -> no rank correlation
    ic = float(d.iloc[:, 0].rank().corr(d.iloc[:, 1].rank()))
    try:
        from scipy.stats import spearmanr
        p = float(spearmanr(d.iloc[:, 0], d.iloc[:, 1]).pvalue)
    except Exception:                                               # noqa: BLE001
        p = None
    return ic, p, n


def drop_contributor_spreads(inc: BookStats, ch: BookStats) -> dict:
    """§2 (c)3: does the spread survive dropping a single best/worst name?"""
    out = {}
    for book, other, sign in ((ch, inc, 1), (inc, ch, -1)):
        for which, idx in (("top", book.per_name.idxmax()), ("bottom", book.per_name.idxmin())):
            r = book.per_name.drop(idx).mean()
            spread = sign * (r - other.ret)
            out[f"{book.name} without its {which} name ({idx})"] = float(spread)
    return out


# ── decision clauses (§2) ───────────────────────────────────────────────────────

def clauses(inc: BookStats, ch: BookStats, ic: float, drops: dict) -> dict:
    spread = ch.ret - inc.ret
    ic_known = ic == ic
    dd_worse = inc.max_dd - ch.max_dd                    # >0: Challenger deeper
    near_tie = abs(spread) < NOISE_BAND
    flips = any((np.sign(s) != np.sign(spread)) or
                (abs(spread) >= NOISE_BAND and abs(s) < NOISE_BAND)
                for s in drops.values())
    return {
        "a1 Challenger beats Incumbent by >= 3 pp": spread >= NOISE_BAND,
        "a2 value consistency positive (IC > 0)": ic_known and ic > 0,
        "a3 Challenger drawdown <= 5 pp deeper and above -25%":
            dd_worse <= DD_MARGIN and ch.max_dd > DD_FLOOR,
        "a4 Challenger alpha vs SPY >= -3 pp": ch.alpha >= ALPHA_FLOOR,
        "a5 tail acceptable given the win (JUDGEMENT, never automatic)": False,
        "b1 Incumbent beats Challenger by >= 3 pp": -spread >= NOISE_BAND,
        "b2 value consistency negative (IC < 0)": ic_known and ic < 0,
        "b3 Challenger drawdown > 5 pp deeper or breaches -25%":
            dd_worse > DD_MARGIN or ch.max_dd <= DD_FLOOR,
        "b4 near-tie (|spread| < 3 pp): tail tie-breaker keeps Incumbent": near_tie,
        "c1 spread inside the noise band": near_tie,
        "c2 mixed: Challenger wins return with worse drawdown or IC <= 0":
            spread > 0 and (dd_worse > DD_MARGIN or not (ic_known and ic > 0)),
        "c3 spread driven by one name (drop test flips or collapses)": flips,
    }


def verdict(c: dict) -> str:
    if any(v for k, v in c.items() if k.startswith("b")):
        return "KEEP INCUMBENT"
    if all(v for k, v in c.items() if k.startswith("a")):
        return "ADOPT CHALLENGER (with hardening)"         # unreachable: a5 is judgement
    return "INCONCLUSIVE -> stay on the Incumbent"


# ── report ──────────────────────────────────────────────────────────────────────

def _p(x) -> str:
    return "n/a" if x is None or x != x else f"{x:+.2%}"


def evaluate(period_dir: Path, as_of: date | None = None, final: bool = False) -> str:
    eval_d = evaluation_date(period_dir)
    if final:
        if eval_d is None:
            raise ValueError("--final needs the evaluation date in QUARTER_END_PLAN.md")
        as_of = eval_d
    as_of = as_of or date.today()
    if eval_d and as_of > eval_d:
        as_of = eval_d                     # sessions after the evaluation date are excluded
    inp = load(period_dir, as_of)
    last = pd.Timestamp(inp.closes.index[-1]).date()
    if final and last < eval_d:
        raise ValueError(f"--final: data ends {last}, before the evaluation date {eval_d}")

    spy_ret = float(inp.closes["SPY"].ffill().iloc[-1] / inp.spy_base - 1)
    inc = book_stats("Incumbent", inp.closes, inp.inc_base, spy_ret)
    ch = book_stats("Challenger", inp.closes, inp.ch_base, spy_ret)
    per = pd.concat([inc.per_name, ch.per_name])
    per = per[~per.index.duplicated()]
    ic, p, n = (spearman(inp.entry["value_score"], per) if inp.entry is not None
                else (float("nan"), None, 0))
    drops = drop_contributor_spreads(inc, ch)
    c = clauses(inc, ch, ic, drops)
    v = verdict(c)

    label = "FINAL VERDICT INPUTS" if final else "INTERIM READING -- NOT THE VERDICT"
    L = [f"# {inp.period} evaluation -- {label}",
         "",
         f"Window: {pd.Timestamp(inp.closes.index[0]).date()} -> {last} "
         f"({len(inp.closes)} sessions). Evaluation date: {eval_d or 'not set'}.",
         f"Baselines: first closes {pd.Timestamp(inp.closes.index[0]).date()}, "
         f"SPY {inp.spy_base:.2f}. Carried-forward cells: {inp.carried}.",
         ""]
    if not final:
        L += ["> Interim only. EVAL_CRITERIA.md judges one window on one date; a reading",
              "> before it is information, not a decision, and must not move the date.", ""]
    L += ["## Metrics (EVAL_CRITERIA §1)", "",
          "| | Incumbent | Challenger |", "|---|---|---|",
          f"| Total return | {_p(inc.ret)} | {_p(ch.ret)} |",
          f"| Alpha vs SPY ({_p(spy_ret)}) | {_p(inc.alpha)} | {_p(ch.alpha)} |",
          f"| Max drawdown | {_p(inc.max_dd)} | {_p(ch.max_dd)} |",
          f"| Hit rate: up | {inc.hit_up:.0%} | {ch.hit_up:.0%} |",
          f"| Hit rate: beat SPY | {inc.hit_spy:.0%} | {ch.hit_spy:.0%} |",
          "",
          f"**Relative spread (Challenger - Incumbent): {_p(ch.ret - inc.ret)}**",
          "",
          f"**Value consistency:** rank IC {ic:+.3f}" if ic == ic else
          ("**Value consistency:** not computable (no entry_scores.csv)"
           if inp.entry is None else
           f"**Value consistency:** not computable (n = {n}, or no variation) -- "
           "counts as NOT satisfied"),
          (f"(n = {n}" + (f", p = {p:.3f})" if p is not None else ", p n/a: scipy missing)"))
          if ic == ic else "",
          "",
          "## Drop-one-name test (§2 c3)", ""]
    L += [f"- {k}: spread {_p(s)}" for k, s in drops.items()]
    L += ["", "## Decision clauses (§2)", ""]
    L += [f"- [{'x' if fired else ' '}] {k}" for k, fired in c.items()]
    L += ["", f"## Mechanical verdict: **{v}**", "",
          "ADOPT can never come from this script: clause a5 is a judgement. KEEP fires on",
          "ANY (b) clause; otherwise the result is INCONCLUSIVE, which means stay on the",
          "Incumbent. PAPER ONLY -- no verdict here moves capital.", "",
          "### Per-name returns", "", "| Name | Incumbent | Challenger | Entry value_score |",
          "|---|---|---|---|"]
    for t in sorted(set(inc.per_name.index) | set(ch.per_name.index)):
        vs = (inp.entry["value_score"].get(t) if inp.entry is not None else None)
        L.append(f"| {t} | {_p(inc.per_name.get(t))} | {_p(ch.per_name.get(t))} | "
                 f"{'n/a' if vs is None or vs != vs else f'{vs:.1f}'} |")
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    from forward_test.snapshot import PERIOD
    ap = argparse.ArgumentParser(description="Evaluate a forward-test period.")
    ap.add_argument("--period", default=PERIOD)
    ap.add_argument("--as-of", default=None, help="YYYY-MM-DD (interim readings)")
    ap.add_argument("--final", action="store_true",
                    help="use the period's evaluation date; refuse if data is incomplete")
    ap.add_argument("--write", action="store_true", help="save the report in the period")
    args = ap.parse_args(argv)
    period_dir = ROOT / "forward_test" / args.period
    try:
        rep = evaluate(period_dir,
                       date.fromisoformat(args.as_of) if args.as_of else None,
                       final=args.final)
    except Exception as e:                                          # noqa: BLE001
        print(f"NOT EVALUATED: {type(e).__name__}: {e}")
        return 1
    print(rep)
    if args.write:
        name = ("EVALUATION_INPUTS_FINAL.md" if args.final
                else f"EVALUATION_INTERIM_{date.today():%Y-%m-%d}.md")
        (period_dir / name).write_text(rep, encoding="utf-8")
        print(f"written: {(period_dir / name).relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
