"""
monitoring/health_check.py — one command that answers "is the pipeline alive?"

    python -m monitoring.health_check              # full run, alerts on failure
    python -m monitoring.health_check -v           # with detail and runbook lines
    python -m monitoring.health_check --json       # machine-readable
    python -m monitoring.health_check --no-alert   # check only, send nothing
    python -m monitoring.health_check --fast       # skip the pytest run

EXIT CODE
    0  every check PASSed, SKIPped or WARNed
    1  at least one check FAILed

The exit code is the contract: the scheduled task, and the broker path's pre-rebalance
gate, both read it. A non-zero exit means something a human must look at.

WHY THIS EXISTS
    forward_test/2026Q4b recorded ZERO sessions from 2026-09-21 to 2026-09-30.
    snapshot.py wrote 0-row price_snapshots.csv files and exited; the scheduled task
    reported success because its wrapper masked the exit code; and the freshness script
    was pointed at a different period than the kill-switch. Three independent holes,
    none of which told anyone. This closes them and shouts when any reopens.

PAPER ONLY. This module reads. It places no orders and writes only under
monitoring/state/.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Windows consoles default to cp1252, and a scheduled task sets no PYTHONIOENCODING.
# Check summaries contain em-dashes, and a UnicodeEncodeError while PRINTING would make
# the health check itself the thing that fails silently. Force UTF-8 on our own streams.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from monitoring import alerting as al           # noqa: E402
from monitoring import checks as ck             # noqa: E402

STATUS_MARK = {ck.PASS: "PASS", ck.FAIL: "FAIL", ck.WARN: "WARN", ck.SKIP: "skip"}


def _alerts_from(results: list[ck.CheckResult]) -> list[al.Alert]:
    return [al.Alert(key=f"health:{r.name}",
                     title=f"health check failed: {r.name}",
                     severity=r.severity, summary=r.summary, detail=r.detail,
                     blocks=r.blocks, runbook=r.runbook,
                     extra={"metrics": r.metrics})
            for r in results if r.failed]


def render(results: list[ck.CheckResult], *, verbose: bool = False) -> str:
    w = max((len(r.name) for r in results), default=20) + 2
    lines = [f"QUANT SYSTEM HEALTH — {datetime.now():%Y-%m-%d %H:%M:%S}  "
             f"(period {ck.PERIOD})", "=" * 78]
    for r in results:
        lines.append(f"  [{STATUS_MARK[r.status]}] {r.name:<{w}} {r.summary}")
        if verbose and r.detail:
            lines.append(f"         {'':<{w}} {r.detail}")
        if verbose and r.failed and r.blocks:
            lines.append(f"         {'':<{w}} BLOCKS: {r.blocks}")
        if verbose and r.failed and r.runbook:
            lines.append(f"         {'':<{w}} RUN:    {r.runbook}")
    n_fail = sum(1 for r in results if r.failed)
    n_pass = sum(1 for r in results if r.status == ck.PASS)
    n_warn = sum(1 for r in results if r.status == ck.WARN)
    n_skip = sum(1 for r in results if r.status == ck.SKIP)
    lines += ["-" * 78,
              f"  {n_pass} pass, {n_fail} FAIL, {n_warn} warn, {n_skip} skip"]
    if n_fail:
        worst = max(al.SEVERITY_ORDER.get(r.severity, 1) for r in results if r.failed)
        label = {0: "INFO", 1: "WARNING", 2: "CRITICAL"}[worst]
        lines.append(f"  worst severity: {label}")
    return "\n".join(lines)


def run(*, skip_tests: bool = False, alert: bool = True,
        send_mail: bool = True) -> tuple[list[ck.CheckResult], dict]:
    """Run the checks, persist history, dispatch alerts. Returns (results, alert_info)."""
    results = ck.run_all(previous=al.previous_metrics(), skip_tests=skip_tests)

    al.append_history({
        "ts": datetime.now().isoformat(timespec="seconds"),
        "period": ck.PERIOD,
        "failed": [r.name for r in results if r.failed],
        "statuses": {r.name: r.status for r in results},
        # metrics keyed by check so the NEXT run can compare (snapshot_growth needs it)
        "metrics": {r.name: r.metrics for r in results if r.metrics},
    })

    info = {"sent": [], "suppressed": [], "resolved": [], "emailed": False,
            "email_message": "alerting disabled for this run"}
    if alert:
        # resolve_scope="health:" — this run evaluated every health key, so a
        # previously-failing key that is absent now has genuinely recovered. Event
        # alerts (kill-switch, rebalance) deliberately pass no scope.
        info = al.dispatch(_alerts_from(results), send_mail=send_mail,
                           resolve_scope="health:")
    return results, info


class PipelineUnhealthy(RuntimeError):
    """Raised by assert_pipeline_healthy(). The rebalance must not proceed."""


# What blocks a rebalance. CRITICAL means the data a book would be sized from is
# missing, stale or wrong -- proceeding would produce a book built on nothing, which is
# precisely the 0-row-snapshot failure expressed as orders. WARNING means degraded but
# usable (e.g. ADV a few sessions old): printed loudly, recorded, but not blocking,
# because a gate that halts on every yellow light is a gate that gets switched off.
BLOCKING_SEVERITIES = ("CRITICAL",)


def assert_pipeline_healthy(*, skip_tests: bool = False, alert: bool = True) -> list:
    """Run the health check and REFUSE if the pipeline is unhealthy.

    Called at the start of the broker path so a rebalance cannot be sized from a
    pipeline that is not recording. Raises PipelineUnhealthy; there is no override
    flag, following the kill-switch precedent -- a gate you can pass a flag to is not
    a gate.
    """
    results, _ = run(skip_tests=skip_tests, alert=alert, send_mail=alert)
    failed = [r for r in results if r.failed]
    blocking = [r for r in failed if r.severity in BLOCKING_SEVERITIES]
    warning = [r for r in failed if r.severity not in BLOCKING_SEVERITIES]

    print(f"\n=== PIPELINE HEALTH GATE ({len(results)} checks) ===")
    for r in results:
        if r.failed:
            print(f"  [{STATUS_MARK[r.status]}] {r.name}: {r.summary}  "
                  f"({r.severity})")
    if warning:
        print(f"  {len(warning)} non-blocking WARNING failure(s) — degraded, not "
              f"blocking. Fix soon:")
        for r in warning:
            print(f"      {r.name}: {r.runbook}")
    if blocking:
        lines = [f"{r.name}: {r.summary} [blocks: {r.blocks}] -> {r.runbook}"
                 for r in blocking]
        raise PipelineUnhealthy(
            "REFUSED: the pipeline is unhealthy, so no book will be sized from it.\n  "
            + "\n  ".join(lines)
            + "\n  Full detail: python -m monitoring.health_check -v")
    print(f"  healthy enough to trade "
          f"({len(results) - len(failed)} of {len(results)} checks clean)")
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Quant system pipeline health check.")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="show detail, what each failure blocks, and the fix command")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--no-alert", action="store_true",
                    help="run the checks but send and log nothing")
    ap.add_argument("--no-email", action="store_true",
                    help="log alerts locally but do not attempt SMTP")
    ap.add_argument("--test-email", action="store_true",
                    help="send one test email with the .env settings and exit "
                         "(runs no checks)")
    ap.add_argument("--weekly-report", action="store_true",
                    help="after the checks, write the weekly summary to "
                         "monitoring/state/ and email it (if email is configured)")
    ap.add_argument("--fast", action="store_true",
                    help="skip the pytest run (everything else still runs)")
    # snapshot.py reads --period from sys.argv itself, so accept and ignore it here.
    ap.add_argument("--period", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.test_email:
        usable, why = al.email_status()
        print(f"  channel: {why}")
        if not usable:
            print("  NOT SENT -- fix the ALERT_* settings in .env (monitoring/README.md)")
            return 1
        ok, msg = al.send_text(
            "[quant-system] test email",
            "This is a test from python -m monitoring.health_check --test-email.\n\n"
            "If you can read it, alerts and the weekly summary will reach you.\n")
        print(f"  {'SENT' if ok else 'FAILED'}: {msg}")
        return 0 if ok else 1

    results, info = run(skip_tests=args.fast, alert=not args.no_alert,
                        send_mail=not args.no_email)
    failed = [r for r in results if r.failed]

    if args.json:
        print(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                          "period": ck.PERIOD, "ok": not failed,
                          "checks": [r.as_dict() for r in results],
                          "alerts": info}, indent=2, default=str))
    else:
        print(render(results, verbose=args.verbose))
        if not args.no_alert:
            usable, why = al.email_status()
            print(f"\n  alerts: {len(info['sent'])} sent, "
                  f"{len(info['suppressed'])} suppressed (dedup), "
                  f"{len(info['resolved'])} resolved")
            print(f"  channel: {why}")
            if info["sent"]:
                print(f"  delivery: {info['email_message']}")
            print(f"  log: {al.ALERT_LOG.relative_to(ROOT)}")
            if not usable and failed:
                print("\n  NOTE: checks FAILED and email is not configured. The "
                      "failures are logged\n        but nothing was sent. See "
                      "monitoring/README.md to enable email.")
        if failed and not args.verbose:
            print("\n  re-run with -v for what each failure blocks and how to fix it")

    if args.weekly_report:
        from monitoring import weekly_report as wr
        subject, body = wr.build_report(results)
        path = wr.write_report(subject, body)
        if not args.json:
            shown = (path.relative_to(ROOT) if path.is_relative_to(ROOT) else path)
            print(f"\n  weekly report: {shown}")
        if not (args.no_alert or args.no_email):
            ok, msg = al.send_text(subject, body)
            if not args.json:
                print(f"  weekly report email: {msg}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
