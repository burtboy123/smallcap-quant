"""
tests/test_strategy_race.py — research/strategy_race.py (the pre-registered race vs SPY).

Synthetic data only. The two end-to-end tests are the ones that matter: a market where
cheap stocks really do rise more must be found, and a market of pure noise must not
produce a "BEATS" anywhere.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from research import strategy_race as sr  # noqa: E402


# ── synthetic market ────────────────────────────────────────────────────────────

def market(n=360, years=(2009, 2016), value_edge=0.0, seed=7):
    """n stocks, 8 sectors. Each stock has a fixed earnings yield; monthly drift adds
    value_edge x (its yield rank - 0.5). value_edge = 0 is pure noise."""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range(f"{years[0]}-01-01", f"{years[1]}-12-31")
    tick = [f"T{i:03d}" for i in range(n)]
    ey = rng.uniform(-0.05, 0.20, n)                       # earnings yield, fixed
    rank = pd.Series(ey).rank(pct=True).to_numpy()
    daily = 0.0004 + value_edge / 21 * (rank - 0.5)
    rets = rng.normal(daily, 0.02, (len(days), n))
    adj = pd.DataFrame(20 * np.exp(np.cumsum(np.log1p(rets), axis=0)), index=days,
                       columns=tick)
    close = adj.copy()
    vol = pd.DataFrame(1e6, index=days, columns=tick)
    shares = 5e6
    rows = []
    for i, t in enumerate(tick):
        for end in pd.date_range(f"{years[0] - 1}-03-31", f"{years[1]}-12-31", freq="QE"):
            px = float(close[t].asof(end)) if end >= days[0] else 20.0
            mcap = px * shares
            ni = ey[i] * mcap
            assets = 3 * mcap
            rows.append({"ticker": t, "available_date": end + pd.Timedelta(days=45),
                         "period_end_date": end, "source": "sec", "ni_ttm": ni,
                         "ocf_ttm": ni * 1.1 + 1e5, "ni_q": [], "total_assets": assets,
                         "total_equity": assets * 0.5, "total_liabilities": assets * 0.5,
                         "cash": assets * 0.1, "shares": shares})
    panel = pd.DataFrame(rows)
    sectors = {t: f"S{i % 8}" for i, t in enumerate(tick)}
    spy = adj.mean(axis=1) * 1.0
    bench = pd.DataFrame({"SPY": spy, "IWM": spy * 0.999})
    return panel, close, adj, vol, sectors, bench


@pytest.fixture(scope="module")
def planted():
    return sr.race(*market(value_edge=0.06))


@pytest.fixture(scope="module")
def noise():
    return sr.race(*market(value_edge=0.0, seed=11))


# ── end to end ──────────────────────────────────────────────────────────────────

def test_a_real_value_edge_is_found(planted):
    t = planted.table
    assert planted.start is not None and len(planted.monthly) >= 60
    assert t.loc["value", "vs_ew"] == "BEATS"
    assert t.loc["value", "part_picking"] > 0.10       # 6%/month spread planted
    assert (t["vs_ew"] == "BEATS").all()      # every style finds a strong real effect


def test_pure_noise_produces_no_winner(noise):
    t = noise.table
    assert not (t["vs_ew"] == "BEATS").any(), t[["vs_ew", "vs_ew_t", "vs_ew_folds_pos"]]
    assert not (t["vs_spy"] == "BEATS").any()


def test_the_gap_to_spy_adds_up_exactly(planted):
    t = planted.table
    total = t["part_smallcap"] + t["part_universe"] + t["part_picking"]
    assert np.allclose(total, t["gap_to_spy"])


def test_costs_are_charged_and_quarterly_trades_less(planted):
    d = planted.detail["value"]
    assert d["cost"].iloc[0] == pytest.approx(sr.COST_PER_SIDE * 1.0)   # first buy
    assert (d["cost"] > 0).all()
    q = sr.simulate([{"A": 1.0}] * 4, [pd.Series({"A": 1.0})] * 5, every=3)
    assert q["turnover"].tolist() == [1.0, 0.0, 0.0, 0.0]


# ── pieces ──────────────────────────────────────────────────────────────────────

def test_fundamentals_are_used_only_once_filed():
    panel, close, adj, vol, sectors, bench = market(n=40, years=(2009, 2010))
    pf = sr.price_features(close, adj, vol)
    d, q1 = pd.Timestamp("2010-06-01"), pd.Timestamp("2010-03-31")
    without_q1 = sr.with_prior_year(panel[panel["period_end_date"] != q1])
    late = panel.copy()
    late.loc[late["period_end_date"] == q1, "ni_ttm"] *= -50       # garbled numbers
    late.loc[late["period_end_date"] == q1, "available_date"] = d + pd.Timedelta(days=1)
    s_late = sr.scores_on(d, sr.with_prior_year(late), pf).sort_index()
    s_without = sr.scores_on(d, without_q1, pf).sort_index()
    pd.testing.assert_frame_equal(s_late, s_without)               # filed tomorrow: unseen
    late.loc[late["period_end_date"] == q1, "available_date"] = d - pd.Timedelta(days=1)
    s_filed = sr.scores_on(d, sr.with_prior_year(late), pf).sort_index()
    assert not s_filed["value"].equals(s_without["value"])         # filed: used


def test_health_checks_count_and_missing_or_unfiled_prior_fails():
    f = pd.DataFrame({
        "roa": [0.05] * 3, "roa_prev": [0.02, np.nan, 0.02], "ocf_ttm": [10.0] * 3,
        "ni_ttm": [8.0] * 3, "lev": [0.4] * 3, "lev_prev": [0.5, np.nan, 0.5],
        "shares": [100.0] * 3, "shares_prev": [100.0, np.nan, 100.0],
        "prev_available": [pd.Timestamp("2010-01-01"), pd.NaT, pd.Timestamp("2010-07-01")]},
        index=["full", "noprior", "notyet"])
    c = sr.health_checks(f, pd.Timestamp("2010-06-01"))
    # the year-earlier filing exists for "notyet" but was published after the date:
    # all three year-over-year checks fail, exactly as if it did not exist
    assert c["full"] == 6 and c["noprior"] == 3 and c["notyet"] == 3


def test_pick_respects_the_sector_cap_and_the_gate():
    idx = [f"X{i}" for i in range(30)]
    s = pd.DataFrame({"value": np.linspace(1, 0, 30), "quality": 0.5, "momentum": 0.5,
                      "lowvol": 0.5, "checks": [6] * 29 + [0]}, index=idx)
    sectors = {t: ("Energy" if i < 10 else f"S{i}") for i, t in enumerate(idx)}
    chosen = sr.pick(s, {"value": 1.0}, 2, sectors)
    assert len(chosen) == sr.N_HOLD
    assert sum(sectors[t] == "Energy" for t in chosen) == sr.MAX_PER_SECTOR
    s.loc["X0", "checks"] = 1
    assert "X0" not in sr.pick(s, {"value": 1.0}, 2, sectors)      # fails the gate


def test_simulate_returns_and_drift_exactly():
    prices = [pd.Series({"A": 10.0, "B": 10.0}), pd.Series({"A": 11.0, "B": 10.0}),
              pd.Series({"A": 11.0, "B": 10.0})]
    out = sr.simulate([{"A": 0.5, "B": 0.5}] * 2, prices)
    assert out["gross"].iloc[0] == pytest.approx(0.05)
    assert out["net"].iloc[0] == pytest.approx((1 - 0.004) * 1.05 - 1)
    # month 2: drifted to 11/21 and 10/21, back to 50/50 -> trades |0.5-11/21|*2
    assert out["turnover"].iloc[1] == pytest.approx(2 * (11 / 21 - 0.5))
    assert out["gross"].iloc[1] == pytest.approx(0.0)


def test_holding_rule_keeps_a_name_in_the_top_30_only():
    idx = [f"X{i:02d}" for i in range(60)]
    s = pd.DataFrame({"value": np.linspace(1, 0, 60), "quality": 0.5, "momentum": 0.5,
                      "lowvol": 0.5, "checks": 6}, index=idx)
    sectors = {t: f"S{i % 20}" for i, t in enumerate(idx)}
    kept = sr.pick(s, {"value": 1.0}, 2, sectors, held={"X20"})     # ranked 21st
    gone = sr.pick(s, {"value": 1.0}, 2, sectors, held={"X40"})     # ranked 41st
    assert "X20" in kept and len(kept) == sr.N_HOLD and "X14" not in kept
    assert "X40" not in gone and gone == sr.pick(s, {"value": 1.0}, 2, sectors)


def test_blend_is_the_average_portfolio():
    b = sr.blend([{"A": 0.5, "B": 0.5}, {"A": 1.0}])
    assert b == pytest.approx({"A": 0.75, "B": 0.25})


def test_verdict_rules():
    strong = {"active": 0.05, "t": 3.0, "folds_pos": 4}
    assert sr.verdict(strong) == "BEATS"
    assert sr.verdict({**strong, "t": 2.5}) == "PROMISING"           # Bonferroni bar
    assert sr.verdict({**strong, "folds_pos": 2}) == "DOES NOT BEAT"
    assert sr.verdict({**strong, "active": -0.01}) == "DOES NOT BEAT"
    assert sr.verdict(strong, dd=-0.40, dd_bench=-0.25) == "PROMISING"   # drawdown
    assert sr.verdict(strong, dd=-0.30, dd_bench=-0.25) == "BEATS"


def _matrices(close, adj, vol, bench, with_bench=True):
    """What research.price_store.load_matrices returns, built from a synthetic market."""
    a = adj.copy()
    if with_bench:
        a["SPY"], a["IWM"] = bench["SPY"], bench["IWM"]
    return {"raw_close": close, "adj_close": a, "raw_volume": vol, "sectors": {},
            "iwm": list(close.columns), "iwv": list(close.columns)}


def test_market_reads_the_store_and_needs_both_benchmarks():
    panel, close, adj, vol, sectors, bench = market(n=20, years=(2009, 2010))
    c, a, v, bm = sr.market(list(close.columns) + ["GONE"],
                            load=lambda t: _matrices(close, adj, vol, bench))
    assert list(a.columns) == list(close.columns)          # unknown ticker dropped
    assert list(bm.columns) == ["SPY", "IWM"]
    with pytest.raises(ValueError, match="benchmark"):
        sr.market(list(close.columns),
                  load=lambda t: _matrices(close, adj, vol, bench, with_bench=False))


def test_main_fails_loudly_without_the_database(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sr, "_db_file", lambda: tmp_path / "missing.db")
    assert sr.main([]) == 1
    assert "FAILED" in capsys.readouterr().out


def _wire(monkeypatch, tmp_path, panel, sectors, matrices):
    """main() against temp files: a stocks table, an SEC panel, a price store."""
    import sqlite3
    from data import sec_xbrl
    import data.universe_builder as ub
    from research import price_store
    db = tmp_path / "q.db"
    with sqlite3.connect(db) as c:
        pd.DataFrame({"ticker": list(sectors), "sector": list(sectors.values())}) \
            .to_sql("stocks", c, index=False)
    for f in ("sec.db", "prices.db"):
        (tmp_path / f).write_bytes(b"")
    monkeypatch.setattr(sr, "_db_file", lambda: db)
    monkeypatch.setattr(sec_xbrl, "DB_PATH", tmp_path / "sec.db")
    monkeypatch.setattr(sec_xbrl, "cached_panel", lambda: panel)
    monkeypatch.setattr(ub, "get_universe", lambda: list(sectors))
    monkeypatch.setattr(price_store, "DB_PATH", tmp_path / "prices.db")
    monkeypatch.setattr(price_store, "load_matrices", lambda tickers: matrices)
    monkeypatch.setattr(sr, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(sr.write_report, "__defaults__", (tmp_path / "out",))


def test_the_command_runs_end_to_end(monkeypatch, tmp_path, capsys):
    """main(): stocks table, SEC panel and the research price store in; REPORT.md with
    every verdict out; exit 0."""
    panel, close, adj, vol, sectors, bench = market(n=330, years=(2009, 2013),
                                                    value_edge=0.03)
    _wire(monkeypatch, tmp_path, panel, sectors, _matrices(close, adj, vol, bench))
    assert sr.main([]) == 0
    report = (tmp_path / "out" / "REPORT.md").read_text(encoding="utf-8")
    summary = pd.read_csv(tmp_path / "out" / "summary.csv", index_col=0)
    for name in sr.NAMES:                       # the verdict table, line by line
        r = summary.loc[name]
        assert f"| {name} | {r.vs_spy} | {r.vs_ew} |" in report
    assert "research store" in report
    assert "Where the gap to the S&P 500 comes from" in report
    assert report.rstrip().endswith("No action taken.")
    assert (tmp_path / "out" / "monthly_returns.csv").exists()
    assert "report:" in capsys.readouterr().out


def test_the_command_fails_loudly_without_benchmarks_or_store(monkeypatch, tmp_path,
                                                              capsys):
    panel, close, adj, vol, sectors, bench = market(n=20, years=(2009, 2010))
    _wire(monkeypatch, tmp_path, panel, sectors,
          _matrices(close, adj, vol, bench, with_bench=False))
    assert sr.main([]) == 1
    assert "benchmark prices missing" in capsys.readouterr().out
    (tmp_path / "prices.db").unlink()
    assert sr.main([]) == 1
    assert "python -m research.price_store" in capsys.readouterr().out
