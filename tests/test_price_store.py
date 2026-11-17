"""
tests/test_price_store.py — research/price_store.py: real past prices for research.

The point of the store is the split arithmetic: a past price must be the price that
day, not one scaled by a split that happened later. Temp files and fake downloads only.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from research import price_store as ps  # noqa: E402

ISHARES = """iShares Russell 3000 ETF
Fund Holdings as of,"Oct 10, 2026"
Inception Date,"May 22, 2000"
Shares Outstanding,"40,000,000.00"
\xa0
Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Price
"AAPL","APPLE INC","Information Technology","Equity","1,000","6.1","230.0"
"BRK.B","BERKSHIRE HATHAWAY INC CLASS B","Financials","Equity","500","1.6","470.0"
"KODK","EASTMAN KODAK","Industrials","Equity","10","0.01","6.0"
"USD","USD CASH","Cash and/or Derivatives","Cash","5","0.1","1.0"
"XTSLA","BLK CSH FND TREASURY SL AGENCY","Cash and/or Derivatives","Money Market","5","0.1","1.0"
"-","MONEY MARKET","Cash and/or Derivatives","Money Market","5","0.1","1.0"
"XTOOLONG","SOMETHING","Financials","Equity","1","0.0","1.0"
\xa0
"The content contained herein is owned or licensed by BlackRock","x","y","z","1","2","3","4","5"
"""


def _csv(tmp_path, name, text=ISHARES):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_ishares_file_gives_equity_tickers_only(tmp_path):
    h = ps.parse_ishares(_csv(tmp_path, "iwv.csv"))
    assert list(h["ticker"]) == ["AAPL", "BRK.B", "KODK"]
    assert h.set_index("ticker").loc["KODK", "sector"] == "Industrials"


def _xml_row(*cells, skip_to=None):
    out = []
    for i, c in enumerate(cells):
        idx = f' ss:Index="{skip_to}"' if (skip_to and i == len(cells) - 1) else ""
        out.append(f'<ss:Cell{idx}><ss:Data ss:Type="String">{c}</ss:Data></ss:Cell>')
    return "<ss:Row>" + "".join(out) + "</ss:Row>"


SPREADSHEETML = (
    '<?xml version="1.0"?>\n'
    '<ss:Workbook xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">\n'
    '<ss:Styles><ss:Style ss:ID="Default"><ss:Alignment ss:Horizontal="Left"/>'
    '</ss:Style></ss:Styles>\n'
    '<ss:Worksheet ss:Name="Overview"><ss:Table>'
    + _xml_row("iShares Russell 3000 ETF") + _xml_row("Fund Holdings as of", "Oct 10, 2026")
    + '</ss:Table></ss:Worksheet>\n'
    '<ss:Worksheet ss:Name="Holdings"><ss:Table>'
    + _xml_row("iShares Russell 3000 ETF") + "<ss:Row></ss:Row>"
    + _xml_row("Ticker", "Name", "Sector", "Asset Class", "Market Value")
    + _xml_row("AAPL", "APPLE INC", "Information Technology", "Equity", "1,000")
    + _xml_row("BRK.B", "BERKSHIRE HATHAWAY INC CLASS B", "Financials", "Equity", "500")
    + _xml_row("SPGI", "S&P GLOBAL INC", "Financials", "Equity", "300")     # bare &
    + _xml_row("KODK", "EASTMAN KODAK", "Industrials", "Equity")              # short row
    + _xml_row("USD", "USD CASH", "Cash and/or Derivatives", "Cash", "5")
    + _xml_row("XTSLA", "BLK CSH FND TREASURY", "Cash and/or Derivatives", "Money Market")
    + _xml_row("MOV", "MOVADO", "Equity", skip_to=4)          # Sector left empty
    + '</ss:Table></ss:Worksheet>\n</ss:Workbook>\n')


def test_the_excel_xml_export_reads_like_the_csv(tmp_path):
    """iShares sometimes serves Excel 2003 XML under a .csv name (it did for IWV)."""
    h = ps.parse_ishares(_csv(tmp_path, "iwv.csv", SPREADSHEETML)).set_index("ticker")
    assert list(h.index) == ["AAPL", "BRK.B", "SPGI", "KODK", "MOV"]
    assert h.loc["SPGI", "name"] == "S&P GLOBAL INC"
    assert h.loc["KODK", "sector"] == "Industrials"
    assert h.loc["MOV", "sector"] == ""                   # the skipped cell stays empty


def test_an_unreadable_holdings_file_fails_loudly(tmp_path, monkeypatch, capsys):
    bad = _csv(tmp_path, "iwv.csv", '<?xml version="1.0"?>\n<ss:Workbook xmlns:ss="x">')
    with pytest.raises(ValueError, match="not a readable iShares holdings file"):
        ps.parse_ishares(bad)
    monkeypatch.setattr(ps, "IWV_CSV", bad)
    monkeypatch.setattr(ps, "IWM_CSV", _csv(tmp_path, "iwm.csv"))
    monkeypatch.setattr(ps, "DB_PATH", tmp_path / "p.db")
    assert ps.main(["--skip-sec"]) == 1
    assert "FAILED" in capsys.readouterr().out


def test_holdings_flags_membership(tmp_path):
    iwm = ISHARES.replace('"AAPL","APPLE INC","Information Technology","Equity","1,000",'
                          '"6.1","230.0"\n', "").replace("BRK.B", "MOV")
    h = ps.holdings(_csv(tmp_path, "iwv.csv"), _csv(tmp_path, "iwm.csv", iwm)) \
        .set_index("ticker")
    assert h.loc["AAPL", "in_iwv"] == 1 and h.loc["AAPL", "in_iwm"] == 0
    assert h.loc["KODK", "in_iwv"] == 1 and h.loc["KODK", "in_iwm"] == 1
    assert h.loc["MOV", "in_iwv"] == 0 and h.loc["MOV", "in_iwm"] == 1


def _download(prices: dict, splits: dict | None = None, ticker_first=False):
    """Fake yf.download output: MultiIndex (Price, Ticker) like yfinance."""
    days = pd.bdate_range("2020-01-01", periods=len(next(iter(prices.values()))))
    frames = {}
    for t, closes in prices.items():
        sp = pd.Series(0.0, index=days)
        for d, r in (splits or {}).get(t, {}).items():
            sp[pd.Timestamp(d)] = r
        frames[t] = pd.DataFrame({"Adj Close": np.array(closes) * 0.9, "Close": closes,
                                  "Dividends": 0.0, "Stock Splits": sp,
                                  "Volume": 1000.0}, index=days)
    raw = pd.concat(frames, axis=1)                         # (Ticker, Price)
    return raw if ticker_first else raw.swaplevel(axis=1).sort_index(axis=1)


@pytest.mark.parametrize("ticker_first", [False, True])
def test_download_is_parsed_in_either_column_order(ticker_first):
    raw = _download({"BRK-B": [10.0, 11.0, 12.0], "KODK": [5.0, 5.0, 5.0]},
                    {"KODK": {"2020-01-02": 2.0}}, ticker_first)
    px, sp = ps.parse_download(raw, ["BRK.B", "KODK"])
    assert set(px["ticker"]) == {"BRK.B", "KODK"}             # back to our ticker
    assert px[px["ticker"] == "BRK.B"]["adj_close"].tolist() == pytest.approx([9, 9.9, 10.8])
    assert sp.to_dict("records") == [{"ticker": "KODK", "ratio": 2.0, "date": "2020-01-02"}]


def test_split_factor_scales_only_dates_before_the_split():
    days = pd.bdate_range("2020-01-01", periods=4)
    f = ps.split_factor(days, pd.DataFrame({"date": ["2020-01-03"], "ratio": [2.0]}))
    assert f.tolist() == [2.0, 2.0, 1.0, 1.0]                # 2-for-1 on day 3
    r = ps.split_factor(days, pd.DataFrame({"date": ["2020-01-02"], "ratio": [0.1]}))
    assert r.tolist() == pytest.approx([0.1, 1.0, 1.0, 1.0])  # 1-for-10 reverse split


def test_fetch_store_and_read_back_real_prices(tmp_path):
    db = tmp_path / "p.db"
    # split-adjusted closes: real price was 20 before a 2-for-1 on day 3, then 10
    raw = _download({"AAA": [10.0, 10.0, 10.0, 10.0], "SPY": [1.0] * 4, "IWM": [1.0] * 4},
                    {"AAA": {"2020-01-03": 2.0}})
    calls = []

    def dl(syms, start, end):
        calls.append(list(syms))
        return raw
    counts = ps.fetch(["AAA", "SPY", "IWM"], db=db, download=dl, sleep=0)
    assert counts == {"ok": 3}
    assert ps.fetch(["AAA", "SPY", "IWM"], db=db, download=dl, sleep=0) == {}   # resumes
    assert len(calls) == 1
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE IF NOT EXISTS holdings (ticker, name, sector, in_iwm, in_iwv)")
        c.execute("INSERT INTO holdings VALUES ('AAA', 'A', 'Tech', 1, 1)")
    m = ps.load_matrices(["AAA"], db=db)
    assert m["raw_close"]["AAA"].tolist() == [20.0, 20.0, 10.0, 10.0]
    assert m["raw_volume"]["AAA"].tolist() == [500.0, 500.0, 1000.0, 1000.0]
    dv = (m["raw_close"] * m["raw_volume"])["AAA"]
    assert dv.tolist() == [10000.0] * 4                       # real dollar volume
    assert m["sectors"] == {"AAA": "Tech"} and m["iwm"] == ["AAA"]
    assert ps.split_check(db) == {"splits": 1, "match": 1}


def test_split_check_catches_a_split_the_prices_do_not_show(tmp_path):
    db = tmp_path / "p.db"
    # the close was NOT split-adjusted: real price 10 -> 5 on the split day already
    raw = _download({"BBB": [10.0, 10.0, 5.0, 5.0]}, {"BBB": {"2020-01-03": 2.0}})
    ps.fetch(["BBB"], db=db, download=lambda s, a, b: raw, sleep=0)
    assert ps.split_check(db) == {"splits": 1, "match": 0}


def test_a_failed_batch_is_logged_not_fatal(tmp_path):
    db = tmp_path / "p.db"

    def boom(syms, start, end):
        raise ConnectionError("network down")
    assert ps.fetch(["AAA"], db=db, download=boom, sleep=0) == {"error": 1}
    with sqlite3.connect(db) as c:
        status, detail = c.execute("SELECT status, detail FROM fetch_log").fetchone()
    assert status == "error" and "network down" in detail


def test_main_fails_loudly_without_the_holdings_files(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ps, "IWV_CSV", tmp_path / "missing.csv")
    assert ps.main([]) == 1
    assert "FAILED" in capsys.readouterr().out


def test_main_fails_loudly_without_benchmarks(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ps, "IWV_CSV", _csv(tmp_path, "iwv.csv"))
    monkeypatch.setattr(ps, "IWM_CSV", _csv(tmp_path, "iwm.csv"))
    monkeypatch.setattr(ps, "DB_PATH", tmp_path / "p.db")
    real_fetch = ps.fetch
    stocks_only = _download({"AAPL": [1.0] * 3, "BRK-B": [1.0] * 3, "KODK": [1.0] * 3})
    monkeypatch.setattr(ps, "fetch", lambda tickers, **k: real_fetch(
        tickers, db=tmp_path / "p.db", download=lambda s, a, b: stocks_only, sleep=0))
    assert ps.main(["--skip-sec"]) == 1
    assert "benchmark prices missing" in capsys.readouterr().out


def test_main_writes_only_to_the_configured_store(monkeypatch, tmp_path):
    """A redirected DB_PATH must be honoured by every function main() calls; a default
    bound at import once wrote the holdings into the real data/research/prices.db."""
    store = tmp_path / "p.db"
    monkeypatch.setattr(ps, "IWV_CSV", _csv(tmp_path, "iwv.csv"))
    monkeypatch.setattr(ps, "IWM_CSV", _csv(tmp_path, "iwm.csv"))
    monkeypatch.setattr(ps, "DB_PATH", store)
    real_fetch = ps.fetch
    full = _download({"AAPL": [1.0] * 3, "BRK-B": [1.0] * 3, "KODK": [1.0] * 3,
                      "SPY": [1.0] * 3, "IWM": [1.0] * 3})
    monkeypatch.setattr(ps, "fetch", lambda tickers, **k: real_fetch(
        tickers, download=lambda s, a, b: full, sleep=0))
    assert ps.main(["--skip-sec"]) == 0
    with sqlite3.connect(store) as c:
        assert c.execute("SELECT COUNT(*) FROM holdings").fetchone()[0] == 3
        assert c.execute("SELECT COUNT(*) FROM fetch_log WHERE status='ok'").fetchone()[0] == 5

