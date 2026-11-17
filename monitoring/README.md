# Monitoring — making silent failure impossible

`forward_test/2026Q4b` recorded **zero sessions** from 2026-09-21 to 2026-09-30.
`snapshot.py` wrote header-only `price_snapshots.csv` files and exited; nothing
noticed for nine days, and it was found by accident.

Three independent things had to go wrong at once, and all three did:

| # | Hole | Now |
|---|---|---|
| 1 | `snapshot.py` crashed and wrote a 0-row file | `snapshot_rows` FAILs on 0 rows (CRITICAL) |
| 2 | The wrapper masked the exit code, so `LastTaskResult` was **0** while python exited **1** | wrapper fixed (`a3bb065`); `scheduled_task` believes the job's **log** over `LastTaskResult` |
| 3 | `check_snapshot_fresh.py` was hardcoded to `2026Q3_parallel` while the kill-switch read the **active** period | period and paths come from the snapshot **registry**; a test asserts they are the same objects |
| 4 | A 5-for-1 split (SENEA, 2026-11-17) made yfinance restate its history while the locked baseline stayed in old shares: the book read -12.9% instead of -7.5% and the risk watch emailed "-80%", with every check green | `baselines_match` FAILs (CRITICAL); the snapshot records a split yfinance reports in `corporate_actions.json` or exits 1 (`forward_test/baselines.py`) |

None of them told anyone. That is the thing being fixed — not any one bug.

---

## Quick start

```powershell
python -m monitoring.health_check -v        # full run; alerts on any FAIL
python -m monitoring.health_check --fast    # skip the pytest run
python -m monitoring.health_check --json    # machine-readable
python -m monitoring.health_check --no-alert  # check only, send and log nothing
python -m monitoring.health_check --weekly-report  # + write and email the weekly summary
python -m monitoring.health_check --test-email     # send one test email, run no checks
python -m monitoring.break_test            # prove the monitor actually works
```

**Exit code is the contract.** `0` = every check passed, skipped or warned. `1` = at
least one FAIL. The scheduled task and the broker's pre-rebalance gate both read it.

---

## The checks

Severity decides whether a rebalance is blocked, so it is per *failure mode*, not per
check. Two checks have both kinds: a soft data-quality problem is WARNING, but not being
able to **read the data at all** is CRITICAL.

| Check | FAILs when | Severity | Blocks a rebalance? |
|---|---|---|---|
| `snapshot_exists` | the file is missing for a period that has started | CRITICAL | yes |
| `snapshot_rows` | **0 rows** — a crashed run, not a session | CRITICAL | yes |
| | the file is unreadable | CRITICAL | yes |
| `snapshot_fresh` | more than 1 trading session behind | CRITICAL | yes |
| `snapshot_growth` | rows **decreased**, or a new session added no rows | CRITICAL | yes |
| `tickers_and_carried` | expected names missing, or >20% of closes carried forward | WARNING | no |
| `baselines_match` | a locked baseline (restated for recorded splits) disagrees with the stored close on the baseline date by > 1% | CRITICAL | yes |
| | the snapshot or book CSVs cannot be read | CRITICAL | yes |
| `scheduled_task` | not registered, disabled, last run >30h ago, or a non-zero exit | CRITICAL | yes |
| `health_task_registered` | the health check itself is not scheduled, or is disabled | WARNING | no |
| `prices_task` | `QuantPricesUpdate` not registered, disabled, last run >8 days ago, or a non-zero exit | WARNING | no |
| `backup_fresh` | no backup zip, the newest is > 8 days old, or it does not restore (CRC, DB integrity, manifest row counts; `scripts/backup.py`, run by the Monday task) | WARNING | no |
| `backup_fresh` (WARN, not FAIL) | verified, but on the same disk as the database; or in a OneDrive folder while the OneDrive client is not running (only the running client copies it off the disk) | INFO | no |
| `prices_db_fresh` | `prices` more than 5 trading sessions behind | WARNING | no |
| | the `prices` table is unreadable or empty | CRITICAL | yes |
| `book_inputs_fresh` | universe file > 200 days, newest fundamentals quarter > 200 days or factor scores > 100 days old, **and** the next book is due within 21 days (before that: WARN only) | WARNING | no |
| `factors_live` (WARN, not FAIL) | a factor with a weight is the same for every stock in the latest scores, so it ranks nothing (sentiment and revenue acceleration on 2026-11-17) | INFO | no |
| `test_suite` | a **new** test failure appears, or the suite did not run as a whole (a test file that cannot be imported stops every test; exit code >= 2, 0 passed, or output it cannot read) | CRITICAL | yes |
| | pytest cannot be run, or exceeds its timeout | WARNING | no |
| `test_suite` (WARN, not FAIL) | a known-bad test now passes — tighten the baseline | INFO | no |

