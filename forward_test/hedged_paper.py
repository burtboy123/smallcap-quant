"""
forward_test/hedged_paper.py — paper forward test of the hedged structure.

Rules: docs/research/hedged_paper_PREREG.md (fixed before any forward result). Hold the
S&P 500, add the forward test's small-cap picks, short the Russell 2000 (IWM): the
small-cap drag cancels and what is left is the picking. PAPER ONLY: nothing is traded.

    hedged return = SPY + (portfolio − IWM) − 2%/yr financing     (daily)

Inputs: the active period's equity_curves.csv (portfolio and SPY, as the forward test
records them) and IWM raw daily closes from yfinance, on the same price-only basis
(auto_adjust=False, like forward_test/snapshot.py). IWM closes are cached; the
computation can run any day and backfills: the book was fixed before the start and
every close is a fact of its own day, so nothing here uses later information.

Writes forward_test/hedged_paper/<period>.csv. Reads the forward test, never writes it.

    python -m forward_test.hedged_paper
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FT_ROOT = ROOT / "forward_test"
OUT_DIR = FT_ROOT / "hedged_paper"
FINANCING = 0.02                      # per year (pre-registered estimate)
EVAL_DATE = date(2027, 9, 30)         # pre-registered: on/after, ~250 trading days
DD_SLACK = 0.10


def load_curve(period_dir: Path) -> pd.DataFrame:
    """date, portfolio index, SPY index from the forward test's equity curve."""
    c = pd.read_csv(period_dir / "equity_curves.csv", parse_dates=["date"])
    return (c[["date", "incumbent_index", "spy_index"]]
            .rename(columns={"incumbent_index": "portfolio", "spy_index": "spy"})
            .sort_values("date").reset_index(drop=True))


def fetch_iwm(start: date, end: date, cache: Path | None = None, fetch=None) -> pd.Series:
    """IWM raw daily closes for [start, end], cached in OUT_DIR/iwm.csv. Raises
    RuntimeError when they cannot be had."""
    cache = cache or OUT_DIR / "iwm.csv"
    have = (pd.read_csv(cache, parse_dates=["date"]).set_index("date")["close"]
            if cache.exists() else pd.Series(dtype=float))
    if len(have) and have.index.min().date() <= start and have.index.max().date() >= end:
        return have
    if fetch is None:
        def fetch(a, b):
            import yfinance as yf
            d = yf.download(["IWM"], start=a.isoformat(),
                            end=(b + timedelta(days=1)).isoformat(),
                            auto_adjust=False, progress=False)["Close"]
            return d["IWM"] if isinstance(d, pd.DataFrame) else d
    got = fetch(start, end)
    if got is None or len(got) == 0:
        raise RuntimeError("IWM closes could not be downloaded (yfinance)")
    got = pd.Series(got.to_numpy(dtype=float),
                    index=pd.DatetimeIndex(pd.to_datetime(got.index)).tz_localize(None),
                    name="close").dropna()
    keep = have[~have.index.isin(got.index)]
    merged = (pd.concat([keep, got]) if len(keep) else got).sort_index()
    cache.parent.mkdir(parents=True, exist_ok=True)
    merged.rename_axis("date").rename("close").to_csv(cache)
    return merged


def hedged_curve(curve: pd.DataFrame, iwm: pd.Series,
                 financing: float = FINANCING) -> pd.DataFrame:
    """Daily hedged index on the forward test's dates. Stops at the last date with an
    IWM close (never fills a missing close)."""
    c = curve.set_index("date")
    c["iwm"] = iwm.reindex(c.index)
    c = c.loc[: c["iwm"].last_valid_index()] if c["iwm"].notna().any() else c.iloc[:0]
    if c["iwm"].isna().any():
        raise ValueError(f"IWM close missing on {list(c.index[c['iwm'].isna()].date)}")
    r = c[["portfolio", "spy", "iwm"]].pct_change().fillna(0.0)
    daily = r["spy"] + (r["portfolio"] - r["iwm"]) - financing / 252
    daily.iloc[0] = 0.0                              # day 1 is the baseline
    out = pd.DataFrame({"hedged_index": (1 + daily).cumprod(),
                        "spy_index": c["spy"] / c["spy"].iloc[0],
                        "portfolio_index": c["portfolio"] / c["portfolio"].iloc[0],
                        "iwm_index": c["iwm"] / c["iwm"].iloc[0]}, index=c.index)
    out["hedged_ret"] = out["hedged_index"] - 1
    out["spy_ret"] = out["spy_index"] - 1
    out["hedged_drawdown"] = out["hedged_index"] / out["hedged_index"].cummax() - 1
    return out.rename_axis("date").reset_index()


