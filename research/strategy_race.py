"""
research/strategy_race.py — the pre-registered strategy race vs the S&P 500.

Implements docs/research/strategy_race_PREREG.md exactly: five fixed contestants built
from point-in-time SEC fundamentals and prices, monthly rebalancing after costs, and
each contestant's gap to SPY split into small caps vs large caps (IWM − SPY), the
current-constituent universe itself (universe EW − IWM) and stock picking
(contestant − universe EW). Verdicts by the pre-registered rule.

PRICES come from research/price_store.py: the real price on each past day for size,
the $3 floor and dollar volume; the total-return close for returns; SPY and IWM from the
same store. Run 1 (2026-10-11) read the main `prices` table, whose close is already
adjusted for LATER splits and dividends: past market caps used information from the
future. That run is superseded (docs/research/strategy_race_RESULT.md).

READ-ONLY: every database is opened with mode=ro; output goes only to
research/output/strategy_race/. Changes nothing in the live system.

    python -m research.strategy_race
    python -m research.strategy_race --end 2026-09-30
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "research" / "output" / "strategy_race"

# ── pre-registered constants (docs/research/strategy_race_PREREG.md) ────────────
# name -> (factor weights, minimum health checks of 6)
CONTESTANTS = {
    "incumbent_like": ({"quality": 0.415, "value": 0.338, "momentum": 0.246}, 4),
    "value": ({"value": 0.70, "quality": 0.30}, 2),
    "quality_defensive": ({"quality": 0.60, "lowvol": 0.40}, 4),
    "momentum": ({"momentum": 1.0}, 2),
}
BLEND = "blend"                            # fixed 25% of each contestant above
NAMES = list(CONTESTANTS) + [BLEND]
N_HOLD, MAX_PER_SECTOR = 15, 4
KEEP_WITHIN = 30                           # a holding stays while ranked in the top 30
COST_PER_SIDE = 0.004                      # 0.8% round trip (TRANSACTION_COSTS)
MIN_PRICE, MIN_DOLLAR_VOL, MIN_MCAP, MAX_MCAP = 3.0, 5e6, 50e6, 10e9
MIN_SCORED, FIRST_YEAR = 300, 2010
T_BAR, N_FOLDS, MAX_DD_SLACK = 2.6, 4, 0.10
STALE_MONTHS = 15                          # an older balance sheet is not "latest"


# ── data ────────────────────────────────────────────────────────────────────────

def _db_file() -> Path:
    from config.settings import DATABASE_URL
    p = Path(DATABASE_URL.replace("sqlite:///", ""))
    return p if p.is_absolute() else ROOT / p


def load_sectors(db: Path) -> dict:
    """ticker -> sector from the main database's stocks table (today's sector)."""
    with closing(sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)) as c:
        st = pd.read_sql("SELECT ticker, sector FROM stocks", c)
    return {r.ticker: r.sector for r in st.itertuples() if isinstance(r.sector, str)}


def market(tickers: list[str], load=None) -> tuple[pd.DataFrame, pd.DataFrame,
                                                   pd.DataFrame, pd.DataFrame]:
    """(real close, total-return close, real volume, SPY/IWM total-return closes) from
    the research price store. Raises ValueError when the store or a benchmark is missing."""
    if load is None:
        from research import price_store
        if not price_store.DB_PATH.exists():
            raise ValueError(f"no research price store at {price_store.DB_PATH} -- run: "
                             f"python -m research.price_store")
        load = price_store.load_matrices
    m = load(list(tickers) + ["SPY", "IWM"])
    adj = m["adj_close"]
    missing = [b for b in ("SPY", "IWM") if b not in adj or adj[b].dropna().empty]
    if missing:
        raise ValueError(f"benchmark prices missing from the price store: {missing}")
    names = [t for t in tickers if t in adj.columns]
    bench = adj[["SPY", "IWM"]].astype(float)
    return m["raw_close"][names], adj[names], m["raw_volume"][names], bench


def with_prior_year(panel: pd.DataFrame) -> pd.DataFrame:
    """Each row gains the same company's ROA, leverage and shares from the period about
    one year earlier (period end 365 ± 45 days before), for the year-over-year checks."""
    p = panel.copy()
    p["roa"] = p["ni_ttm"] / p["total_assets"]
    p["lev"] = p["total_liabilities"] / p["total_assets"]
    p["target"] = p["period_end_date"] - pd.Timedelta(days=365)
    right = (p[["ticker", "period_end_date", "roa", "lev", "shares", "available_date"]]
             .rename(columns={"period_end_date": "target", "roa": "roa_prev",
                              "lev": "lev_prev", "shares": "shares_prev",
                              "available_date": "prev_available"})
             .sort_values("target"))
    out = pd.merge_asof(p.sort_values("target"), right, on="target", by="ticker",
                        direction="nearest", tolerance=pd.Timedelta(days=45))
    return out.drop(columns="target")


# ── scores ──────────────────────────────────────────────────────────────────────

def _row(m: pd.DataFrame, d: pd.Timestamp) -> pd.Series:
    h = m.loc[:d]
    return h.iloc[-1] if len(h) else pd.Series(dtype=float)


def price_features(close: pd.DataFrame, adj: pd.DataFrame, vol: pd.DataFrame) -> dict:
    a = adj.ffill()
    rets = adj.pct_change(fill_method=None)
    return {"close": close.ffill(limit=5),
            "mom": a.shift(21) / a.shift(252) - 1,
            "vol": rets.rolling(252, min_periods=200).std(),
            "dv": (close * vol).rolling(63, min_periods=40).median()}


def health_checks(f: pd.DataFrame, d: pd.Timestamp) -> pd.Series:
    """F-score-lite: 6 of Piotroski's 9 checks the SEC facts allow. A check whose
    inputs are missing (no year-earlier period, not yet filed) counts as failed."""
    prior_ok = f["prev_available"].notna() & (f["prev_available"] <= d)
    checks = pd.DataFrame({
        "roa_pos": f["roa"] > 0,
        "ocf_pos": f["ocf_ttm"] > 0,
        "roa_up": prior_ok & (f["roa"] > f["roa_prev"]),
        "ocf_gt_ni": f["ocf_ttm"] > f["ni_ttm"],
        "lev_down": prior_ok & (f["lev"] < f["lev_prev"]),
        "no_new_shares": prior_ok & (f["shares"] <= f["shares_prev"]),
    }, index=f.index)
    return checks.fillna(False).sum(axis=1)


def scores_on(d: pd.Timestamp, panel: pd.DataFrame, pf: dict,
              max_mcap: float = MAX_MCAP, largest: int | None = None,
              min_dollar_vol: float = MIN_DOLLAR_VOL) -> pd.DataFrame:
    """Every eligible, fully scored name on date d: factor scores (0-1 percentile means)
    and health checks. Fundamentals are used only once filed (available_date <= d).
    `largest` keeps only the N biggest by market cap (ranks are within that pool)."""
    usable = panel[panel["available_date"] <= d]
    if usable.empty:
        return pd.DataFrame()
    latest = (usable.sort_values(["available_date", "period_end_date"])
              .groupby("ticker").tail(1).set_index("ticker"))
    latest = latest[latest["period_end_date"] >= d - pd.DateOffset(months=STALE_MONTHS)]
    f = latest.join(_row(pf["close"], d).rename("price"), how="inner")
    for k in ("mom", "vol", "dv"):
        f[k] = _row(pf[k], d).reindex(f.index)
    f["mcap"] = f["price"] * f["shares"]
    ok = ((f["price"] >= MIN_PRICE) & (f["dv"] >= min_dollar_vol)
          & f["mcap"].between(MIN_MCAP, max_mcap) & (f["total_assets"] > 0))
    f = f[ok.fillna(False)]
    if largest is not None:
        f = f.loc[f["mcap"].nlargest(largest).index]
    raw = pd.DataFrame({
        "ey": f["ni_ttm"] / f["mcap"], "bm": f["total_equity"] / f["mcap"],
        "cfy": f["ocf_ttm"] / f["mcap"],
        "roa": f["roa"], "cfoa": f["ocf_ttm"] / f["total_assets"],
        "lowlev": -f["lev"], "lowacc": -(f["ni_ttm"] - f["ocf_ttm"]) / f["total_assets"],
        "mom": f["mom"], "lowvol": -f["vol"]}, index=f.index)
    raw = raw.replace([np.inf, -np.inf], np.nan).dropna()      # a FULL score only
    if raw.empty:
        return pd.DataFrame()
    r = raw.rank(pct=True)
    out = pd.DataFrame({
        "value": r[["ey", "bm", "cfy"]].mean(axis=1),
        "quality": r[["roa", "cfoa", "lowlev", "lowacc"]].mean(axis=1),
        "momentum": r["mom"], "lowvol": r["lowvol"],
        "checks": health_checks(f.loc[raw.index], d)}, index=raw.index)
    return out


def pick(scores: pd.DataFrame, weights: dict, min_checks: int, sectors: dict,
         held: set | frozenset = frozenset(), n: int = N_HOLD,
         per_sector: int = MAX_PER_SECTOR, keep_within: int = KEEP_WITHIN) -> list[str]:
    """n names by the weighted score among those passing the health gate, at most
    `per_sector` from any one sector (unknown sector counts as one sector).

    Holding rule: a current holding is kept while it still passes the gate and ranks
    in the top `keep_within`; the free slots are filled best-first. Without it a
    monthly top-15 churns ~11x a year, ~4%/yr of costs (synthetic test, before any
    real run); the live system damps turnover the same way."""
    s = scores[scores["checks"] >= min_checks]
    comp = sum(w * s[k] for k, w in weights.items()).sort_values(ascending=False)
    keep = [t for t in comp.index[:keep_within] if t in held]
    chosen, used = [], {}
    for t in keep + [t for t in comp.index if t not in keep]:
        sec = sectors.get(t, "Unknown")
        if used.get(sec, 0) >= per_sector:
            continue
        chosen.append(t)
        used[sec] = used.get(sec, 0) + 1
        if len(chosen) == n:
            break
    return chosen


# ── simulation ──────────────────────────────────────────────────────────────────

def simulate(targets: list[dict], prices: list[pd.Series], every: int = 1) -> pd.DataFrame:
    """Monthly net returns. targets[i] = {ticker: weight} decided at rebalance i (sum
    <= 1, the rest cash at 0%); prices[i] = adjusted closes at rebalance i. Trades only
    every `every` months (1 monthly, 3 quarterly); costs COST_PER_SIDE on each weight
    traded, including the first buy. Returns one row per holding month."""
    held: dict = {}
    rows = []
    for i in range(len(prices) - 1):
        traded = 0.0
        if i % every == 0:
            tgt = targets[i]
            traded = sum(abs(tgt.get(t, 0.0) - held.get(t, 0.0)) for t in set(tgt) | set(held))
            held = dict(tgt)
        cost = COST_PER_SIDE * traded
        p0, p1 = prices[i], prices[i + 1]
        r = {t: (p1.get(t, np.nan) / p0.get(t, np.nan) - 1) for t in held}
        r = {t: (0.0 if not np.isfinite(v) else v) for t, v in r.items()}
        gross = sum(w * r[t] for t, w in held.items())
        net = (1 - cost) * (1 + gross) - 1
        rows.append({"turnover": traded, "cost": cost, "gross": gross, "net": net,
                     "n_held": len(held)})
        g1 = 1 + gross
        held = {t: w * (1 + r[t]) / g1 for t, w in held.items()} if g1 > 0 else {}
    return pd.DataFrame(rows)


def blend(portfolios: list[dict]) -> dict:
    out: dict = {}
    for p in portfolios:
        for t, w in p.items():
            out[t] = out.get(t, 0.0) + w / len(portfolios)
    return out


# ── statistics and verdicts ─────────────────────────────────────────────────────

def stats(r: pd.Series) -> dict:
    n = len(r)
    eq = pd.concat([pd.Series([1.0]), (1 + r).cumprod()], ignore_index=True)
    k = max(1, math.ceil(0.05 * n))
    dn = np.sqrt((np.minimum(r, 0) ** 2).mean()) * math.sqrt(12)
    vol = r.std() * math.sqrt(12)
    return {"months": n, "cagr": float((1 + r).prod() ** (12 / n) - 1),
            "vol": float(vol), "sharpe": float(r.mean() * 12 / vol) if vol else np.nan,
            "sortino": float(r.mean() * 12 / dn) if dn else np.nan,
            "max_dd": float((eq / eq.cummax() - 1).min()),
            "best_month": float(r.max()), "worst_month": float(r.min()),
            "cvar95": float(r.nsmallest(k).mean())}


def active(r: pd.Series, b: pd.Series) -> dict:
    a = (r - b).dropna()
    n = len(a)
    sd = a.std()
    folds = [float(x.mean()) for x in np.array_split(a.to_numpy(), N_FOLDS)]
    return {"active": float(a.mean() * 12), "te": float(sd * math.sqrt(12)),
            "ir": float(a.mean() * 12 / (sd * math.sqrt(12))) if sd else np.nan,
            "t": float(a.mean() / (sd / math.sqrt(n))) if sd else np.nan,
            "folds_pos": int(sum(x > 0 for x in folds)), "folds": folds, "n": n}


def verdict(a: dict, dd: float | None = None, dd_bench: float | None = None) -> str:
    """BEATS / PROMISING / DOES NOT BEAT, by the pre-registered rule. The drawdown
    condition applies only against SPY (pass dd and dd_bench)."""
    pos = a["active"] > 0 and a["folds_pos"] >= 3
    dd_ok = dd is None or dd >= dd_bench - MAX_DD_SLACK
    if pos and np.isfinite(a["t"]) and a["t"] >= T_BAR and dd_ok:
        return "BEATS"
    if pos:
        return "PROMISING"
    return "DOES NOT BEAT"


@dataclass
class RaceResult:
    monthly: pd.DataFrame                    # date x series, net monthly returns
    quarterly: pd.DataFrame                  # same contestants, quarterly trading
    detail: dict                             # name -> simulate() frame
    coverage: pd.DataFrame
    start: pd.Timestamp | None
    table: pd.DataFrame = field(default_factory=pd.DataFrame)


def race(panel: pd.DataFrame, close: pd.DataFrame, adj: pd.DataFrame, vol: pd.DataFrame,
         sectors: dict, bench: pd.DataFrame, end: pd.Timestamp | None = None) -> RaceResult:
    # a year of history before FIRST_YEAR feeds momentum and volatility; older rows
    # only cost memory
    since = pd.Timestamp(f"{FIRST_YEAR - 1}-01-01")
    close, adj, vol = (m.loc[since:].astype("float32") for m in (close, adj, vol))
    pf = price_features(close, adj, vol)
    panel = with_prior_year(panel)
    idx = adj.index[adj.index.year >= FIRST_YEAR]
    if end is not None:
        idx = idx[idx <= end]
    dates = list(pd.Series(idx, index=idx).groupby(idx.to_period("M")).first())
    cov, scored = [], {}
    start = None
    for k, d in enumerate(dates):
        if k % 24 == 0:
            print(f"  ... {d:%Y-%m} ({k + 1}/{len(dates)})", flush=True)
        s = scores_on(d, panel, pf)
        cov.append({"date": d, "scored": len(s)})
        if start is None and len(s) >= MIN_SCORED:
            start = d
        if start is not None:
            scored[d] = s
    if start is None:
        return RaceResult(pd.DataFrame(), pd.DataFrame(), {}, pd.DataFrame(cov), None)
    dates = [d for d in dates if d >= start]
    a = adj.ffill()
    prices = [_row(a, d) for d in dates]
    tg = {n: [] for n in NAMES}
    ew = []
    for d in dates:
        s = scored[d]
        ports = {}
        for name, (w, gate) in CONTESTANTS.items():
            held = set(tg[name][-1]) if tg[name] else set()
            ch = pick(s, w, gate, sectors, held) if len(s) else []
            ports[name] = {t: 1 / N_HOLD for t in ch}     # unfilled slots stay cash
            tg[name].append(ports[name])
        tg[BLEND].append(blend(list(ports.values())))
        ew.append({t: 1 / len(s) for t in s.index} if len(s) else {})
    months = pd.DatetimeIndex(dates[:-1])
    detail = {n: simulate(tg[n], prices).set_index(months) for n in NAMES}
    quarterly = pd.DataFrame({n: simulate(tg[n], prices, every=3)["net"].to_numpy()
                              for n in NAMES}, index=months)
    b = bench.ffill()
    bp = [_row(b, d) for d in dates]
    monthly = pd.DataFrame({n: detail[n]["net"] for n in NAMES}, index=months)
    monthly["universe_ew"] = simulate(ew, prices)["gross"].to_numpy()
    for t in ("SPY", "IWM"):
        monthly[t] = [bp[i + 1][t] / bp[i][t] - 1 for i in range(len(dates) - 1)]
    res = RaceResult(monthly, quarterly, detail, pd.DataFrame(cov), start)
    res.table = summarize(res)
    return res


def summarize(res: RaceResult) -> pd.DataFrame:
    m = res.monthly
    base = {k: stats(m[k]) for k in ("SPY", "IWM", "universe_ew")}
    rows = []
    for n in NAMES:
        st = stats(m[n])
        vs_spy, vs_ew = active(m[n], m["SPY"]), active(m[n], m["universe_ew"])
        d = res.detail[n]
        rows.append({
            "contestant": n, **st,
            "turnover_yr": float(d["turnover"].mean() * 12),
            "cost_drag_yr": float(d["cost"].mean() * 12),
            "avg_held": float(d["n_held"].mean()),
            "vs_spy_active": vs_spy["active"], "vs_spy_ir": vs_spy["ir"],
            "vs_spy_t": vs_spy["t"], "vs_spy_folds_pos": vs_spy["folds_pos"],
            "vs_spy": verdict(vs_spy, st["max_dd"], base["SPY"]["max_dd"]),
            "vs_ew_active": vs_ew["active"], "vs_ew_ir": vs_ew["ir"],
            "vs_ew_t": vs_ew["t"], "vs_ew_folds_pos": vs_ew["folds_pos"],
            "vs_ew": verdict(vs_ew),
            # CAGR gap to SPY in three parts that add up exactly
            "gap_to_spy": st["cagr"] - base["SPY"]["cagr"],
            "part_smallcap": base["IWM"]["cagr"] - base["SPY"]["cagr"],
            "part_universe": base["universe_ew"]["cagr"] - base["IWM"]["cagr"],
            "part_picking": st["cagr"] - base["universe_ew"]["cagr"],
            "quarterly_cagr": stats(res.quarterly[n])["cagr"],
        })
    return pd.DataFrame(rows).set_index("contestant")


def environments(res: RaceResult, bench: pd.DataFrame) -> pd.DataFrame:
    """Descriptive only: mean monthly active return vs SPY when SPY was above vs below
    its 200-day average at the start of the month."""
    spy = bench["SPY"].ffill()
    ma = spy.rolling(200, min_periods=150).mean()
    up = [(_row(spy, d) > _row(ma, d)) for d in res.monthly.index]
    up = pd.Series(up, index=res.monthly.index)
    rows = []
    for n in NAMES + ["universe_ew", "IWM"]:
        a = res.monthly[n] - res.monthly["SPY"]
        rows.append({"series": n,
                     "above_200d_mean_active": float(a[up].mean()), "n_above": int(up.sum()),
                     "below_200d_mean_active": float(a[~up].mean()),
                     "n_below": int((~up).sum())})
    return pd.DataFrame(rows).set_index("series")


# ── report ──────────────────────────────────────────────────────────────────────

def _pct(x) -> str:
    return "n/a" if x is None or not np.isfinite(x) else f"{x:+.1%}"


def write_report(res: RaceResult, env: pd.DataFrame, out_dir: Path = OUT_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    res.monthly.to_csv(out_dir / "monthly_returns.csv")
    res.table.to_csv(out_dir / "summary.csv")
    res.coverage.to_csv(out_dir / "coverage_by_date.csv", index=False)
    env.to_csv(out_dir / "environments.csv")
    t, m = res.table, res.monthly
    spy, iwm, ew = (stats(m[k]) for k in ("SPY", "IWM", "universe_ew"))
    lines = [
        "# Strategy race vs the S&P 500 — result",
        "",
        "Prices: research store (real price on each past day; "
        "research/price_store.py). "
        f"Pre-registered in `docs/research/strategy_race_PREREG.md`. Window "
        f"{m.index[0]:%Y-%m} to {m.index[-1]:%Y-%m} ({len(m)} months, first month with "
        f">= {MIN_SCORED} scored names). Costs 0.8% round trip included.",
        "",
        "## Verdicts",
        "",
        "| Contestant | vs S&P 500 | stock picking (vs universe EW) | compounded vs SPY |",
        "|---|---|---|---|",
        *[f"| {n} | {r.vs_spy} | {r.vs_ew} | {_pct(r.gap_to_spy)}/yr |"
          for n, r in t.iterrows()],
        "",
        "The verdicts follow the pre-registered rule (average monthly active return). "
        "The last column is what an account actually compounded relative to SPY.",
        "",
        "## Returns and risk (net of costs)",
        "",
        "| | CAGR | vol | Sharpe | Sortino | max DD | worst month | best month | "
        "CVaR95 | turnover/yr | cost/yr | quarterly CAGR |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
        *[f"| {n} | {_pct(r.cagr)} | {r.vol:.1%} | {r.sharpe:.2f} | {r.sortino:.2f} | "
          f"{_pct(r.max_dd)} | {_pct(r.worst_month)} | {_pct(r.best_month)} | "
          f"{_pct(r.cvar95)} | {r.turnover_yr:.1f}x | {r.cost_drag_yr:.1%} | "
          f"{_pct(r.quarterly_cagr)} |" for n, r in t.iterrows()],
        *[f"| {k} | {_pct(s['cagr'])} | {s['vol']:.1%} | {s['sharpe']:.2f} | "
          f"{s['sortino']:.2f} | {_pct(s['max_dd'])} | {_pct(s['worst_month'])} | "
          f"{_pct(s['best_month'])} | {_pct(s['cvar95'])} | | | |"
          for k, s in (("SPY", spy), ("IWM", iwm), ("universe EW (no costs)", ew))],
        "",
        "## Against the S&P 500 and against the universe",
        "",
        "| | active vs SPY /yr | IR | t | folds + (of 4) | active vs universe /yr | IR | "
        "t | folds + |",
        "|---|---|---|---|---|---|---|---|---|",
        *[f"| {n} | {_pct(r.vs_spy_active)} | {r.vs_spy_ir:.2f} | {r.vs_spy_t:.2f} | "
          f"{r.vs_spy_folds_pos} | {_pct(r.vs_ew_active)} | {r.vs_ew_ir:.2f} | "
          f"{r.vs_ew_t:.2f} | {r.vs_ew_folds_pos} |" for n, r in t.iterrows()],
        "",
        f"n = {len(m)} non-overlapping monthly returns per series (effective n = "
        f"{len(m)} months, ~{len(m) / 12:.0f} years). BEATS needs t >= {T_BAR}.",
        "",
        "## Where the gap to the S&P 500 comes from (CAGR, adds up exactly)",
        "",
        "| | gap to SPY | = small caps vs large (IWM − SPY) | + universe artifact "
        "(EW − IWM) | + stock picking (− EW) |",
        "|---|---|---|---|---|",
        *[f"| {n} | {_pct(r.gap_to_spy)} | {_pct(r.part_smallcap)} | "
          f"{_pct(r.part_universe)} | {_pct(r.part_picking)} |" for n, r in t.iterrows()],
        "",
        "## Environments (descriptive only, no verdict)",
        "",
        "Mean monthly return vs SPY when SPY started the month above / below its "
        "200-day average.",
        "",
        "| | above (n) | below (n) |",
        "|---|---|---|",
        *[f"| {n} | {_pct(r.above_200d_mean_active)} ({r.n_above}) | "
          f"{_pct(r.below_200d_mean_active)} ({r.n_below}) |" for n, r in env.iterrows()],
        "",
        "## Limits (from the pre-registration)",
        "",
        "Current-constituent survivorship (the universe-artifact column is its size here); "
        "thin SEC coverage before ~2012 (coverage_by_date.csv); today's sectors; style "
        "approximations, not the production model; one history.",
        "",
        "No action taken.",
    ]
    p = out_dir / "REPORT.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Pre-registered strategy race vs SPY.")
    ap.add_argument("--end", default=None, help="last date used (YYYY-MM-DD)")
    args = ap.parse_args(argv)
    db = _db_file()
    if not db.exists():
        print(f"FAILED: database not found at {db}")
        return 1
    from data import sec_xbrl
    if not sec_xbrl.DB_PATH.exists():
        print(f"FAILED: no SEC database at {sec_xbrl.DB_PATH} -- run: python -m data.sec_xbrl")
        return 1
    from data.universe_builder import get_universe
    print("loading prices (research store) and SEC fundamentals ...", flush=True)
    try:
        close, adj, vol, bench = market(get_universe())
    except ValueError as e:
        print(f"FAILED: {e}")
        return 1
    universe = list(adj.columns)
    if not universe:
        print("FAILED: no universe ticker has prices in the research store")
        return 1
    sectors = load_sectors(db)
    panel = sec_xbrl.cached_panel()
    panel = panel[panel["ticker"].isin(universe)]
    print(f"{len(universe)} tickers, {panel['ticker'].nunique()} with SEC fundamentals",
          flush=True)
    res = race(panel, close, adj, vol, sectors, bench,
               pd.Timestamp(args.end) if args.end else None)
    if res.monthly.empty:
        print(f"FAILED: no month reached {MIN_SCORED} scored names -- "
              f"see coverage in the output folder")
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        res.coverage.to_csv(OUT_DIR / "coverage_by_date.csv", index=False)
        return 1
    p = write_report(res, environments(res, bench))
    for n, r in res.table.iterrows():
        print(f"{n:<18} vs SPY {r.vs_spy:<14} picking {r.vs_ew:<14} "
              f"CAGR {r.cagr:+.1%}  gap to SPY {r.gap_to_spy:+.1%}")
    print(f"report: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
