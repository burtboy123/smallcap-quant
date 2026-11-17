"""
tests/test_sec_xbrl.py — data/sec_xbrl.py (SEC XBRL fundamentals, research store).

Offline: a fake HTTP session and a synthetic companyfacts document with known
answers. Checks SEC fair-access behaviour (User-Agent required, cache, backoff, rate
limit), the TTM arithmetic, as-first-reported point-in-time values, and that the
fetch is resumable without duplicate rows.
"""

from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data import sec_xbrl as sx  # noqa: E402

UA = "Test Person test@example.com"


def _f(start, end, val, filed, accn, form="10-Q", fy=2021, fp="Q1"):
    d = {"end": end, "val": val, "filed": filed, "accn": accn, "form": form,
         "fy": fy, "fp": fp}
    if start:
        d["start"] = start
    return d


# Calendar fiscal year. Known answers:
#   TTM NI @2021-06-30 = FY2020 100 + H1'21 70 - H1'20 45 = 125
#   TTM NI @2021-03-31 = 100 + 30 - 20 = 110
#   quarters to 2021-06-30 = [Q2'21 40, Q1'21 30, Q4'20 100-70=30, Q3'20 70-45=25]
DOC = {"facts": {
    "us-gaap": {
        "NetIncomeLoss": {"units": {"USD": [
            _f("2020-01-01", "2020-03-31", 20, "2020-05-01", "q120"),
            _f("2020-01-01", "2020-06-30", 45, "2020-08-01", "q220"),
            _f("2020-01-01", "2020-09-30", 70, "2020-11-01", "q320"),
            _f("2020-01-01", "2020-12-31", 100, "2021-02-15", "k20", form="10-K"),
            _f("2021-01-01", "2021-03-31", 30, "2021-05-01", "q121"),
            _f("2021-04-01", "2021-06-30", 40, "2021-08-01", "q221"),
            _f("2021-01-01", "2021-06-30", 70, "2021-08-01", "q221"),
            _f("2021-01-01", "2021-03-31", 999, "2021-05-02", "x8k", form="8-K"),
        ]}},
        "ProfitLoss": {"units": {"USD": [                       # must NOT be mixed in
            _f("2021-01-01", "2021-06-30", -5, "2021-08-01", "q221")]}},
        "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [
            _f("2020-01-01", "2020-12-31", 90, "2021-02-15", "k20", form="10-K"),
            _f("2021-01-01", "2021-06-30", 50, "2021-08-01", "q221"),
            _f("2020-01-01", "2020-06-30", 40, "2020-08-01", "q220")]}},
        "Assets": {"units": {"USD": [
            _f(None, "2021-03-31", 1000, "2021-05-01", "q121"),
            _f(None, "2021-03-31", 1111, "2021-08-01", "q221"),   # later restatement
            _f(None, "2021-06-30", 1200, "2021-08-01", "q221")]}},
        "Liabilities": {"units": {"USD": [_f(None, "2021-06-30", 700, "2021-08-01", "q221")]}},
        "StockholdersEquity": {"units": {"USD": [_f(None, "2021-06-30", 500, "2021-08-01", "q221")]}},
        "CashAndCashEquivalentsAtCarryingValue": {"units": {"USD": [
            _f(None, "2021-06-30", 80, "2021-08-01", "q221")]}},
    },
    "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
        _f(None, "2021-04-25", 990, "2021-05-01", "q121"),
        _f(None, "2021-07-25", 1000, "2021-08-01", "q221")]}}},
}}


def test_parse_uses_one_tag_per_concept_and_only_periodic_forms():
    f = sx.parse_facts(DOC, "AAA", 1)
    ni = f[f["concept"] == "net_income"]
    assert set(ni["tag"]) == {"NetIncomeLoss"}                    # ProfitLoss not mixed in
    assert 999 not in set(ni["val"])                              # 8-K value excluded


def test_ttm_direct_annual_and_ytd_arithmetic():
    ni = sx._first_reported(sx.parse_facts(DOC, "AAA", 1).query("concept == 'net_income'"))
    assert sx._ttm(ni, "2020-12-31")[0] == 100
    assert sx._ttm(ni, "2021-06-30") == (125, "2021-08-01")
    assert sx._ttm(ni, "2021-03-31")[0] == 110


