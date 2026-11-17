"""
monitoring/alerting.py — notify the user when something is actually wrong.

DESIGN RULES, in priority order:

1. The alert LOG is never optional. monitoring/state/alert_log.jsonl is appended on
   every alert whether or not email is configured, so monitoring works the moment this
   module exists and email is an upgrade rather than a prerequisite. A monitor that
   only works once SMTP is set up is a monitor that silently does nothing.

2. A failure to send is itself reported, never swallowed. If SMTP fails the alert is
   still logged and the reason is printed; the health check's exit code does not depend
   on whether email got through.

3. One persistent failure must not produce a daily flood. Repeats are suppressed on an
   exponential cooldown (1d, 2d, 4d ... capped at 7d) and ESCALATED instead: after
   ESCALATE_AFTER consecutive occurrences the severity is raised and a fresh alert is
   sent, because "still broken on day 3" is different news from "broken today".

4. Recovery is news too. A check that was failing and now passes sends exactly one
   RESOLVED alert, then its state is cleared.

CONFIGURATION (all optional; email is off unless ALERT_EMAIL_ENABLED=true)

  Put these in the gitignored .env at the repo root -- see .env.example. NEVER commit
  a real password. For Gmail, ALERT_SMTP_PASSWORD must be a 16-character App Password
  (Google Account -> Security -> 2-Step Verification -> App passwords), not the account
  password; Google rejects plain passwords for SMTP.

    ALERT_EMAIL_ENABLED=true
    ALERT_SMTP_HOST=smtp.gmail.com
    ALERT_SMTP_PORT=587
    ALERT_SMTP_USER=you@gmail.com
    ALERT_SMTP_PASSWORD=xxxxxxxxxxxxxxxx     # app password, 16 chars, no spaces
    ALERT_EMAIL_FROM=you@gmail.com
    ALERT_EMAIL_TO=you@gmail.com             # comma-separated for several recipients
"""
from __future__ import annotations

import json
import os
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except Exception:                                   # dotenv absent is not fatal
    pass

STATE_DIR = Path(__file__).parent / "state"
ALERT_LOG = STATE_DIR / "alert_log.jsonl"
ALERT_STATE = STATE_DIR / "alert_state.json"
HEALTH_HISTORY = STATE_DIR / "health_history.json"

# Dedup / escalation policy.
COOLDOWN_BASE_HOURS = 24        # first suppression window
COOLDOWN_MAX_HOURS = 24 * 7     # never wait longer than a week to re-nag
ESCALATE_AFTER = 3              # consecutive occurrences before severity is raised

SEVERITY_ORDER = {"INFO": 0, "WARNING": 1, "CRITICAL": 2}


@dataclass
class Alert:
    key: str                     # stable identity, e.g. "health:snapshot_rows"
    title: str
    severity: str                # INFO | WARNING | CRITICAL
    summary: str
    detail: str = ""
    blocks: str = ""
    runbook: str = ""
    since: str = ""              # first time this was seen failing
    occurrences: int = 1
    resolved: bool = False
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = {"ts": datetime.now().isoformat(timespec="seconds"), "key": self.key,
             "title": self.title, "severity": self.severity, "summary": self.summary,
             "detail": self.detail, "blocks": self.blocks, "runbook": self.runbook,
             "since": self.since, "occurrences": self.occurrences,
             "resolved": self.resolved}
        if self.extra:
            d["extra"] = self.extra
        return d

    def render(self) -> str:
        """Plain-text body. Answers: what, since when, what it blocks, what to run."""
        if self.resolved:
            return (f"RESOLVED: {self.title}\n\n"
                    f"{self.summary}\n\n"
                    f"Was failing since {self.since or 'unknown'} "
                    f"({self.occurrences} occurrence(s)). No action needed.\n")
        lines = [
            f"[{self.severity}] {self.title}",
            "",
            f"WHAT FAILED   {self.summary}",
            f"SINCE         {self.since or 'this run'}"
            + (f"  ({self.occurrences} consecutive runs)" if self.occurrences > 1 else ""),
        ]
        if self.blocks:
            lines.append(f"BLOCKS        {self.blocks}")
        if self.detail:
            lines += ["", "DETAIL", f"  {self.detail}"]
        if self.runbook:
            lines += ["", "INVESTIGATE (from broker/RUNBOOK.md and monitoring/README.md)",
                      f"  {self.runbook}"]
        lines += ["", "-" * 66,
                  "Sent by monitoring/health_check.py. PAPER ONLY — this system is "
                  "not authorised for real money."]
        return "\n".join(lines)


