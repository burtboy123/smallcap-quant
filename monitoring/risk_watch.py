"""
monitoring/risk_watch.py — early warnings, BEFORE a kill-switch has to fire.

The kill-switches act at the hard limits: 5.1 halts all trading at a -15% drawdown,
5.2 blocks additions to a name down 50% from entry, the gate caps a sector at 30%. By
then the damage is done. This watch emails at the approach, from local data only (no
IB Gateway needed), after the nightly snapshot:

  risk:book_drawdown     Q4b tracked book, peak-to-now       WARN -8%   CRIT -12%
  risk:account_drawdown  paper account vs its recorded HWM   WARN -8%   CRIT -12%
                         (broker/nav_history.py; as fresh as the last broker connection)
  risk:name:<TICKER>     any tracked name since its baseline WARN -25%  CRIT -40%
  risk:sector:<SECTOR>   sector weight in the paper account  WARN > 28% (cap 30%)

Alerts go through monitoring/alerting.dispatch with resolve_scope "risk:", so they are
deduplicated, emailed immediately, escalate if they persist, and a warning that clears
sends a RESOLVED note. Informational only: nothing here blocks or places an order.

    python -m monitoring.risk_watch            # evaluate + alert
    python -m monitoring.risk_watch --no-alert # print only
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from monitoring import alerting as al          # noqa: E402
from monitoring import checks as ck            # noqa: E402

DD_WARN, DD_CRIT = -0.08, -0.12                # the 5.1 halt is at -15%
NAME_WARN, NAME_CRIT = -0.25, -0.40            # 5.2 blocks additions at -50%
SECTOR_WARN = 0.28                             # the sector cap is 30%


def _book_drawdown(period_dir: Path) -> list[al.Alert]:
    eq_p = period_dir / "equity_curves.csv"
    if not eq_p.exists():
        return []
    eq = pd.read_csv(eq_p)
    if eq.empty or "incumbent_index" not in eq:
        return []
    curve = eq["incumbent_index"]
    dd = float(curve.iloc[-1] / curve.cummax().iloc[-1] - 1)
    if dd > DD_WARN:
        return []
    sev = "CRITICAL" if dd <= DD_CRIT else "WARNING"
    return [al.Alert(
        key="risk:book_drawdown", severity=sev,
        title=f"Q4b book drawdown {dd:.1%} (the 5.1 halt is at -15%)",
        summary=f"tracked Incumbent book is {dd:.1%} below its peak "
                f"as of {eq['date'].iloc[-1]}",
        detail="The kill-switch halts ALL trading at -15% on the paper account. The "
               "tracked book is the closest daily proxy for it.",
        blocks="nothing yet -- this is the early warning",
        runbook="python forward_test/evaluate.py   (and broker/RUNBOOK.md §7)")]


def _account_drawdown(account: str | None) -> list[al.Alert]:
    from broker import nav_history
    rows = nav_history.history(account) if account else []
    if not rows:
        return []
    last = rows[-1]
    hw = nav_history.high_water_mark(account, last["nav"])
    dd = hw["drawdown"]
    if dd > DD_WARN:
        return []
    sev = "CRITICAL" if dd <= DD_CRIT else "WARNING"
    return [al.Alert(
        key="risk:account_drawdown", severity=sev,
        title=f"paper account {account} drawdown {dd:.1%} (halt at -15%)",
        summary=f"NAV {last['nav']:,.0f} {last['currency']} on {last['ts'][:10]} vs "
                f"high-water mark {hw['hwm']:,.0f} ({hw['hwm_ts'][:10]})",
        detail="Measured in the account's base currency at the last broker "
               "connection. Run --read-only for a fresh figure.",
        blocks="nothing yet -- at -15% the 5.1 switch halts every order",
        runbook="python -m broker.ibkr_paper --read-only")]


def _names(period_dir: Path) -> list[al.Alert]:
    from forward_test.baselines import current_baselines
    base = current_baselines(period_dir)["incumbent"]       # split-restated
    snap = pd.read_csv(ck.SNAP)
    if snap.empty or not base:
        return []
    last = snap.sort_values("date").groupby("ticker")["close"].last()
    out = []
    for t, b in base.items():
        if t not in last or not b:
            continue
        r = float(last[t] / b - 1)
        if r > NAME_WARN:
            continue
        sev = "CRITICAL" if r <= NAME_CRIT else "WARNING"
        out.append(al.Alert(
            key=f"risk:name:{t}", severity=sev,
            title=f"{t} down {r:.0%} since the Q4b baseline",
            summary=f"{t} closed {last[t]:.2f} vs baseline {b:.2f} ({r:+.1%})",
            detail="At -50% from entry the 5.2 switch blocks any addition to it. Check "
                   "for news: a delisting, halt or filing changes what to do.",
            blocks="nothing yet", runbook=f"python forward_test/evaluate.py"))
    return out


def _sectors(period_dir: Path) -> list[al.Alert]:
    from monitoring import weekly_report as wr
    got = wr._latest_recon()
    if not got:
        return []
    _p, rec = got
    pos = pd.DataFrame(rec.get("positions_after", []))
    if pos.empty:
        return []
    book = pd.read_csv(period_dir / "incumbent_book.csv")
    sector = dict(zip(book["ticker"], book.get("sector", pd.Series(dtype=str))))
    snap = pd.read_csv(ck.SNAP)
    closes = snap.sort_values("date").groupby("ticker")["close"].last()
    pos["value"] = pos["position"] * pos["ticker"].map(closes).fillna(pos["avg_cost"])
    total = pos["value"].sum()
    if not total:
        return []
    w = pos.groupby(pos["ticker"].map(sector).fillna("Unknown"))["value"].sum() / total
    return [al.Alert(
        key=f"risk:sector:{s}", severity="WARNING",
        title=f"{s} is {x:.1%} of the paper account (cap 30%)",
        summary=f"sector weight {x:.1%} from the last reconciled positions at the "
                f"latest closes",
        detail="The gate blocks BUYs that take a sector over 30%. Drift up here comes "
               "from that sector outperforming; the next rebalance trims it.",
        blocks="further buys in this sector once over 30%",
        runbook=f"python -m broker.ibkr_paper --book forward_test/{ck.PERIOD}/"
                "incumbent_book.csv --execution-engine")
        for s, x in w.items() if x > SECTOR_WARN]


HEALTH_RUN_MAX_DAYS = 8                        # weekly task, plus a day of slack
PRICE_XCHECK_MAX = 0.02                        # second-source close may differ by 2%


def _finnhub_quote(ticker: str) -> dict:
    """Finnhub /quote (free tier): c = last price, pc = previous close, t = last trade."""
    import json as _json
    import urllib.parse
    import urllib.request
    from config.settings import FINNHUB_API_KEY
    url = "https://finnhub.io/api/v1/quote?" + urllib.parse.urlencode(
        {"symbol": ticker, "token": FINNHUB_API_KEY})
    with urllib.request.urlopen(url, timeout=20) as r:
        return _json.load(r)


def _price_crosscheck(period_dir: Path, quote=None, has_key: bool | None = None
                      ) -> list[al.Alert]:
    """The snapshot's newest closes (Yahoo) against a second, independent source.

    Splits are caught by baselines_match; a bad print is not, and every return, the
    risk watch and the evaluation would carry it. Finnhub's free quote has no history,
    so only the newest session is checked -- nightly, before a bad close settles into
    the record. A close is compared with Finnhub's close FOR THAT SESSION: `c` when its
    last trade is on that day, `pc` when its last trade is on the next session."""
    from forward_test.market_calendar import most_recent_trading_day
    from datetime import timedelta, timezone
    if has_key is None:
        from config.settings import FINNHUB_API_KEY
        has_key = bool(FINNHUB_API_KEY)
    if not has_key:
        return [al.Alert(
            key="risk:price_crosscheck", severity="WARNING",
            title="price cross-check cannot run: no FINNHUB_API_KEY",
            summary="the snapshot's closes are not checked against a second source",
            runbook="python -m monitoring.risk_watch --no-alert")]
    quote = quote or _finnhub_quote
    snap = pd.read_csv(ck.SNAP)
    if snap.empty:
        return []
    day = str(snap["date"].max())
    last = snap[(snap["date"].astype(str) == day) & (snap.get("is_carried", 0) == 0)]
    d = date.fromisoformat(day)
    off, failed, aligned = [], 0, 0
    for t, close in zip(last["ticker"], last["close"]):
        try:
            q = quote(t)
        except Exception:                                           # noqa: BLE001
            failed += 1
            continue
        if not q or not q.get("t"):
            failed += 1
            continue
        qd = datetime.fromtimestamp(q["t"], tz=timezone.utc).astimezone(
            ZoneInfo("America/New_York")).date()
        if qd == d:
            ref = q.get("c")
        elif most_recent_trading_day(qd - timedelta(days=1)) == d:
            ref = q.get("pc")
        else:
            continue                                                # not the same session
        if not ref:
            continue
        aligned += 1
        r = float(close) / float(ref) - 1
        if abs(r) > PRICE_XCHECK_MAX:
            off.append((t, close, ref, r))
    if not aligned and len(last):
        # Nothing compared reads exactly like "all agree" unless it is said: an outage,
        # or quotes from a session other than the snapshot's newest.
        why = (f"Finnhub unreachable ({failed} failed)" if failed
               else "no quote was for the same session")
        return [al.Alert(
            key="risk:price_crosscheck", severity="WARNING",
            title=f"price cross-check compared nothing: {why}",
            summary=f"the {day} closes were not checked against a second source",
            runbook="python -m monitoring.risk_watch --no-alert")]
    if not off:
        return []
    names = ", ".join(f"{t} {r:+.1%}" for t, _c, _ref, r in off)
    return [al.Alert(
        key="risk:price_crosscheck", severity="WARNING",
        title=f"{len(off)} close(s) on {day} differ from a second source by more than "
              f"{PRICE_XCHECK_MAX:.0%}: {names}",
        summary="; ".join(f"{t} snapshot {c:.2f} vs Finnhub {ref:.2f}"
                          for t, c, ref, _r in off),
        detail="Yahoo (the snapshot) and Finnhub disagree on that session's close: a "
               "bad print or an unrecorded corporate action. Every return, risk alert "
               "and the evaluation use the snapshot. Check the name; a re-run of the "
               "snapshot picks up a corrected print.",
        runbook=f"python forward_test/snapshot.py --period {ck.PERIOD}")]


def _local(ts: str) -> datetime:
    t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return t.astimezone().replace(tzinfo=None) if t.tzinfo else t


def _health_task_ran(now: datetime | None = None, info: dict | None = None) -> list[al.Alert]:
    """The dead-man's switch for the Monday health check, run from the NIGHTLY job.

    The health check sends the weekly email, so if it stops running nothing says so:
    no email reads like "all clear". It cannot see its own absence; this daily watch
    can. Reads Task Scheduler's last run, not health_history.json (manual runs append
    to that and would hide a dead task). Silent while the task runs on time."""
    if info is None and os.name != "nt":
        return []                                      # no Task Scheduler (CI, Linux)
    now = now or datetime.now()
    info = ck._task_info(ck.HEALTH_TASK_NAME) if info is None else info
    rb = "python -m monitoring.health_check -v --weekly-report"
    if not info:
        return [al.Alert(
            key="risk:health_task_stale", severity="WARNING",
            title=f"{ck.HEALTH_TASK_NAME} is not registered (or Task Scheduler could not "
                  f"be asked)",
            summary="the Monday health check and its weekly email are not scheduled",
            detail="Re-register: powershell -ExecutionPolicy Bypass -File "
                   "monitoring\\register_tasks.ps1 -Elevate",
            runbook=rb)]
    never = info.get("last_result") == ck.TASK_NEVER_RUN or not info.get("last_run")
    if never:
        nxt = info.get("next_run")
        if nxt and (_local(nxt) - now).days <= HEALTH_RUN_MAX_DAYS:
            return []                                  # newly registered, due soon
        return [al.Alert(
            key="risk:health_task_stale", severity="WARNING",
            title=f"{ck.HEALTH_TASK_NAME} has never run and is not due within "
                  f"{HEALTH_RUN_MAX_DAYS} days",
            summary=f"next run {nxt or 'none'}", runbook=rb)]
    age = (now - _local(info["last_run"])).total_seconds() / 86400
    if age <= HEALTH_RUN_MAX_DAYS:
        return []
    return [al.Alert(
        key="risk:health_task_stale", severity="WARNING",
        title=f"the Monday health check has not run for {age:.0f} days",
        summary=f"{ck.HEALTH_TASK_NAME} last ran {info['last_run'][:16]}; next "
                f"{str(info.get('next_run'))[:16]}",
        detail="It runs the checks, the backup and the weekly email, so a missing "
               "email is not an all-clear. Usually the PC was off or asleep through "
               "Monday; run it now.",
        runbook=rb)]


def evaluate(period_dir: Path | None = None, account: str | None = None) -> list[al.Alert]:
    period_dir = period_dir or ck.SNAP.parent
    alerts = []
    for part in (lambda: _book_drawdown(period_dir), lambda: _account_drawdown(account),
                 lambda: _names(period_dir), lambda: _sectors(period_dir),
                 lambda: _health_task_ran(), lambda: _price_crosscheck(period_dir)):
        try:
            alerts += part()
        except Exception as e:                                      # noqa: BLE001
            # A watch that crashes silently is the failure this repo keeps fixing.
            alerts.append(al.Alert(
                key="risk:watch_error", severity="WARNING",
                title="risk watch could not evaluate a check",
                summary=f"{type(e).__name__}: {e}",
                runbook="python -m monitoring.risk_watch --no-alert"))
    return alerts


def _account_from_recon() -> str | None:
    from monitoring import weekly_report as wr
    got = wr._latest_recon()
    return got[1].get("account") if got else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Early-warning risk alerts.")
    ap.add_argument("--no-alert", action="store_true", help="print only; send nothing")
    args = ap.parse_args(argv)
    alerts = evaluate(account=_account_from_recon())
    print(f"risk watch {datetime.now():%Y-%m-%d %H:%M}: "
          f"{len(alerts)} warning(s)" + ("" if alerts else " -- all clear"))
    for a in alerts:
        print(f"  [{a.severity}] {a.title}")
    if not args.no_alert:
        info = al.dispatch(alerts, resolve_scope="risk:")
        print(f"  alerts: {len(info['sent'])} sent, {len(info['suppressed'])} "
              f"suppressed, {len(info['resolved'])} resolved; {info['email_message']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
