"""
tests/test_market_calendar.py -- the NYSE holiday calendar matches the NYSE rules.

A holiday missing from forward_test/market_calendar.py counts as a session nobody
recorded: the snapshot looks two sessions behind, the health check FAILs and the 5.3
kill-switch HALTS. A wrong date does the same a day early. So every year in the
calendar is recomputed here from the rules, independently of the hand-typed list.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from forward_test import market_calendar as cal  # noqa: E402


def _nth_weekday(y, m, wd, n):
    d = date(y, m, 1)
    return d + timedelta((wd - d.weekday()) % 7) + timedelta(weeks=n - 1)


def _last_weekday(y, m, wd):
    d = date(y, m + 1, 1) - timedelta(days=1)
    return d - timedelta((d.weekday() - wd) % 7)


def _easter(y):
    """Anonymous Gregorian algorithm."""
    a, b, c = y % 19, y // 100, y % 100
    d, e = divmod(b, 4)
    g = (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    n = h + ll - 7 * m + 114
    return date(y, n // 31, n % 31 + 1)


def _observed(d, new_year=False):
    if d.weekday() == 5:      # Saturday -> Friday; not for New Year (year-end session)
        return None if new_year else d - timedelta(days=1)
    if d.weekday() == 6:      # Sunday -> Monday
        return d + timedelta(days=1)
    return d


def nyse_holidays(y: int) -> set[date]:
    hs = {_observed(date(y, 1, 1), new_year=True), _nth_weekday(y, 1, 0, 3),
          _nth_weekday(y, 2, 0, 3), _easter(y) - timedelta(days=2),
          _last_weekday(y, 5, 0), _observed(date(y, 6, 19)), _observed(date(y, 7, 4)),
          _nth_weekday(y, 9, 0, 1), _nth_weekday(y, 11, 3, 4),
          _observed(date(y, 12, 25))}
    return {h for h in hs if h}


def test_rules_reproduce_known_dates():
    """The rule engine itself, on dates checked by hand."""
    assert _easter(2026) == date(2026, 4, 5) and _easter(2027) == date(2027, 3, 28)
    assert date(2027, 6, 18) in nyse_holidays(2027)       # Juneteenth, Sat -> Fri
    assert date(2027, 7, 5) in nyse_holidays(2027)        # July 4, Sun -> Mon
    assert not any(d.month == 1 and d.day < 10 for d in nyse_holidays(2028))  # Sat NY


def test_every_calendar_year_matches_the_rules():
    years = sorted({d.year for d in cal.US_MARKET_HOLIDAYS})
    assert years == list(range(years[0], cal.CALENDAR_LAST_YEAR + 1)), years
    for y in years:
        have = {d for d in cal.US_MARKET_HOLIDAYS if d.year == y} - cal.SPECIAL_CLOSURES
        want = nyse_holidays(y)
        assert have == want, (y, sorted(want - have), sorted(have - want))


def test_no_holiday_falls_on_a_weekend():
    assert all(d.weekday() < 5 for d in cal.US_MARKET_HOLIDAYS)
