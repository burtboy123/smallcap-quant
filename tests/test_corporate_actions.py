"""
tests/test_corporate_actions.py -- a stock split is never read as a loss.

On 2026-11-17 SENEA split 5-for-1. yfinance restated its whole history to ~1/5, the
snapshot stored the restated series, and the locked baseline stayed 178.00: the book
read -12.9% instead of -7.5%, the risk watch emailed "SENEA -80%", and the broker's
buy-and-hold target for SENEA shrank to a fifth. These tests fail if any reader goes
back to raw meta.json baselines, if the snapshot stops checking baselines against the
series, or if it accepts a mismatch no reported split explains.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import forward_test.snapshot as snap  # noqa: E402
from forward_test import baselines as bl  # noqa: E402

ET = ZoneInfo("America/New_York")
D1, D2, D3 = date(2026, 11, 12), date(2026, 11, 13), date(2026, 11, 16)
SPLIT = [{"ticker": "AAA", "date": "2026-11-16", "ratio": 5.0, "source": "test"}]


# ── the arithmetic ──────────────────────────────────────────────────────────────

def test_split_factor_and_adjust():
    assert bl.split_factor(SPLIT, "AAA") == 5.0
    assert bl.split_factor(SPLIT, "BBB") == 1.0
    assert bl.adjust({"AAA": 100.0, "BBB": 10.0, "CCC": None}, SPLIT) == {"AAA": 20.0, "BBB": 10.0}


def test_reverse_split_restates_upward():
    rev = [{"ticker": "AAA", "date": "2026-11-16", "ratio": 0.1}]
    assert bl.adjust({"AAA": 2.0}, rev) == {"AAA": 20.0}


def test_mismatch_found_and_explained_only_by_the_right_ratio():
    off = bl.mismatches({"AAA": 100.0, "BBB": 10.0}, {"AAA": 20.0, "BBB": 10.05}, [])
    assert list(off) == ["AAA"] and off["AAA"] == pytest.approx(0.2)
    assert bl.explain(off["AAA"], [("2026-11-16", 5.0)])
    assert not bl.explain(off["AAA"], [("2026-11-16", 4.0)])
    assert not bl.explain(off["AAA"], [])
    # once recorded, the same series no longer mismatches
    assert bl.mismatches({"AAA": 100.0}, {"AAA": 20.0}, SPLIT) == {}


def test_append_is_append_only(tmp_path):
    bl.append_splits(tmp_path, SPLIT)
    bl.append_splits(tmp_path, [dict(SPLIT[0], ratio=9.0)])      # same ticker+date: ignored
    assert [s["ratio"] for s in bl.read_splits(tmp_path)] == [5.0]


# ── every reader sees restated baselines ───────────────────────────────────────

def _period(d: Path, with_split: bool) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps({
        "baselines_locked": True, "baseline_date": D1.isoformat(), "spy_baseline": 500.0,
        "incumbent_baselines": {"AAA": 100.0, "BBB": 10.0},
        "challenger_baselines": {"BBB": 10.0}}), encoding="utf-8")
    pd.DataFrame({"ticker": ["AAA", "BBB"], "weight_equal": [0.5, 0.5]}
                 ).to_csv(d / "incumbent_book.csv", index=False)
    pd.DataFrame({"ticker": ["BBB"], "weight_equal": [1.0]}
                 ).to_csv(d / "value_tilt_book.csv", index=False)
    if with_split:
        bl.append_splits(d, SPLIT)
    return d


def test_no_corporate_actions_file_returns_meta_unchanged(tmp_path):
    cb = bl.current_baselines(_period(tmp_path / "P", with_split=False))
    assert cb["incumbent"] == {"AAA": 100.0, "BBB": 10.0} and cb["spy"] == 500.0


def test_broker_targets_use_restated_baselines(tmp_path):
    """Post-split broker price 21 against baseline 100 would size AAA at a fifth."""
    from broker.targets import baselines_for, buy_and_hold_weights
    d = _period(tmp_path / "P", with_split=True)
    base = baselines_for(d / "incumbent_book.csv")
    assert base == {"AAA": 20.0, "BBB": 10.0}
    book = pd.read_csv(d / "incumbent_book.csv")
    w, _ = buy_and_hold_weights(book, base, {"AAA": 21.0, "BBB": 10.0})
    assert w["AAA"] > w["BBB"]                     # AAA rose 5%, BBB flat


def test_evaluation_uses_restated_baselines(tmp_path):
    from forward_test import evaluate
    d = _period(tmp_path / "P", with_split=True)
    pd.DataFrame([(D1.isoformat(), "AAA", 20.0, 0), (D1.isoformat(), "BBB", 10.0, 0),
                  (D1.isoformat(), "SPY", 500.0, 0)],
                 columns=["date", "ticker", "close", "is_carried"]
                 ).to_csv(d / "price_snapshots.csv", index=False)
    inp = evaluate.load(d, D1)
    assert inp.inc_base["AAA"] == 20.0


def test_dashboard_holding_returns_are_not_a_split_loss(tmp_path):
    from dashboard.today import holding_returns
    d = _period(tmp_path / "P", with_split=True)
    pd.DataFrame([(D3.isoformat(), "AAA", 21.0, 0), (D3.isoformat(), "BBB", 10.0, 0)],
                 columns=["date", "ticker", "close", "is_carried"]
                 ).to_csv(d / "price_snapshots.csv", index=False)
    r = holding_returns(d)
    assert r["AAA"] == pytest.approx(0.05)


def test_positions_seen_before_a_split_are_restated(tmp_path):
    d = _period(tmp_path / "P", with_split=True)              # AAA 5-for-1 on 2026-11-16
    pos = [{"ticker": "AAA", "position": 100, "avg_cost": 100.0},
           {"ticker": "BBB", "position": 50, "avg_cost": 10.0}]
    before = bl.positions_in_current_shares(pos, d, "2026-10-01T20:24:04")
    assert before[0] == {"ticker": "AAA", "position": 500.0, "avg_cost": 20.0}
    assert before[1] == pos[1]
    after = bl.positions_in_current_shares(pos, d, "2026-11-17T10:00:00")
    assert after == pos                                        # seen post-split: as is


def test_weekly_account_view_is_in_current_shares(tmp_path, monkeypatch):
    """The 2026-10-01 view valued pre-split SENEA shares at post-split prices."""
    from monitoring import weekly_report as wr
    d = _period(tmp_path / "P", with_split=True)
    recon = tmp_path / "recon"
    recon.mkdir()
    (recon / "recon_20261001_202404.json").write_text(json.dumps({
        "timestamp": "2026-10-01T20:24:04", "account": "DU_TEST",
        "positions_after": [{"ticker": "AAA", "position": 100, "avg_cost": 100.0}]}),
        encoding="utf-8")
    monkeypatch.setattr(wr, "RECON_DIR", recon)
    monkeypatch.setattr(wr.ck, "SNAP", d / "price_snapshots.csv")
    _p, rec = wr._latest_recon()
    assert rec["positions_after"][0]["position"] == 500.0


# ── the snapshot checks the series against the locked baselines ───────────────

def _restated():
    """yfinance after a 5-for-1 split of AAA: the whole AAA history at 1/5."""
    return pd.DataFrame({"AAA": [20.0, 20.5, 21.0], "BBB": [10.0, 10.0, 10.0],
                         "SPY": [500.0, 505.0, 510.0]}, index=[D1, D2, D3])


@pytest.fixture
def locked(tmp_path, monkeypatch):
    d = _period(tmp_path / "P", with_split=False)
    monkeypatch.setattr(snap, "DIR", d)
    monkeypatch.setattr(snap, "SNAP", d / "price_snapshots.csv")
    monkeypatch.setattr(snap, "CURVE", d / "equity_curves.csv")
    monkeypatch.setattr(snap, "START", D1.isoformat())
    monkeypatch.setattr(snap, "CFG", {"start": D1.isoformat(), "spy_baseline": None,
                                      "baseline_mode": "first_close", "baseline_col": None})
    monkeypatch.setattr(snap, "SPY_BASELINE", None)
    monkeypatch.setattr(snap, "fetch", lambda *a: _restated())
    monkeypatch.setattr(snap, "_now_et", lambda: datetime(2026, 11, 17, 9, 0, tzinfo=ET))
    return d


def test_reported_split_is_recorded_and_the_curve_is_right(locked, monkeypatch, capsys):
    monkeypatch.setattr(snap, "fetch_splits",
                        lambda tickers, start, end: {"AAA": [("2026-11-16", 5.0)]})
    curve = snap.main()
    # AAA 20 -> 21 (+5%), BBB flat: incumbent +2.5%, not (21/100 + 1)/2 - 1 = -39.5%
    assert curve["incumbent_ret"].iloc[-1] == pytest.approx(0.025)
    assert [(s["ticker"], s["ratio"]) for s in bl.read_splits(locked)] == [("AAA", 5.0)]
    assert "split recorded: AAA x5" in capsys.readouterr().out
    meta = json.loads((locked / "meta.json").read_text(encoding="utf-8"))
    assert meta["incumbent_baselines"]["AAA"] == 100.0          # the lock is untouched


def test_unexplained_mismatch_exits_1_and_writes_no_curve(locked, monkeypatch, capsys):
    monkeypatch.setattr(snap, "fetch_splits", lambda tickers, start, end: {})
    with pytest.raises(SystemExit) as e:
        snap.main()
    assert e.value.code == 1
    assert not (locked / "equity_curves.csv").exists()
    assert not (locked / bl.CA_NAME).exists()
    assert "FAILED: AAA" in capsys.readouterr().out


def test_wrong_ratio_is_not_accepted(locked, monkeypatch):
    monkeypatch.setattr(snap, "fetch_splits",
                        lambda tickers, start, end: {"AAA": [("2026-11-16", 4.0)]})
    with pytest.raises(SystemExit):
        snap.main()


def test_no_mismatch_makes_no_split_request(locked, monkeypatch):
    unsplit = _restated()
    unsplit["AAA"] = [100.0, 102.5, 105.0]
    monkeypatch.setattr(snap, "fetch", lambda *a: unsplit)

    def boom(*a):
        raise AssertionError("fetch_splits called without a mismatch")
    monkeypatch.setattr(snap, "fetch_splits", boom)
    assert snap.main()["incumbent_ret"].iloc[-1] == pytest.approx(0.025)


def test_rewritten_history_is_reported(locked, monkeypatch, capsys):
    pd.DataFrame([(D1.isoformat(), "AAA", 100.0, 0), (D2.isoformat(), "AAA", 102.5, 0)],
                 columns=["date", "ticker", "close", "is_carried"]
                 ).to_csv(locked / "price_snapshots.csv", index=False)
    monkeypatch.setattr(snap, "fetch_splits",
                        lambda tickers, start, end: {"AAA": [("2026-11-16", 5.0)]})
    snap.main()
    assert "revised 2 stored close(s) of AAA" in capsys.readouterr().out


def test_a_row_with_no_close_is_not_a_session(locked, monkeypatch):
    """yfinance's split row for an ex-date that has not traded (all NaN closes)."""
    f = pd.concat([_restated(), pd.DataFrame({"AAA": [float("nan")], "BBB": [float("nan")],
                                              "SPY": [float("nan")]}, index=[date(2026, 11, 17)])])
    monkeypatch.setattr(snap, "fetch", lambda *a: f)
    monkeypatch.setattr(snap, "_now_et", lambda: datetime(2026, 11, 17, 17, 0, tzinfo=ET))
    monkeypatch.setattr(snap, "fetch_splits",
                        lambda tickers, start, end: {"AAA": [("2026-11-16", 5.0)]})
    snap.main()
    s = pd.read_csv(locked / "price_snapshots.csv")
    assert "2026-11-17" not in set(s["date"]) and int(s["is_carried"].sum()) == 0