A check that **raises** is reported as a CRITICAL FAIL, never a crash. A monitor that
dies quietly is worse than no monitor.

Thresholds live in one place, `monitoring/checks.py`:
`MAX_SNAPSHOT_STALE_SESSIONS=1` (matching `killswitch.MAX_STALE_SESSIONS`),
`MAX_TASK_AGE_HOURS=30`, `MAX_PRICES_DB_STALE_SESSIONS=5`,
`MAX_CARRIED_FRACTION=0.20`.

### Why each one exists

- **`snapshot_rows`** — the actual bug. Nine days of header-only CSVs. Verify it still
  catches it: scenario A of `break_test.py` writes exactly that file.
- **`snapshot_fresh`** — this is the file the **5.3 STALE_DATA kill-switch** reads. If
  it is stale, the order path halts; better to know before a rebalance than during one.
- **`snapshot_growth`** — a file can exist, be non-empty, and still be wrong. Rows
  *decreasing* means `snapshot.py`'s promise never to drop a recorded day has
  broken (a fresh close may restate one, e.g. after a split, and says so). A new session appended with no new rows is a
  partial write.
- **`tickers_and_carried`** — right row count, wrong content. Carried-forward closes are
  frozen last-known prices; fine for one delisting, suspicious across many names,
  because returns then understate real moves.
- **`scheduled_task`** — reads **both** Task Scheduler and the job's own log exit line,
  and **believes the log**. `LastTaskResult` was masked by the wrapper bug, and any
  future wrapper edit could mask it again. Scenario C proves this: the log says exit 1
  while the live task genuinely reports 0, and the check fails.
- **`prices_db_fresh`** — ADV and 20-day sigma feed order sizing and the
  volatility-sized limit buffer. Stale prices mis-size orders.
- **`baselines_match`** — returns are close / baseline, and yfinance restates a whole
  history when a name splits. A baseline in old shares against prices in new ones
  reads a split as a loss of 1 - 1/ratio, in the curve, the risk watch and the
  broker's buy-and-hold targets (which would sell the name down). CRITICAL for that
  reason. The RUN command is the snapshot, which records the split or says why not.
- **`book_inputs_fresh`** — nothing refreshes the universe, fundamentals or factor
  scores on a schedule, and the next quarter's book is picked from them. Quiet (a
  to-do line in the Monday email) until three weeks before the book is due.
- **WARN** never emails on its own. The Monday summary lists every WARN as a
  "to do (not urgent)" line, so it is seen without becoming an alarm.
- **`test_suite`** — measured against a **committed baseline**
  (`known_test_failures.txt`, **empty since 2026-10-04**; its 16 sentiment entries
  were rewritten and pass). Reporting the same failures daily would train you to
  ignore the alert, so only
  **new** failures FAIL. A known-bad test that starts passing raises a WARN so the
  baseline gets tightened instead of rotting. **That file is a debt register, not an
  exemption** — shorten it, never lengthen it to quieten an alarm.

---

## Alerting

### Channel

Alerts always append to **`monitoring/state/alert_log.jsonl`**, whether or not email is
configured, and always appear on the dashboard. Email is an *upgrade*, not a
prerequisite — otherwise a missing setting silently switches monitoring off.

