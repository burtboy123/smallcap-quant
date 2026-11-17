"""
tests/test_oos_picking.py — research/oos_picking.py (the pre-registered out-of-sample
picking test, docs/research/oos_picking_PREREG.md).

Synthetic markets only (tests/test_universe_study.market). Each rule of the
pre-registration has a test that fails when the rule is dropped: the edge bar, the t
bar, the periods rule, the 36-month minimum, the window ending 2019-02 and the $2m
dollar-volume floor.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from research import oos_picking as oos  # noqa: E402
from research import strategy_race as sr  # noqa: E402
from tests.test_universe_study import market  # noqa: E402

LATE_EDGE = pd.Timestamp("2019-03-01")


@pytest.fixture(autouse=True)
def scaled(monkeypatch):
    """A 240-stock market has ~150 small names: start at 60 scored, as the universe
    study's tests do."""
    monkeypatch.setattr(oos, "MIN_SCORED", 60)


def _pair(edge_per_month: float, n: int = 60, noise: float = 0.01, seed: int = 0):
    rng = np.random.default_rng(seed)
    pool = pd.Series(rng.normal(0.008, 0.05, n))
    return pool + edge_per_month + rng.normal(0, noise, n), pool


# ── the verdict rule ────────────────────────────────────────────────────────────

def test_a_steady_edge_is_confirmed():
    picks, pool = _pair(0.004)                     # ~+5%/yr, steady
    v = oos.verdict(picks, pool)
    assert v["edge"] >= oos.EDGE_BAR and v["t"] >= oos.T_BAR and v["periods_ahead"] == 4
    assert v["verdict"] == "CONFIRMED"


def test_an_edge_below_the_bar_is_only_consistent():
    picks, pool = _pair(0.0012, noise=0.002)       # ~+1.5%/yr, very steady: t is huge
    v = oos.verdict(picks, pool)
    assert 0 < v["edge"] < oos.EDGE_BAR and v["t"] >= oos.T_BAR and v["periods_ahead"] >= 3
    assert v["verdict"] == "CONSISTENT"


def test_a_big_but_noisy_edge_is_only_consistent():
    rng = np.random.default_rng(4)
    pool = pd.Series(rng.normal(0.008, 0.05, 48))
    jumps = np.tile([0.09, -0.07], 24)             # +3.5%/yr, monthly gap swings ±8%
    picks = pool + jumps + 0.0003
    v = oos.verdict(picks, pool)
    assert v["edge"] >= oos.EDGE_BAR and v["periods_ahead"] >= 3 and v["t"] < oos.T_BAR
    assert v["verdict"] == "CONSISTENT"


def test_an_edge_from_one_period_only_is_not_confirmed():
    """Far ahead overall and significant, but all of it in one quarter of the window:
    the periods rule (>= 3 of 4) keeps it out of CONFIRMED and CONSISTENT alike."""
    rng = np.random.default_rng(1)
    pool = pd.Series(rng.normal(0.008, 0.05, 60))
    gap = np.r_[np.full(45, -0.001), np.full(15, 0.03)] + rng.normal(0, 0.002, 60)
    v = oos.verdict(pool + gap, pool)
    assert v["edge"] >= oos.EDGE_BAR and v["t"] >= oos.T_BAR and v["periods_ahead"] == 1
    assert v["verdict"] == "NOT CONFIRMED"


def test_the_edge_is_compounded_growth_not_the_monthly_average():
    pool = pd.Series([0.01] * 60)
    bumpy = pd.Series([0.20, -0.16] * 30)          # higher average, lower growth
    assert bumpy.mean() > pool.mean() and oos._cagr(bumpy) < oos._cagr(pool)
    v = oos.verdict(bumpy, pool)
    assert v["edge"] < 0 and v["verdict"] == "NOT CONFIRMED"


def test_under_36_months_is_insufficient_data():
    picks, pool = _pair(0.01, n=oos.MIN_MONTHS - 1)
    assert oos.verdict(picks, pool)["verdict"] == "INSUFFICIENT DATA"
    picks, pool = _pair(0.01, n=oos.MIN_MONTHS)
    assert oos.verdict(picks, pool)["verdict"] == "CONFIRMED"


