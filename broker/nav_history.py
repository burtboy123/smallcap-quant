"""
broker/nav_history.py — the account's NAV over time, so the drawdown kill-switch has a
real high-water mark.

Until 2026-10-05 main() built the gate's PortfolioState with high_water_mark = the
current NAV ("flat account sitting at its HWM"). Right for an empty account, but once
the account was invested it meant drawdown was 0% on every run, so the 5.1
MAX_DRAWDOWN halt (-15%) could never fire.

Every broker connection now appends the account's NetLiquidation, in its BASE currency,
to monitoring/state/nav_history.jsonl (machine-local, included in the weekly backup).
The high-water mark is the highest NAV ever recorded for that account. Drawdown is
measured in the base currency (EUR for DU1234567): a EURUSD move must neither fake a
drawdown nor hide one.

PAPER ONLY. Read/append only; nothing here talks to IB.

    python -m broker.nav_history            # list rows a test run wrote by mistake
    python -m broker.nav_history --apply    # remove them (a backup is kept)
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAV_LOG = ROOT / "monitoring" / "state" / "nav_history.jsonl"


def record(account: str, nav: float, currency: str, *, when: datetime | None = None,
           path: Path | None = None) -> None:
    """Append one NAV observation. Ignores non-positive NAVs (a failed read)."""
    if not nav or nav <= 0:
        return
    p = path or NAV_LOG
    p.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": (when or datetime.now()).isoformat(timespec="seconds"),
           "account": account, "nav": float(nav), "currency": currency}
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def history(account: str, *, path: Path | None = None) -> list[dict]:
    p = path or NAV_LOG
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue                                    # a torn line never blocks a run
        if r.get("account") == account:
            out.append(r)
    return out


def high_water_mark(account: str, current: float, *, path: Path | None = None) -> dict:
    """{'hwm', 'hwm_ts', 'drawdown'} with the current NAV included in the max."""
    best, ts = current, "now"
    for r in history(account, path=path):
        if r["nav"] > best:
            best, ts = r["nav"], r["ts"]
    dd = (current / best - 1.0) if best else 0.0
    return {"hwm": best, "hwm_ts": ts, "drawdown": dd}


# Until 2026-10-11 a test ran the broker against a fake account that reported exactly
# 1,000,000 USD, and the row landed in the real log (tests/conftest.py now prevents it).
# The real paper account reports its BASE currency (EUR for DU1234567).
TEST_NAV, TEST_CCY = 1_000_000.0, "USD"


def test_rows(path: Path | None = None) -> list[int]:
    """Line numbers (0-based) of rows a test run wrote: exactly 1,000,000 USD."""
    p = path or NAV_LOG
    if not p.exists():
        return []
    out = []
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines()):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if float(r.get("nav", 0)) == TEST_NAV and r.get("currency") == TEST_CCY:
            out.append(i)
    return out


def clean(path: Path | None = None, apply: bool = False) -> dict:
    """Remove test-written rows. Dry run unless apply; always backs up first."""
    p = path or NAV_LOG
    bad = set(test_rows(p))
    res = {"path": str(p), "found": len(bad), "removed": 0, "backup": None}
    if not bad or not apply:
        return res
    backup = p.with_name(p.name + f".bak-{datetime.now():%Y%m%d%H%M%S}")
    shutil.copy2(p, backup)
    keep = [ln for i, ln in enumerate(p.read_text(encoding="utf-8").splitlines())
            if i not in bad]
    p.write_text("".join(ln + "\n" for ln in keep), encoding="utf-8")
    return {**res, "removed": len(bad), "backup": str(backup)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Find/remove NAV rows written by tests.")
    ap.add_argument("--apply", action="store_true", help="remove them (backup kept)")
    args = ap.parse_args(argv)
    r = clean(apply=args.apply)
    if not r["found"]:
        print(f"clean: no test-written rows in {r['path']}")
    elif not args.apply:
        print(f"found {r['found']} test-written row(s) (1,000,000 USD) in {r['path']}. "
              f"Run again with --apply to remove them (a backup is kept).")
    else:
        print(f"removed {r['removed']} row(s); backup: {r['backup']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
