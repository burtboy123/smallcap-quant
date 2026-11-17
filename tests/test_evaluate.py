"""
tests/test_evaluate.py — forward_test/evaluate.py, the EVAL_CRITERIA calculator.

Synthetic periods test each decision clause; one test cross-checks the evaluator
against snapshot.py's own equity curve on the real 2026Q4b data.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from forward_test import evaluate as ev  # noqa: E402

INC = [f"I{i}" for i in range(5)]
CH = [f"C{i}" for i in range(5)]


def make_period(tmp_path, inc_end, ch_end, *, spy_end=1.0, ch_path=None, values=None,
                eval_date="2026-12-29"):
    """Baselines all 100; one middle session; final closes = 100 * end."""
    d = tmp_path / "P"
    d.mkdir()
    days = ["2026-09-30", "2026-11-02", "2026-12-29"]
    rows = []
    for names, end in ((INC, inc_end), (CH, ch_end)):
        for t in names:
            mid = (ch_path or {}).get(t, 1.0) if names is CH else 1.0
            for day, px in zip(days, [100.0, 100.0 * mid, 100.0 * end]):
                rows.append((day, t, px, 0))
    for day, px in zip(days, [100.0, 100.0, 100.0 * spy_end]):
        rows.append((day, "SPY", px, 0))
    pd.DataFrame(rows, columns=["date", "ticker", "close", "is_carried"]).to_csv(
        d / "price_snapshots.csv", index=False)
    (d / "meta.json").write_text(json.dumps({
        "baselines_locked": True, "spy_baseline": 100.0,
        "incumbent_baselines": {t: 100.0 for t in INC},
        "challenger_baselines": {t: 100.0 for t in CH}}))
    vals = values or {t: v for t, v in zip(INC + CH, range(10))}
    pd.DataFrame({"ticker": list(vals), "value_score": list(vals.values())}).to_csv(
        d / "entry_scores.csv", index=False)
    (d / "QUARTER_END_PLAN.md").write_text(
        f"| Evaluation date (x) | **{eval_date}** (Tue) | src |\n")
    return d


def run(d, **kw):
    inp = ev.load(d, kw.pop("as_of", date(2026, 12, 29)))
    spy = float(inp.closes["SPY"].iloc[-1] / inp.spy_base - 1)
    inc = ev.book_stats("Incumbent", inp.closes, inp.inc_base, spy)
    ch = ev.book_stats("Challenger", inp.closes, inp.ch_base, spy)
    per = pd.concat([inc.per_name, ch.per_name])
    ic, _p, _n = ev.spearman(inp.entry["value_score"], per)
    c = ev.clauses(inc, ch, ic, ev.drop_contributor_spreads(inc, ch))
    return inc, ch, ic, c, ev.verdict(c)


def test_incumbent_winning_by_more_than_the_band_is_keep(tmp_path):
    *_, c, v = run(make_period(tmp_path, 1.06, 1.01))
    assert c["b1 Incumbent beats Challenger by >= 3 pp"] and v == "KEEP INCUMBENT"


def test_challenger_win_is_never_an_automatic_adopt(tmp_path):
    """+5 pp, IC positive (cheap = high value_score = C names), drawdown fine:
    every computable (a) clause holds, yet a5 is judgement -> INCONCLUSIVE."""
    *_, c, v = run(make_period(tmp_path, 1.00, 1.05, spy_end=1.0))
    assert c["a1 Challenger beats Incumbent by >= 3 pp"]
    assert c["a2 value consistency positive (IC > 0)"]
    assert not c["a5 tail acceptable given the win (JUDGEMENT, never automatic)"]
    assert v.startswith("INCONCLUSIVE")


def test_negative_value_ic_is_keep_even_if_challenger_wins(tmp_path):
    vals = {t: v for t, v in zip(INC + CH, range(10, 0, -1))}   # C names score LOWEST on value
    _, _, ic, c, v = run(make_period(tmp_path, 1.00, 1.05, values=vals))
    assert ic < 0 and c["b2 value consistency negative (IC < 0)"] and v == "KEEP INCUMBENT"


def test_drawdown_breach_is_keep(tmp_path):
    path = {t: 0.70 for t in CH}                                # -30% in the middle
    _, ch, _, c, v = run(make_period(tmp_path, 1.00, 1.05, ch_path=path))
    assert ch.max_dd == pytest.approx(-0.30)
    assert c["b3 Challenger drawdown > 5 pp deeper or breaches -25%"] and v == "KEEP INCUMBENT"


def test_near_tie_is_keep_via_tail_tie_breaker(tmp_path):
    *_, c, v = run(make_period(tmp_path, 1.02, 1.03))
    assert c["b4 near-tie (|spread| < 3 pp): tail tie-breaker keeps Incumbent"]
    assert v == "KEEP INCUMBENT"


def test_sessions_after_the_evaluation_date_are_excluded(tmp_path):
    d = make_period(tmp_path, 1.06, 1.01, eval_date="2026-11-02")
    rep = ev.evaluate(d, as_of=date(2026, 12, 31))
    assert "-> 2026-11-02 (2 sessions)" in rep


def test_final_refuses_incomplete_data(tmp_path):
    d = make_period(tmp_path, 1.0, 1.0, eval_date="2027-01-15")
    with pytest.raises(ValueError, match="before the evaluation date"):
        ev.evaluate(d, final=True)


def test_interim_report_is_labelled_not_the_verdict(tmp_path):
    rep = ev.evaluate(make_period(tmp_path, 1.0, 1.0), as_of=date(2026, 11, 2))
    assert "INTERIM READING -- NOT THE VERDICT" in rep


@pytest.mark.skipif(
    not (ROOT / "forward_test" / "2026Q4b" / "price_snapshots.csv").exists(),
    reason="raw vendor (Yahoo) closes are not redistributed in the public repo; "
           "python forward_test/snapshot.py downloads them")
def test_matches_snapshot_equity_curve_on_real_q4b_data():
    """Independent cross-check: same returns and drawdowns as snapshot.py computed."""
    d = ROOT / "forward_test" / "2026Q4b"
    curve = pd.read_csv(d / "equity_curves.csv")
    last = curve.iloc[-1]
    inp = ev.load(d, date.fromisoformat(last["date"]))
    spy = float(inp.closes["SPY"].iloc[-1] / inp.spy_base - 1)
    inc = ev.book_stats("Incumbent", inp.closes, inp.inc_base, spy)
    ch = ev.book_stats("Challenger", inp.closes, inp.ch_base, spy)
    assert spy == pytest.approx(last["spy_ret"], abs=1e-9)
    assert inc.ret == pytest.approx(last["incumbent_ret"], abs=1e-9)
    assert ch.ret == pytest.approx(last["challenger_ret"], abs=1e-9)
    assert ch.max_dd == pytest.approx(curve["challenger_drawdown"].min(), abs=1e-9)


def test_q4b_evaluation_date_is_read_from_the_plan():
    assert ev.evaluation_date(ROOT / "forward_test" / "2026Q4b") == date(2026, 12, 29)