# ── the run ─────────────────────────────────────────────────────────────────────

def test_a_real_edge_before_2019_is_confirmed():
    res = oos.run(*market(years=(2014, 2020), value_edge=0.03))
    o = res.oos
    assert res.start is not None and o["months"] >= oos.MIN_MONTHS
    assert o["verdict"] == "CONFIRMED", o
    assert res.later["months"] > 0 and res.later["edge"] > 0


def test_pure_noise_is_not_confirmed():
    res = oos.run(*market(years=(2014, 2020), value_edge=0.0, seed=11))
    assert res.oos["verdict"] != "CONFIRMED", res.oos


def test_the_verdict_uses_only_months_through_2019_02():
    """The edge exists only from 2019-03: the out-of-sample years must not see it,
    the later years must."""
    res = oos.run(*market(years=(2014, 2020), value_edge=0.06, edge_from=LATE_EDGE))
    m = res.monthly
    assert res.oos["to"] <= oos.OOS_END < m.index[-1]
    assert res.oos["months"] == int((m.index <= oos.OOS_END).sum())
    assert res.later["months"] == int((m.index > oos.OOS_END).sum()) > 0
    assert res.oos["verdict"] == "NOT CONFIRMED", res.oos
    assert res.later["edge"] > oos.EDGE_BAR


def test_a_late_start_is_insufficient_data():
    res = oos.run(*market(years=(2016, 2020), value_edge=0.06))
    assert res.start >= pd.Timestamp("2016-12-01")
    assert res.oos["months"] < oos.MIN_MONTHS
    assert res.oos["verdict"] == "INSUFFICIENT DATA"


def test_a_start_after_2019_02_is_insufficient_data_not_a_crash(tmp_path):
    res = oos.run(*market(years=(2019, 2021), value_edge=0.03))
    assert res.start > oos.OOS_END and res.later["months"] > 0
    assert res.oos["verdict"] == "INSUFFICIENT DATA" and res.oos["months"] == 0
    assert "**INSUFFICIENT DATA**" in oos.write_report(res, tmp_path).read_text("utf-8")


def test_scores_use_the_small_pool_and_the_2m_floor():
    """Volume set so most names trade $2m-$5m a day: the $2m floor keeps them, the
    race's $5m would not. Only IWM members are scored."""
    panel, close, adj, vol, members, sectors, bench = market(years=(2014, 2017))
    vol = vol * 0.17                                     # ~$3.4m a day at $20
    res = oos.run(panel, close, adj, vol, members, sectors, bench)
    d = res.start
    pf = sr.price_features(close.astype("float32"), adj.astype("float32"),
                           vol.astype("float32"))
    full = sr.with_prior_year(panel)
    small = full[full["ticker"].isin(set(members["iwm"]))]
    got = int(res.coverage.set_index("date").loc[d, "scored"])
    assert got == len(sr.scores_on(d, small, pf, min_dollar_vol=2e6))
    assert got > len(sr.scores_on(d, small, pf)) + 20           # the race's $5m floor
    assert got < len(sr.scores_on(d, full, pf, min_dollar_vol=2e6))   # not the wide pool


def test_with_the_race_settings_it_reproduces_the_universe_study(monkeypatch):
    """With the $5m floor and the same start, the picks, the pool and the hedged returns
    are the universe study's `small` contestant exactly. So a gap between this study's
    2019-03+ numbers and the universe study's comes from the pre-registered changes
    (the $2m floor, an earlier start), not from different code."""
    import math
    from research import universe_study as us
    monkeypatch.setattr(sr, "MIN_SCORED", 60)
    monkeypatch.setattr(us, "POOLS", {"small": ("iwm", sr.MAX_MCAP, None),
                                      "wide": ("iwv", math.inf, None),
                                      "large": ("iwv", math.inf, 80)})
    monkeypatch.setattr(oos, "MIN_DOLLAR_VOL", sr.MIN_DOLLAR_VOL)
    mk = market(years=(2014, 2017), value_edge=0.03)
    u, o = us.study(*mk), oos.run(*mk)
    assert o.start == u.start and o.monthly.index.equals(u.monthly.index)
    for mine, theirs in (("picks", "small"), ("pool", "small_ew"), ("hedged", "hedged")):
        assert np.allclose(o.monthly[mine], u.monthly[theirs], rtol=0, atol=1e-12), mine


