"""
tests/test_dashboard_pages.py -- every dashboard page renders without an exception.

The dashboard is where the system is read, and until 2026-11-17 no test rendered it: a
page that raised was found by opening it. Each page in dashboard/app.py's _SECTIONS is
run headless with streamlit.testing (one page per test, so a failure names the page).
Pages are read from app.py itself, so a new page is covered without editing this file.

On the PC the pages read the real database and forward-test files; in CI there is no
database and they show their "no data" notes. Either way, an exception is a failure.
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
RUNNER = Path(__file__).resolve().parent / "dashboard_page_runner.py"


def _page_titles() -> list[str]:
    tree = ast.parse((ROOT / "dashboard" / "app.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (isinstance(node, (ast.Assign, ast.AnnAssign))
                and any(getattr(t, "id", None) == "_SECTIONS"
                        for t in (node.targets if isinstance(node, ast.Assign)
                                  else [node.target]))):
            return [elt.elts[0].value for lst in node.value.values for elt in lst.elts]
    raise AssertionError("dashboard/app.py has no _SECTIONS")


PAGES = _page_titles()


def _render(page: str, monkeypatch) -> AppTest:
    monkeypatch.setenv("DASH_PAGE", page)
    at = AppTest.from_file(str(RUNNER), default_timeout=180)
    at.run()
    return at


def test_pages_were_found():
    assert len(PAGES) >= 10 and "Today" in PAGES and "Forward Test" in PAGES


@pytest.mark.parametrize("page", PAGES)
def test_page_renders_without_an_exception(page, monkeypatch):
    at = _render(page, monkeypatch)
    assert not at.exception, f"{page}: {at.exception[0].value[:500]}"


def test_the_runner_reports_a_broken_page(monkeypatch):
    """A harness that cannot fail proves nothing: an unknown page must raise."""
    at = _render("No Such Page", monkeypatch)
    assert at.exception and "No Such Page" in at.exception[0].value