# ── state ───────────────────────────────────────────────────────────────────────

def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except Exception:
        return default


def _save(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)          # atomic: a killed run cannot leave a half-written state


def load_alert_state() -> dict:
    return _load(ALERT_STATE, {})


def save_alert_state(st: dict) -> None:
    _save(ALERT_STATE, st)


def load_history() -> list:
    h = _load(HEALTH_HISTORY, [])
    return h if isinstance(h, list) else []


def append_history(entry: dict, keep: int = 180) -> None:
    h = load_history()
    h.append(entry)
    _save(HEALTH_HISTORY, h[-keep:])


def previous_metrics() -> dict:
    """Per-check metrics from the most recent recorded health run."""
    h = load_history()
    return (h[-1].get("metrics", {}) if h else {}) or {}


# ── dedup / escalation ──────────────────────────────────────────────────────────

def _cooldown_hours(sends: int) -> float:
    return min(COOLDOWN_BASE_HOURS * (2 ** max(sends - 1, 0)), COOLDOWN_MAX_HOURS)


def decide(alerts: list[Alert], state: dict, now: datetime | None = None,
           resolve_scope: str | None = None
           ) -> tuple[list[Alert], list[Alert], dict]:
    """Split incoming alerts into (to_send, suppressed) and return the new state.

    `resolve_scope` is the key prefix whose absence means "recovered". The health check
    passes "health:" because it evaluates every health key on every run, so a key it
    does not report is genuinely fixed. An EVENT (a kill-switch trip, a blocked
    rebalance) passes None: a trip alert says nothing about whether the snapshot is
    still stale, and without this scoping one event would silently mark every
    outstanding health alert resolved.
    """
    now = now or datetime.now()
    state = dict(state)
    incoming = {a.key: a for a in alerts}
    to_send: list[Alert] = []
    suppressed: list[Alert] = []

    for key, a in incoming.items():
        prev = state.get(key)
        if not prev:
            a.since = now.isoformat(timespec="seconds")
            a.occurrences = 1
            state[key] = {"since": a.since, "occurrences": 1,
                          "last_sent": now.isoformat(timespec="seconds"),
                          "sends": 1, "severity": a.severity,
                          "summary": a.summary}
            to_send.append(a)
            continue

        occ = int(prev.get("occurrences", 0)) + 1
        a.since = prev.get("since", now.isoformat(timespec="seconds"))
        a.occurrences = occ

        # Escalate a failure that will not go away: day 3 of broken is new information.
        escalated = False
        if occ >= ESCALATE_AFTER and SEVERITY_ORDER.get(a.severity, 1) < 2:
            a.severity = "CRITICAL"
            escalated = a.severity != prev.get("severity")

        last_sent = prev.get("last_sent")
        try:
            due = (now - datetime.fromisoformat(last_sent)
                   ) >= timedelta(hours=_cooldown_hours(int(prev.get("sends", 1))))
        except Exception:
            due = True

        rec = {"since": a.since, "occurrences": occ, "severity": a.severity,
               "summary": a.summary, "sends": int(prev.get("sends", 1)),
               "last_sent": last_sent}
        if escalated or due:
            a.title += f" (ESCALATED, {occ} consecutive runs)" if escalated else \
                       f" (still failing, {occ} consecutive runs)"
            rec["sends"] = int(prev.get("sends", 1)) + 1
            rec["last_sent"] = now.isoformat(timespec="seconds")
            to_send.append(a)
        else:
            suppressed.append(a)
        state[key] = rec

    # Recovery: previously-failing keys IN SCOPE that this run did not report.
    if resolve_scope is not None:
        gone = [k for k in state
                if k.startswith(resolve_scope) and k not in incoming]
        for key in gone:
            prev = state.pop(key)
            to_send.append(Alert(
                key=key, title=f"RESOLVED - {key}", severity="INFO",
                summary=f"{prev.get('summary', key)} - now passing.",
                since=prev.get("since", ""),
                occurrences=int(prev.get("occurrences", 1)), resolved=True))

    return to_send, suppressed, state


# ── delivery ────────────────────────────────────────────────────────────────────

def log_alert(a: Alert) -> None:
    """Append-only local record. Always happens, email or not."""
    ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ALERT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(a.as_dict()) + "\n")


