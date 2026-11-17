"""
forward_test/market_calendar.py — the ONE NYSE holiday calendar and trading-day helpers.

Two copies of this calendar used to exist: check_snapshot_fresh.py (used by the health
check and the dashboard) and broker/killswitch.py (the 5.3 STALE_DATA switch). Two
calendars drift: a holiday present in one and missing from the other makes the health
check cry STALE on the Monday after it while the kill-switch waves the same day through,
or the reverse. Everything that asks "was that a trading day" imports from here.

A holiday MISSING from this set is counted as a trading session that nobody recorded. On
the next Monday the snapshot looks two sessions behind, the health check FAILs and the
5.3 switch HALTS a rebalance that day. So the calendar must run ahead of the tests that
use it: 2026Q4b evaluates 2026-12-29 and the hedged paper test runs to 2027-09-30.
tests/test_monitoring.py fails when the calendar ends before the year the hedged test
evaluates in, or before the current year.

Full-day closures only. Early closes (13:00 ET: the day after Thanksgiving, Christmas
Eve) are trading days here; snapshot.py records a bar only after the regular 16:00 ET
close, so on those days the bar is picked up by the next run.
"""
from __future__ import annotations

from datetime import date, timedelta

US_MARKET_HOLIDAYS = {
    # 2026
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # MLK Day
    date(2026, 2, 16),   # Washington's Birthday
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day (observed; Jul 4 = Sat)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
    # 2027
    date(2027, 1, 1),    # New Year's Day
    date(2027, 1, 18),   # MLK Day
    date(2027, 2, 15),   # Washington's Birthday
    date(2027, 3, 26),   # Good Friday
    date(2027, 5, 31),   # Memorial Day
    date(2027, 6, 18),   # Juneteenth (observed; Jun 19 = Sat)
    date(2027, 7, 5),    # Independence Day (observed; Jul 4 = Sun)
    date(2027, 9, 6),    # Labor Day
    date(2027, 11, 25),  # Thanksgiving
    date(2027, 12, 24),  # Christmas (observed; Dec 25 = Sat)
    # 2028. Jan 1 is a Saturday: the NYSE does not close the preceding Friday when it is
    # the last trading day of the year, so 2027-12-31 is a session and there is no
    # New Year holiday in 2028.
    date(2028, 1, 17),   # MLK Day
    date(2028, 2, 21),   # Washington's Birthday
    date(2028, 4, 14),   # Good Friday
    date(2028, 5, 29),   # Memorial Day
    date(2028, 6, 19),   # Juneteenth
    date(2028, 7, 4),    # Independence Day
    date(2028, 9, 4),    # Labor Day
    date(2028, 11, 23),  # Thanksgiving
    date(2028, 12, 25),  # Christmas
}
# Every year is checked against the NYSE rules by tests/test_market_calendar.py. A
# one-off closure (e.g. a national day of mourning) goes in SPECIAL_CLOSURES too.
SPECIAL_CLOSURES: set[date] = set()
US_MARKET_HOLIDAYS |= SPECIAL_CLOSURES
CALENDAR_LAST_YEAR = 2028


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in US_MARKET_HOLIDAYS


def most_recent_trading_day(d: date) -> date:
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def sessions_behind(latest_recorded: date, today: date | None = None) -> int:
    """Trading sessions strictly after `latest_recorded`, up to and including the most
    recent trading day on or before `today` (weekend- and holiday-aware). The 5.3
    kill-switch halts when this exceeds its 1-session grace; the health check and
    check_snapshot_fresh.py read the same number."""
    today = today or date.today()
    missing, d = 0, most_recent_trading_day(today)
    while d > latest_recorded:
        if is_trading_day(d):
            missing += 1
        d -= timedelta(days=1)
    return missing
