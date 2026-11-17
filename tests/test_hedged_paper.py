"""
tests/test_hedged_paper.py — forward_test/hedged_paper.py, the paper forward test of
SPY + (portfolio − IWM). Temp folders and fake downloads only.
"""

from __future__ import annotations

import hashlib
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from forward_test import hedged_paper as hp  # noqa: E402

DAYS = pd.bdate_range("2026-09-30", periods=4)


def _period(tmp_path, portfolio=(1.0, 1.02, 1.02, 1.03), spy=(1.0, 1.01, 1.01, 1.0)):
    d = tmp_path / "ft" / "2026Q4b"
    d.mkdir(parents=True)
    pd.DataFrame({"date": DAYS.strftime("%Y-%m-%d"), "incumbent_index": portfolio,
                  "challenger_index": 1.0, "spy_index": spy}).to_csv(
        d / "equity_curves.csv", index=False)
    return d


def _iwm(values=(100.0, 101.5, 101.5, 101.5), days=DAYS):
    return lambda a, b: pd.Series(values, index=days[:len(values)])


def test_daily_return_is_spy_plus_picking_minus_financing(tmp_path):
    _period(tmp_path)
    h, _ = hp.update("2026Q4b", tmp_path / "ft", tmp_path / "out", _iwm())
    day1 = h["hedged_index"].iloc[1] - 1
    assert day1 == pytest.approx(0.01 + (0.02 - 0.015) - 0.02 / 252)
    assert h["hedged_index"].iloc[0] == 1.0                         # baseline day
    assert (tmp_path / "out" / "2026Q4b.csv").exists()


def test_a_missing_iwm_close_is_never_filled(tmp_path):
    _period(tmp_path)
    gap = pd.Series([100.0, 101.0, 102.0], index=DAYS[[0, 1, 3]])
    with pytest.raises(ValueError, match="IWM close missing"):
        hp.update("2026Q4b", tmp_path / "ft", tmp_path / "out", lambda a, b: gap)
    short = hp.update("2026Q4b", tmp_path / "ft", tmp_path / "out2",
                      _iwm((100.0, 101.0)))[0]
    assert len(short) == 2                     # stops at the last day IWM has a close


def test_iwm_is_cached_and_a_failed_download_is_loud(tmp_path):
    cache = tmp_path / "iwm.csv"
    s = hp.fetch_iwm(DAYS[0].date(), DAYS[-1].date(), cache, _iwm())
    assert len(s) == 4
    again = hp.fetch_iwm(DAYS[0].date(), DAYS[-1].date(), cache,
                         lambda a, b: pytest.fail("cache not used"))
    assert again.tolist() == s.tolist()
    with pytest.raises(RuntimeError):
        hp.fetch_iwm(DAYS[0].date(), date(2026, 12, 31), cache, lambda a, b: pd.Series(dtype=float))


def test_more_return_bought_with_more_risk_does_not_pass(monkeypatch):
    """The pre-registered fix: out-earning SPY only by taking more risk is NOT a pass."""
    a = np.tile([1.0, -1.0], 130)                            # two patterns that do not
    b = np.tile([1.0, 1.0, -1.0, -1.0], 65)                  # move together
    spy = 0.001 + 0.01 * a
    hedged = spy + 0.0006 + 0.02 * b                         # more return, much more risk
    days = pd.bdate_range(end="2027-10-15", periods=261)     # ends after the eval date
    h = pd.DataFrame({"date": days,
                      "hedged_index": np.r_[1, np.cumprod(1 + hedged)],
                      "spy_index": np.r_[1, np.cumprod(1 + spy)]})
    r = hp.reading(h)
    assert r["hedged"]["cagr"] > r["spy"]["cagr"]
    assert r["hedged"]["sharpe"] < r["spy"]["sharpe"]
    assert r["would_pass_today"] is False
    assert r["verdict"] == "NOT SHOWN"                       # past the evaluation date
    better = pd.DataFrame({"date": days, "hedged_index": np.r_[1, np.cumprod(1 + spy + 0.0004)],
                           "spy_index": h["spy_index"]})
    assert hp.reading(better)["verdict"] == "PASS"


def test_before_the_evaluation_date_there_is_no_verdict():
    days = pd.bdate_range("2026-09-30", periods=10)
    h = pd.DataFrame({"date": days, "hedged_index": np.linspace(1, 1.1, 10),
                      "spy_index": np.linspace(1, 1.01, 10)})
    assert hp.reading(h)["verdict"] == "interim (no verdict)"


def test_the_forward_test_is_read_never_written(tmp_path):
    d = _period(tmp_path)
    before = hashlib.sha256((d / "equity_curves.csv").read_bytes()).hexdigest()
    hp.update("2026Q4b", tmp_path / "ft", tmp_path / "out", _iwm())
    assert hashlib.sha256((d / "equity_curves.csv").read_bytes()).hexdigest() == before
    assert sorted(p.name for p in d.iterdir()) == ["equity_curves.csv"]


def test_main_runs_and_fails_loudly(monkeypatch, tmp_path, capsys):
    from config import settings
    _period(tmp_path)
    monkeypatch.setattr(settings, "ACTIVE_FT_PERIOD", "2026Q4b")
    monkeypatch.setattr(hp, "FT_ROOT", tmp_path / "ft")
    monkeypatch.setattr(hp, "OUT_DIR", tmp_path / "out")
    real = hp.fetch_iwm
    monkeypatch.setattr(hp, "fetch_iwm", lambda a, b, cache, fetch=None: real(a, b, cache, _iwm()))
    assert hp.main([]) == 0
    out = capsys.readouterr().out
    assert "3 trading days" in out and "interim (no verdict)" in out
    monkeypatch.setattr(hp, "fetch_iwm", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("IWM closes could not be downloaded (yfinance)")))
    assert hp.main([]) == 1 and "FAILED" in capsys.readouterr().out
    monkeypatch.setattr(settings, "ACTIVE_FT_PERIOD", "NOPE")
    assert hp.main([]) == 1 and "no forward-test curve" in capsys.readouterr().out