A send failure is **reported, never swallowed**, and never changes the exit code.

### Configuring email

Put these in the **gitignored** `.env` at the repo root (`.env.example` has the
template). **Never commit a real password.**

```ini
ALERT_EMAIL_ENABLED=true
ALERT_SMTP_HOST=smtp.gmail.com
ALERT_SMTP_PORT=587
ALERT_SMTP_USER=you@gmail.com
ALERT_SMTP_PASSWORD=xxxxxxxxxxxxxxxx     # 16-char APP PASSWORD, not your password
ALERT_EMAIL_FROM=you@gmail.com
ALERT_EMAIL_TO=you@gmail.com             # comma-separated for several recipients
```

**Gmail:** `ALERT_SMTP_PASSWORD` must be a 16-character **App Password**, not your
account password — Google rejects plain passwords for SMTP. Generate one at
*Google Account → Security → 2-Step Verification → App passwords* (2-Step must be on
for the option to appear). Paste the 16 characters with no spaces. Treat it as a
password: it grants send-as-you access.

**Outlook / Office 365:** `ALERT_SMTP_HOST=smtp.office365.com`, port `587`.
Port `587` uses STARTTLS, `465` uses implicit SSL; both are handled.

Then send one real test message (runs no checks; exit 0 = sent):

```powershell
python -m monitoring.health_check --test-email
```

It never prints the password. If it says `SMTP send FAILED (SMTPAuthenticationError…)`,
the App Password is wrong or has spaces in it.

### Early-warning risk watch (nightly)

`monitoring/risk_watch.py` runs after the 23:30 snapshot (`run_snapshot.bat`) and
emails at the *approach* to a kill-switch, from local data only:

| Alert key | What | WARNING | CRITICAL | Anticipates |
|---|---|---|---|---|
| `risk:book_drawdown` | Q4b tracked book, peak to now | -8% | -12% | 5.1 halt at -15% |
| `risk:account_drawdown` | paper account vs its recorded high-water mark | -8% | -12% | 5.1 halt at -15% |
| `risk:name:<T>` | a tracked name since its baseline | -25% | -40% | 5.2 blocks additions at -50% |
| `risk:sector:<S>` | sector weight in the paper account | > 28% | | 30% sector cap |
| `risk:price_crosscheck` | a close of the newest session differs from Finnhub's close for that session by > 2%; or nothing could be compared (Finnhub down, no key) | yes | | a bad print reaching every return and the evaluation |
| `risk:health_task_stale` | the Monday health check (checks, backup, weekly email) has not run for > 8 days, or is not registered | yes | | a silent week: no email is not an all-clear |

Deduplicated like every alert; a warning that clears sends a RESOLVED note. Nothing
here blocks or places an order. Run by hand: `python -m monitoring.risk_watch --no-alert`.

### The weekly summary

The Monday 15:30 task runs with `--weekly-report`: after the checks it writes
`monitoring/state/weekly_report_<date>.txt` and emails it (same `ALERT_*` settings),
**whether or not anything failed**. One page:

- **Coming up** (first): the next dated action, from the system's own dates (the
  active period's evaluation date in its `QUARTER_END_PLAN.md`, the hedged test's in
  `forward_test/hedged_paper.py`). From its date it is marked **DUE** with the exact
  command, every week, until its result file exists. The dashboard's Today page shows
  the same (`monitoring/upcoming.py`)
1. **Pipeline health**: N/M checks clean, each FAIL with its fix command
2. **Forward test**: Incumbent, Challenger, SPY and IWM over the week and since the
   start, max drawdown, and Incumbent vs SPY and vs IWM. IWM (small caps, fetched from
   yfinance at report time) is information only: the evaluation criteria judge on SPY
3. **Return attribution**: the Incumbent's return split by sector, in percentage points
   that sum to the book's return, plus the three best and worst names of the week
