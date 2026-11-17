"""
tests/test_silent_failure_rules.py

The repo's recurring bug is code that looks like it worked: a freshness checker
hardcoded to an old period, a wrapper whose exit code was always 0, a snapshot that
wrote 0 rows and called it success. Each rule below turns one of those lessons into
something CI enforces, so it is not re-learned the expensive way.

  1. One active period. Only config/settings.py names it; everything else reads
     ACTIVE_FT_PERIOD. The .bat wrapper (which cannot import Python) must agree.
  2. Scheduled wrappers propagate failure: the main python call's exit code is captured
     on the very next line and returned; no other line reads %ERRORLEVEL%.
  3. A job that did nothing exits non-zero (the snapshot on an empty fetch).

The human-judgement rules (does a test assert anything; would it fail if the code
were broken) are in .claude/skills/verify-before-commit/SKILL.md.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import ACTIVE_FT_PERIOD  # noqa: E402

PERIOD_RE = re.compile(r"20\d\dQ\d\w*")

# Code allowed to name a period in a string literal, and why.
PERIOD_LITERAL_ALLOWED = {
    "config/settings.py": "the single definition",
    "forward_test/snapshot.py": "the period registry (start dates, baseline modes)",
    "forward_test/build_q4_books.py": "a per-quarter builder, frozen with its quarter",
    "broker/test_book_guard.py": "regression test on the retired 2026Q4 book",
    "dashboard/app.py": "named HISTORICAL periods only (checked below)",
}
# The dashboard may cite concluded periods by name; never the active one.
DASHBOARD_HISTORICAL = {"2026Q3", "2026Q3_parallel", "2026Q4", "2026Q4_retired"}


def _tracked(pattern: str) -> list[str]:
    try:
        out = subprocess.check_output(["git", "ls-files", pattern], cwd=ROOT, text=True)
        return out.split()
    except (OSError, subprocess.CalledProcessError):
        return [str(p.relative_to(ROOT)).replace("\\", "/") for p in ROOT.rglob(pattern)
                if "venv" not in p.parts]


def _string_literals(path: Path) -> list[tuple[int, str]]:
    """(line, text) of every string literal that is not a docstring."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = set()
    for n in ast.walk(tree):
        if (isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                           ast.ClassDef)) and n.body
                and isinstance(n.body[0], ast.Expr)
                and isinstance(n.body[0].value, ast.Constant)):
            docs.add(id(n.body[0].value))
    return [(n.lineno, n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docs]


# ── 1. one active period ────────────────────────────────────────────────────────

def test_no_code_hardcodes_a_forward_test_period():
    bad = []
    for f in _tracked("*.py"):
        if f.startswith("tests/") or f.startswith("forward_test/20"):
            continue
        for line, text in _string_literals(ROOT / f):
            for m in PERIOD_RE.findall(text):
                if f not in PERIOD_LITERAL_ALLOWED:
                    bad.append(f"{f}:{line} names {m!r}")
                elif f == "dashboard/app.py" and m not in DASHBOARD_HISTORICAL:
                    bad.append(f"{f}:{line} names {m!r} (use ACTIVE_FT_PERIOD)")
    assert not bad, ("period hardcoded -- read config.settings.ACTIVE_FT_PERIOD "
                     "instead:\n  " + "\n  ".join(bad))


def test_registry_and_broker_follow_the_setting():
    import forward_test.snapshot as snap
    from broker import ibkr_paper
    assert ACTIVE_FT_PERIOD in snap.PERIODS, "active period missing from the registry"
    assert ibkr_paper.ACTIVE_PERIOD == ACTIVE_FT_PERIOD
    assert ibkr_paper.Q4_DIR.name == ACTIVE_FT_PERIOD


def test_snapshot_wrapper_default_matches_the_setting():
    bat = (ROOT / "forward_test" / "run_snapshot.bat").read_text(encoding="utf-8")
    m = re.search(r'^if "%PERIOD%"=="" set PERIOD=(\S+)\s*$', bat, re.M)
    assert m, "run_snapshot.bat lost its default-period line"
    assert m.group(1) == ACTIVE_FT_PERIOD, (
        f"run_snapshot.bat records {m.group(1)} but the active period is "
        f"{ACTIVE_FT_PERIOD}: the nightly task would snapshot the wrong test")


def test_active_period_directory_has_its_books():
    d = ROOT / "forward_test" / ACTIVE_FT_PERIOD
    assert (d / "incumbent_book.csv").exists(), f"{d} has no incumbent_book.csv"


# ── 2. scheduled wrappers propagate failure ─────────────────────────────────────

BATS = _tracked("*.bat")
PY_CALL = re.compile(r'python(\.exe)?"?\s', re.I)       # an invocation, not PYTHONPATH


def _is_python_call(ln: str) -> bool:
    return bool(PY_CALL.search(ln)) and not ln.lower().startswith(("set ", "rem"))


def _code_lines(bat: str) -> list[str]:
    return [ln.strip() for ln in bat.splitlines()
            if ln.strip() and not ln.strip().upper().startswith("REM")]


def test_there_are_wrappers_to_check():
    assert len(BATS) >= 3, BATS


@pytest.mark.parametrize("bat", BATS)
def test_wrapper_captures_python_exit_code_immediately(bat):
    lines = _code_lines((ROOT / bat).read_text(encoding="utf-8"))
    captures = [i for i, ln in enumerate(lines) if ln == "set RC=%ERRORLEVEL%"]
    assert len(captures) == 1, f"{bat}: expected exactly one 'set RC=%ERRORLEVEL%'"
    prev = lines[captures[0] - 1]
    assert _is_python_call(prev), (
        f"{bat}: RC is captured after {prev!r}, not after the python call. Any command "
        f"in between (even a redirected echo) resets ERRORLEVEL to 0")
    reads = [ln for ln in lines if "%ERRORLEVEL%" in ln.upper() or "ERRORLEVEL" in ln.upper()]
    assert reads == ["set RC=%ERRORLEVEL%"], f"{bat}: other ERRORLEVEL reads: {reads}"


@pytest.mark.parametrize("bat", BATS)
def test_wrapper_returns_the_captured_code(bat):
    lines = _code_lines((ROOT / bat).read_text(encoding="utf-8"))
    assert lines[-1] == "endlocal & exit /b %RC%", (
        f"{bat} ends with {lines[-1]!r}: Task Scheduler would record the wrong result")
    assert sum(ln.startswith("set RC=") for ln in lines) == 1, f"{bat}: RC reassigned"


@pytest.mark.parametrize("bat", BATS)
def test_uncaptured_python_calls_are_explained(bat):
    """A python call whose exit code is NOT the job's result must say why, in a REM
    directly above it -- otherwise it reads like an oversight, and may be one."""
    raw = [ln.strip() for ln in (ROOT / bat).read_text(encoding="utf-8").splitlines()]
    for i, ln in enumerate(raw):
        if not _is_python_call(ln):
            continue
        nxt = next((x for x in raw[i + 1:] if x and not x.upper().startswith("REM")), "")
        if nxt == "set RC=%ERRORLEVEL%":
            continue
        above = []
        for x in reversed(raw[:i]):
            if x.upper().startswith("REM"):
                above.append(x)
            elif x.lower().startswith("if not exist"):
                continue
            else:
                break
        assert any(w in " ".join(above).lower() for w in ("exit code", " rc", "rc ")), (
            f"{bat}:{i + 1}: python call whose exit code is ignored, with no REM "
            f"explaining why its failure is still visible")


# ── 3. a job that did nothing exits non-zero ────────────────────────────────────

@pytest.fixture
def snapshot_sandbox(tmp_path, monkeypatch):
    import forward_test.snapshot as snap
    monkeypatch.setattr(snap, "SNAP", tmp_path / "price_snapshots.csv")
    monkeypatch.setattr(snap, "load_books", lambda: ({}, {}, ["AAA"]))
    monkeypatch.setattr(snap, "fetch", lambda *a: pd.DataFrame())
    return snap


def test_snapshot_empty_fetch_after_sessions_are_recorded_exits_1(snapshot_sandbox):
    pd.DataFrame({"date": ["2026-09-30", "2026-10-01"], "ticker": "AAA",
                  "close": 1.0, "is_carried": 0}).to_csv(snapshot_sandbox.SNAP, index=False)
    with pytest.raises(SystemExit) as e:
        snapshot_sandbox.main()
    assert e.value.code == 1


def test_snapshot_empty_fetch_before_any_session_is_not_an_error(snapshot_sandbox, capsys):
    assert snapshot_sandbox.main() is None
    assert "NOT STARTED" in capsys.readouterr().out


def test_snapshot_unreadable_file_is_never_called_not_started(snapshot_sandbox):
    snapshot_sandbox.SNAP.write_text("", encoding="utf-8")          # the 0-row incident
    with pytest.raises(SystemExit):
        snapshot_sandbox.main()


def test_prices_updater_zero_rows_exits_1(monkeypatch):
    from data import prices_updater as pu
    monkeypatch.setattr(pu, "update_recent_prices", lambda *a, **k: 0)
    monkeypatch.setattr(pu, "_latest_recorded_date", lambda: None)
    assert pu.main(["--tickers", "AAA"]) == 1
