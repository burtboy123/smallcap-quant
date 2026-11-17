"""
tests/conftest.py — guards that apply to EVERY test.

Tests must never write machine-local state. Until 2026-10-11 a broker test ran
`ibkr_paper.main()` against a fake broker and appended fake NAV rows (1,000,000 USD for
the real paper account id) to monitoring/state/nav_history.jsonl -- the file the 5.1
drawdown kill-switch takes its high-water mark from. The Monday health check runs the
suite, so this happened weekly on the trading machine. Every test now gets a private
NAV log; tests/test_test_isolation.py proves it.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _private_nav_log(tmp_path, monkeypatch):
    from broker import nav_history
    monkeypatch.setattr(nav_history, "NAV_LOG", tmp_path / "nav_history.jsonl")


@pytest.fixture(autouse=True)
def _private_recon_dir(tmp_path, monkeypatch):
    """Every broker connection saves the positions it saw (positions_*.json), and the
    weekly report and risk watch read the newest one. A fake broker's empty positions
    written to the real folder would tell the Monday email the account is flat."""
    from broker import ibkr_paper
    monkeypatch.setattr(ibkr_paper, "RECON_DIR", tmp_path / "reconciliation")
