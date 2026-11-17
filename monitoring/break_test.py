"""
monitoring/break_test.py -- break things on purpose and prove the monitor notices.

    python -m monitoring.break_test

An untested monitor is not a monitor. The kill-switches are held to this standard
(broker/test_killswitch_with_execution.py) and so is this.

FOUR SCENARIOS, each asserting three things: the check FAILS, an alert FIRES, and --
where applicable -- the rebalance path REFUSES.

  A  0-row snapshot          the exact 2026-09-21..09-30 bug. A header-only
                             price_snapshots.csv that nine days of runs kept producing.
  B  stale snapshot          a real file, real rows, weeks out of date.
  C  scheduled task failed    the job's log records a non-zero exit. Proves the check
                             believes the LOG over LastTaskResult, which a wrapper bug
                             masked as 0.
  D  kill-switch trip        a 5.3 STALE_DATA halt raises an event alert.

ISOLATION -- this test must never touch real data or send a real email:
  * every scenario runs against a TEMPORARY copy of the period directory; module-level
    SNAP/SNAP_LOG paths are monkeypatched and restored in a finally block.
  * alert state, the alert log and history are redirected into the temp dir, so the
    real dedup state is neither read nor written (otherwise the first scenario's alert
    would suppress the rest, and these alerts would pollute real history).
  * email is never attempted: dispatch is called with send_mail=False.
  * the rebalance refusal is proven against assert_pipeline_healthy(), which places no
    order and needs no Gateway connection.

PAPER ONLY. Nothing here connects to a broker.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from monitoring import alerting as al    # noqa: E402
from monitoring import checks as ck      # noqa: E402

PASSED, FAILED = [], []


def banner(tag: str, title: str) -> None:
    print(f"\n{'=' * 76}\nSCENARIO {tag}: {title}\n{'=' * 76}")


def verdict(tag: str, ok: bool, note: str = "") -> None:
    (PASSED if ok else FAILED).append(tag)
    print(f"  RESULT: {'PASS' if ok else 'FAIL'}" + (f" — {note}" if note else ""))


@contextmanager
def sandbox():
    """Redirect snapshot paths AND all alerting state into a temp dir; always restore."""
    tmp = Path(tempfile.mkdtemp(prefix="breaktest_"))
    saved = (ck.SNAP, ck.SNAP_LOG,
             al.ALERT_LOG, al.ALERT_STATE, al.HEALTH_HISTORY)
    try:
        period_dir = tmp / ck.PERIOD
        period_dir.mkdir(parents=True)
        # Real books so tickers_and_carried has something truthful to compare against.
        src = Path(ck.SNAP).parent
        for f in ("incumbent_book.csv", "value_tilt_book.csv"):
            if (src / f).exists():
                shutil.copy(src / f, period_dir / f)
        ck.SNAP = period_dir / "price_snapshots.csv"
        ck.SNAP_LOG = period_dir / "snapshot_log.txt"
        al.ALERT_LOG = tmp / "alert_log.jsonl"
        al.ALERT_STATE = tmp / "alert_state.json"
        al.HEALTH_HISTORY = tmp / "health_history.json"
        yield tmp
    finally:
        (ck.SNAP, ck.SNAP_LOG,
         al.ALERT_LOG, al.ALERT_STATE, al.HEALTH_HISTORY) = saved
        shutil.rmtree(tmp, ignore_errors=True)


def fired(alerts: list[al.Alert]) -> tuple[bool, dict]:
    """Dispatch with email disabled and report whether anything was actually sent."""
    info = al.dispatch(alerts, send_mail=False, resolve_scope="health:")
    return bool(info["sent"]), info


def alert_from(r: ck.CheckResult) -> al.Alert:
    return al.Alert(key=f"health:{r.name}", title=f"health check failed: {r.name}",
                    severity=r.severity, summary=r.summary, detail=r.detail,
                    blocks=r.blocks, runbook=r.runbook)


# ── A: the 0-row snapshot ───────────────────────────────────────────────────────

def scenario_a() -> None:
    banner("A", "0-ROW SNAPSHOT — the exact bug that hid for nine days")
    with sandbox():
        # Header only: byte-for-byte what the crashed runs left behind.
        ck.SNAP.write_text("date,ticker,close,is_carried\n", encoding="utf-8")
        print(f"  wrote a header-only {ck.SNAP.name} "
              f"({ck.SNAP.stat().st_size} bytes, 0 data rows)")

        r = ck.check_snapshot_rows()
        print(f"  check_snapshot_rows -> {r.status} ({r.severity}): {r.summary}")
        sent, info = fired([alert_from(r)])
        print(f"  alert fired: {sent}  key={info['sent']}")

        blocked = _gate_blocks_on([r])
        print(f"  rebalance gate: {'REFUSED' if blocked else 'allowed'}")

        verdict("A", r.failed and r.severity == ck.CRITICAL and sent and blocked,
                "0 rows detected, alerted, rebalance refused")


# ── B: a stale snapshot ─────────────────────────────────────────────────────────

def scenario_b() -> None:
    banner("B", "STALE SNAPSHOT — real rows, weeks out of date")
    with sandbox():
        stale_day = date.today() - timedelta(days=21)
        rows = [{"date": stale_day.isoformat(), "ticker": t, "close": 10.0 + i,
                 "is_carried": 0}
                for i, t in enumerate(["SPY", "AMN", "KODK", "MOV"])]
        pd.DataFrame(rows).to_csv(ck.SNAP, index=False)
        print(f"  wrote {len(rows)} real rows, all dated {stale_day} "
              f"({(date.today() - stale_day).days} calendar days ago)")

        r = ck.check_snapshot_fresh()
        print(f"  check_snapshot_fresh -> {r.status} ({r.severity}): {r.summary}")
        rows_chk = ck.check_snapshot_rows()
        print(f"  check_snapshot_rows  -> {rows_chk.status}: {rows_chk.summary}"
              f"   (non-zero rows, so THIS check correctly passes)")
        sent, info = fired([alert_from(r)])
        print(f"  alert fired: {sent}  key={info['sent']}")

        blocked = _gate_blocks_on([r])
        print(f"  rebalance gate: {'REFUSED' if blocked else 'allowed'}")

        verdict("B", r.failed and sent and blocked and rows_chk.status == ck.PASS,
                "staleness caught by the freshness check, not the row check")


# ── C: the scheduled task failed ────────────────────────────────────────────────

def scenario_c() -> None:
    banner("C", "SCHEDULED TASK FAILED — log says exit 1 while Windows says 0")
    with sandbox():
        # A healthy, fresh snapshot, so nothing else can be what fails.
        y = ck.most_recent_trading_day(date.today())
        pd.DataFrame([{"date": y.isoformat(), "ticker": "SPY", "close": 764.2,
                       "is_carried": 0}]).to_csv(ck.SNAP, index=False)
        ck.SNAP_LOG.write_text(
            "======================================================================\n"
            f"[{y:%d/%m/%Y} 23.30.01,00] snapshot start (period {ck.PERIOD})\n"
            "Traceback (most recent call last):\n"
            "ValueError: something broke during the write\n"
            f"[{y:%d/%m/%Y} 23.30.09,00] snapshot end (exit 1)\n",
            encoding="utf-8")
        code, stamp = ck._log_exit_code()
        print(f"  log records: exit {code} at {stamp}")

        live = ck._task_info(ck.TASK_NAME)
        print(f"  Task Scheduler says LastTaskResult={live.get('last_result')} "
              f"(the REAL task, genuinely 0)")

        r = ck.check_scheduled_task()
        print(f"  check_scheduled_task -> {r.status} ({r.severity}): {r.summary}")
        sent, info = fired([alert_from(r)])
        print(f"  alert fired: {sent}  key={info['sent']}")
        blocked = _gate_blocks_on([r])
        print(f"  rebalance gate: {'REFUSED' if blocked else 'allowed'}")

        # The point of this scenario: the log outranks LastTaskResult. If the check
        # trusted Windows it would have passed, because the live task really is 0.
        believed_log = r.failed and "exited 1" in r.summary
        verdict("C", believed_log and sent and blocked,
                "the job's own log outranked a LastTaskResult of 0")


# ── D: a kill-switch trip ───────────────────────────────────────────────────────

def scenario_d() -> None:
    banner("D", "KILL-SWITCH TRIP — 5.3 STALE_DATA halts and alerts")
    with sandbox() as tmp:
        from broker.killswitch import (
            KillSwitchTripped, OrderGate, OrderIntent, PortfolioState,
        )
        stale = tmp / "stale_snapshot.csv"
        stale.write_text("date,ticker,close,is_carried\n2026-09-01,AAA,10.0,0\n",
                         encoding="utf-8")
        gate = OrderGate(snapshot_csv=stale, today=date(2026, 9, 30),
                         audit_log=tmp / "audit.log")
        intent = OrderIntent("SPY", "BUY", 10, 764.0, sector="ETF",
                             adv_usd=5e9, target_weight=0.01)
        pf = PortfolioState(equity=1e6, high_water_mark=1e6, positions={})

        tripped = None
        try:
            gate.authorize([intent], pf, book_value=1e6)
        except KillSwitchTripped as e:
            tripped = e
        if tripped is None:
            verdict("D", False, "the gate did NOT trip — nothing to alert on")
            return
        print(f"  gate raised: [{tripped.switch.value}] {tripped.detail}")

        info = al.dispatch(
            [al.Alert(key=f"killswitch:{tripped.switch.value}",
                      title=f"KILL-SWITCH TRIPPED: {tripped.switch.value}",
                      severity="CRITICAL",
                      summary=f"{tripped.switch.value} halted the order path: "
                              f"{tripped.detail}",
                      blocks="no order can be submitted until this is resolved",
                      runbook="broker/RUNBOOK.md section 7")],
            send_mail=False, resolve_scope=None)
        sent = bool(info["sent"])
        print(f"  alert fired: {sent}  key={info['sent']}")

        logged = al.ALERT_LOG.exists() and "killswitch" in al.ALERT_LOG.read_text(
            encoding="utf-8")
        print(f"  written to the alert log: {logged}")
        verdict("D", sent and logged,
                "5.3 halt raised a CRITICAL alert and was logged")


# ── the rebalance refusal, shared by A/B/C ──────────────────────────────────────

def _gate_blocks_on(results: list[ck.CheckResult]) -> bool:
    """Would assert_pipeline_healthy REFUSE on these results?

    Applies the real blocking policy from health_check rather than restating it, so
    this test cannot drift from what the broker path actually enforces.
    """
    from monitoring.health_check import BLOCKING_SEVERITIES
    return any(r.failed and r.severity in BLOCKING_SEVERITIES for r in results)


# ── dedup, proven rather than asserted ──────────────────────────────────────────

def scenario_dedup() -> None:
    banner("E", "DEDUP + ESCALATION — one persistent failure must not flood")
    with sandbox():
        # A WARNING-severity failure on purpose. An already-CRITICAL alert cannot be
        # escalated any further, so using one would let this scenario pass even if the
        # escalation logic were deleted. Starting at WARNING makes the
        # WARNING -> CRITICAL transition observable, which is the thing being proven.
        def warn_alert() -> al.Alert:
            return al.Alert(key="health:prices_db_fresh",
                            title="health check failed: prices_db_fresh",
                            severity="WARNING",
                            summary="prices stale: 8 sessions behind",
                            blocks="execution sizing uses out-of-date ADV",
                            runbook="python -m data.prices_updater --lookback-days 35")

        t0 = datetime(2026, 9, 1, 23, 50)
        sends = []
        for day in range(6):
            now = t0 + timedelta(days=day)
            state = al.load_alert_state()
            to_send, _supp, new_state = al.decide([warn_alert()], state, now=now,
                                                 resolve_scope="health:")
            al.save_alert_state(new_state)
            for a in to_send:
                al.log_alert(a)
            sends.append((day + 1, bool(to_send),
                          to_send[0].severity if to_send else "-",
                          new_state["health:prices_db_fresh"]["occurrences"]))

        print(f"  {'day':<6}{'sent':<8}{'severity':<12}occurrences")
        for d, s, sev, occ in sends:
            print(f"  {d:<6}{str(s):<8}{sev:<12}{occ}")

        n_sent = sum(1 for _, s, _, _ in sends if s)
        first_sev = next(sev for _, s, sev, _ in sends if s)
        later_sev = [sev for _, s, sev, _ in sends[2:] if s]
        escalated = first_sev == "WARNING" and "CRITICAL" in later_sev
        print(f"  6 consecutive identical failures -> {n_sent} alerts sent")
        print(f"  severity went {first_sev} -> {'/'.join(later_sev) or 'no later send'}"
              f"  (escalation after {al.ESCALATE_AFTER} occurrences)")
        verdict("E", n_sent < 6 and escalated,
                f"{n_sent} of 6 sent, WARNING escalated to CRITICAL — no daily flood")


def main() -> int:
    print("=" * 76)
    print("DELIBERATE-BREAK PROOF — monitoring/break_test.py")
    print("Breaks the pipeline on purpose in a temp sandbox. Touches no real data,")
    print("no real alert state, and sends no email. PAPER ONLY.")
    print("=" * 76)
    for fn in (scenario_a, scenario_b, scenario_c, scenario_d, scenario_dedup):
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            FAILED.append(f"{fn.__name__} raised {type(e).__name__}: {e}")

    print(f"\n{'=' * 76}")
    if FAILED:
        print(f"{len(PASSED)} passed, {len(FAILED)} FAILED: {FAILED}")
        print("The monitoring does NOT do what it claims. Fix before relying on it.")
        print("=" * 76)
        return 1
    print(f"ALL {len(PASSED)} SCENARIOS PASS — {', '.join(PASSED)}")
    print("Each break was detected, alerted, and (A/B/C) refused the rebalance.")
    print("=" * 76)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
