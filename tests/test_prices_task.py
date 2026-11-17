"""
tests/test_prices_task.py

The scheduled price refresh (QuantPricesUpdate) and the health check that watches it.

The prices table drifted 8 sessions stale because nothing ran prices_updater on a
schedule. These pin the pieces that close that gap: the wrapper really runs the updater
and propagates its exit code, the registration script registers THAT wrapper under the
name the monitor looks for, and the monitor fails when the job is missing, failing or
not running. No Task Scheduler is touched: _task_info is monkeypatched.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from monitoring import checks as ck  # noqa: E402
from tests.test_runbook_commands import problems_with  # noqa: E402

BAT = ROOT / "data" / "run_prices_update.bat"
PS1 = ROOT / "monitoring" / "register_tasks.ps1"
NOW = datetime(2026, 10, 1, 16, 0)


# ── the wrapper and the registration agree with the monitor ─────────────────────

class TestWiring:
    def test_wrapper_runs_a_working_updater_command(self):
        txt = BAT.read_text(encoding="utf-8")
        cmd = re.search(r"-m data\.prices_updater[^>\r\n]*", txt).group(0).strip()
        assert problems_with(f"python {cmd}") == []

    def test_wrapper_propagates_the_exit_code(self):
        lines = [l.strip() for l in BAT.read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.strip().upper().startswith("REM")]
        call = next(i for i, l in enumerate(lines) if "prices_updater" in l)
        # Nothing between the python call and capturing ERRORLEVEL: a redirected echo
        # resets it, which is how the snapshot wrapper hid nine days of failure.
        assert lines[call + 1] == "set RC=%ERRORLEVEL%"
        assert lines[-1] == "endlocal & exit /b %RC%"

    def test_wrapper_log_line_is_what_the_monitor_parses(self, tmp_path, monkeypatch):
        txt = BAT.read_text(encoding="utf-8")
        assert "monitoring\\state\\prices_update_log.txt" in txt
        end_line = next(l for l in txt.splitlines() if "prices update end" in l)
        written = end_line.split("echo ", 1)[1].replace(
            "%DATE% %TIME%", "01.10.2026 15.02.11,40").replace("%RC%", "1")
        log = tmp_path / "prices_update_log.txt"
        log.write_text(written + "\n", encoding="utf-8")
        monkeypatch.setattr(ck, "PRICES_LOG", log)
        assert ck._prices_log_exit() == (1, "01.10.2026 15.02.11,40")

    def test_registration_script_registers_this_wrapper_under_the_monitored_name(self):
        src = PS1.read_text(encoding="utf-8-sig")
        block = src[src.index("Prices = @{"):]
        assert f'TaskName = "{ck.PRICES_TASK_NAME}"' in block
        assert 'Bat      = "data\\run_prices_update.bat"' in block
        assert 'Time     = "15:00"' in block and 'Days     = @("Monday")' in block

    def test_registration_script_is_ascii_with_bom(self):
        raw = PS1.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")        # PS 5.1 reads BOM-less as cp1252
        raw[3:].decode("ascii")

    def test_default_job_is_still_the_health_check(self):
        src = PS1.read_text(encoding="utf-8-sig")
        assert '[string]$Job = "Health"' in src
        assert f'TaskName = "{ck.HEALTH_TASK_NAME}"' in src


# ── the monitor ─────────────────────────────────────────────────────────────────

@pytest.fixture
def task(monkeypatch, tmp_path):
    """Set what Task Scheduler and the job's log say."""
    log = tmp_path / "prices_update_log.txt"
    monkeypatch.setattr(ck, "PRICES_LOG", log)

    def set_(info: dict, log_exit: int | None = None):
        monkeypatch.setattr(ck, "_task_info", lambda name: info)
        if log_exit is not None:
            log.write_text(f"[01.10.2026 15.02.11,40] prices update end "
                           f"(exit {log_exit})\n", encoding="utf-8")
    return set_


def _ran(hours_ago: float, **kw) -> dict:
    return {"state": "Ready", "last_result": 0,
            "last_run": (NOW - timedelta(hours=hours_ago)).isoformat(),
            "next_run": "2026-10-02T15:00:00", **kw}


class TestCheckPricesTask:
    def test_not_registered_fails_with_a_hint_that_registers_it(self, task):
        task({})
        r = ck.check_prices_task(NOW)
        assert (r.status, r.severity) == (ck.FAIL, ck.WARNING)
        assert "-Job Prices" in r.runbook
        assert problems_with(r.runbook) == []

    def test_disabled_fails(self, task):
        task(_ran(2, state="Disabled"))
        r = ck.check_prices_task(NOW)
        assert r.status == ck.FAIL and "DISABLED" in r.summary
        assert ck.PRICES_TASK_NAME in r.runbook

    def test_log_exit_outranks_a_zero_last_result(self, task):
        task(_ran(2), log_exit=1)
        r = ck.check_prices_task(NOW)
        assert r.status == ck.FAIL and "exited 1" in r.summary

    def test_nonzero_last_result_fails(self, task):
        task(_ran(2, last_result=1))
        assert ck.check_prices_task(NOW).status == ck.FAIL

    def test_old_run_fails(self, task):
        task(_ran(ck.MAX_PRICES_TASK_AGE_HOURS + 1), log_exit=0)
        r = ck.check_prices_task(NOW)
        assert r.status == ck.FAIL and "ago" in r.summary

    def test_a_full_week_between_runs_is_not_a_failure(self, task):
        task(_ran(169), log_exit=0)              # weekly: Mon 15:00 -> next Mon 16:00
        assert ck.check_prices_task(NOW).status == ck.PASS

    def test_recent_success_passes(self, task):
        task(_ran(1), log_exit=0)
        assert ck.check_prices_task(NOW).status == ck.PASS

    def test_registered_but_never_run_is_a_warn_not_a_fail(self, task):
        task({"state": "Ready", "last_result": None, "last_run": None,
              "next_run": "2026-10-01T15:00:00"})
        assert ck.check_prices_task(NOW).status == ck.WARN

    def test_windows_never_run_code_is_not_a_failure(self, task):
        """Seen for real on 2026-09-30: a freshly registered task reports
        LastTaskResult=267011 and a 1999 LastRunTime. That is 'not run yet'."""
        task({"state": "Ready", "last_result": 267011,
              "last_run": "1999-11-30T00:00:00", "next_run": "2026-10-01T15:00:00"})
        r = ck.check_prices_task(NOW)
        assert r.status == ck.WARN and "not run yet" in r.summary

    def test_windows_running_code_is_not_a_failure(self, task):
        task(_ran(0.01, last_result=267009))
        assert ck.check_prices_task(NOW).status == ck.PASS

    def test_never_blocks_a_rebalance(self, task):
        """prices_db_fresh measures the table and is the check that matters."""
        for info, code in [({}, None), (_ran(2), 1), (_ran(500), 0)]:
            task(info, code)
            assert ck.check_prices_task(NOW).severity != ck.CRITICAL

    def test_is_part_of_the_health_run(self, task, monkeypatch):
        task({})
        for name in dir(ck):
            if name.startswith("check_") and name != "check_prices_task":
                monkeypatch.setattr(ck, name, lambda *a, **k: None)
        assert any(r is not None and r.name == "prices_task"
                   for r in ck.run_all(skip_tests=True))
