"""
monitoring/checks.py — the individual health checks.

Each check returns a CheckResult. None of them raise for an ordinary failure: a check
that throws is itself reported as a FAIL (severity CRITICAL), because a monitor that
dies quietly is worse than no monitor.

Every check carries the exact RUNBOOK command to investigate it, so an alert is
actionable without going to look up what to do.

WHY EACH CHECK EXISTS
  snapshot_exists / snapshot_rows   the 0-row bug: snapshot.py wrote a header-only
                                    price_snapshots.csv and exited. Nine days lost.
  snapshot_fresh                    the 5.3 kill-switch reads this file; if it is
                                    stale the order path halts, so we want to know
                                    BEFORE a rebalance, not during one.
  snapshot_growth                   catches truncation and non-advancing writes -- a
                                    file that exists, is non-empty, and is still wrong.
  scheduled_task                    the job can die without anyone looking. Reads BOTH
                                    Task Scheduler AND the log's own exit line, because
                                    LastTaskResult was masked by the wrapper until
                                    commit a3bb065 and may be masked again by any
                                    future wrapper edit.
  tickers_and_carried               a snapshot with the right row count can still be
                                    wrong: missing names, or everything carried forward
                                    from one real session.
  prices_task                       nothing refreshed the prices table automatically,
                                    so it drifted 8 sessions stale. QuantPricesUpdate
                                    now does; this makes sure it stays registered,
                                    enabled, recent and succeeding (its own log, like
                                    scheduled_task).
  backup_fresh                      the DB, execution log and monitoring state exist
                                    only on this machine; scripts/backup.py zips them
                                    weekly. Warns if the newest zip is > 8 days old.
  prices_db_fresh                   ADV and volatility inputs to execution come from
                                    the prices table. Stale ADV sizes orders wrongly.
  test_suite                        a regression that breaks the factor or broker code
                                    should not wait for a human to run pytest.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Period, snapshot path and start date come from the snapshot registry -- NEVER
# hardcoded here. check_snapshot_fresh.py was hardcoded to 2026Q3_parallel while the
# kill-switch read the active period, so a green check sat beside a real halt on a
# different file. One source of truth, or the monitor lies.
from forward_test.check_snapshot_fresh import (  # noqa: E402
    is_trading_day, most_recent_trading_day,
)
from forward_test.snapshot import PERIOD, SNAP, START  # noqa: E402

TASK_NAME = "QuantForwardTestSnapshot"
HEALTH_TASK_NAME = "QuantHealthCheck"
PRICES_TASK_NAME = "QuantPricesUpdate"
PRICES_LOG = ROOT / "monitoring" / "state" / "prices_update_log.txt"
SNAP_LOG = SNAP.parent / "snapshot_log.txt"

# Thresholds. Stated here rather than inline so they are reviewable in one place.
MAX_SNAPSHOT_STALE_SESSIONS = 1     # matches killswitch.MAX_STALE_SESSIONS
MAX_TASK_AGE_HOURS = 30             # a 23:30 daily job, plus slack for a late boot
MAX_PRICES_DB_STALE_SESSIONS = 5    # prices_updater is not daily; 5 = a trading week
MAX_PRICES_TASK_AGE_HOURS = 192     # weekly (Mon 15:00): 168h, plus a day of slack
MAX_BACKUP_AGE_DAYS = 8             # weekly backup (Mon 15:30), plus a day of slack
MAX_CARRIED_FRACTION = 0.20         # >20% of the latest session carried = suspect
# Inputs to the NEXT quarter's book (book_inputs_fresh). Stale inputs WARN in the table;
# they FAIL (one deduplicated email) only once the book is due within BOOK_DUE_DAYS.
MAX_UNIVERSE_AGE_DAYS = 200         # Russell reconstitutes twice a year (June, December)
MAX_FUNDAMENTALS_AGE_DAYS = 200     # newest quarter-end on file; filings land within ~90d
MAX_FACTOR_SCORES_AGE_DAYS = 100    # last factor-score calculation
BOOK_DUE_DAYS = 21                  # three weeks to refresh before the book is built

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"
INFO, WARNING, CRITICAL = "INFO", "WARNING", "CRITICAL"


@dataclass
class CheckResult:
    name: str
    status: str                      # PASS | FAIL | WARN | SKIP
    summary: str                     # one line, shown in the table
    severity: str = WARNING          # INFO | WARNING | CRITICAL (meaningful when failing)
    detail: str = ""                 # the numbers behind the verdict
    blocks: str = ""                 # what this failure prevents you from doing
    runbook: str = ""                # exact command to investigate
    metrics: dict = field(default_factory=dict)   # persisted for trend comparisons

    @property
    def failed(self) -> bool:
        return self.status == FAIL

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "summary": self.summary,
                "severity": self.severity, "detail": self.detail,
                "blocks": self.blocks, "runbook": self.runbook,
                "metrics": self.metrics}


def _sessions_between(earlier: date, later: date) -> int:
    """Trading sessions strictly after `earlier`, up to and including `later`."""
    n, d = 0, later
    while d > earlier:
        if is_trading_day(d):
            n += 1
        d -= timedelta(days=1)
    return n


# ── snapshot checks ─────────────────────────────────────────────────────────────

def check_snapshot_exists(today: date | None = None) -> CheckResult:
    today = today or date.today()
    mrtd = most_recent_trading_day(today)
    start = date.fromisoformat(START)
    nm = "snapshot_exists"
    rb = "python forward_test/check_snapshot_fresh.py"

    if start > mrtd:
        return CheckResult(nm, SKIP, f"{PERIOD} starts {START} — no session due yet",
                           INFO, detail=f"most recent completed session {mrtd}",
                           runbook=rb)
    if not SNAP.exists():
        if start == mrtd:
            return CheckResult(
                nm, SKIP, f"{PERIOD} starts today ({START}) — snapshot runs 23:30",
                INFO, detail="Not a fault. The 5.3 kill-switch still halts until the "
                             "first session is recorded.", runbook=rb)
        return CheckResult(
            nm, FAIL, f"{SNAP.name} missing for {PERIOD}", CRITICAL,
            detail=f"period started {START}; {mrtd} has passed and nothing was written",
            blocks="the forward test records nothing; 5.3 STALE_DATA halts all trading",
            runbook=rb)
    return CheckResult(nm, PASS, f"{SNAP.name} present", INFO,
                       detail=str(SNAP.relative_to(ROOT)), runbook=rb)


def check_snapshot_rows(today: date | None = None) -> CheckResult:   # noqa: ARG001
    """THE 0-ROW CHECK. This is the one that would have caught the 9-day failure."""
    nm = "snapshot_rows"
    rb = f"type forward_test\\{PERIOD}\\snapshot_log.txt"
    if not SNAP.exists():
        return CheckResult(nm, SKIP, "no snapshot file yet", INFO,
                           detail="see snapshot_exists", runbook=rb)
    try:
        s = pd.read_csv(SNAP)
    except Exception as e:
        return CheckResult(nm, FAIL, f"{SNAP.name} unreadable", CRITICAL,
                           detail=f"{type(e).__name__}: {e}",
                           blocks="the 5.3 kill-switch treats an unreadable snapshot "
                                  "as stale and halts trading",
                           runbook=rb)
    rows = len(s)
    if rows == 0:
        return CheckResult(
            nm, FAIL, f"{SNAP.name} has 0 rows — crashed run, not a session", CRITICAL,
            detail="A header-only file is exactly the 2026-09-21..09-30 failure: "
                   "snapshot.py wrote an empty CSV and exited, and nothing noticed "
                   "for nine days.",
            blocks="the forward test is recording nothing; 5.3 halts all trading",
            runbook=rb, metrics={"rows": 0})
    return CheckResult(nm, PASS, f"{rows:,} rows recorded", INFO,
                       detail=f"{s['date'].nunique()} sessions x "
                              f"{s['ticker'].nunique()} tickers",
                       runbook=rb,
                       metrics={"rows": rows, "sessions": int(s["date"].nunique()),
                                "tickers": int(s["ticker"].nunique())})


def check_snapshot_fresh(today: date | None = None) -> CheckResult:
    today = today or date.today()
    mrtd = most_recent_trading_day(today)
    nm = "snapshot_fresh"
    rb = "python forward_test/check_snapshot_fresh.py"
    if not SNAP.exists():
        return CheckResult(nm, SKIP, "no snapshot file yet", INFO,
                           detail="see snapshot_exists", runbook=rb)
    try:
        s = pd.read_csv(SNAP)
        if s.empty:
            return CheckResult(nm, SKIP, "snapshot empty", INFO,
                              detail="see snapshot_rows", runbook=rb)
        latest = pd.to_datetime(s["date"]).dt.date.max()
    except Exception as e:
        return CheckResult(nm, FAIL, "snapshot unreadable", CRITICAL,
                           detail=f"{type(e).__name__}: {e}", runbook=rb)

    behind = _sessions_between(latest, mrtd)
    m = {"latest_recorded": latest.isoformat(), "sessions_behind": behind}
    if behind > MAX_SNAPSHOT_STALE_SESSIONS:
        return CheckResult(
            nm, FAIL, f"stale: latest {latest}, {behind} sessions behind", CRITICAL,
            detail=f"most recent trading day {mrtd}; grace is "
                   f"{MAX_SNAPSHOT_STALE_SESSIONS} session",
            blocks="5.3 STALE_DATA halts the order path — no rebalance can run",
            runbook=rb, metrics=m)
    return CheckResult(nm, PASS, f"fresh: latest {latest} ({behind} behind)", INFO,
                       detail=f"within {MAX_SNAPSHOT_STALE_SESSIONS}-session grace",
                       runbook=rb, metrics=m)


def check_snapshot_growth(previous: dict | None, today: date | None = None) -> CheckResult:
    """Did the file move in the right direction since the last health run?

    Three distinct wrongs a row count alone cannot see:
      * rows DECREASED  -> the file was rebuilt or truncated. snapshot.py promises
                           never to destroy a recorded price, so this is a broken
                           guarantee, not a data quirk.
      * a new session was recorded but rows did not grow -> partial write.
      * nothing changed and nothing was due -> genuinely fine (weekend, holiday).
    """
    nm = "snapshot_growth"
    rb = f"type forward_test\\{PERIOD}\\snapshot_log.txt"
    if not SNAP.exists():
        return CheckResult(nm, SKIP, "no snapshot file yet", INFO, runbook=rb)
    try:
        s = pd.read_csv(SNAP)
    except Exception as e:
        return CheckResult(nm, FAIL, "snapshot unreadable", CRITICAL,
                           detail=f"{type(e).__name__}: {e}", runbook=rb)
    rows = len(s)
    latest = (pd.to_datetime(s["date"]).dt.date.max().isoformat() if rows else None)
    m = {"rows": rows, "latest_recorded": latest}

    if not previous:
        return CheckResult(nm, SKIP, f"no previous run to compare ({rows:,} rows now)",
                           INFO, detail="baseline recorded for the next run",
                           runbook=rb, metrics=m)

    prev_rows = previous.get("rows")
    prev_latest = previous.get("latest_recorded")
    if prev_rows is None:
        return CheckResult(nm, SKIP, "previous run recorded no row count", INFO,
                           runbook=rb, metrics=m)

    if rows < prev_rows:
        return CheckResult(
            nm, FAIL, f"rows DECREASED {prev_rows:,} -> {rows:,}", CRITICAL,
            detail="snapshot.py guarantees a recorded price is never destroyed "
                   "(prices are merged, not rebuilt). A drop means that guarantee "
                   "broke -- suspect a rebuild-from-scratch or a truncated write.",
            blocks="recorded history is being lost; the equity curve is unreliable",
            runbook=rb, metrics=m)

    if latest != prev_latest and rows == prev_rows:
        return CheckResult(
            nm, FAIL, f"new session {prev_latest} -> {latest} but rows unchanged "
                      f"({rows:,})", CRITICAL,
            detail="a session was appended without adding rows — a partial write",
            blocks="the newest session is incomplete; the equity curve is wrong",
            runbook=rb, metrics=m)

    if rows == prev_rows:
        return CheckResult(
            nm, PASS, f"unchanged at {rows:,} rows (no new session due)", INFO,
            detail=f"latest recorded {latest}; nothing expected since the previous "
                   f"health run", runbook=rb, metrics=m)
    return CheckResult(nm, PASS, f"grew {prev_rows:,} -> {rows:,} rows", INFO,
                       detail=f"latest recorded {latest}", runbook=rb, metrics=m)


def check_tickers_and_carried(today: date | None = None) -> CheckResult:
    """Right row count, wrong content: missing names, or a session all carried forward."""
    nm = "tickers_and_carried"
    rb = f"python forward_test/snapshot.py --period {PERIOD}"
    if not SNAP.exists():
        return CheckResult(nm, SKIP, "no snapshot file yet", INFO, runbook=rb)
    try:
        s = pd.read_csv(SNAP)
        if s.empty:
            return CheckResult(nm, SKIP, "snapshot empty", INFO, runbook=rb)
        d = SNAP.parent
        inc = pd.read_csv(d / "incumbent_book.csv")
        ch = pd.read_csv(d / "value_tilt_book.csv")
    except Exception as e:
        return CheckResult(nm, FAIL, "could not read snapshot or books", CRITICAL,
                           detail=f"{type(e).__name__}: {e}", runbook=rb)

    expected = set(inc["ticker"]) | set(ch["ticker"]) | {"SPY"}
    latest = pd.to_datetime(s["date"]).dt.date.max().isoformat()
    last = s[s["date"].astype(str) == latest]
    present = set(last["ticker"])
    missing = sorted(expected - present)
    carried = int(last["is_carried"].sum()) if "is_carried" in last.columns else 0
    frac = carried / max(len(last), 1)
    m = {"expected_tickers": len(expected), "present_tickers": len(present),
         "missing": missing, "carried": carried, "carried_fraction": round(frac, 4)}

    if missing:
        return CheckResult(
            nm, FAIL, f"{len(missing)} expected tickers missing on {latest}", WARNING,
            detail=f"missing: {', '.join(missing[:10])}"
                   + (f" (+{len(missing)-10} more)" if len(missing) > 10 else "")
                   + f"; expected {len(expected)}, present {len(present)}",
            blocks="the equity curve for the affected book is incomplete",
            runbook=rb, metrics=m)
    if frac > MAX_CARRIED_FRACTION:
        return CheckResult(
            nm, FAIL, f"{carried}/{len(last)} closes carried forward on {latest} "
                      f"({frac:.0%})", WARNING,
            detail=f"above the {MAX_CARRIED_FRACTION:.0%} threshold. Carried cells "
                   f"mean yfinance returned no price and the last close was frozen "
                   f"-- fine for one delisting, suspicious across many names.",
            blocks="prices may be frozen rather than real; returns understate moves",
            runbook=rb, metrics=m)
    return CheckResult(nm, PASS, f"{len(present)}/{len(expected)} tickers, "
                                 f"{carried} carried on {latest}", INFO,
                       detail=f"carried fraction {frac:.1%} "
                              f"(threshold {MAX_CARRIED_FRACTION:.0%})",
                       runbook=rb, metrics=m)


def check_baselines_match(today: date | None = None) -> CheckResult:   # noqa: ARG001
    """The locked baselines, restated for recorded splits, agree with the stored prices
    on the baseline date. yfinance restates a name's whole history when it splits; until
    the snapshot records that split, every return, risk alert and broker target reads it
    as a loss of 1 - 1/ratio (SENEA 5-for-1, 2026-11-17: -80% on a name that was +1%).
    CRITICAL because the broker's buy-and-hold targets would sell the name down."""
    nm = "baselines_match"
    rb = f"python forward_test/snapshot.py --period {PERIOD}"
    from forward_test import baselines as bl
    cb = bl.current_baselines(SNAP.parent)
    if not cb["locked"]:
        return CheckResult(nm, SKIP, "baselines not locked yet", INFO, runbook=rb)
    if not SNAP.exists():
        return CheckResult(nm, SKIP, "no snapshot file yet", INFO, runbook=rb)
    s = pd.read_csv(SNAP)
    day1 = s[s["date"].astype(str) == str(cb["baseline_date"])]
    if day1.empty:
        return CheckResult(nm, FAIL, f"baseline date {cb['baseline_date']} not in the "
                                     f"snapshot", CRITICAL,
                           detail="returns cannot be checked against their baselines",
                           blocks="every return and buy-and-hold target is unverified",
                           runbook=rb)
    current = {**cb["challenger"], **cb["incumbent"], "SPY": cb["spy"]}
    off = bl.mismatches(current, dict(zip(day1["ticker"], day1["close"])), [])
    n_splits = len(bl.read_splits(SNAP.parent))
    m = {"names": len(current), "splits_recorded": n_splits,
         "mismatched": {t: round(r, 4) for t, r in off.items()}}
    if off:
        worst = ", ".join(f"{t} x{r:.3f}" for t, r in sorted(off.items()))
        return CheckResult(
            nm, FAIL, f"{len(off)} baseline(s) disagree with the prices: {worst}",
            CRITICAL,
            detail=f"series close on {cb['baseline_date']} / baseline. A split shows as "
                   f"x1/ratio (x0.200 for 5-for-1). The snapshot records a split "
                   f"yfinance reports, or exits 1 explaining why it cannot.",
            blocks="returns, risk alerts and the broker's buy-and-hold targets are "
                   "wrong for these names",
            runbook=rb, metrics=m)
    return CheckResult(nm, PASS, f"{len(current)} baselines agree with the prices",
                       INFO, detail=f"{n_splits} split(s) restated "
                                    f"(forward_test/{PERIOD}/{bl.CA_NAME})",
                       runbook=rb, metrics=m)


