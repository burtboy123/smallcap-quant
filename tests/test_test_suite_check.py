"""
tests/test_test_suite_check.py -- the health check's test_suite verdict.

Until 2026-11-17 the check ignored pytest's exit code and read only FAILED lines. One
test file that cannot be imported makes pytest stop before running ANY test (exit 2,
"1 error"), with no FAILED line, so the check reported PASS, "0 passed": the Monday
regression guard was off and said it was on. Proven on the real pytest before the fix.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from monitoring import checks as ck  # noqa: E402


class _Done:
    def __init__(self, rc, stdout):
        self.returncode, self.stdout, self.stderr = rc, stdout, ""


@pytest.fixture
def fake_pytest(monkeypatch, tmp_path):
    """Replace the pytest subprocess with a canned result; record the call."""
    calls = []
    monkeypatch.setattr(ck, "ROOT", tmp_path)
    monkeypatch.setattr(ck, "_known_failures", lambda: set())

    def install(rc, stdout):
        def run(args, **kw):
            calls.append((args, kw))
            return _Done(rc, stdout)
        monkeypatch.setattr(ck.subprocess, "run", run)
        return calls
    return install


def test_clean_run_passes_and_uses_a_private_temp_dir(fake_pytest, tmp_path):
    """A fresh temp dir per run, outside the project (monitoring/state is backed up),
    removed afterwards; the old fixed dir is cleared when this logon can."""
    legacy = tmp_path / "monitoring" / "state" / "pytest_tmp"
    (legacy / "junk").mkdir(parents=True)
    calls = fake_pytest(0, "....\n505 passed in 90.0s\n")
    r = ck.check_test_suite()
    assert r.status == ck.PASS and r.metrics["passed"] == 505
    args = calls[0][0]
    bt = Path(next(a for a in args if a.startswith("--basetemp=")).split("=", 1)[1])
    assert bt.name.startswith("quant_health_pytest_")
    assert tmp_path not in bt.parents                      # not inside the project
    assert not bt.exists()                                 # removed after the run
    assert not legacy.exists()
    assert "no:cacheprovider" in args
    ck.check_test_suite()
    bt2 = Path(next(a for a in calls[1][0] if a.startswith("--basetemp=")).split("=", 1)[1])
    assert bt2 != bt                                       # never shared between runs


def test_new_failure_is_critical(fake_pytest):
    fake_pytest(1, "FAILED tests/test_a.py::test_x - assert 1 == 2\n1 failed, 504 passed\n")
    r = ck.check_test_suite()
    assert r.status == ck.FAIL and r.severity == ck.CRITICAL
    assert r.metrics["new_failures"] == ["tests/test_a.py::test_x"]


def test_collection_error_is_not_a_pass(fake_pytest):
    fake_pytest(2, "ERROR tests/test_b.py - ModuleNotFoundError: No module named 'x'\n"
                   "!!!!!!! Interrupted: 1 error during collection !!!!!!!\n1 error in 0.3s\n")
    r = ck.check_test_suite()
    assert r.status == ck.FAIL and r.severity == ck.CRITICAL
    assert "tests/test_b.py" in r.detail


def test_no_tests_collected_is_not_a_pass(fake_pytest):
    fake_pytest(5, "no tests ran in 0.01s\n")
    assert ck.check_test_suite().status == ck.FAIL


def test_everything_skipped_is_not_a_pass(fake_pytest):
    """Exit 0, nothing actually tested (e.g. every test skips on a missing package)."""
    fake_pytest(0, "sssss\n5 skipped in 0.1s\n")
    assert ck.check_test_suite().status == ck.FAIL


def test_internal_error_after_some_passes_is_not_a_pass(fake_pytest):
    fake_pytest(3, "....\nINTERNALERROR> Traceback (most recent call last):\n4 passed in 1.0s\n")
    assert ck.check_test_suite().status == ck.FAIL


def test_exit_1_with_unreadable_output_is_not_a_pass(fake_pytest):
    fake_pytest(1, "something pytest printed that this check cannot parse\n3 passed\n")
    assert ck.check_test_suite().status == ck.FAIL


def test_a_real_broken_test_file_fails_the_check(tmp_path, monkeypatch):
    """The real thing: pytest on a project whose one test file cannot be imported."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_good.py").write_text("def test_ok():\n    assert True\n")
    (tmp_path / "tests" / "test_broken.py").write_text(
        "import does_not_exist_xyz\n\ndef test_never():\n    assert True\n")
    monkeypatch.setattr(ck, "ROOT", tmp_path)
    monkeypatch.setattr(ck, "_known_failures", lambda: set())
    r = ck.check_test_suite(timeout_s=120)
    assert r.status == ck.FAIL, r.summary
    assert r.metrics["exit_code"] == 2
    # and the same project without the broken file passes
    (tmp_path / "tests" / "test_broken.py").unlink()
    assert ck.check_test_suite(timeout_s=120).status == ck.PASS
