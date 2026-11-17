"""
research/price_store.py — research prices that are honest about the past.

WHY: the main `prices` table stores yfinance's auto-adjusted close (data/prices.py
fetches with auto_adjust=True and sets adj_close = close). Every past price is scaled by
LATER splits and dividends. That is right for returns and wrong for a past price level
or market cap: a company that later split 2-for-1 shows half its real price, so its past
valuation used information from the future (the split usually follows a big rise).
Found 2026-10-11 in the strategy race (docs/research/strategy_race_RESULT.md).

WHAT: for every ticker in the iShares IWV (Russell 3000) and IWM (Russell 2000) holdings
files, plus SPY and IWM themselves, one bulk yfinance download with auto_adjust=False and
actions=True. Stored in a SEPARATE database, data/research/prices.db (the main database
and every live process are untouched):

    split-adjusted close, total-return (adjusted) close, volume, split events

The real price on a day = split-adjusted close x every split ratio after that day.
Market cap = real price x shares as filed. Dollar volume = split-adjusted close x volume
(both scale the same way, so the product is the real dollar volume).

Then fetches SEC fundamentals (data/sec_xbrl.py) for tickers not yet in data/sec/.

    python -m research.price_store              # fetch everything missing (resumable)
    python -m research.price_store --status     # coverage and the split sanity check

Download IWV holdings first: https://www.ishares.com/us/products/239714/ -> Holdings ->
save as data/iwv_holdings.csv (same way as data/iwm_holdings.csv). Either iShares format
works: the CSV, or the Excel 2003 XML file the site sometimes serves instead.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "data" / "research" / "prices.db"
IWV_CSV = ROOT / "data" / "iwv_holdings.csv"
IWM_CSV = ROOT / "data" / "iwm_holdings.csv"
BENCHMARKS = ["SPY", "IWM"]
START = "2008-01-01"
BATCH = 50

SCHEMA = """
CREATE TABLE IF NOT EXISTS px (
    ticker TEXT NOT NULL, date TEXT NOT NULL, close REAL, adj_close REAL, volume REAL,
    PRIMARY KEY (ticker, date));
CREATE TABLE IF NOT EXISTS splits (
    ticker TEXT NOT NULL, date TEXT NOT NULL, ratio REAL NOT NULL,
    PRIMARY KEY (ticker, date));
CREATE TABLE IF NOT EXISTS holdings (
    ticker TEXT PRIMARY KEY, name TEXT, sector TEXT, in_iwm INTEGER, in_iwv INTEGER);
CREATE TABLE IF NOT EXISTS fetch_log (
    ticker TEXT PRIMARY KEY, status TEXT NOT NULL, detail TEXT, n_rows INTEGER,
    fetched_at TEXT NOT NULL);
"""


# ── holdings files ──────────────────────────────────────────────────────────────

SS_NS = "{urn:schemas-microsoft-com:office:spreadsheet}"


def _spreadsheetml_rows(text: str) -> list[list[str]]:
    """Rows of the first worksheet with a 'Ticker' header, from the Excel 2003 XML
    file iShares serves (sometimes under a .csv name). Its company names can carry a
    bare '&' ("S&P"), which is invalid XML, so those are escaped before parsing."""
    import re
    import xml.etree.ElementTree as ET
    text = re.sub(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", text)
    root = ET.fromstring(text.lstrip("\ufeff").lstrip())
    for ws in root.iter(f"{SS_NS}Worksheet"):
        rows = []
        for row in ws.iter(f"{SS_NS}Row"):
            cells: list[str] = []
            for cell in row.findall(f"{SS_NS}Cell"):
                idx = cell.get(f"{SS_NS}Index")              # skipped empty cells
                if idx:
                    cells += [""] * (int(idx) - 1 - len(cells))
                data = cell.find(f"{SS_NS}Data")
                cells.append("" if data is None else "".join(data.itertext()).strip())
            rows.append(cells)
        if any(r and r[0].strip().lower() == "ticker" for r in rows):
            return rows
    raise ValueError("no worksheet with a 'Ticker' header row")


def parse_ishares(path: Path) -> pd.DataFrame:
    """ticker, name, sector from an iShares holdings download: the CSV (metadata rows
    above the header) or the Excel 2003 XML export. Equities only; the same ticker
    cleaning as data/universe_builder.py."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    if text.lstrip("\ufeff").lstrip().startswith("<"):
        try:
            rows = _spreadsheetml_rows(text)
        except Exception as e:                                         # noqa: BLE001
            raise ValueError(f"{path}: not a readable iShares holdings file ({e})") from e
        h = next(i for i, r in enumerate(rows) if r and r[0].strip().lower() == "ticker")
        header = [c.strip() for c in rows[h]]
        body = [(r + [""] * len(header))[:len(header)] for r in rows[h + 1:] if any(r)]
        df = pd.DataFrame(body, columns=header, dtype=str)
    else:
        lines = text.splitlines()
        head = next((i for i, ln in enumerate(lines) if ln.lower().startswith("ticker")
                     or "ticker" in ln.lower().split(",")[0]), None)
        if head is None:
            raise ValueError(f"{path}: no 'Ticker' header row -- download the Holdings file")
        df = pd.read_csv(path, skiprows=head, dtype=str, encoding="utf-8",
                         encoding_errors="replace", on_bad_lines="skip")
        df.columns = df.columns.str.strip()
    if "Ticker" not in df:
        raise ValueError(f"{path}: no 'Ticker' column (found {list(df.columns)[:6]})")
    if "Asset Class" in df:
        df = df[df["Asset Class"].astype(str).str.strip().str.lower() == "equity"]
    out = pd.DataFrame({"ticker": df["Ticker"].astype(str).str.strip().str.upper(),
                        "name": df.get("Name", pd.Series(index=df.index, dtype=str)),
                        "sector": df.get("Sector", pd.Series(index=df.index, dtype=str))})
    ok = out["ticker"].map(lambda t: 1 <= len(t) <= 5
                           and all(c.isalpha() or c == "." for c in t)
                           and t not in {"CASH", "USD", "OTHER"})
    return out[ok].drop_duplicates("ticker").reset_index(drop=True)