# ── scheduled task ──────────────────────────────────────────────────────────────

# Windows PowerShell 5.1: no ternary, no null-coalescing, and `if` is a statement --
# so the timestamps are formatted in plain `if` blocks. The task name is substituted
# with str.replace, not str.format: this script is mostly braces and every one of them
# would have to be doubled for format(), which is how the first version broke.
# Emits an empty JSON object for a task that is not registered.
_TASK_INFO_PS = """
$t = Get-ScheduledTask -TaskName '__TASK__' -ErrorAction SilentlyContinue
if (-not $t) { Write-Output '{}'; exit 0 }
$i = Get-ScheduledTaskInfo -TaskName '__TASK__'
$lr = $null; if ($i.LastRunTime) { $lr = $i.LastRunTime.ToString('o') }
$nr = $null; if ($i.NextRunTime) { $nr = $i.NextRunTime.ToString('o') }
[pscustomobject]@{
    state       = [string]$t.State
    last_run    = $lr
    last_result = $i.LastTaskResult
    next_run    = $nr
} | ConvertTo-Json -Compress
"""


def _task_info(task_name: str) -> dict:
    """Task Scheduler state via PowerShell.

    Returns {} both for "not registered" and for "could not ask". The caller treats
    that as a CRITICAL failure either way: if we cannot confirm the recorder is
    scheduled, we must not report the pipeline healthy.
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             _TASK_INFO_PS.replace("__TASK__", task_name)],
            capture_output=True, text=True, timeout=60)
        txt = (out.stdout or "").strip()
        return json.loads(txt) if txt else {}
    except Exception:
        return {}


def _log_exit_code() -> tuple[int | None, str | None]:
    """Last 'snapshot end (exit N)' recorded in the ACTIVE period's log.

    An independent second opinion on LastTaskResult. That value was masked by the
    wrapper's trailing echo until commit a3bb065, and any future wrapper edit could
    mask it again -- so the job's own log is read too, and a non-zero here FAILS even
    if Task Scheduler claims success.
    """
    if not SNAP_LOG.exists():
        return None, None
    try:
        txt = SNAP_LOG.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None, None
    hits = re.findall(r"\[([^\]]+)\]\s*snapshot end \(exit (-?\d+)\)", txt)
    if not hits:
        return None, None
    stamp, code = hits[-1]
    return int(code), stamp


# Registers the SNAPSHOT task from its committed export. NOT register_tasks.ps1: that
# script registers QuantHealthCheck, so following it here would "fix" the alert by
# registering a different task and leave the forward test unrecorded. The XML declares
# UTF-16 but is saved ASCII, which `schtasks /XML` rejects; passing it as a string does not.
SNAPSHOT_TASK_REGISTER = (
    f'powershell -Command "Register-ScheduledTask -TaskName {TASK_NAME} '
    f'-Xml (Get-Content forward_test\\{TASK_NAME}.xml -Raw)"')


def check_scheduled_task(task_name: str = TASK_NAME,
                         now: datetime | None = None) -> CheckResult:
    now = now or datetime.now()
    nm = "scheduled_task"
    rb = (f'powershell -Command "Get-ScheduledTaskInfo -TaskName {task_name}"')
    info = _task_info(task_name)
    log_code, log_stamp = _log_exit_code()
    m = {"task": task_name, "state": info.get("state"),
         "last_result": info.get("last_result"), "last_run": info.get("last_run"),
         "log_exit": log_code, "log_stamp": log_stamp}

    if not info:
        return CheckResult(
            nm, FAIL, f"scheduled task {task_name} not registered", CRITICAL,
            detail="nothing is recording the forward test on a schedule",
            blocks="the snapshot only runs if you run it by hand",
            runbook=SNAPSHOT_TASK_REGISTER, metrics=m)

    # The log's own exit line outranks LastTaskResult: it is written by the job, and
    # LastTaskResult has already been proven maskable by a wrapper bug.
    if log_code not in (None, 0):
        return CheckResult(
            nm, FAIL, f"last snapshot run exited {log_code} (log: {log_stamp})",
            CRITICAL,
            detail=f"Task Scheduler reports LastTaskResult="
                   f"{info.get('last_result')}. The job's own log is believed over "
                   f"that value, which a wrapper bug masked as 0 until a3bb065.",
            blocks="the newest session was probably not recorded",
            runbook=f"type forward_test\\{PERIOD}\\snapshot_log.txt", metrics=m)

    lr = info.get("last_result")
    if lr not in (None, 0):
        return CheckResult(
            nm, FAIL, f"{task_name} LastTaskResult={lr}", CRITICAL,
            detail=f"last run {info.get('last_run')}; a non-zero result means the "
                   f"wrapper reported failure",
            blocks="the newest session was probably not recorded",
            runbook=f"type forward_test\\{PERIOD}\\snapshot_log.txt", metrics=m)

    last_run = info.get("last_run")
    if not last_run:
        return CheckResult(nm, FAIL, f"{task_name} has never run", CRITICAL,
                           detail=f"state {info.get('state')}",
                           blocks="nothing has been recorded",
                           runbook=rb, metrics=m)
    try:
        lrt = datetime.fromisoformat(str(last_run).replace("Z", "+00:00"))
        if lrt.tzinfo:
            lrt = lrt.astimezone().replace(tzinfo=None)
    except Exception:
        return CheckResult(nm, WARN, f"{task_name} last run unparseable: {last_run}",
                           WARNING, runbook=rb, metrics=m)

    age_h = (now - lrt).total_seconds() / 3600.0
    m["age_hours"] = round(age_h, 1)
    if age_h > MAX_TASK_AGE_HOURS:
        return CheckResult(
            nm, FAIL, f"{task_name} last ran {age_h:.0f}h ago", CRITICAL,
            detail=f"last run {lrt:%Y-%m-%d %H:%M}; a 23:30 daily job should be under "
                   f"{MAX_TASK_AGE_HOURS}h. Machine asleep at 23:30, or the task is "
                   f"disabled.",
            blocks="sessions are being missed silently — the original failure mode",
            runbook=rb, metrics=m)
    if info.get("state") == "Disabled":
        return CheckResult(nm, FAIL, f"{task_name} is DISABLED", CRITICAL,
                           detail=f"last ran {lrt:%Y-%m-%d %H:%M}",
                           blocks="no further sessions will be recorded",
                           runbook=rb, metrics=m)
    return CheckResult(
        nm, PASS, f"{task_name} ran {age_h:.0f}h ago, result 0", INFO,
        detail=f"last {lrt:%Y-%m-%d %H:%M}, next {info.get('next_run')}, "
               f"log exit {log_code}", runbook=rb, metrics=m)


def check_health_task_registered(now: datetime | None = None) -> CheckResult:
    """Who watches the watchman.

    Deliberately checks REGISTRATION, ENABLEMENT and RECENCY only -- never the health
    task's own LastTaskResult. That result is 1 whenever any check fails, so testing it
    here would make the monitor fail because the monitor found something, a feedback
    loop that reports nothing useful.

    This cannot catch "the health check never ran at all" from inside a run that is
    happening. Nothing self-hosted can. The dashboard's System Health panel shows the
    age of the last recorded health run, which is where that gap is actually visible.
    """
    now = now or datetime.now()
    nm = "health_task_registered"
    rb = "powershell -ExecutionPolicy Bypass -File monitoring\\register_tasks.ps1 -Elevate"
    info = _task_info(HEALTH_TASK_NAME)
    m = {"task": HEALTH_TASK_NAME, "state": info.get("state"),
         "next_run": info.get("next_run")}
    if not info:
        return CheckResult(
            nm, FAIL, f"{HEALTH_TASK_NAME} is not registered", WARNING,
            detail="this health check is only running because you ran it by hand; "
                   "nothing will run it tomorrow",
            blocks="unattended monitoring — a failure would go unnoticed again",
            runbook=rb, metrics=m)
    if info.get("state") == "Disabled":
        return CheckResult(nm, FAIL, f"{HEALTH_TASK_NAME} is DISABLED", WARNING,
                           detail=f"next run {info.get('next_run')}",
                           blocks="unattended monitoring",
                           runbook=f'powershell -Command "Enable-ScheduledTask '
                                   f'-TaskName {HEALTH_TASK_NAME}"', metrics=m)
    return CheckResult(nm, PASS, f"{HEALTH_TASK_NAME} scheduled, next "
                                 f"{info.get('next_run')}", INFO,
                       detail=f"state {info.get('state')} (its exit code is "
                              f"deliberately not checked here — see docstring)",
                       runbook=rb, metrics=m)


# ── prices task ─────────────────────────────────────────────────────────────────

TASK_NEVER_RUN = 267011    # SCHED_S_TASK_HAS_NOT_RUN
TASK_RUNNING = 267009      # SCHED_S_TASK_RUNNING

PRICES_TASK_REGISTER = ("powershell -ExecutionPolicy Bypass -File "
                        "monitoring\\register_tasks.ps1 -Job Prices -Elevate")


def _prices_log_exit() -> tuple[int | None, str | None]:
    """Last 'prices update end (exit N)' written by data/run_prices_update.bat."""
    try:
        txt = PRICES_LOG.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None, None
    hits = re.findall(r"\[([^\]]+)\]\s*prices update end \(exit (-?\d+)\)", txt)
    if not hits:
        return None, None
    stamp, code = hits[-1]
    return int(code), stamp


def check_prices_task(now: datetime | None = None) -> CheckResult:
    """The scheduled price refresh is registered, enabled, recent and succeeding.

    WARNING, never CRITICAL: a missed refresh does not by itself mis-size anything --
    prices_db_fresh measures the table directly and is the check that matters. This one
    catches the job dying days before the table crosses that threshold.
    """
    now = now or datetime.now()
    nm = "prices_task"
    rb = f'powershell -Command "Get-ScheduledTaskInfo -TaskName {PRICES_TASK_NAME}"'
    log_rb = "type monitoring\\state\\prices_update_log.txt"
    info = _task_info(PRICES_TASK_NAME)
    log_code, log_stamp = _prices_log_exit()
    m = {"task": PRICES_TASK_NAME, "state": info.get("state"),
         "last_result": info.get("last_result"), "last_run": info.get("last_run"),
         "log_exit": log_code, "log_stamp": log_stamp}
    if not info:
        return CheckResult(
            nm, FAIL, f"{PRICES_TASK_NAME} is not registered", WARNING,
            detail="prices only refresh when someone runs prices_updater by hand -- "
                   "how the table fell 8 sessions behind",
            blocks="the prices table will drift stale again",
            runbook=PRICES_TASK_REGISTER, metrics=m)
    if info.get("state") == "Disabled":
        return CheckResult(nm, FAIL, f"{PRICES_TASK_NAME} is DISABLED", WARNING,
                           blocks="the prices table will drift stale again",
                           runbook=f'powershell -Command "Enable-ScheduledTask '
                                   f'-TaskName {PRICES_TASK_NAME}"', metrics=m)
    # The job's own log outranks LastTaskResult, as for the snapshot task.
    if log_code not in (None, 0):
        return CheckResult(
            nm, FAIL, f"last price refresh exited {log_code} (log: {log_stamp})",
            WARNING,
            detail="exit 1 = every batch came back empty (connectivity or yfinance)",
            blocks="prices did not refresh", runbook=log_rb, metrics=m)
    lr = info.get("last_result")
    # Task Scheduler status codes, not exit codes: 267011 (0x41303) = "has not yet
    # run" -- LastRunTime is then a 1999 placeholder, not null -- and 267009
    # (0x41301) = "currently running". Neither is a failure.
    if lr == TASK_RUNNING:
        return CheckResult(nm, PASS, f"{PRICES_TASK_NAME} is running now", INFO,
                           runbook=rb, metrics=m)
    if lr not in (None, 0, TASK_NEVER_RUN):
        return CheckResult(nm, FAIL, f"{PRICES_TASK_NAME} LastTaskResult={lr}",
                           WARNING, detail=f"last run {info.get('last_run')}",
                           blocks="prices did not refresh", runbook=log_rb, metrics=m)
    last_run = info.get("last_run")
    if not last_run or lr == TASK_NEVER_RUN:
        return CheckResult(nm, WARN, f"{PRICES_TASK_NAME} registered, not run yet",
                           INFO, detail=f"next run {info.get('next_run')}",
                           runbook=rb, metrics=m)
    try:
        lrt = datetime.fromisoformat(str(last_run).replace("Z", "+00:00"))
        if lrt.tzinfo:
            lrt = lrt.astimezone().replace(tzinfo=None)
    except Exception:
        return CheckResult(nm, WARN, f"{PRICES_TASK_NAME} last run unparseable: "
                                     f"{last_run}", WARNING, runbook=rb, metrics=m)
    age_h = (now - lrt).total_seconds() / 3600.0
    m["age_hours"] = round(age_h, 1)
    if age_h > MAX_PRICES_TASK_AGE_HOURS:
        return CheckResult(
            nm, FAIL, f"{PRICES_TASK_NAME} last ran {age_h:.0f}h ago", WARNING,
            detail=f"last run {lrt:%Y-%m-%d %H:%M}; a weekly job should be under "
                   f"{MAX_PRICES_TASK_AGE_HOURS}h. Machine off at 15:00, or on battery.",
            blocks="the prices table will drift stale", runbook=rb, metrics=m)
    return CheckResult(nm, PASS, f"{PRICES_TASK_NAME} ran {age_h:.0f}h ago, result 0",
                       INFO, detail=f"last {lrt:%Y-%m-%d %H:%M}, next "
                                    f"{info.get('next_run')}, log exit {log_code}",
                       runbook=rb, metrics=m)


# ── backups ─────────────────────────────────────────────────────────────────────

def _same_device(a: Path, b: Path | None) -> bool:
    """True when both paths exist on one storage device (st_dev: the volume on Windows,
    the device on Linux)."""
    if b is None or not a.exists() or not b.exists():
        return False
    return os.stat(a).st_dev == os.stat(b).st_dev


def _in_onedrive(p: Path) -> bool:
    return any(part.lower().startswith("onedrive") for part in p.resolve().parts)


def _onedrive_running(is_windows: bool | None = None, run=None) -> bool | None:
    """Whether the OneDrive client is running (None: not Windows, or could not ask). A
    OneDrive folder is on this disk; only the running client copies it off. On
    2026-11-17 BACKUP_DIR pointed into OneDrive with the client closed, and a check that
    trusted the folder name alone called that backup off-disk. `is_windows` and `run`
    are injectable for tests."""
    if not (os.name == "nt" if is_windows is None else is_windows):
        return None
    try:
        out = (run or subprocess.run)(
            ["tasklist", "/FI", "IMAGENAME eq OneDrive.exe", "/NH"],
            capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    # no match prints "INFO: No tasks are running which match the specified criteria."
    return "onedrive.exe" in (out or "").lower()


def check_backup(now: datetime | None = None) -> CheckResult:
    """The newest backup zip is recent AND restores. WARNING only: a bad backup risks
    data if the disk dies, but it mis-sizes nothing, so it never blocks a rebalance."""
    now = now or datetime.now()
    nm = "backup_fresh"
    rb = "python scripts/backup.py"
    from scripts.backup import backup_dir, list_backups
    dest = backup_dir()
    zips = list_backups(dest)
    m = {"dir": str(dest), "count": len(zips)}
    if not zips:
        return CheckResult(nm, FAIL, "no backup has ever been made", WARNING,
                           detail=f"looked in {dest}. The DB, execution log and "
                                  f"monitoring state exist only on this machine.",
                           blocks="a disk failure would lose them", runbook=rb,
                           metrics=m)
    newest = zips[-1]
    age_d = (now - datetime.fromtimestamp(newest.stat().st_mtime)).total_seconds() / 86400
    m.update(newest=newest.name, age_days=round(age_d, 1))
    if age_d > MAX_BACKUP_AGE_DAYS:
        return CheckResult(nm, FAIL, f"newest backup is {age_d:.0f} days old", WARNING,
                           detail=f"{newest.name} in {dest}; the weekly task should "
                                  f"keep this under {MAX_BACKUP_AGE_DAYS} days",
                           blocks="recent data is unprotected", runbook=rb, metrics=m)
    # Fresh is not enough: prove it restores (CRC, DB integrity, manifest counts).
    from scripts.backup import verify_backup
    v = verify_backup(newest)
    m.update(verified=v["ok"], tables=len(v["tables"]))
    if not v["ok"]:
        return CheckResult(nm, FAIL, f"newest backup does not restore: "
                                     f"{v['problems'][0]}", WARNING,
                           detail=f"{newest.name} in {dest}: " + "; ".join(v["problems"]),
                           blocks="a disk failure would lose the data this backup "
                                  "claims to hold", runbook=rb, metrics=m)
    from scripts.backup import db_path
    if _in_onedrive(dest):
        syncing = _onedrive_running()
        m.update(onedrive=True, onedrive_running=syncing)
        if syncing is False:
            return CheckResult(nm, WARN, "backup is in OneDrive but OneDrive is not "
                                         "running: it is not leaving this disk", INFO,
                               detail=f"{newest.name} in {dest}. Start OneDrive (and let "
                                      f"it start with Windows) so the folder syncs.",
                               runbook=rb, metrics=m)
        return CheckResult(nm, PASS, f"backup {age_d:.0f}d old, verified, in OneDrive "
                                     f"({len(zips)} kept)", INFO,
                           detail=f"{newest.name} in {dest}"
                                  + ("" if syncing else " (sync status not checked "
                                                        "on this OS)"),
                           runbook=rb, metrics=m)
    same_disk = _same_device(dest, db_path())
    m.update(same_disk=same_disk)
    if same_disk:
        # Verified, but on the disk it protects: one drive failure loses both. WARN, not
        # FAIL: it reaches the weekly email as a to-do and sends nothing on its own.
        return CheckResult(nm, WARN, "backup verified but on the same disk as the DB",
                           INFO,
                           detail=f"{newest.name} in {dest}. Set BACKUP_DIR in .env to a "
                                  f"synced folder (OneDrive) or another drive.",
                           runbook=rb, metrics=m)
    return CheckResult(nm, PASS, f"backup {age_d:.0f}d old, verified "
                                 f"({len(zips)} kept)", INFO,
                       detail=f"{newest.name} in {dest}", runbook=rb, metrics=m)


# ── prices DB ───────────────────────────────────────────────────────────────────

PRICES_DEFAULT_LOOKBACK_DAYS = 35     # prices_updater's own default


def prices_refresh_command(latest: date | None = None,
                           today: date | None = None) -> str:
    """The command that actually closes a gap back to `latest`.

    Uses `-m` so the module resolves from the repo root regardless of sys.path. The
    lookback is widened past the default when the gap needs it, with a week of margin,
    so running the printed line is enough -- no arithmetic left to the reader.
    """
    days = PRICES_DEFAULT_LOOKBACK_DAYS
    if latest is not None:
        days = max(days, ((today or date.today()) - latest).days + 7)
    return f"python -m data.prices_updater --lookback-days {days}"


def check_prices_db(today: date | None = None) -> CheckResult:
    today = today or date.today()
    mrtd = most_recent_trading_day(today)
    nm = "prices_db_fresh"
    rb = prices_refresh_command()
    try:
        from sqlalchemy import create_engine, text
        from config.settings import DATABASE_URL
        eng = create_engine(DATABASE_URL)
        with eng.connect() as c:
            n = c.execute(text("SELECT COUNT(*) FROM prices")).scalar() or 0
            mx = c.execute(text("SELECT MAX(date) FROM prices")).scalar()
    except Exception as e:
        return CheckResult(nm, FAIL, "prices table unreadable", CRITICAL,
                           detail=f"{type(e).__name__}: {e}",
                           blocks="ADV and volatility inputs to execution are "
                                  "unavailable",
                           runbook=rb)
    if not n or not mx:
        return CheckResult(nm, FAIL, "prices table empty", CRITICAL,
                           detail=f"{n} rows",
                           blocks="ADV screening and volatility-sized limits cannot "
                                  "be computed",
                           runbook="python scripts/bootstrap_data.py")
    latest = pd.to_datetime(mx).date()
    rb = prices_refresh_command(latest, today)
    behind = _sessions_between(latest, mrtd)
    m = {"rows": int(n), "latest": latest.isoformat(), "sessions_behind": behind}
    if behind > MAX_PRICES_DB_STALE_SESSIONS:
        return CheckResult(
            nm, FAIL, f"prices stale: latest {latest}, {behind} sessions behind",
            WARNING,
            detail=f"{int(n):,} rows; threshold is {MAX_PRICES_DB_STALE_SESSIONS} "
                   f"sessions. ADV and 20-day sigma feed order sizing and the "
                   f"volatility-sized limit buffer, so stale prices mis-size orders.",
            blocks="execution sizing uses out-of-date ADV and volatility",
            runbook=rb, metrics=m)
    return CheckResult(nm, PASS, f"prices latest {latest} ({behind} behind)", INFO,
                       detail=f"{int(n):,} rows", runbook=rb, metrics=m)


# ── factors that actually vary ──────────────────────────────────────────────────

def check_factors_live(today: date | None = None) -> CheckResult:   # noqa: ARG001
    """Every factor with a weight varies across the latest scored universe.

    A factor that is the same for every stock changes no rank: its weight is weight in
    name only, while every row looks healthy. On 2026-11-17 sentiment (11%) and revenue
    acceleration (5%) were constant, sentiment already at the 2026Q4b build. WARN only:
    ranks are unaffected, and whether to fix, drop or reweight them is a strategy
    decision (NEXT_STEPS). Sentiment is read through the composite's own loader, with
    its 90-day window, so this sees what the next build would."""
    nm = "factors_live"
    rb = "python -m scripts.monthly_run"
    from sqlalchemy import create_engine, text
    from config.settings import DATABASE_URL, FACTOR_WEIGHTS
    with create_engine(DATABASE_URL).connect() as c:
        df = pd.read_sql(text("SELECT * FROM factor_scores WHERE calculation_date = "
                              "(SELECT MAX(calculation_date) FROM factor_scores)"), c)
    if df.empty:
        return CheckResult(nm, SKIP, "no factor scores yet", INFO, runbook=rb)
    from factors.composite import add_sentiment_scores
    from factors.health import constant_factors, describe
    df = add_sentiment_scores(df)
    dead = constant_factors(df, FACTOR_WEIGHTS)
    calc = str(df["calculation_date"].iloc[0])[:10]
    m = {"calculation_date": calc, "constant": {f: w for f, w in dead},
         "constant_weight": round(sum(w for _, w in dead), 4)}
    if not dead:
        return CheckResult(nm, PASS, f"all {len(FACTOR_WEIGHTS)} weighted factors vary "
                                     f"(scores of {calc})", INFO, runbook=rb, metrics=m)
    return CheckResult(
        nm, WARN, f"constant for every stock, so they rank nothing: {describe(dead)}",
        INFO,
        detail=f"scores of {calc}. The composite is really built from the other "
               f"{1 - m['constant_weight']:.0%}. Sentiment needs a transcript from the "
               f"last 90 days; revenue acceleration needs 6 quarters (yfinance gives 5). "
               f"Fix, drop or reweight after the {PERIOD} verdict (NEXT_STEPS).",
        runbook=rb, metrics=m)


# ── inputs to the next book ─────────────────────────────────────────────────────

UNIVERSE_CSV = ROOT / "data" / "iwm_holdings.csv"


def _book_input_ages(today: date) -> dict:
    """Age in days of each input the next quarter's book is picked from (None = absent)."""
    ages = {"universe": None, "fundamentals": None, "factor_scores": None}
    if UNIVERSE_CSV.exists():
        ages["universe"] = (today - date.fromtimestamp(UNIVERSE_CSV.stat().st_mtime)).days
    from sqlalchemy import create_engine, text
    from config.settings import DATABASE_URL
    with create_engine(DATABASE_URL).connect() as c:
        for key, sql in (("fundamentals", "SELECT MAX(period_end_date) FROM fundamentals"),
                         ("factor_scores", "SELECT MAX(calculation_date) FROM factor_scores")):
            mx = c.execute(text(sql)).scalar()
            if mx:
                ages[key] = (today - pd.to_datetime(mx).date()).days
    return ages