4. **Paper account vs target**: from the last reconciliation file (no Gateway needed),
   valued at the latest snapshot closes: names missing, names not in the book, and any
   name more than 2 pp off its target. The target is the **buy-and-hold** weight the
   forward test tracks (entry weight moved with the price since the baseline), so
   ordinary price moves are not reported as drift; only sizing errors are
5. **Executions in the last 7 days**: orders, fill rate, notional, unfilled
6. **Execution cost vs reference price**: per executed run and cumulative, the
   notional-weighted bps paid versus the price each order was sized off (reconstructed
   from the PLANNED events for runs logged before the reference was stored). Above
   ~10 bps, or with unfilled orders, it says to weigh real-time market data
   (`forward_test/2026Q4b/QUARTER_END_PLAN.md` §6)
7. **Hedged paper test**: updated by the report itself (downloads the IWM closes it
   does not have yet), so it needs no button or command: hedged vs S&P 500 since the
   start, drawdowns, and the verdict date. The dashboard reads the same file

A section whose data is missing prints `unavailable: <why>` rather than stopping the
report. Built by `monitoring/weekly_report.py`.

### What triggers an alert

- any health-check **FAIL**
- a **kill-switch trip** (`killswitch:<switch>`, always CRITICAL)
- a **blocked rebalance** — escalating to CRITICAL when *nothing* was approved
- a **refused rebalance** (the health gate stopped it)
- the **scheduled task not having run** (via `scheduled_task`)

### Deduplication and escalation

One persistent failure must not produce a daily flood, and it must not be forgotten
either. So:

- **first occurrence** — sent immediately
- **repeats** — suppressed on an exponential cooldown: 1 day, then 2, 4, 8… capped at 7
- **after 3 consecutive occurrences** — severity **escalates** to CRITICAL and a fresh
  alert is sent, because "still broken on day 3" is different news from "broken today"
- **recovery** — exactly one `RESOLVED` alert, then the state is cleared

Measured over 6 consecutive identical daily failures: **3 alerts, not 6**, with severity
moving WARNING → CRITICAL (scenario E).

Every alert states **what failed, since when, what it blocks, and the exact command to
investigate**. An alert without a next action is noise.

---

## The rebalance gate

`broker/ibkr_paper.main()` calls `assert_pipeline_healthy()` **before it reads the
book**. A book must never be sized from a pipeline that is not recording — that failure,
expressed as orders, is a rebalance against data that does not exist.

- **CRITICAL failures block.** The data a book would be sized from is missing, stale or
  wrong.
- **WARNING failures do not block.** They print loudly with their fix command. A gate
  that halts on every yellow light is a gate that gets switched off.
- **The dry run is gated too**, deliberately: the dry run is what you read before
  executing, so it must not present numbers derived from a broken pipeline.
- **`--read-only` is exempt** — it sizes nothing.
- **No override flag**, following the kill-switch precedent. `--skip-health-tests` only
  skips pytest; every data check still runs and still blocks.

Proven by `tests/test_monitoring.py::TestPipelineGate::test_broker_refuses_before_sizing_anything`,
which stubs the broker and asserts `reference_prices()` and the book read are **never
reached**.

---

## Scheduling

| Task | When | Wrapper |
|---|---|---|
| `QuantForwardTestSnapshot` | 23:30 daily | `forward_test/run_snapshot.bat` |
| `QuantHealthCheck` | **Mondays 15:30** (weekly) | `monitoring/run_health_check.bat` |
| `QuantPricesUpdate` | **Mondays 15:00** (weekly) | `data/run_prices_update.bat` (log: `monitoring\state\prices_update_log.txt`) |

```powershell
powershell -ExecutionPolicy Bypass -File monitoring\register_tasks.ps1 -Elevate
powershell -ExecutionPolicy Bypass -File monitoring\register_tasks.ps1 -Job Prices -Elevate
powershell -ExecutionPolicy Bypass -File monitoring\register_tasks.ps1 -WhatIf
powershell -ExecutionPolicy Bypass -File monitoring\register_tasks.ps1 -Unregister
```