def test_hedged_is_spy_plus_picks_minus_iwm_minus_financing():
    res = oos.run(*market(years=(2014, 2017), value_edge=0.03))
    m = res.monthly
    want = m["SPY"] + m["picks"] - m["IWM"] - 0.02 / 12
    assert np.allclose(m["hedged"], want)


def test_no_month_with_enough_names_gives_an_empty_result(monkeypatch):
    monkeypatch.setattr(oos, "MIN_SCORED", 10_000)
    res = oos.run(*market(years=(2014, 2016)))
    assert res.monthly.empty and res.start is None and len(res.coverage) > 0


# ── report and command ──────────────────────────────────────────────────────────

def test_report_states_the_verdict_and_the_rule(tmp_path):
    res = oos.run(*market(years=(2014, 2020), value_edge=0.03))
    text = oos.write_report(res, tmp_path).read_text(encoding="utf-8")
    assert f"**{res.oos['verdict']}**" in text
    assert f"{res.oos['periods_ahead']} of 4" in text
    assert "## Same rules on 2019-03 onward" in text
    assert "| 2016 |" in text                                   # names per year
    assert text.rstrip().endswith("No action taken.")
    for f in ("monthly_returns.csv", "coverage_by_date.csv"):
        assert (tmp_path / f).exists()


def test_report_says_why_when_data_is_insufficient(tmp_path):
    res = oos.run(*market(years=(2016, 2020), value_edge=0.06))
    text = oos.write_report(res, tmp_path).read_text(encoding="utf-8")
    assert "**INSUFFICIENT DATA**" in text
    assert f"{oos.MIN_MONTHS} are needed" in text


@pytest.mark.parametrize("missing, fix", [("prices", "python -m research.price_store"),
                                          ("sec", "python -m data.sec_xbrl")])
def test_main_fails_loudly_without_a_database(missing, fix, monkeypatch, tmp_path, capsys):
    from data import sec_xbrl
    from research import price_store
    for f in ("prices.db", "sec.db"):
        if not f.startswith(missing):
            (tmp_path / f).write_bytes(b"")
    monkeypatch.setattr(price_store, "DB_PATH", tmp_path / "prices.db")
    monkeypatch.setattr(sec_xbrl, "DB_PATH", tmp_path / "sec.db")
    assert oos.main([]) == 1
    out = capsys.readouterr().out
    assert out.startswith("FAILED:") and out.rstrip().endswith(fix)


def _command(monkeypatch, tmp_path, mk):
    from data import sec_xbrl
    from research import price_store
    panel, close, adj, vol, members, sectors, bench = mk
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
    monkeypatch.setattr(oos, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(oos.write_report, "__defaults__", (tmp_path / "out",))


def test_the_command_runs_end_to_end(monkeypatch, tmp_path, capsys):
    _command(monkeypatch, tmp_path, market(years=(2014, 2020), value_edge=0.03))
    assert oos.main([]) == 0
    out = capsys.readouterr().out
    assert "CONFIRMED: picking +" in out and "4 of 4 periods ahead" in out
    assert "hedged " in out and "vs S&P 500" in out
    assert (tmp_path / "out" / "REPORT.md").exists()


def test_the_command_fails_when_no_month_has_enough_names(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(oos, "MIN_SCORED", 10_000)
    _command(monkeypatch, tmp_path, market(years=(2014, 2016)))
    assert oos.main([]) == 1
    out = capsys.readouterr().out
    assert "FAILED: no month had 10000 scored names" in out
    assert (tmp_path / "out" / "coverage_by_date.csv").exists()
    assert not (tmp_path / "out" / "REPORT.md").exists()
