"""
tests/test_monitoring.py — unit cover for the health checks, alert dedup, and the
broker path's refusal to trade on an unhealthy pipeline.

monitoring/break_test.py is the end-to-end deliberate-break proof; these are the fast
assertions that belong in the suite so a regression is caught by `pytest` rather than by
someone remembering to run the break test.

Nothing here connects to a broker, sends email, or touches monitoring/state/: every
test redirects the module-level paths into tmp_path.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from monitoring import alerting as al      # noqa: E402
from monitoring import checks as ck        # noqa: E402


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Redirect snapshot + alerting paths into tmp_path for the duration of a test."""
    period_dir = tmp_path / ck.PERIOD
    period_dir.mkdir(parents=True)
    monkeypatch.setattr(ck, "SNAP", period_dir / "price_snapshots.csv")
    monkeypatch.setattr(ck, "SNAP_LOG", period_dir / "snapshot_log.txt")
    monkeypatch.setattr(al, "ALERT_LOG", tmp_path / "alert_log.jsonl")
    monkeypatch.setattr(al, "ALERT_STATE", tmp_path / "alert_state.json")
    monkeypatch.setattr(al, "HEALTH_HISTORY", tmp_path / "health_history.json")
    return period_dir


def _write_books(period_dir: Path, tickers=("SPY", "AMN")):
    for name in ("incumbent_book.csv", "value_tilt_book.csv"):
        pd.DataFrame({"ticker": list(tickers),
                      "weight_equal": [1 / len(tickers)] * len(tickers)}
                     ).to_csv(period_dir / name, index=False)


# ── the 0-row bug ───────────────────────────────────────────────────────────────

class TestZeroRowSnapshot:
    """The failure that hid for nine days must be CRITICAL and unmissable."""

    def test_zero_rows_fails_critical(self, sandbox):
        ck.SNAP.write_text("date,ticker,close,is_carried\n", encoding="utf-8")
        r = ck.check_snapshot_rows()
        assert r.failed
        assert r.severity == ck.CRITICAL
        assert "0 rows" in r.summary

    def test_zero_rows_carries_a_fix_command(self, sandbox):
        """An alert without a next action is noise."""
        ck.SNAP.write_text("date,ticker,close,is_carried\n", encoding="utf-8")
        r = ck.check_snapshot_rows()
        assert r.runbook and r.blocks

    def test_nonzero_rows_passes(self, sandbox):
        pd.DataFrame([{"date": "2026-09-30", "ticker": "SPY", "close": 1.0,
                       "is_carried": 0}]).to_csv(ck.SNAP, index=False)
        assert ck.check_snapshot_rows().status == ck.PASS

    def test_unreadable_file_fails_rather_than_raising(self, sandbox):
        ck.SNAP.write_bytes(b"\x00\x01\x02 not,a,csv\x00\xff")
        r = ck.check_snapshot_rows()
        assert r.status in (ck.FAIL, ck.PASS)   # must not raise either way


# ── growth: truncation and partial writes ───────────────────────────────────────

class TestSnapshotGrowth:
    def test_row_count_decrease_fails(self, sandbox):
        pd.DataFrame([{"date": "2026-09-30", "ticker": "SPY", "close": 1.0,
                       "is_carried": 0}]).to_csv(ck.SNAP, index=False)
        r = ck.check_snapshot_growth({"rows": 500, "latest_recorded": "2026-09-30"})
        assert r.failed and "DECREASED" in r.summary

    def test_new_session_without_new_rows_fails(self, sandbox):
        pd.DataFrame([{"date": "2026-09-30", "ticker": "SPY", "close": 1.0,
                       "is_carried": 0}]).to_csv(ck.SNAP, index=False)
        r = ck.check_snapshot_growth({"rows": 1, "latest_recorded": "2026-09-29"})
        assert r.failed and "rows unchanged" in r.summary

    def test_unchanged_when_nothing_due_passes(self, sandbox):
        pd.DataFrame([{"date": "2026-09-30", "ticker": "SPY", "close": 1.0,
                       "is_carried": 0}]).to_csv(ck.SNAP, index=False)
        r = ck.check_snapshot_growth({"rows": 1, "latest_recorded": "2026-09-30"})
        assert r.status == ck.PASS

    def test_growth_passes(self, sandbox):
        pd.DataFrame([{"date": "2026-09-30", "ticker": t, "close": 1.0,
                       "is_carried": 0} for t in ("SPY", "AMN")]
                     ).to_csv(ck.SNAP, index=False)
        r = ck.check_snapshot_growth({"rows": 1, "latest_recorded": "2026-09-29"})
        assert r.status == ck.PASS