def email_config() -> dict:
    """Read SMTP settings from the environment. Never logs the password."""
    return {
        "enabled": os.getenv("ALERT_EMAIL_ENABLED", "").strip().lower()
                   in ("1", "true", "yes", "on"),
        "host": os.getenv("ALERT_SMTP_HOST", "").strip(),
        "port": int(os.getenv("ALERT_SMTP_PORT", "587") or 587),
        "user": os.getenv("ALERT_SMTP_USER", "").strip(),
        "password": os.getenv("ALERT_SMTP_PASSWORD", ""),
        "sender": (os.getenv("ALERT_EMAIL_FROM", "")
                   or os.getenv("ALERT_SMTP_USER", "")).strip(),
        "to": [x.strip() for x in os.getenv("ALERT_EMAIL_TO", "").split(",")
               if x.strip()],
    }


def email_status() -> tuple[bool, str]:
    """(usable, human explanation). Never includes the password."""
    c = email_config()
    if not c["enabled"]:
        return False, ("email disabled (ALERT_EMAIL_ENABLED not true) — alerts go to "
                       "monitoring/state/alert_log.jsonl only")
    missing = [k for k in ("host", "user", "password", "sender") if not c[k]]
    if not c["to"]:
        missing.append("to")
    if missing:
        return False, ("email ENABLED but incomplete: missing "
                       + ", ".join("ALERT_" + m.upper() for m in missing)
                       + " — see monitoring/README.md")
    return True, f"email -> {', '.join(c['to'])} via {c['host']}:{c['port']}"


def send_email(alerts: list[Alert]) -> tuple[bool, str]:
    """One digest email for the batch. Returns (sent, message)."""
    usable, why = email_status()
    if not usable:
        return False, why

    worst = max((SEVERITY_ORDER.get(a.severity, 1) for a in alerts), default=1)
    tag = {0: "INFO", 1: "WARNING", 2: "CRITICAL"}[worst]
    failing = [a for a in alerts if not a.resolved]
    head = (f"[quant-system {tag}] {len(failing)} check(s) failing"
            if failing else "[quant-system] recovered")

    body = ["Quant system health alert",
            f"generated {datetime.now():%Y-%m-%d %H:%M:%S} (Europe/Helsinki local)",
            "=" * 66, ""]
    for a in alerts:
        body += [a.render(), ""]

    return send_text(head, "\n".join(body))


