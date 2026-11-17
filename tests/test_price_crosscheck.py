"""
tests/test_price_crosscheck.py -- the snapshot's newest closes against a second source.

Splits are caught by baselines_match; a bad print was caught by nothing. The nightly
risk watch now compares the newest session's closes (Yahoo) with Finnhub's close for
the same session and alerts above 2%. Measured 2026-11-17: 26 of 27 closes identical,
SENEA 0.15% apart (split rounding). "Compared nothing" must alert, never read as fine.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from monitoring import risk_watch as rw  # noqa: E402

ET = ZoneInfo("America/New_York")


def _ts(y, m, d, h, mi):
    return int(datetime(y, m, d, h, mi, tzinfo=ET).timestamp())


@pytest.fixture
def snap(tmp_path, monkeypatch):
    p = tmp_path / "price_snapshots.csv"
    pd.DataFrame([("2026-11-16", "AAA", 10.0, 0), ("2026-11-16", "BBB", 20.0, 0),
                  ("2026-11-16", "DEAD", 5.0, 1)],
                 columns=["date", "ticker", "close", "is_carried"]).to_csv(p, index=False)
    monkeypatch.setattr(rw.ck, "SNAP", p)
    return tmp_path


def _quotes(table):
    def q(t):
        if t == "DEAD":
            raise AssertionError("a carried-forward (delisted) cell must not be checked")
        return table[t]
    return q


# Tuesday 2026-11-17 during the session: last trade today, so pc is Monday's close
DURING = {"AAA": {"c": 10.5, "pc": 10.0, "t": _ts(2026, 11, 17, 12, 0)},
          "BBB": {"c": 21.0, "pc": 20.0, "t": _ts(2026, 11, 17, 12, 0)}}


def test_matching_closes_raise_nothing(snap):
    assert rw._price_crosscheck(snap, quote=_quotes(DURING), has_key=True) == []


def test_a_bad_print_is_flagged(snap):
    bad = {**DURING, "BBB": {"c": 21.0, "pc": 22.0, "t": _ts(2026, 11, 17, 12, 0)}}
    (a,) = rw._price_crosscheck(snap, quote=_quotes(bad), has_key=True)
    assert a.key == "risk:price_crosscheck" and "BBB" in a.title and "AAA" not in a.title


def test_after_the_close_the_same_day_quote_is_compared_with_c(snap):
    """Snapshot through Monday, quote's last trade Monday: compare Monday's c."""
    mon = {"AAA": {"c": 10.0, "pc": 9.0, "t": _ts(2026, 11, 16, 15, 59)},
           "BBB": {"c": 20.1, "pc": 18.0, "t": _ts(2026, 11, 16, 15, 59)}}
    assert rw._price_crosscheck(snap, quote=_quotes(mon), has_key=True) == []


def test_quotes_from_another_session_compare_nothing_and_say_so(snap):
    old = {t: {"c": 1.0, "pc": 1.0, "t": _ts(2026, 11, 10, 15, 0)} for t in ("AAA", "BBB")}
    (a,) = rw._price_crosscheck(snap, quote=_quotes(old), has_key=True)
    assert "compared nothing" in a.title and "same session" in a.title


def test_finnhub_unreachable_says_so(snap):
    def down(t):
        if t == "DEAD":
            raise AssertionError
        raise OSError("network down")
    (a,) = rw._price_crosscheck(snap, quote=down, has_key=True)
    assert "unreachable" in a.title


def test_no_key_says_so(snap):
    (a,) = rw._price_crosscheck(snap, quote=_quotes(DURING), has_key=False)
    assert "FINNHUB_API_KEY" in a.title


def test_watch_includes_the_crosscheck():
    import inspect
    assert "_price_crosscheck" in inspect.getsource(rw.evaluate)
