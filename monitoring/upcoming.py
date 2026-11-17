"""
monitoring/upcoming.py — the next dated actions, so none is missed.

Read by the Monday email (top of the summary) and the dashboard's Today page. No date
is written here: the forward-test evaluation date comes from the active period's
QUARTER_END_PLAN.md (forward_test/evaluate.py), the hedged test's from its
pre-registered constant (forward_test/hedged_paper.py). An action is DUE once its date
has passed and its result file does not exist yet; writing the result clears it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FT_ROOT = ROOT / "forward_test"
RESEARCH_DOCS = ROOT / "docs" / "research"


@dataclass(frozen=True)
class Action:
    when: date               # on or after this date
    what: str
    command: str             # runs as written (tests/test_runbook_commands.py)
    then: str                # what to do with its output
    result: Path             # its existence means the action is done

    def done(self) -> bool:
        return self.result.exists()

    def due(self, today: date) -> bool:
        return today >= self.when and not self.done()


def actions(period: str | None = None, ft_root: Path | None = None,
            docs: Path | None = None) -> list[Action]:
    from forward_test import evaluate as ev
    from forward_test import hedged_paper as hp
    if period is None:
        from config.settings import ACTIVE_FT_PERIOD as period
    pdir = (ft_root or FT_ROOT) / period
    out = []
    eval_date = ev.evaluation_date(pdir)
    if eval_date is not None:
        out.append(Action(eval_date, f"Evaluate the {period} forward test",
                          "python forward_test/evaluate.py --final --write",
                          f"follow forward_test/{period}/QUARTER_END_PLAN.md §4-5",
                          pdir / "EVALUATION_RESULT.md"))
    out.append(Action(hp.EVAL_DATE, "Verdict on the hedged paper test",
                      "python -m forward_test.hedged_paper",
                      "write docs/research/hedged_paper_RESULT.md from its numbers "
                      "(rules: docs/research/hedged_paper_PREREG.md)",
                      (docs or RESEARCH_DOCS) / "hedged_paper_RESULT.md"))
    return sorted(out, key=lambda a: a.when)


def lines(today: date, items: list[Action] | None = None) -> list[str]:
    """Plain-text lines for the Monday email: due first, then the next open action."""
    items = actions() if items is None else items
    open_ = [a for a in items if not a.done()]
    if not open_:
        return ["  nothing dated is open"]
    out = []
    for a in open_:
        if a.due(today):
            out += [f"  DUE since {a.when:%Y-%m-%d}: {a.what}",
                    f"      run: {a.command}", f"      then: {a.then}"]
        else:
            out.append(f"  in {(a.when - today).days} days ({a.when:%Y-%m-%d}): {a.what}")
    return out