def check_book_inputs(today: date | None = None) -> CheckResult:
    """The universe, fundamentals and factor scores the next book is picked from are
    current. Nothing refreshes them on a schedule; on 2026-11-17 the universe file was
    from March, the newest fundamentals quarter 2026-03-31 and the last factor scores
    2026-09-21, and no check said so. WARN while the book is far off; FAIL (WARNING,
    so it never blocks the broker) once it is due within BOOK_DUE_DAYS."""
    today = today or date.today()
    nm = "book_inputs_fresh"
    rb = "python -m scripts.monthly_run"
    ages = _book_input_ages(today)
    limits = {"universe": MAX_UNIVERSE_AGE_DAYS, "fundamentals": MAX_FUNDAMENTALS_AGE_DAYS,
              "factor_scores": MAX_FACTOR_SCORES_AGE_DAYS}
    stale = [f"{k} {'missing' if a is None else f'{a}d old'} (max {limits[k]}d)"
             for k, a in ages.items() if a is None or a > limits[k]]
    from forward_test.evaluate import evaluation_date
    due = evaluation_date(SNAP.parent)
    days_left = (due - today).days if due else None
    m = {"ages_days": ages, "book_due": due.isoformat() if due else None,
         "days_left": days_left}
    ages_txt = ", ".join(f"{k} {a}d" for k, a in ages.items() if a is not None)
    if not stale:
        return CheckResult(nm, PASS, "next book's inputs are current", INFO,
                           detail=ages_txt, runbook=rb, metrics=m)
    how = ("universe: download IWM holdings from ishares.com to data/iwm_holdings.csv "
           "(README step 3); fundamentals and factor scores: the RUN command")
    if days_left is not None and days_left <= BOOK_DUE_DAYS:
        return CheckResult(
            nm, FAIL, f"next book due {due} ({days_left}d) from stale inputs", WARNING,
            detail="; ".join(stale) + ". " + how,
            blocks="the next quarter's book would be picked from out-of-date data",
            runbook=rb, metrics=m)
    when = f"before {due}" if due else "before the next book is built"
    return CheckResult(nm, WARN, f"refresh {when}: " + "; ".join(stale), INFO,
                       detail=how, runbook=rb, metrics=m)