**Why Mondays 15:00 for prices** — 08:00 in New York: the previous close is final and
the open is still ahead. Weekly since 2026-10-04 (it was every weekday): the book
rebalances monthly and sizing uses 60-day ADV and 20-day sigma, so a few days of
staleness barely moves them. `prices_db_fresh` allows 5 sessions, and Monday-to-Monday
peaks at exactly 5 at Friday's health check, so a missed Monday run is caught the next
day. Before an execution run, refresh by hand (`python -m data.prices_updater`) if you
want the latest close. Nothing ran the updater automatically before this task existed,
which is how `prices` drifted 8 sessions stale.

**Why Mondays 15:30** — weekly since 2026-10-04 (it ran nightly at 23:50): half an hour
after the Monday price refresh, so `prices_db_fresh` sees fresh data, and before the US
open. Friday's snapshot is then 1 session behind, inside the 5.3 grace, so a healthy
week reads all-green, and `snapshot_growth` compares a week of growth instead of a day.

**The trade-off, stated plainly:** a broken snapshot can now go unnoticed *by this task*
for up to a week, which is the failure this monitoring was built after. Two things
bound it: every order run (`python -m broker.ibkr_paper`) executes the same checks
itself and refuses on any CRITICAL failure, so a broken pipeline cannot reach a trade;
and you can run `python -m monitoring.health_check -v` any time. The dashboard flags the
check as STALE after 8 days instead of 30 hours.

**Why S4U** — it runs with nobody logged in. Verified on this machine: an
`Interactive`-logon task registers *unelevated*, S4U returns "Access is denied", so
**elevation is required by the logon type**, not by `Register-ScheduledTask`. We keep
S4U anyway, because `Interactive` only runs while you are signed in and a recorder that
silently skips nights is the failure being fixed. `-Elevate` relaunches through UAC and
reports plainly if the prompt is declined.

`register_tasks.ps1` **refuses** to register a wrapper whose text lacks
`exit /b %RC%` — a monitor whose `LastTaskResult` is always 0 is worse than none,
because it reports healthy while doing nothing.

### Verify the schedule actually works

```powershell
Start-ScheduledTask -TaskName QuantHealthCheck
Get-ScheduledTaskInfo -TaskName QuantHealthCheck | Select LastRunTime,LastTaskResult
type monitoring\state\health_log.txt
```

`LastTaskResult` should be `1` while anything is failing and `0` when all clear. If it
is **always 0**, the wrapper is swallowing the exit code again — that is the original
bug, and everything downstream of it is theatre.

---

## What to do when something fires

| Alert | Meaning | Do this |
|---|---|---|
| `snapshot_rows` 0 rows | a crashed run wrote a header-only file | `type forward_test\2026Q4b\snapshot_log.txt` — read the traceback. Fix the cause; do **not** delete the file and call it fixed. |
| `snapshot_fresh` stale | sessions are being missed | `python forward_test/check_snapshot_fresh.py`, then check Task Scheduler. **5.3 will halt trading** until it is fresh. |
| `snapshot_growth` rows decreased | recorded history is being destroyed | Stop and investigate before running the snapshot again — a rebuild-from-scratch is overwriting merged history. |
| `scheduled_task` failed / stale | the job died, or the machine was asleep at 23:30 | Read `snapshot_log.txt`. `StartWhenAvailable` catches up after a late boot, so a one-off late run is normal; a pattern is not. If it is **not registered**: `powershell -Command "Register-ScheduledTask -TaskName QuantForwardTestSnapshot -Xml (Get-Content forward_test\QuantForwardTestSnapshot.xml -Raw)"` — not `register_tasks.ps1`, which registers the *health* task. |
| `tickers_and_carried` | names missing, or prices frozen | Check for delistings. Many carried cells at once usually means a yfinance outage, not many delistings. |
| `prices_task` | the daily refresh is missing, off or failing | Run the `RUN:` line. Exit 1 in `prices_update_log.txt` = every batch came back empty: check connectivity, then `python -m data.prices_updater --dry-run`. |
| `prices_db_fresh` | ADV and sigma inputs are old | `python -m data.prices_updater --lookback-days N` — the alert prints `N` sized to the gap (default 35). Re-run the health check to confirm it PASSes. |
| `test_suite` new failure | a regression is live | `python -m pytest tests/ -q`. Fix it — do **not** add it to `known_test_failures.txt`. |
| `killswitch:*` | the order path HALTED | **broker/RUNBOOK.md §7** for that switch. There is no override flag; resuming is a deliberate human decision. |
| `rebalance:blocked` approved 0 | the book violates a hard limit | `python -m broker.build_compliant_book` (RUNBOOK §8). Rebuild the book under the limits; do **not** relax a limit to make an order fit. |
| `rebalance:refused` | the health gate stopped it | `python -m monitoring.health_check -v` and fix what it names. |
| dashboard: *last health run N hours ago* | the monitor itself stopped | `Get-ScheduledTaskInfo -TaskName QuantHealthCheck`. See the honest gap below. |