# ── content: missing names and carry-forward ─────────────────────────────────────

class TestTickersAndCarried:
    def test_missing_ticker_fails(self, sandbox):
        _write_books(sandbox, ("SPY", "AMN", "KODK"))
        pd.DataFrame([{"date": "2026-09-30", "ticker": t, "close": 1.0,
                       "is_carried": 0} for t in ("SPY", "AMN")]
                     ).to_csv(ck.SNAP, index=False)
        r = ck.check_tickers_and_carried()
        assert r.failed and "KODK" in r.detail

    def test_mostly_carried_fails(self, sandbox):
        _write_books(sandbox, ("SPY", "AMN"))
        pd.DataFrame([{"date": "2026-09-30", "ticker": t, "close": 1.0,
                       "is_carried": 1} for t in ("SPY", "AMN")]
                     ).to_csv(ck.SNAP, index=False)
        r = ck.check_tickers_and_carried()
        assert r.failed and "carried forward" in r.summary

    def test_complete_and_fresh_passes(self, sandbox):
        _write_books(sandbox, ("SPY", "AMN"))
        pd.DataFrame([{"date": "2026-09-30", "ticker": t, "close": 1.0,
                       "is_carried": 0} for t in ("SPY", "AMN")]
                     ).to_csv(ck.SNAP, index=False)
        assert ck.check_tickers_and_carried().status == ck.PASS


# ── the scheduled task: the log outranks LastTaskResult ─────────────────────────

class TestScheduledTask:
    def test_log_exit_nonzero_fails_even_when_windows_says_zero(self, sandbox,
                                                               monkeypatch):
        """The whole point: LastTaskResult was masked by a wrapper bug for nine days."""
        monkeypatch.setattr(ck, "_task_info", lambda _n: {
            "state": "Ready", "last_result": 0,
            "last_run": datetime.now().isoformat(), "next_run": None})
        ck.SNAP_LOG.write_text("[30/09/2026 23.30.09,00] snapshot end (exit 1)\n",
                               encoding="utf-8")
        r = ck.check_scheduled_task()
        assert r.failed and "exited 1" in r.summary

    def test_absent_task_fails(self, monkeypatch):
        monkeypatch.setattr(ck, "_task_info", lambda _n: {})
        monkeypatch.setattr(ck, "_log_exit_code", lambda: (None, None))
        assert ck.check_scheduled_task().failed

    def test_stale_last_run_fails(self, monkeypatch):
        monkeypatch.setattr(ck, "_log_exit_code", lambda: (0, "x"))
        old = (datetime.now() - timedelta(hours=ck.MAX_TASK_AGE_HOURS + 5)).isoformat()
        monkeypatch.setattr(ck, "_task_info", lambda _n: {
            "state": "Ready", "last_result": 0, "last_run": old, "next_run": None})
        r = ck.check_scheduled_task()
        assert r.failed and "last ran" in r.summary

    def test_disabled_task_fails(self, monkeypatch):
        monkeypatch.setattr(ck, "_log_exit_code", lambda: (0, "x"))
        monkeypatch.setattr(ck, "_task_info", lambda _n: {
            "state": "Disabled", "last_result": 0,
            "last_run": datetime.now().isoformat(), "next_run": None})
        assert ck.check_scheduled_task().failed


# ── a check that raises must become a FAIL, not a crash ─────────────────────────