def send_text(subject: str, body: str) -> tuple[bool, str]:
    """Send one plain-text email with the configured SMTP settings.

    Shared by the alert digest, the weekly report and `health_check --test-email`.
    Returns (sent, message); never raises, never logs the password.
    """
    usable, why = email_status()
    if not usable:
        return False, why
    c = email_config()

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = c["sender"]
    msg["To"] = ", ".join(c["to"])
    msg.set_content(body)

    try:
        ctx = ssl.create_default_context()
        if c["port"] == 465:
            with smtplib.SMTP_SSL(c["host"], c["port"], context=ctx, timeout=45) as s:
                s.login(c["user"], c["password"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(c["host"], c["port"], timeout=45) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.login(c["user"], c["password"])
                s.send_message(msg)
        return True, f"emailed {', '.join(c['to'])}"
    except Exception as e:
        # Never swallow. The alert is already logged; say why the mail failed.
        return False, f"SMTP send FAILED ({type(e).__name__}: {e})"


def dispatch(alerts: list[Alert], *, now: datetime | None = None,
             send_mail: bool = True, resolve_scope: str | None = None) -> dict:
    """Dedup, log, and (if configured) email. Returns a summary for the caller."""
    state = load_alert_state()
    to_send, suppressed, new_state = decide(alerts, state, now=now,
                                            resolve_scope=resolve_scope)
    for a in to_send:
        log_alert(a)
    save_alert_state(new_state)

    mailed, mail_msg = False, "not attempted"
    if to_send and send_mail:
        mailed, mail_msg = send_email(to_send)
    elif not to_send:
        mail_msg = "nothing new to send"

    return {"sent": [a.key for a in to_send],
            "suppressed": [a.key for a in suppressed],
            "resolved": [a.key for a in to_send if a.resolved],
            "emailed": mailed, "email_message": mail_msg}


# ── event alerts (called from the trading path, not from the health check) ──────

def _safe_dispatch(a: Alert) -> dict:
    """Send one event alert. NEVER raises.

    These are called from inside the broker path. An alerting failure -- unreachable
    SMTP, an unwritable state dir -- must not turn a clean refusal into a crash, and
    must never be able to affect whether an order is placed. Monitoring observes the
    trading path; it does not get to break it.
    """
    try:
        return dispatch([a], resolve_scope=None)
    except Exception as e:                                       # noqa: BLE001
        try:
            print(f"  [alerting failed: {type(e).__name__}: {e}]")
        except Exception:
            pass
        return {"sent": [], "suppressed": [], "resolved": [], "emailed": False,
                "email_message": f"alerting raised {type(e).__name__}"}


def alert_killswitch_trip(switch: str, detail: str, *, context: str = "") -> dict:
    """A kill-switch HALTed the order path."""
    return _safe_dispatch(Alert(
        key=f"killswitch:{switch}",
        title=f"KILL-SWITCH TRIPPED: {switch}",
        severity="CRITICAL",
        summary=f"{switch} halted the order path: {detail}",
        detail=(f"{context}\n  " if context else "")
               + "Every decision is recorded in broker/killswitch_audit.log. There is "
                 "no override flag by design -- resuming after a halt is a deliberate "
                 "human decision (RUNBOOK section 7).",
        blocks="no order can be submitted until this is resolved",
        runbook="see broker/RUNBOOK.md section 7 for this switch, then "
                "type broker\\killswitch_audit.log"))


def alert_rebalance_blocked(n_blocked: int, n_approved: int, reasons: list[str],
                            *, book: str = "") -> dict:
    """The gate dropped orders, or approved none at all."""
    worst = "CRITICAL" if n_approved == 0 else "WARNING"
    head = ("the gate approved NOTHING" if n_approved == 0
            else f"{n_blocked} of {n_blocked + n_approved} orders blocked")
    return _safe_dispatch(Alert(
        key="rebalance:blocked",
        title=f"rebalance blocked: {head}",
        severity=worst,
        summary=f"{head}"
                + (f" for {book}" if book else ""),
        detail="; ".join(reasons[:8])
               + (f" (+{len(reasons)-8} more)" if len(reasons) > 8 else "")
               + ("\n  approved:0 is a legitimate result, not a bug -- it means the "
                  "book violates a hard limit. Rebuild the book under the limits; do "
                  "NOT relax the limit to make the order fit."
                  if n_approved == 0 else ""),
        blocks="the target book cannot be reached in full",
        runbook="python -m broker.build_compliant_book   (RUNBOOK section 8)"))


def alert_rebalance_refused(reason: str, *, detail: str = "") -> dict:
    """The rebalance refused to start -- unhealthy pipeline, no prices, bad book."""
    return _safe_dispatch(Alert(
        key="rebalance:refused",
        title="rebalance REFUSED before submitting anything",
        severity="CRITICAL",
        summary=reason,
        detail=detail or "Nothing was submitted. This is the intended behaviour: the "
                         "book is not sized from an unhealthy pipeline.",
        blocks="no rebalance can run until the underlying failure is fixed",
        runbook="python -m monitoring.health_check -v"))


def resolve_after_clean_gate(*, blocked: bool) -> list[str]:
    """The kill-switch gate just ran WITHOUT a halt: any open kill-switch alert is over.
    If it also blocked nothing, any open blocked/refused-rebalance alert is over too
    (a refused rebalance never reaches the gate, so reaching it means it ran).

    Event alerts are raised by the broker path, but until 2026-10-06 nothing ever
    closed them: a 5.3 halt from 09-30 still read "CRITICAL, active" a week after the
    switch had been healthy on every run. Sends a RESOLVED note per key it clears.
    Returns the cleared keys. NEVER raises (same contract as _safe_dispatch).
    """
    cleared: list[str] = []
    for scope, applies in (("killswitch:", True), ("rebalance:", not blocked)):
        if not applies:
            continue
        try:
            cleared += dispatch([], resolve_scope=scope)["resolved"]
        except Exception as e:                                   # noqa: BLE001
            try:
                print(f"  [alert resolution failed: {type(e).__name__}: {e}]")
            except Exception:
                pass
    return cleared


def recent_alerts(limit: int = 50) -> list[dict]:
    """Newest-first, for the dashboard."""
    if not ALERT_LOG.exists():
        return []
    out = []
    for line in ALERT_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return list(reversed(out))[:limit]