def holdings(iwv: Path | None = None, iwm: Path | None = None) -> pd.DataFrame:
    a, b = parse_ishares(iwv or IWV_CSV), parse_ishares(iwm or IWM_CSV)
    h = a.merge(b[["ticker", "sector"]].rename(columns={"sector": "s2"}), on="ticker",
                how="outer", indicator=True)
    h["sector"] = h["sector"].fillna(h["s2"])
    h["in_iwv"] = (h["_merge"] != "right_only").astype(int)
    h["in_iwm"] = (h["_merge"] != "left_only").astype(int)
    return h[["ticker", "name", "sector", "in_iwm", "in_iwv"]]


# ── prices ──────────────────────────────────────────────────────────────────────

def yahoo(t: str) -> str:
    return t.replace(".", "-")


def parse_download(raw: pd.DataFrame, tickers: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(prices, splits) from yf.download(..., auto_adjust=False, actions=True)."""
    if raw is None or raw.empty:
        return pd.DataFrame(), pd.DataFrame()
    back = {yahoo(t): t for t in tickers}
    px, sp = [], []
    if isinstance(raw.columns, pd.MultiIndex):
        level = 1 if "Close" in raw.columns.get_level_values(0) else 0
        names = raw.columns.get_level_values(level).unique()
        frames = {n: raw.xs(n, axis=1, level=level) for n in names}
    else:
        frames = {yahoo(tickers[0]): raw}
    for ysym, f in frames.items():
        t = back.get(ysym, ysym)
        if "Close" not in f or f["Close"].dropna().empty:
            continue
        d = pd.DataFrame({"ticker": t, "date": pd.DatetimeIndex(f.index).strftime("%Y-%m-%d"),
                          "close": f["Close"].to_numpy(dtype=float),
                          "adj_close": f.get("Adj Close", f["Close"]).to_numpy(dtype=float),
                          "volume": f.get("Volume", pd.Series(np.nan, index=f.index))
                          .to_numpy(dtype=float)})
        px.append(d.dropna(subset=["close"]))
        if "Stock Splits" in f:
            s = f["Stock Splits"]
            s = s[(s.notna()) & (s != 0) & (s != 1)]
            if len(s):
                sp.append(pd.DataFrame({"ticker": t, "ratio": s.to_numpy(dtype=float),
                                        "date": pd.DatetimeIndex(s.index).strftime("%Y-%m-%d")}))
    return (pd.concat(px, ignore_index=True) if px else pd.DataFrame(),
            pd.concat(sp, ignore_index=True) if sp else pd.DataFrame())


def connect(db: Path | None = None) -> sqlite3.Connection:
    # paths resolve at CALL time: a default bound at import ignored a redirected
    # DB_PATH and let a test write the real store
    db = db or DB_PATH
    db.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db)
    c.executescript(SCHEMA)
    return c


def fetch(tickers: list[str], db: Path | None = None, download=None, start: str = START,
          end: str | None = None, sleep: float = 1.0) -> dict:
    """Download and store every ticker not yet fetched OK. Never raises for one batch:
    each ticker's outcome goes to fetch_log. Returns status counts."""
    db = db or DB_PATH
    if download is None:
        import yfinance as yf

        def download(syms, start, end):
            return yf.download(syms, start=start, end=end, auto_adjust=False,
                               actions=True, progress=False, threads=True)
    end = end or date.today().isoformat()
    counts: dict[str, int] = {}
    with closing(connect(db)) as c:
        done = {r[0] for r in c.execute("SELECT ticker FROM fetch_log WHERE status='ok'")}
        todo = [t for t in dict.fromkeys(tickers) if t not in done]
        print(f"{len(set(tickers))} tickers, {len(done)} already stored, {len(todo)} to fetch",
              flush=True)
        for i in range(0, len(todo), BATCH):
            batch = todo[i:i + BATCH]
            try:
                px, sp = parse_download(download([yahoo(t) for t in batch], start, end),
                                        batch)
                err = ""
            except Exception as e:                                     # noqa: BLE001
                px, sp, err = pd.DataFrame(), pd.DataFrame(), f"{type(e).__name__}: {e}"
            now = datetime.now().isoformat(timespec="seconds")
            for t in batch:
                rows = px[px["ticker"] == t] if len(px) else px
                c.execute("DELETE FROM px WHERE ticker=?", (t,))
                c.execute("DELETE FROM splits WHERE ticker=?", (t,))
                if len(rows):
                    rows.to_sql("px", c, if_exists="append", index=False)
                    if len(sp):
                        sp[sp["ticker"] == t].to_sql("splits", c, if_exists="append",
                                                    index=False)
                status = "ok" if len(rows) else "error" if err else "no_data"
                c.execute("INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,?,?)",
                          (t, status, err or ("" if len(rows) else "no prices returned"),
                           len(rows), now))
                counts[status] = counts.get(status, 0) + 1
            c.commit()
            print(f"  {min(i + BATCH, len(todo))}/{len(todo)}  {counts}", flush=True)
            if sleep and i + BATCH < len(todo):
                time.sleep(sleep)
    return counts


# ── reading ─────────────────────────────────────────────────────────────────────

def split_factor(dates: pd.DatetimeIndex, splits: pd.DataFrame) -> pd.Series:
    """Product of every split ratio strictly AFTER each date (1 when none)."""
    f = pd.Series(1.0, index=dates)
    for r in splits.itertuples():
        f[dates < pd.Timestamp(r.date)] *= float(r.ratio)
    return f


def load_matrices(tickers: list[str] | None = None, db: Path | None = None,
                  since: str | None = None) -> dict:
    """{'raw_close', 'adj_close', 'raw_volume'} date x ticker, and 'sectors'. Real
    price = split-adjusted close x later splits; real volume = volume / later splits,
    so raw_close x raw_volume is the real dollar volume."""
    db = db or DB_PATH
    since = f"{pd.Timestamp(since):%Y-%m-%d}" if since else "0000"
    cols: dict = {"close": {}, "adj_close": {}, "volume": {}}
    with closing(sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)) as c:
        sp = pd.read_sql("SELECT ticker, date, ratio FROM splits", c)
        hold = pd.read_sql("SELECT ticker, sector, in_iwm, in_iwv FROM holdings", c)
        stored = [r[0] for r in c.execute("SELECT DISTINCT ticker FROM px")]
        wanted = stored if tickers is None else [t for t in stored if t in set(tickers)]
        # one company at a time into float32 columns: the whole table at once would
        # hold ~13 million rows of strings in memory
        for t in wanted:
            g = pd.read_sql("SELECT date, close, adj_close, volume FROM px "
                            "WHERE ticker = ? AND date >= ? ORDER BY date", c,
                            params=(t, since), index_col="date", parse_dates=["date"])
            for k in cols:
                cols[k][t] = g[k].astype("float32")
    if not wanted:
        raise ValueError(f"no stored prices for the requested tickers in {db}")
    close, adj, vol = (pd.DataFrame(cols[k]).sort_index() for k in ("close", "adj_close",
                                                                    "volume"))
    fac = pd.DataFrame(1.0, index=close.index, columns=close.columns, dtype="float32")
    for t, g in sp.groupby("ticker"):
        if t in fac:
            fac[t] = split_factor(close.index, g).to_numpy(dtype="float32")
    return {"raw_close": close * fac, "adj_close": adj, "raw_volume": vol / fac,
            "sectors": {r.ticker: r.sector for r in hold.itertuples()
                        if isinstance(r.sector, str) and r.sector.strip()},
            "iwm": [r.ticker for r in hold.itertuples() if r.in_iwm],
            "iwv": [r.ticker for r in hold.itertuples() if r.in_iwv]}