---

## Proving it works

```powershell
python -m monitoring.break_test          # 5 deliberate-break scenarios
python -m pytest tests/test_monitoring.py -q   # 34 unit tests
```

`break_test.py` breaks the pipeline on purpose and asserts the check FAILS, the alert
FIRES, and the rebalance REFUSES:

| | Scenario | Proves |
|---|---|---|
| A | 0-row snapshot | the actual nine-day bug is caught, CRITICAL, and blocks |
| B | stale snapshot | `snapshot_fresh` catches it while `snapshot_rows` correctly passes — the two cover different failures |
| C | task failed | the check believes the job's **log** over a `LastTaskResult` of 0 |
| D | kill-switch trip | a real 5.3 halt raises a CRITICAL alert and is logged |
| E | dedup/escalation | 6 identical daily failures → 3 alerts, WARNING → CRITICAL |

Everything runs in a temp sandbox: snapshot paths *and* alert log/state/history are
redirected, so real dedup state is neither read nor written, and email is never
attempted. Nothing connects to a broker.

---

## Files

| Path | What |
|---|---|
| `checks.py` | the individual checks; each returns a `CheckResult` with severity, what it blocks, and its fix command |
| `health_check.py` | the CLI runner, and `assert_pipeline_healthy()` used by the broker gate |
| `alerting.py` | alert model, dedup/escalation, SMTP, alert log |
| `break_test.py` | the deliberate-break proof |
| `known_test_failures.txt` | committed baseline of known-bad tests (a debt register) |
| `run_health_check.bat` | scheduled-task wrapper; propagates the exit code |
| `register_tasks.ps1` | task registration |
| `state/` | **gitignored, machine-local**: `alert_log.jsonl`, `alert_state.json`, `health_history.json`, `health_log.txt` |

---

## The honest gap

**Nothing self-hosted can report "the health check never ran at all" from inside a run
that is happening.** `health_task_registered` catches the task being *unregistered or
disabled*, but not a machine that is simply off.

That gap is covered by putting the **age of the last health run** on the dashboard as
its own metric, with a red banner past 30 hours. It is the one failure mode you have to
look for rather than be told about. Closing it properly needs something outside this
machine — a dead-man's-switch ping to an external service — which is not built here.

Two deliberate non-goals, stated so they are not mistaken for oversights:

- **`health_task_registered` never checks its own `LastTaskResult`.** That value is 1
  whenever any check fails, so testing it would make the monitor fail because the
  monitor found something — a feedback loop that reports nothing useful.
- **Monitoring cannot break trading.** Every event alert goes through a wrapper that
  never raises. Unreachable SMTP or an unwritable state directory must not turn a clean
  refusal into a crash, and must never influence whether an order is placed.

---

**PAPER ONLY.** Nothing in this package places an order, edits a book, or writes to the
production DB. It writes only under `monitoring/state/`.