def test_quarter_recovers_q4_from_annual_minus_nine_months():
    ni = sx._first_reported(sx.parse_facts(DOC, "AAA", 1).query("concept == 'net_income'"))
    assert sx._quarter(ni, "2020-12-31") == 30
    assert sx._quarter(ni, "2021-06-30") == 40


def test_panel_is_as_first_reported_and_dated_by_the_latest_filing():
    p = sx.build_panel(sx.parse_facts(DOC, "AAA", 1)).set_index("period_end_date")
    q1 = p.loc[pd.Timestamp("2021-03-31")]
    assert q1["total_assets"] == 1000                             # not the 1111 restatement
    assert q1["shares"] == 990                                    # its own cover page
    assert q1["available_date"] == pd.Timestamp("2021-05-01")     # not pushed to 08-01
    q2 = p.loc[pd.Timestamp("2021-06-30")]
    assert q2["ni_ttm"] == 125 and q2["ocf_ttm"] == 90 + 50 - 40
    assert q2["total_liabilities"] == 700 and q2["shares"] == 1000  # same-filing count
    assert q2["ni_q"] == [40, 30, 30, 25]
    assert q2["available_date"] == pd.Timestamp("2021-08-01")


# ── client: SEC fair access ─────────────────────────────────────────────────────

class _Resp:
    def __init__(self, code, data=None):
        self.status_code, self._data = code, data

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Session:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers))
        return self.responses.pop(0)


def test_client_requires_a_real_user_agent(tmp_path, monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(ValueError, match="SEC_USER_AGENT"):
        sx.SecClient(raw_dir=tmp_path, session=_Session([]))
    with pytest.raises(ValueError):
        sx.SecClient("quant_system", raw_dir=tmp_path, session=_Session([]))


def test_client_caches_so_nothing_is_downloaded_twice(tmp_path):
    s = _Session([_Resp(200, {"x": 1})])
    c = sx.SecClient(UA, raw_dir=tmp_path, session=s)
    assert c.get_json("https://data.sec.gov/a.json") == {"x": 1}
    assert c.get_json("https://data.sec.gov/a.json") == {"x": 1}
    assert len(s.calls) == 1 and s.calls[0][1]["User-Agent"] == UA


def test_client_backs_off_on_429_and_returns_none_on_404(tmp_path, monkeypatch):
    slept = []
    monkeypatch.setattr(sx.time, "sleep", lambda x: slept.append(x))
    s = _Session([_Resp(429), _Resp(200, {"ok": True}), _Resp(404)])
    c = sx.SecClient(UA, raw_dir=tmp_path, session=s)
    assert c.get_json("https://data.sec.gov/b.json") == {"ok": True}
    assert 1 in slept                                            # exponential backoff
    assert c.get_json("https://data.sec.gov/missing.json") is None


def test_client_never_exceeds_the_rate_limit(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(sx.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(sx.time, "sleep", lambda x: clock.__setitem__(0, clock[0] + x))
    s = _Session([_Resp(200, {"i": i}) for i in range(20)])
    c = sx.SecClient(UA, raw_dir=tmp_path, session=s, max_rps=8)
    for i in range(20):
        c.get_json(f"https://data.sec.gov/{i}.json")
    assert clock[0] >= 19 / 8 - 1e-9                             # 20 requests >= 2.375 s


# ── resumable fetch ─────────────────────────────────────────────────────────────

class _FakeClient:
    def __init__(self):
        self.requests_made = 0

    def get_json(self, url, max_age_days=None):
        self.requests_made += 1
        if url == sx.TICKERS_URL:
            return {"0": {"ticker": "AAA", "cik_str": 1},
                    "1": {"ticker": "BRK-B", "cik_str": 2},
                    "2": {"ticker": "BAD", "cik_str": 3}}
        if url.endswith("CIK0000000001.json"):
            return DOC
        if url.endswith("CIK0000000002.json"):
            return None
        raise RuntimeError("connection reset")


def test_fetch_logs_every_outcome_and_is_resumable(tmp_path):
    db = tmp_path / "sec.db"
    counts = sx.fetch_universe(["AAA", "BRK.B", "BAD", "ZZZZ"], _FakeClient(), db=db)
    assert counts == {"ok": 1, "no_facts": 1, "error": 1, "no_cik": 1}
    with closing(sqlite3.connect(db)) as c:
        n1 = c.execute("SELECT COUNT(*) FROM sec_facts").fetchone()[0]
        log = dict(c.execute("SELECT ticker, status FROM sec_fetch_log").fetchall())
        detail = c.execute("SELECT detail FROM sec_fetch_log WHERE ticker='BAD'").fetchone()[0]
    assert log == {"AAA": "ok", "BRK.B": "no_facts", "BAD": "error", "ZZZZ": "no_cik"}
    assert "connection reset" in detail                          # the reason is recorded
    again = sx.fetch_universe(["AAA", "BRK.B", "BAD", "ZZZZ"], _FakeClient(), db=db)
    assert "ok" not in again                                     # AAA is not redone
    sx.fetch_universe(["AAA"], _FakeClient(), db=db, refetch=True)
    with closing(sqlite3.connect(db)) as c:
        assert c.execute("SELECT COUNT(*) FROM sec_facts").fetchone()[0] == n1  # no dupes


def test_status_without_a_database(tmp_path):
    assert "no SEC database yet" in sx.status(tmp_path / "none.db")


def test_cli_refuses_without_user_agent(monkeypatch, capsys):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    assert sx.main(["--tickers", "AAA"]) == 1
    assert "SEC_USER_AGENT" in capsys.readouterr().out


def test_a_later_share_count_never_delays_an_earlier_period():
    doc = {"facts": {k: dict(v) for k, v in DOC["facts"].items()}}
    doc["facts"]["dei"] = {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
        _f(None, "2021-07-25", 1000, "2021-08-01", "q221")]}}}       # Q1 cover count missing
    p = sx.build_panel(sx.parse_facts(doc, "AAA", 1)).set_index("period_end_date")
    q1 = p.loc[pd.Timestamp("2021-03-31")]
    assert q1["available_date"] == pd.Timestamp("2021-05-01") and pd.isna(q1["shares"])