def split_check(db: Path | None = None) -> dict:
    """Sanity check of the split arithmetic on the stored data: on a split day the
    real price should jump by about 1/ratio (a 2-for-1 halves it). Returns how many
    splits match within 25%."""
    db = db or DB_PATH
    with closing(sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)) as c:
        sp = pd.read_sql("SELECT ticker, date, ratio FROM splits", c)
        if sp.empty:
            return {"splits": 0, "match": 0}
        tick = tuple(sp["ticker"].unique())
        px = pd.read_sql(f"SELECT ticker, date, close FROM px WHERE ticker IN "
                         f"({','.join('?' * len(tick))})", c, params=tick)
    px["date"] = pd.to_datetime(px["date"])
    match = 0
    for t, g in sp.groupby("ticker"):
        p = px[px["ticker"] == t].set_index("date")["close"].sort_index()
        raw = p * split_factor(p.index, g)
        for r in g.itertuples():
            d = pd.Timestamp(r.date)
            before, after = raw[raw.index < d], raw[raw.index >= d]
            if len(before) and len(after):
                jump = after.iloc[0] / before.iloc[-1]
                if abs(jump * r.ratio - 1) < 0.25:
                    match += 1
    return {"splits": len(sp), "match": match}


def status(db: Path | None = None) -> str:
    db = db or DB_PATH
    if not db.exists():
        return f"no research price store yet at {db} -- run: python -m research.price_store"
    with closing(sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)) as c:
        log = pd.read_sql("SELECT status, COUNT(*) n FROM fetch_log GROUP BY status", c)
        rng = pd.read_sql("SELECT MIN(date) a, MAX(date) b, COUNT(DISTINCT ticker) n "
                          "FROM px", c).iloc[0]
        h = pd.read_sql("SELECT SUM(in_iwm) iwm, SUM(in_iwv) iwv FROM holdings", c).iloc[0]
    sc = split_check(db)
    return (f"fetch log:\n{log.to_string(index=False)}\n"
            f"prices: {rng['n']} tickers, {rng['a']} to {rng['b']}\n"
            f"holdings: {h['iwm']} in IWM, {h['iwv']} in IWV\n"
            f"split check: {sc['match']} of {sc['splits']} splits show the expected "
            f"price jump on the split day")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Research price store (real past prices).")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--skip-sec", action="store_true",
                    help="prices only; do not fetch SEC fundamentals for new tickers")
    args = ap.parse_args(argv)
    if args.status:
        print(status())
        return 0
    for p in (IWV_CSV, IWM_CSV):
        if not p.exists():
            print(f"FAILED: {p} not found. Download the Holdings CSV from iShares "
                  f"(IWV: https://www.ishares.com/us/products/239714/) and save it there.")
            return 1
    try:
        h = holdings()
    except ValueError as e:
        print(f"FAILED: {e}")
        return 1
    with closing(connect()) as c:
        c.execute("DELETE FROM holdings")
        h.to_sql("holdings", c, if_exists="append", index=False)
        c.commit()
    print(f"holdings: {int(h['in_iwv'].sum())} in IWV, {int(h['in_iwm'].sum())} in IWM, "
          f"{len(h)} together", flush=True)
    counts = fetch(list(h["ticker"]) + BENCHMARKS)
    with closing(sqlite3.connect(DB_PATH)) as c:
        ok = {r[0] for r in c.execute("SELECT ticker FROM fetch_log WHERE status='ok'")}
    if not set(BENCHMARKS) <= ok:
        print(f"FAILED: benchmark prices missing ({sorted(set(BENCHMARKS) - ok)}) -- "
              f"check the internet connection and run again")
        return 1
    if len(ok) < 0.8 * len(h):
        print(f"FAILED: only {len(ok)} of {len(h)} tickers have prices -- run again "
              f"(it resumes); see fetch_log for the reasons")
        return 1
    print(f"prices done: {counts}", flush=True)
    if not args.skip_sec:
        from dotenv import load_dotenv
        from data import sec_xbrl
        load_dotenv(ROOT / ".env")
        try:
            client = sec_xbrl.SecClient()
        except ValueError as e:
            print(f"FAILED: {e}")
            return 1
        sec = sec_xbrl.fetch_universe(list(h["ticker"]), client)
        print(f"SEC fundamentals: {sec}")
    print(status())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
