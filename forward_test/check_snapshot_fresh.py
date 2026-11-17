"""
forward_test/check_snapshot_fresh.py — staleness self-check for the daily snapshot.

Weekend- and US-market-holiday-aware, so it does NOT false-alarm on non-trading
days. Warns (exit 1) only if price_snapshots.csv has missed MORE THAN ONE trading
session (a 1-session lag is within grace, since today's 23:30 run may not have fired
yet at check time).

CHECKS THE SAME FILE THE KILL-SWITCH CHECKS. This script used to hardcode
2026Q3_parallel while broker/ibkr_paper.py's gate read the ACTIVE period's snapshot
(2026Q4b). The runbook told you to run this before trading, so a green check here
could sit next to a 5.3 STALE_DATA halt there, on a different file. The period and
path now come from forward_test/snapshot.py's registry — one source of truth, and
--period / FT_PERIOD select the same way they do for the snapshot itself.

Usage:  python forward_test/check_snapshot_fresh.py [--period 2026Q4b]
"""
from __future__ import annotations
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

# Run directly (python forward_test/check_snapshot_fresh.py) and sys.path[0] is
# forward_test/, not the repo root, so the package import below needs the root.
sys.path.insert(0, str(Path(__file__).parent.parent))

# Registry, active period and snapshot path all come from the snapshot module, so
# this check can never drift onto a different file than the one being recorded.
# Importing it is side-effect-free: it resolves paths and reads no data.
from forward_test.snapshot import PERIOD, SNAP, START  # noqa: E402

# The NYSE calendar and the trading-day helpers live in forward_test/market_calendar.py,
# shared with monitoring/checks.py, the dashboard and the broker's 5.3 STALE_DATA switch.
# Re-exported here because every caller imports these names from this module. A holiday
# missing from the calendar counts as a missed session and cries STALE on a snapshot that
# was working perfectly, so the calendar runs ahead of the tests (2027 is in).
from forward_test.market_calendar import (  # noqa: E402,F401
    US_MARKET_HOLIDAYS, is_trading_day, most_recent_trading_day,
)


def main() -> int:
    today = date.today()
    mrtd = most_recent_trading_day(today)
    start = date.fromisoformat(START)
    print(f"period {PERIOD}  (start {START})  ->  {SNAP.name}")

    # "Not started" is not "broken". A test whose start date has no completed
    # session yet has nothing to record, and reporting that as STALE would train
    # you to ignore a real staleness alarm. The kill-switch still HALTS on it --
    # that is the gate's job and it is not weakened here.
    if start > mrtd:
        print(f"NOT STARTED: period begins {START}; most-recent completed trading "
              f"day is {mrtd}. Nothing to record yet — not a fault.")
        print("  NOTE: the 5.3 STALE_DATA kill-switch will still HALT the order "
              "path until a session is recorded. That is correct.")
        return 0

    if not SNAP.exists():
        if start == mrtd:
            print(f"NOT STARTED: period begins today ({START}); the snapshot runs "
                  f"after the US close (23:30 Finland). Not a fault.")
            print("  NOTE: the 5.3 STALE_DATA kill-switch will HALT the order path "
                  "until that run completes. That is correct.")
            return 0
        print(f"STALE: {SNAP.name} does not exist — the snapshot has never run for "
              f"{PERIOD}, though {START} has passed. Check Task Scheduler + "
              f"snapshot_log.txt.")
        return 1

    s = pd.read_csv(SNAP)
    if s.empty:
        print(f"STALE: {SNAP.name} exists but has 0 rows — a crashed run, not a "
              f"recorded session. Check snapshot_log.txt.")
        return 1
    latest = pd.to_datetime(s["date"]).dt.date.max()

    missing = 0
    d = mrtd
    while d > latest:
        if is_trading_day(d):
            missing += 1
        d -= timedelta(days=1)

    if missing >= 2:
        print(f"STALE: latest recorded {latest}; most-recent trading day {mrtd}; "
              f"missed {missing} trading sessions. Check Task Scheduler + "
              f"snapshot_log.txt.")
        return 1
    print(f"FRESH: latest recorded {latest} (most-recent trading day {mrtd}; "
          f"{missing} session behind — within 1-session grace).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
