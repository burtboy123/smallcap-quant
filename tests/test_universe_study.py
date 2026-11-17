"""
tests/test_universe_study.py — research/universe_study.py (wider pools vs SPY).

Synthetic markets only. The verdict is on compounded growth; one test pins that a
volatile series with a higher monthly AVERAGE but lower growth does not pass (the flaw
in the strategy race's rule).
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from research import strategy_race as sr  # noqa: E402
from research import universe_study as us  # noqa: E402


def market(n=240, years=(2009, 2014), value_edge=0.0, seed=3, edge_from=None):
    """n stocks of very different sizes; the smaller half are 'IWM members'. Each has a
    fixed earnings yield; value_edge adds monthly drift by yield rank (0 = noise), from
    `edge_from` on when given (noise before it)."""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range(f"{years[0]}-01-01", f"{years[1]}-12-31")
    tick = [f"T{i:03d}" for i in range(n)]
    shares = np.exp(rng.uniform(np.log(5e6), np.log(2e9), n))      # $100m .. $40bn
    ey = rng.uniform(-0.05, 0.20, n)
    rank = pd.Series(ey).rank(pct=True).to_numpy()
    on = np.ones(len(days)) if edge_from is None else (days >= edge_from).astype(float)
    drift = 0.0004 + value_edge / 21 * np.outer(on, rank - 0.5)
    rets = rng.normal(drift, 0.02, (len(days), n))
    adj = pd.DataFrame(20 * np.exp(np.cumsum(np.log1p(rets), axis=0)), index=days,
                       columns=tick)
    close, vol = adj.copy(), pd.DataFrame(1e6, index=days, columns=tick)
    rows = []
    for i, t in enumerate(tick):
        for end in pd.date_range(f"{years[0] - 1}-03-31", f"{years[1]}-12-31", freq="QE"):
            px = float(close[t].asof(end)) if end >= days[0] else 20.0
            mcap = px * shares[i]
            ni, assets = ey[i] * mcap, 3 * px * shares[i]
            rows.append({"ticker": t, "available_date": end + pd.Timedelta(days=45),
                         "period_end_date": end, "source": "sec", "ni_ttm": ni,
                         "ocf_ttm": ni * 1.1 + 1e5, "ni_q": [], "total_assets": assets,
                         "total_equity": assets * 0.5, "total_liabilities": assets * 0.5,
                         "cash": assets * 0.1, "shares": shares[i]})
    small = [t for t, s in zip(tick, shares) if s * 20 < 5e9]
    members = {"iwm": small, "iwv": tick}
    sectors = {t: f"S{i % 8}" for i, t in enumerate(tick)}
    spy = adj.mean(axis=1)
    bench = pd.DataFrame({"SPY": spy, "IWM": adj[small].mean(axis=1)})
    return pd.DataFrame(rows), close, adj, vol, members, sectors, bench


@pytest.fixture
def small_pools(monkeypatch):
    """Pools and thresholds scaled to a 240-stock market."""
    monkeypatch.setattr(sr, "MIN_SCORED", 60)
    monkeypatch.setattr(us, "POOLS", {"small": ("iwm", sr.MAX_MCAP, None),
                                      "wide": ("iwv", math.inf, None),
                                      "large": ("iwv", math.inf, 80)})


# ── the two structures ──────────────────────────────────────────────────────────

def test_core_satellite_rebalances_and_charges_the_drift():
    out = us.core_satellite(pd.Series([0.10]), pd.Series([0.0]))
    r = 0.7 * 0.10
    drift = abs(0.7 * 1.10 / (1 + r) - 0.7)
    assert out.iloc[0] == pytest.approx((1 + r) * (1 - drift * (0.004 + 0.0005)) - 1)
    flat = us.core_satellite(pd.Series([0.05]), pd.Series([0.05]))
    assert flat.iloc[0] == pytest.approx(0.05)                  # no drift, no cost


def test_hedged_is_spy_plus_picking_minus_financing():
    out = us.hedged(pd.Series([0.01]), pd.Series([0.03]), pd.Series([0.02]))
    assert out.iloc[0] == pytest.approx(0.01 + 0.03 - 0.02 - 0.02 / 12)


# ── the verdict ─────────────────────────────────────────────────────────────────

def test_a_higher_monthly_average_with_lower_growth_does_not_pass():
    spy = pd.Series([0.01] * 96)
    bumpy = pd.Series([0.20, -0.16] * 48)          # average +2%/month, grows ~5%/yr
    assert bumpy.mean() > spy.mean()
    assert us._cagr(bumpy) < us._cagr(spy)
    v = us.verdict(bumpy, spy)
    assert v["verdict"] == "DOES NOT BEAT" and v["cagr_gap"] < 0
    # the strategy race's rule (average monthly active) would have called it PROMISING
    assert sr.verdict(sr.active(bumpy, spy)) == "PROMISING"


def test_ahead_in_three_periods_but_behind_overall_does_not_pass():
    """Steadily ahead for three quarters of the time, then a wild last quarter whose
    monthly AVERAGE is high but which loses money: behind SPY overall, so no pass even
    though 3 of 4 periods are ahead and the overall monthly average is ahead."""
    spy = pd.Series([0.01] * 96)
    r = pd.Series([0.013] * 72 + [0.40, -0.35] * 12)
    assert r.mean() > spy.mean() and us._cagr(r) < us._cagr(spy)
    v = us.verdict(r, spy)
    assert v["folds_cagr_ahead"] == 3
    assert v["verdict"] == "DOES NOT BEAT"


def test_verdict_levels():
    rng = np.random.default_rng(0)
    spy = pd.Series(rng.normal(0.008, 0.04, 120))
    strong = spy + 0.01 + rng.normal(0, 0.005, 120)
    weak = spy + 0.002 + rng.normal(0, 0.03, 120)
    assert us.verdict(strong, spy)["verdict"] == "BEATS"
    assert us.verdict(weak, spy)["verdict"] in ("PROMISING", "DOES NOT BEAT")
    assert us.verdict(spy - 0.003, spy)["verdict"] == "DOES NOT BEAT"
    # far ahead, highly significant, ahead in every period -- but one -35% month: a
    # drawdown more than 10 pp deeper than SPY's keeps it out of BEATS
    crashy = spy + 0.02 + rng.normal(0, 0.003, 120)
    crashy.iloc[60] = -0.35
    v = us.verdict(crashy, spy)
    assert v["cagr_gap"] > 0 and v["t"] >= sr.T_BAR and v["folds_cagr_ahead"] == 4
    assert sr.stats(crashy)["max_dd"] < sr.stats(spy)["max_dd"] - sr.MAX_DD_SLACK
    assert v["verdict"] == "PROMISING"


# ── pools ───────────────────────────────────────────────────────────────────────

def test_pools_hold_the_right_companies(small_pools):
    panel, close, adj, vol, members, sectors, bench = market()
    pf = sr.price_features(close, adj, vol)
    full = sr.with_prior_year(panel)
    panels = {n: full[full["ticker"].isin(set(members[src]))]
              for n, (src, _, _) in us.POOLS.items()}
    d = pd.Timestamp("2012-06-01")
    s = us.pool_scores(d, panels, pf)
    assert set(s["small"].index) <= set(members["iwm"])
    assert len(s["large"]) == 80 and len(s["wide"]) > 80
    shares = panel.groupby("ticker")["shares"].first()
    mcap = sr._row(close, d) * shares
    biggest = set(mcap[s["wide"].index].nlargest(80).index)
    assert set(s["large"].index) == biggest                  # the 80 largest, by size
    # ranks are within each pool: the top value score is 1.0 in both
    assert s["large"]["momentum"].max() == pytest.approx(1.0)


# ── end to end ──────────────────────────────────────────────────────────────────

def test_a_real_edge_is_found_and_hedging_keeps_it(small_pools):
    res = us.study(*market(value_edge=0.06))
    t = res.table
    assert res.start is not None and len(res.monthly) >= 36
    for n in ("small", "wide", "large"):
        assert t.loc[n, "part_picking"] > 0.05
    assert t.loc["hedged", "verdict"] == "BEATS"
    assert t.loc["wide", "verdict"] == "BEATS"


def test_pure_noise_produces_no_winner(small_pools):
    res = us.study(*market(value_edge=0.0, seed=11))
    assert not (res.table["verdict"] == "BEATS").any(), res.table[["verdict", "t"]]


def test_the_common_window_waits_for_both_pools(small_pools, monkeypatch):
    panel, close, adj, vol, members, sectors, bench = market()
    late = panel.copy()
    small = late["ticker"].isin(set(members["iwm"]))
    # the small companies' filings only become usable from 2012: the large pool is
    # full from 2010 (it is "the N largest WITH data"), the small pool is not
    late.loc[small, "available_date"] = \
        late.loc[small, "available_date"].clip(lower=pd.Timestamp("2012-01-01"))
    res = us.study(late, close, adj, vol, members, sectors, bench)
    assert res.start >= pd.Timestamp("2012-01-01")
    early = us.study(panel, close, adj, vol, members, sectors, bench)
    assert early.start < res.start


def test_report_lists_every_verdict(small_pools, tmp_path):
    res = us.study(*market(value_edge=0.03))
    p = us.write_report(res, tmp_path)
    text = p.read_text(encoding="utf-8")
    for n, r in res.table.iterrows():
        assert f"| {n} | {r.verdict} |" in text
    assert "## Longer history" in text
    assert text.rstrip().endswith("No action taken.")


def test_main_fails_loudly_without_the_price_store(monkeypatch, tmp_path, capsys):
    from research import price_store
    monkeypatch.setattr(price_store, "DB_PATH", tmp_path / "missing.db")
    assert us.main([]) == 1
    assert "python -m research.price_store" in capsys.readouterr().out


def test_the_command_runs_end_to_end(small_pools, monkeypatch, tmp_path, capsys):
    from data import sec_xbrl
    from research import price_store
    panel, close, adj, vol, members, sectors, bench = market(value_edge=0.03)
    a = adj.copy()
    a["SPY"], a["IWM"] = bench["SPY"], bench["IWM"]
    mats = {"raw_close": close, "adj_close": a, "raw_volume": vol, "sectors": sectors,
            "iwm": members["iwm"], "iwv": members["iwv"]}
    for f in ("prices.db", "sec.db"):
        (tmp_path / f).write_bytes(b"")
    monkeypatch.setattr(price_store, "DB_PATH", tmp_path / "prices.db")
    monkeypatch.setattr(price_store, "load_matrices", lambda: mats)
    monkeypatch.setattr(sec_xbrl, "DB_PATH", tmp_path / "sec.db")
    monkeypatch.setattr(sec_xbrl, "cached_panel", lambda: panel)
    monkeypatch.setattr(us, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(us.write_report, "__defaults__", (tmp_path / "out",))
    assert us.main([]) == 0
    out = capsys.readouterr().out
    for n in us.NAMES:
        assert out.count(f"{n:<8} ") >= 1
    assert (tmp_path / "out" / "REPORT.md").exists()
