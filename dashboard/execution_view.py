"""
dashboard/execution_view.py — wording and defaults for the Execution page.

Pure functions (no Streamlit) so they are tested. The page used to open on the newest
run even when that was a dry run, which shows "0 filled" by design and read as if
nothing had ever been bought. It now opens on the newest run that actually sent
orders, and calls a dry run what it is: a rehearsal.
"""

from __future__ import annotations

import pandas as pd


def run_label(timestamp, account: str, engine: str, dry_run: bool,
              share_fill_rate: float) -> str:
    when = f"{pd.to_datetime(timestamp):%Y-%m-%d %H:%M}"
    what = ("Rehearsal (dry run, nothing sent)" if dry_run
            else f"{min(share_fill_rate, 1.0) * 100:.1f}% filled")
    return f"{when} · {what} · {account} · {engine}"


def default_run(runs: pd.DataFrame) -> int:
    """Position of the newest run that sent orders; 0 when every run was a dry run.
    `runs` is execution_store.run_summary(): newest first."""
    for i, dry in enumerate(runs["dry_run"].tolist()):
        if not dry:
            return i
    return 0


def holdings_line(rec: dict) -> str | None:
    """'You hold N stocks ...' from the latest broker reconciliation, or None."""
    pos = [p for p in rec.get("positions_after", []) or []
           if float(p.get("position") or 0) > 0]
    if not pos:
        return None
    when = rec.get("timestamp", "")
    try:
        when = f"{pd.to_datetime(when):%d %b %Y %H:%M}"
    except (ValueError, TypeError):
        pass
    return (f"The paper account holds {len(pos)} stocks, as last seen by the broker "
            f"connection ({when}).")