def reading(h: pd.DataFrame) -> dict:
    """The pre-registered evaluation numbers. Before EVAL_DATE they are information,
    not a verdict."""
    def stats(idx: pd.Series) -> dict:
        r = idx.pct_change().dropna()
        days = len(r)
        sd = r.std()
        return {"total": float(idx.iloc[-1] / idx.iloc[0] - 1),
                "cagr": float((idx.iloc[-1] / idx.iloc[0]) ** (252 / days) - 1) if days else 0.0,
                "sharpe": float(r.mean() / sd * math.sqrt(252)) if days > 1 and sd > 0 else np.nan,
                "max_dd": float((idx / idx.cummax() - 1).min())}
    hs, ss = stats(h["hedged_index"]), stats(h["spy_index"])
    last = pd.Timestamp(h["date"].iloc[-1]).date()
    passed = (hs["cagr"] > ss["cagr"] and np.isfinite(hs["sharpe"])
              and np.isfinite(ss["sharpe"]) and hs["sharpe"] >= ss["sharpe"]
              and hs["max_dd"] >= ss["max_dd"] - DD_SLACK)
    verdict = ("PASS" if passed else "NOT SHOWN") if last >= EVAL_DATE else "interim (no verdict)"
    return {"days": len(h) - 1, "last": last, "hedged": hs, "spy": ss, "verdict": verdict,
            "would_pass_today": passed}


def update(period: str, ft_root: Path | None = None, out_dir: Path | None = None,
           fetch=None) -> tuple[pd.DataFrame, dict]:
    ft_root, out_dir = ft_root or FT_ROOT, out_dir or OUT_DIR
    curve = load_curve(ft_root / period)
    start, end = curve["date"].iloc[0].date(), curve["date"].iloc[-1].date()
    iwm = fetch_iwm(start, end, out_dir / "iwm.csv", fetch)
    h = hedged_curve(curve, iwm)
    out_dir.mkdir(parents=True, exist_ok=True)
    h.to_csv(out_dir / f"{period}.csv", index=False, date_format="%Y-%m-%d")
    return h, reading(h)


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="Paper forward test of the hedged structure.") \
        .parse_args(argv)
    from config.settings import ACTIVE_FT_PERIOD
    curve_p = FT_ROOT / ACTIVE_FT_PERIOD / "equity_curves.csv"
    if not curve_p.exists():
        print(f"FAILED: no forward-test curve at {curve_p} -- run: python forward_test/snapshot.py")
        return 1
    try:
        h, r = update(ACTIVE_FT_PERIOD)
    except (RuntimeError, ValueError) as e:
        print(f"FAILED: {e}")
        return 1
    if r["days"] < 1:
        print(f"nothing to compare yet: the forward test has only its start day "
              f"({r['last']}); written {OUT_DIR / (ACTIVE_FT_PERIOD + '.csv')}")
        return 0
    print(f"hedged paper test, {ACTIVE_FT_PERIOD}: {r['days']} trading days to {r['last']}")
    print(f"  hedged {r['hedged']['total']:+.2%}   S&P 500 {r['spy']['total']:+.2%}   "
          f"max drawdown {r['hedged']['max_dd']:+.1%} vs {r['spy']['max_dd']:+.1%}")
    print(f"  {r['verdict']}; evaluation on/after {EVAL_DATE} "
          f"(docs/research/hedged_paper_PREREG.md)")
    print(f"written {OUT_DIR / (ACTIVE_FT_PERIOD + '.csv')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
