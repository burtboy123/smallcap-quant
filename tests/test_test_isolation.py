"""
tests/test_test_isolation.py — the suite never touches the real NAV history.

The drawdown kill-switch (5.1) takes its high-water mark from
monitoring/state/nav_history.jsonl; fake rows written by a test could fake a drawdown.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from broker import nav_history  # noqa: E402

REAL = ROOT / "monitoring" / "state" / "nav_history.jsonl"


def test_nav_log_is_redirected_for_every_test():
    assert nav_history.NAV_LOG != REAL
    assert "monitoring" not in nav_history.NAV_LOG.parts[-3:]


def test_recording_inside_a_test_leaves_the_real_file_alone():
    before = REAL.read_bytes() if REAL.exists() else None
    nav_history.record("DU_TEST", 1_000_000, "USD")
    assert (REAL.read_bytes() if REAL.exists() else None) == before
    assert nav_history.NAV_LOG.exists()


def test_broker_reconciliation_dir_is_redirected_for_every_test():
    from broker import ibkr_paper
    assert ibkr_paper.RECON_DIR != ROOT / "broker" / "reconciliation"
    assert "broker" not in ibkr_paper.RECON_DIR.parts[-3:]


def test_clean_finds_and_removes_only_the_test_rows(tmp_path):
    import json
    p = tmp_path / "nav.jsonl"
    rows = [{"ts": "2026-10-05T10:00", "account": "DU1234567", "nav": 1019276.43, "currency": "EUR"},
            {"ts": "2026-10-05T15:31", "account": "DU1234567", "nav": 1000000.0, "currency": "USD"},
            {"ts": "2026-10-06T14:03", "account": "DU1234567", "nav": 1000000.0, "currency": "EUR"}]
    p.write_text("".join(json.dumps(r) + "\n" for r in rows) + "torn{\n")
    assert nav_history.test_rows(p) == [1]
    dry = nav_history.clean(p)
    assert dry["found"] == 1 and dry["removed"] == 0 and len(p.read_text().splitlines()) == 4
    done = nav_history.clean(p, apply=True)
    assert done["removed"] == 1 and Path(done["backup"]).exists()
    left = p.read_text().splitlines()
    assert len(left) == 3 and all('"USD"' not in ln for ln in left)   # EUR 1,000,000 kept
