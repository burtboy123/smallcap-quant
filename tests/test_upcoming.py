"""
tests/test_upcoming.py — monitoring/upcoming.py: the next dated actions (Monday email
and the dashboard's Today page). Dates come from the system's own files, never from
this module; an action is DUE from its date until its result file exists.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dashboard import today as td            # noqa: E402
from forward_test import hedged_paper as hp  # noqa: E402
from monitoring import upcoming as up        # noqa: E402
from tests.test_runbook_commands import problems_with  # noqa: E402


def _period(tmp_path, eval_date="2026-12-29"):
    d = tmp_path / "ft" / "P1"
    d.mkdir(parents=True)
    (d / "QUARTER_END_PLAN.md").write_text(
        "| Criteria field | value | Source |\n|---|---|---|\n"
        f"| Evaluation date (2026-09-14) | **{eval_date}** (Tue): x | y |\n",
        encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    return d, docs


def _actions(tmp_path, **kw):
    d, docs = _period(tmp_path, **kw)
    return d, docs, up.actions("P1", ft_root=d.parent, docs=docs)


def test_dates_come_from_the_period_plan_and_the_hedged_prereg(tmp_path):
    _d, _docs, acts = _actions(tmp_path, eval_date="2027-01-15")
    assert [(a.when, a.what) for a in acts] == [
        (date(2027, 1, 15), "Evaluate the P1 forward test"),
        (hp.EVAL_DATE, "Verdict on the hedged paper test")]


def test_without_a_plan_only_the_hedged_verdict_is_listed(tmp_path):
    (tmp_path / "ft" / "P2").mkdir(parents=True)
    acts = up.actions("P2", ft_root=tmp_path / "ft", docs=tmp_path)
    assert [a.what for a in acts] == ["Verdict on the hedged paper test"]


def test_due_from_the_date_until_the_result_is_written(tmp_path):
    d, _docs, acts = _actions(tmp_path)
    ev = acts[0]
    assert not ev.due(date(2026, 12, 28)) and ev.due(date(2026, 12, 29))
    assert ev.due(date(2027, 2, 1))                          # stays due: never forgotten
    (d / "EVALUATION_RESULT.md").write_text("verdict", encoding="utf-8")
    assert ev.done() and not ev.due(date(2027, 2, 1))


def test_every_printed_command_runs_as_written():
    for a in up.actions():                                   # the real period and dates
        assert not problems_with(a.command), (a.command, problems_with(a.command))


def test_email_lines_show_due_actions_with_their_command(tmp_path):
    _d, _docs, acts = _actions(tmp_path)
    before = up.lines(date(2026, 10, 12), acts)
    assert before[0] == "  in 78 days (2026-12-29): Evaluate the P1 forward test"
    after = up.lines(date(2027, 1, 4), acts)
    assert after[:3] == ["  DUE since 2026-12-29: Evaluate the P1 forward test",
                         "      run: python forward_test/evaluate.py --final --write",
                         "      then: follow forward_test/P1/QUARTER_END_PLAN.md §4-5"]
    assert after[3].startswith("  in ") and "hedged" in after[3]


def test_today_page_shows_due_actions_and_only_the_next_open_one(tmp_path):
    d, docs, acts = _actions(tmp_path)
    notes = td.coming_up(date(2026, 10, 12), acts)
    assert len(notes) == 1 and notes[0][1] is None and "29 Dec 2026" in notes[0][0]
    notes = td.coming_up(date(2027, 1, 4), acts)
    assert notes[0][1:] == ("warning", "Due")
    assert "`python forward_test/evaluate.py --final --write`" in notes[0][0]
    assert notes[1][0].startswith("Next: **Verdict on the hedged paper test**")
    (d / "EVALUATION_RESULT.md").write_text("verdict", encoding="utf-8")
    (docs / "hedged_paper_RESULT.md").write_text("verdict", encoding="utf-8")
    assert td.coming_up(date(2027, 1, 4), acts) == []