# ── test suite ──────────────────────────────────────────────────────────────────

KNOWN_FAILURES_FILE = Path(__file__).parent / "known_test_failures.txt"


def _known_failures() -> set[str]:
    if not KNOWN_FAILURES_FILE.exists():
        return set()
    out = set()
    for line in KNOWN_FAILURES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.add(line)
    return out


def check_test_suite(timeout_s: int = 600) -> CheckResult:
    """Pass/fail of the suite, measured against a COMMITTED baseline of known failures.

    Reporting "16 tests fail" every day when 16 have failed for weeks trains you to
    ignore the alert. Only NEW failures fail this check; a known failure that starts
    passing is reported so the baseline can be tightened.
    """
    nm = "test_suite"
    rb = "python -m pytest tests/ -q"
    # A fresh temp dir per run, made and removed by this process, and no cache. The
    # Monday task runs pytest under its S4U logon; anything it leaves in a shared place
    # is unreadable for the interactive user. Twice so far: the pytest-current link in
    # the shared temp root (a PermissionError after every manual run), then a fixed
    # monitoring/state/pytest_tmp that broke the next manual run and put 542 temp files
    # into every backup (monitoring/state is backed up).
    import shutil
    import tempfile
    _legacy = ROOT / "monitoring" / "state" / "pytest_tmp"
    if _legacy.exists():                      # removable only by the logon that made it
        shutil.rmtree(_legacy, ignore_errors=True)
    basetemp = Path(tempfile.mkdtemp(prefix="quant_health_pytest_"))
    try:
        out = subprocess.run([sys.executable, "-m", "pytest", "tests/", "-q",
                              "--no-header", "-rfE", "-p", "no:cacheprovider",
                              f"--basetemp={basetemp}"],
                             capture_output=True, text=True, timeout=timeout_s,
                             cwd=str(ROOT),
                             env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except subprocess.TimeoutExpired:
        return CheckResult(nm, FAIL, f"pytest exceeded {timeout_s}s", WARNING,
                           detail="a hanging test is a broken test",
                           blocks="regressions cannot be detected", runbook=rb)
    except Exception as e:
        return CheckResult(nm, FAIL, "could not run pytest", WARNING,
                           detail=f"{type(e).__name__}: {e}", runbook=rb)
    finally:
        shutil.rmtree(basetemp, ignore_errors=True)

    txt = (out.stdout or "") + "\n" + (out.stderr or "")
    failed = set(re.findall(r"^FAILED\s+(\S+?)(?:\s|$)", txt, re.M))
    errors = set(re.findall(r"^ERROR\s+(\S+?)(?:\s|$)", txt, re.M))
    m_tail = re.search(r"(\d+) failed,?\s*(\d+)? ?passed", txt)
    n_failed = int(m_tail.group(1)) if m_tail else len(failed)
    m_pass = re.search(r"(\d+) passed", txt)
    n_passed = int(m_pass.group(1)) if m_pass else 0
    rc = out.returncode
    tail = " | ".join(ln for ln in txt.strip().splitlines()[-3:])

    # The suite did not run as a whole. Exit 2 is a collection error: one test file that
    # cannot be imported stops EVERY test, and this check used to read that as "0
    # passed" with no FAILED line -- PASS. Exit 3-5: internal error, usage error, no
    # tests collected. Exit 1 with nothing parsed is output this check cannot read.
    if errors or rc not in (0, 1) or n_passed == 0 or (rc == 1 and not failed):
        what = (f"{len(errors)} test file(s)/test(s) errored" if errors
                else f"pytest exit {rc}, {n_passed} passed")
        return CheckResult(
            nm, FAIL, f"test suite did not run cleanly: {what}", CRITICAL,
            detail=("errored: " + ", ".join(sorted(errors)[:6]) + "; " if errors else "")
                   + f"exit code {rc}; last lines: {tail[:300]}",
            blocks="regressions cannot be detected while the suite does not run",
            runbook=rb, metrics={"exit_code": rc, "passed": n_passed,
                                 "errors": sorted(errors)})

    known = _known_failures()
    new = sorted(failed - known)
    fixed = sorted(known - failed)
    m = {"failed": n_failed, "passed": n_passed, "new_failures": new,
         "known_failures": len(known), "newly_passing": fixed}

    if new:
        return CheckResult(
            nm, FAIL, f"{len(new)} NEW test failure(s)", CRITICAL,
            detail="new: " + ", ".join(new[:6])
                   + (f" (+{len(new)-6} more)" if len(new) > 6 else "")
                   + f"; {n_passed} passed, {n_failed} failed, {len(known)} known-bad "
                     f"are excluded via monitoring/known_test_failures.txt",
            blocks="a regression is live in the codebase",
            runbook=rb, metrics=m)
    if fixed:
        return CheckResult(
            nm, WARN, f"{len(fixed)} known-bad test(s) now PASS — tighten the baseline",
            INFO,
            detail="now passing: " + ", ".join(fixed[:6])
                   + ". Remove them from monitoring/known_test_failures.txt so a "
                     "future break is caught.",
            runbook=rb, metrics=m)
    return CheckResult(nm, PASS, f"{n_passed} passed, {len(known)} known-bad excluded",
                       INFO, detail="no new failures", runbook=rb, metrics=m)


# ── registry ────────────────────────────────────────────────────────────────────

def run_all(previous: dict | None = None, *, skip_tests: bool = False,
            today: date | None = None) -> list[CheckResult]:
    """Run every check. A check that raises becomes a CRITICAL FAIL, never a crash."""
    prev_growth = (previous or {}).get("snapshot_growth", {}) or {}
    plan = [
        ("snapshot_exists", lambda: check_snapshot_exists(today)),
        ("snapshot_rows", lambda: check_snapshot_rows(today)),
        ("snapshot_fresh", lambda: check_snapshot_fresh(today)),
        ("snapshot_growth", lambda: check_snapshot_growth(prev_growth, today)),
        ("tickers_and_carried", lambda: check_tickers_and_carried(today)),
        ("baselines_match", lambda: check_baselines_match(today)),
        ("scheduled_task", lambda: check_scheduled_task()),
        ("health_task_registered", lambda: check_health_task_registered()),
        ("prices_task", lambda: check_prices_task()),
        ("backup_fresh", lambda: check_backup()),
        ("prices_db_fresh", lambda: check_prices_db(today)),
        ("book_inputs_fresh", lambda: check_book_inputs(today)),
        ("factors_live", lambda: check_factors_live(today)),
    ]
    if not skip_tests:
        plan.append(("test_suite", check_test_suite))

    results = []
    for name, fn in plan:
        try:
            results.append(fn())
        except Exception as e:
            results.append(CheckResult(
                name, FAIL, f"check itself raised {type(e).__name__}", CRITICAL,
                detail=f"{e}. A monitor that dies quietly is worse than none, so "
                       f"this is reported as a failure rather than skipped.",
                blocks="this condition is unmonitored",
                runbook="python -m monitoring.health_check -v"))
    return results
