"""
tests/test_q4b_reproduces.py -- the book-building path still produces the Q4b book.

Audit 2026-11-17: rebuilding the 2026Q4b incumbent in memory from its own stored inputs
(factor_scores at meta.json's calc_date, the recorded regime, ADV from prices up to that
date, computed exactly as broker.book_guard.load_adv) gives the committed 15 names. The
next book is built through the same path, so a change to selection, the limits or the
gates shows up here first. If a change is DELIBERATE (a new construction after the
verdict), this test failing is the record that Q4b's construction no longer reproduces:
pin it to the old code or retire it consciously, do not loosen it.

Scope, measured by mutation: it catches changes to the selection stage that change
this book (the liquidity floor, the sector cap, the tier sizes all did). It does NOT
cover the factor calculation or the F-score gate (applied when scores are computed;
this rebuilds from stored, already-gated scores), nor changes that happen to leave
this book unchanged (doubling the regime bias did).

Needs the local database (factor scores and prices), like the book-guard integration
test: it runs on the trading machine (and in the Monday health check), not in CI.
The production risk stage is stubbed: run_risk_check only REPORTS (it downloads prices
for correlations) and cannot change which names are selected.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PERIOD = ROOT / "forward_test" / "2026Q4b"     # a fixed, frozen record, not the active period


def _conn_with_scores(calc: str):
    db = ROOT / "quant_system.db"
    if not db.exists():
        pytest.skip("local database not present (CI / fresh clone)")
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        n = conn.execute("SELECT COUNT(*) FROM factor_scores WHERE calculation_date = ?",
                         [calc]).fetchone()[0]
    except sqlite3.Error:
        n = 0
    if not n:
        conn.close()
        pytest.skip(f"no factor_scores for {calc} in the local database")
    return conn


def test_builder_reproduces_the_committed_q4b_incumbent(monkeypatch):
    from forward_test import build_q4_books as b
    meta = json.loads((PERIOD / "meta.json").read_text(encoding="utf-8"))
    calc = meta["calc_date"]
    conn = _conn_with_scores(calc)
    try:
        scores, sectors = b.load_scores(conn, calc)
        tick = scores["ticker"].tolist()
        q = ",".join("?" * len(tick))
        px = pd.read_sql(f"SELECT ticker, date, close, volume FROM prices "
                         f"WHERE ticker IN ({q}) AND date <= ?",
                         conn, params=tick + [calc + " 23:59:59"])
    finally:
        conn.close()
    adv = {t: float(g.sort_values("date").tail(60).eval("close * volume").dropna().median())
           for t, g in px.groupby("ticker")}
    monkeypatch.setattr(b, "run_risk_check", lambda rc, regime: {"status": "OK", "flags": []})

    book = b.build_incumbent(scores, sectors, {"regime": meta["regime"]}, adv)

    committed = pd.read_csv(PERIOD / "incumbent_book.csv")
    assert sorted(book["ticker"]) == sorted(committed["ticker"]), (
        "the book-building path no longer reproduces the committed 2026Q4b incumbent")
