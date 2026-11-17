"""
tests/test_audit_log_isolation.py

Test and proof code must never write to the REAL broker/killswitch_audit.log.

That log is what you read after a genuine halt. Until 2026-10-04 the live kill-switch
proofs (broker/test_killswitch_*.py) built OrderGate() with the default audit log, so
every proof run appended deliberate HALT/BLOCK lines -- e.g. "SUNS BUY: ADV
$900,000/day below the $5,000,000/day liquidity floor" -- that looked like real events.
Every OrderGate(...) in test/proof code must pass audit_log= explicitly.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEST_CODE = (sorted((ROOT / "tests").glob("*.py"))
             + sorted((ROOT / "broker").glob("test_*.py"))
             + [ROOT / "monitoring" / "break_test.py"])


def _gates_without_audit_log(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bad = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "id", getattr(node.func, "attr", None)) == "OrderGate"
                and not any(k.arg == "audit_log" for k in node.keywords)):
            bad.append(node.lineno)
    return bad


def test_scan_covers_the_known_proof_scripts():
    names = {p.name for p in TEST_CODE}
    assert {"test_killswitch_trips.py", "test_killswitch_in_path.py",
            "test_killswitch_with_execution.py", "break_test.py"} <= names


def test_every_test_gate_uses_its_own_audit_log():
    offenders = {str(p.relative_to(ROOT)): lines for p in TEST_CODE
                 if (lines := _gates_without_audit_log(p))}
    assert not offenders, f"OrderGate() writing to the real audit log: {offenders}"