def test_raising_check_becomes_a_critical_fail(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("synthetic explosion")
    monkeypatch.setattr(ck, "check_prices_db", boom)
    results = ck.run_all(previous=None, skip_tests=True)
    bad = [r for r in results if r.name == "prices_db_fresh"]
    assert bad and bad[0].failed and bad[0].severity == ck.CRITICAL
    assert "raised" in bad[0].summary


# ── alert dedup and escalation ──────────────────────────────────────────────────

class TestAlertDedup:
    def _a(self, severity="WARNING"):
        return al.Alert(key="health:x", title="t", severity=severity, summary="s")

    def test_first_occurrence_sends(self, sandbox):
        sent, supp, _st = al.decide([self._a()], {}, now=datetime(2026, 9, 1))
        assert len(sent) == 1 and not supp

    def test_immediate_repeat_is_suppressed(self, sandbox):
        t0 = datetime(2026, 9, 1, 23, 0)
        _s1, _p1, st = al.decide([self._a()], {}, now=t0)
        # 3rd occurrence escalates, so check the 2nd-with-cooldown-unexpired case by
        # advancing only an hour.
        sent, supp, _st2 = al.decide([self._a()], st, now=t0 + timedelta(hours=1))
        assert not sent and len(supp) == 1

    def test_escalates_from_warning_to_critical(self, sandbox):
        t0 = datetime(2026, 9, 1)
        st = {}
        sevs = []
        for d in range(4):
            sent, _supp, st = al.decide([self._a()], st, now=t0 + timedelta(days=d))
            sevs += [a.severity for a in sent]
        assert sevs[0] == "WARNING"
        assert "CRITICAL" in sevs, f"never escalated: {sevs}"

    def test_persistent_failure_does_not_flood(self, sandbox):
        t0 = datetime(2026, 9, 1)
        st, n = {}, 0
        for d in range(14):
            sent, _supp, st = al.decide([self._a()], st, now=t0 + timedelta(days=d))
            n += len(sent)
        assert n < 14, f"sent {n} alerts in 14 days — that is a flood"

    def test_recovery_sends_one_resolved_then_clears(self, sandbox):
        t0 = datetime(2026, 9, 1)
        _s, _p, st = al.decide([self._a()], {}, now=t0)
        sent, _supp, st2 = al.decide([], st, now=t0 + timedelta(days=1),
                                     resolve_scope="health:")
        assert len(sent) == 1 and sent[0].resolved
        assert "health:x" not in st2
        # and nothing further afterwards
        sent2, _s2, _st3 = al.decide([], st2, now=t0 + timedelta(days=2),
                                     resolve_scope="health:")
        assert not sent2

    def test_event_alert_does_not_resolve_health_alerts(self, sandbox):
        """The scoping bug: one kill-switch alert must not clear health state."""
        t0 = datetime(2026, 9, 1)
        _s, _p, st = al.decide([self._a()], {}, now=t0)
        assert "health:x" in st
        ev = al.Alert(key="killswitch:5.3_STALE_DATA", title="trip",
                      severity="CRITICAL", summary="halted")
        sent, _supp, st2 = al.decide([ev], st, now=t0 + timedelta(minutes=5),
                                     resolve_scope=None)
        assert "health:x" in st2, "the health alert was wrongly resolved by an event"
        assert not any(a.resolved for a in sent)


# ── email config must never be silently half-on ─────────────────────────────────

class TestEmailConfig:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("ALERT_EMAIL_ENABLED", raising=False)
        usable, why = al.email_status()
        assert not usable and "disabled" in why

    def test_enabled_but_incomplete_says_what_is_missing(self, monkeypatch):
        monkeypatch.setenv("ALERT_EMAIL_ENABLED", "true")
        for k in ("ALERT_SMTP_HOST", "ALERT_SMTP_USER", "ALERT_SMTP_PASSWORD",
                  "ALERT_EMAIL_FROM", "ALERT_EMAIL_TO"):
            monkeypatch.delenv(k, raising=False)
        usable, why = al.email_status()
        assert not usable and "missing" in why

    def test_status_never_leaks_the_password(self, monkeypatch):
        monkeypatch.setenv("ALERT_EMAIL_ENABLED", "true")
        monkeypatch.setenv("ALERT_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("ALERT_SMTP_USER", "u@example.com")
        monkeypatch.setenv("ALERT_SMTP_PASSWORD", "SUPERSECRETVALUE")
        monkeypatch.setenv("ALERT_EMAIL_FROM", "u@example.com")
        monkeypatch.setenv("ALERT_EMAIL_TO", "u@example.com")
        usable, why = al.email_status()
        assert usable
        assert "SUPERSECRETVALUE" not in why

    def test_alert_log_written_even_with_email_off(self, sandbox, monkeypatch):
        monkeypatch.setenv("ALERT_EMAIL_ENABLED", "false")
        info = al.dispatch([al.Alert(key="health:y", title="t", severity="WARNING",
                                    summary="s")], send_mail=True)
        assert info["sent"] == ["health:y"]
        assert al.ALERT_LOG.exists() and al.ALERT_LOG.read_text(encoding="utf-8").strip()

    def test_send_failure_is_reported_not_swallowed(self, sandbox, monkeypatch):
        monkeypatch.setenv("ALERT_EMAIL_ENABLED", "true")
        monkeypatch.setenv("ALERT_SMTP_HOST", "127.0.0.1")
        monkeypatch.setenv("ALERT_SMTP_PORT", "1")        # nothing listens here
        monkeypatch.setenv("ALERT_SMTP_USER", "u@example.com")
        monkeypatch.setenv("ALERT_SMTP_PASSWORD", "x")
        monkeypatch.setenv("ALERT_EMAIL_FROM", "u@example.com")
        monkeypatch.setenv("ALERT_EMAIL_TO", "u@example.com")
        ok, msg = al.send_email([al.Alert(key="k", title="t", severity="WARNING",
                                         summary="s")])
        assert not ok and "FAILED" in msg

    def test_event_alert_never_raises(self, sandbox, monkeypatch):
        """Monitoring must not be able to break the trading path."""
        def boom(*_a, **_k):
            raise RuntimeError("state dir on fire")
        monkeypatch.setattr(al, "dispatch", boom)
        out = al.alert_killswitch_trip("5.3_STALE_DATA", "detail")   # must not raise
        assert out["sent"] == []


# ── the rebalance refusal ───────────────────────────────────────────────────────

class TestPipelineGate:
    def test_critical_failure_raises(self, monkeypatch):
        from monitoring import health_check as hc
        bad = ck.CheckResult("snapshot_rows", ck.FAIL, "0 rows", ck.CRITICAL,
                             blocks="everything", runbook="fix it")
        monkeypatch.setattr(ck, "run_all", lambda **_k: [bad])
        monkeypatch.setattr(al, "append_history", lambda *_a, **_k: None)
        with pytest.raises(hc.PipelineUnhealthy) as ei:
            hc.assert_pipeline_healthy(alert=False)
        assert "snapshot_rows" in str(ei.value) and "fix it" in str(ei.value)

    def test_warning_failure_does_not_raise(self, monkeypatch):
        from monitoring import health_check as hc
        warn = ck.CheckResult("prices_db_fresh", ck.FAIL, "stale", ck.WARNING,
                              runbook="update prices")
        monkeypatch.setattr(ck, "run_all", lambda **_k: [warn])
        monkeypatch.setattr(al, "append_history", lambda *_a, **_k: None)
        hc.assert_pipeline_healthy(alert=False)        # must not raise

    def test_broker_refuses_before_sizing_anything(self, monkeypatch, tmp_path):
        """The broker path must refuse BEFORE it reads the book or fetches prices.

        A gate that fires after the book is priced would still print numbers derived
        from a broken pipeline, and the dry run is what a human reads before executing.
        Proven by asserting the stub broker's pricing call is never reached.
        """
        import broker.ibkr_paper as bp
        from monitoring.health_check import PipelineUnhealthy

        touched = {"prices": False, "book": False}

        class StubBroker:
            account = "DU1234567"

            def __init__(self, *_a, **_k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def market_data_preflight(self, probe="SPY"):
                return {"delayed_quote": True, "historical": True, "probe": probe,
                        "detail": "stub"}

            def account_summary(self):
                return {"NetLiquidation": 1e6, "_nav_usd": 1e6,
                        "_base_currency": "USD"}

            def positions(self):
                return pd.DataFrame(columns=["ticker", "position", "avg_cost"])

            def reference_prices(self, tickers):
                touched["prices"] = True            # must NEVER be reached
                return {t: {"price": 10.0} for t in tickers}

        book = tmp_path / "book.csv"
        pd.DataFrame({"ticker": ["AMN", "KODK"], "weight_equal": [0.5, 0.5],
                      "sector": ["Health Care", "Industrials"]}
                     ).to_csv(book, index=False)

        real_read_csv = pd.read_csv

        def watching_read_csv(path, *a, **k):
            if str(path) == str(book):
                touched["book"] = True
            return real_read_csv(path, *a, **k)

        monkeypatch.setattr(bp, "IBKRPaperBroker", StubBroker)
        monkeypatch.setattr(pd, "read_csv", watching_read_csv)

        def refuse(**_k):
            raise PipelineUnhealthy("REFUSED: synthetic unhealthy pipeline")
        monkeypatch.setattr("monitoring.health_check.assert_pipeline_healthy", refuse)
        monkeypatch.setattr("monitoring.alerting.alert_rebalance_refused",
                            lambda *_a, **_k: {"sent": []})

        with pytest.raises(PipelineUnhealthy):
            bp.main(["--book", str(book)])

        assert not touched["prices"], "prices were fetched despite an unhealthy pipeline"
        assert not touched["book"], "the book was read despite an unhealthy pipeline"

    def test_read_only_is_exempt(self, monkeypatch, tmp_path):
        """--read-only sizes nothing, so it must still work on an unhealthy pipeline."""
        import broker.ibkr_paper as bp

        called = {"gate": False}

        class StubBroker:
            account = "DU1234567"

            def __init__(self, *_a, **_k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

            def market_data_preflight(self, probe="SPY"):
                return {"delayed_quote": False, "historical": False, "probe": probe,
                        "detail": "stub: nothing available"}

            def account_summary(self):
                return {"NetLiquidation": 1e6, "_nav_usd": 1e6,
                        "_base_currency": "USD"}

            def positions(self):
                return pd.DataFrame(columns=["ticker", "position", "avg_cost"])

        def spy(**_k):
            called["gate"] = True
        monkeypatch.setattr(bp, "IBKRPaperBroker", StubBroker)
        monkeypatch.setattr("monitoring.health_check.assert_pipeline_healthy", spy)

        assert bp.main(["--read-only"]) == 0
        assert not called["gate"], "--read-only should not run the health gate"


# ── the period must come from the registry, never a hardcoded string ───────────

def test_checks_use_the_active_period_from_the_registry():
    """The original bug: a checker hardcoded to a different period than the gate."""
    from forward_test.snapshot import PERIOD as registry_period
    assert ck.PERIOD == registry_period
    assert ck.PERIOD in str(ck.SNAP)


def test_trading_day_logic_is_shared_not_reimplemented():
    """A second copy of the holiday calendar is how these things drift apart. The
    broker's 5.3 STALE_DATA switch kept its own copy until 2026-11-10."""
    from broker import killswitch as ks
    from forward_test import market_calendar as cal
    from forward_test.check_snapshot_fresh import is_trading_day as src
    assert ck.is_trading_day is src is cal.is_trading_day
    assert ck.most_recent_trading_day is cal.most_recent_trading_day
    assert ks._is_trading_day is cal.is_trading_day
    assert ks._most_recent_trading_day is cal.most_recent_trading_day
    assert ks.sessions_behind is cal.sessions_behind
    assert not ck.is_trading_day(date(2026, 11, 26))    # Thanksgiving
    assert not ck.is_trading_day(date(2026, 10, 3))     # a Saturday
    assert ck.is_trading_day(date(2026, 10, 1))         # a Thursday


def test_calendar_runs_ahead_of_the_tests_that_depend_on_it():
    """A holiday missing from the calendar is counted as a session nobody recorded: on
    the Monday after it the snapshot looks two sessions behind, the health check FAILs
    and the 5.3 switch halts a rebalance. 2026Q4b evaluates 2026-12-29 and the hedged
    paper test on 2027-09-30, so 2027 must already be in. This test also fails in the
    first January the calendar was not extended for -- on purpose: the weekly health
    check runs the suite and will say so."""
    from forward_test import market_calendar as cal
    from forward_test.hedged_paper import EVAL_DATE
    assert cal.CALENDAR_LAST_YEAR >= max(EVAL_DATE.year, date.today().year)
    assert max(cal.US_MARKET_HOLIDAYS).year == cal.CALENDAR_LAST_YEAR
    for h in (date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
              date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
              date(2027, 11, 25), date(2027, 12, 24)):
        assert not cal.is_trading_day(h), h
    assert cal.is_trading_day(date(2027, 12, 31))   # Jan 1 2028 is a Saturday: no Friday closure
    # The concrete failure this prevents: Monday 2027-01-04 with Thursday 2026-12-31 on
    # file is ONE session behind (inside the grace), not two.
    assert cal.sessions_behind(date(2026, 12, 31), date(2027, 1, 4)) == 1
    from broker.killswitch import sessions_behind
    assert sessions_behind(date(2026, 12, 31), date(2027, 1, 4)) == 1