def test_a_period_is_available_only_when_its_last_input_is_filed():
    """Q2's cash flow first appears in a 10-Q/A on 09-15: the TTM accruals for Q2 need
    it, so the period is usable from 09-15, not from the 08-01 10-Q."""
    doc = {"facts": {k: {t: {"units": {u: list(v) for u, v in d["units"].items()}}
                         for t, d in tags.items()}
                     for k, tags in DOC["facts"].items()}}
    ocf = doc["facts"]["us-gaap"]["NetCashProvidedByUsedInOperatingActivities"]["units"]["USD"]
    ocf[:] = [x for x in ocf if x["end"] != "2021-06-30"] + [
        _f("2021-01-01", "2021-06-30", 50, "2021-09-15", "q221a", form="10-Q/A")]
    p = sx.build_panel(sx.parse_facts(doc, "AAA", 1)).set_index("period_end_date")
    assert p.loc[pd.Timestamp("2021-06-30"), "available_date"] == pd.Timestamp("2021-09-15")


def test_the_panel_is_built_once_per_database_version(tmp_path, monkeypatch, capsys):
    """Building takes ~1 s per company; two studies in a row rebuilt it (an hour). The
    saved build is reused until the SEC database changes, and a build shows progress."""
    db = tmp_path / "sec.db"
    with closing(sx.connect(db)) as c:
        sx.store(c, "AAA", 1, sx.parse_facts(DOC, "AAA", 1), "ok")
        c.commit()
    calls = []
    real = sx.build_panel
    monkeypatch.setattr(sx, "build_panel", lambda facts, **k: calls.append(1) or real(facts, **k))
    first = sx.cached_panel(db)
    assert len(calls) == 1 and len(first) > 0
    assert "building for 1 companies" in capsys.readouterr().out
    again = sx.cached_panel(db)
    assert len(calls) == 1                                  # reused, not rebuilt
    pd.testing.assert_frame_equal(first, again)
    assert "reusing the saved build" in capsys.readouterr().out
    with closing(sx.connect(db)) as c:                      # a fetch writes the database
        sx.store(c, "BBB", 2, sx.parse_facts(DOC, "BBB", 2), "ok")
        c.commit()
    third = sx.cached_panel(db)
    assert len(calls) == 2 and set(third["ticker"]) == {"AAA", "BBB"}


def test_build_progress_counts_companies(capsys):
    facts = pd.concat([sx.parse_facts(DOC, t, i) for i, t in enumerate(["A", "B", "C"])])
    sx.build_panel(facts, progress_every=2)
    out = capsys.readouterr().out
    assert "2/3 companies" in out and "3/3 companies" in out
