"""
data/sec_xbrl.py — point-in-time fundamentals from SEC XBRL `companyfacts` (research).

WHY: the fundamentals table only reaches back to 2021 (SimFin free tier) and mixes
quarterly and 12-month rows with no source column. The distress/accruals study could
not be tested on it (docs/research/distress_accruals_RESULT.md). SEC's free XBRL API
holds every 10-K/10-Q fact back to ~2009, each with the date it was FILED, from one
source.

WHAT: for every universe ticker, one request to
    https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json
The selected facts are stored in a SEPARATE database, data/sec/sec_fundamentals.db:
the main quant_system.db, the weekly backup and every live process are untouched.
Everything here can be downloaded again, so it is not backed up.

POINT IN TIME: values are taken AS FIRST REPORTED (the earliest filing of each fact),
and a period is usable only from the latest `filed` date among the facts it needs.
Later restatements are ignored, so nothing known only later can leak backwards.

SEC FAIR ACCESS: a declared User-Agent (SEC_USER_AGENT in .env: "Name email@host"),
at most 8 requests/second (SEC's cap is 10), backoff on 429/5xx, and a gzip cache of
every raw response so nothing is downloaded twice. Resumable: each ticker's outcome is
logged in sec_fetch_log; a rerun skips tickers already fetched OK.

    python -m data.sec_xbrl                    # fetch the universe (resumable)
    python -m data.sec_xbrl --tickers AMN KODK # just these
    python -m data.sec_xbrl --status           # coverage of what is stored

Research only: nothing in the live pipeline reads this database.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SEC_DIR = ROOT / "data" / "sec"
DB_PATH = SEC_DIR / "sec_fundamentals.db"
RAW_DIR = SEC_DIR / "raw"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
MAX_RPS = 8.0
FORMS = {"10-K", "10-Q", "10-K/A", "10-Q/A", "10-KT", "20-F", "40-F"}

# concept -> ordered (taxonomy, tag, unit) candidates; the first one present wins.
CONCEPTS = {
    "net_income": [("us-gaap", "NetIncomeLoss", "USD"),
                   ("us-gaap", "ProfitLoss", "USD"),
                   ("us-gaap", "NetIncomeLossAvailableToCommonStockholdersBasic", "USD")],
    "ocf": [("us-gaap", "NetCashProvidedByUsedInOperatingActivities", "USD"),
            ("us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations", "USD")],
    "assets": [("us-gaap", "Assets", "USD")],
    "liabilities": [("us-gaap", "Liabilities", "USD")],
    "equity": [("us-gaap", "StockholdersEquity", "USD"),
               ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", "USD")],
    "cash": [("us-gaap", "CashAndCashEquivalentsAtCarryingValue", "USD"),
             ("us-gaap", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "USD")],
    "shares": [("dei", "EntityCommonStockSharesOutstanding", "shares"),
               ("us-gaap", "CommonStockSharesOutstanding", "shares")],
}
DURATION = {"net_income", "ocf"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sec_facts (
    cik INTEGER NOT NULL, ticker TEXT NOT NULL, concept TEXT NOT NULL, tag TEXT NOT NULL,
    start TEXT, "end" TEXT NOT NULL, val REAL NOT NULL, fy INTEGER, fp TEXT,
    form TEXT, filed TEXT NOT NULL, accn TEXT NOT NULL
);
-- IFNULL: SQLite treats NULLs as distinct in UNIQUE, so balance-sheet facts (no start)
-- would be inserted again on every rerun without it.
CREATE UNIQUE INDEX IF NOT EXISTS ux_facts ON sec_facts
    (cik, concept, IFNULL(start, ''), "end", accn);
CREATE INDEX IF NOT EXISTS ix_facts_ticker ON sec_facts (ticker, concept, "end", filed);
CREATE TABLE IF NOT EXISTS sec_fetch_log (
    ticker TEXT PRIMARY KEY, cik INTEGER, status TEXT NOT NULL, detail TEXT,
    n_facts INTEGER, fetched_at TEXT NOT NULL
);
"""


# ── client ──────────────────────────────────────────────────────────────────────

class SecClient:
    """Rate-limited, cached GET for SEC endpoints. Never downloads the same URL twice
    within `max_age_days` of the cached copy."""

    def __init__(self, user_agent: str | None = None, raw_dir: Path = RAW_DIR,
                 max_rps: float = MAX_RPS, session=None):
        ua = (user_agent or os.getenv("SEC_USER_AGENT", "")).strip()
        if "@" not in ua or len(ua.split()) < 2:
            raise ValueError(
                "SEC_USER_AGENT must be set in .env as 'Your Name your@email' -- SEC "
                "blocks requests without a real contact (sec.gov/os/accessing-edgar-data)")
        self.headers = {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}
        self.raw_dir = raw_dir
        self.min_gap = 1.0 / max_rps
        self._last = 0.0
        if session is None:
            import requests
            session = requests.Session()
        self.session = session
        self.requests_made = 0

    def _cache_path(self, url: str) -> Path:
        name = url.split("://", 1)[1].replace("/", "_")
        return self.raw_dir / f"{name}.gz"

    def get_json(self, url: str, max_age_days: float | None = None) -> dict | None:
        """Parsed JSON, or None on a 404 (company has no XBRL facts). Raises on other
        failures after retries, so the caller records the reason."""
        p = self._cache_path(url)
        if p.exists() and (max_age_days is None or
                           time.time() - p.stat().st_mtime < max_age_days * 86400):
            with gzip.open(p, "rt", encoding="utf-8") as f:
                return json.load(f)
        for attempt in range(5):
            wait = self.min_gap - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            r = self.session.get(url, headers=self.headers, timeout=30)
            self.requests_made += 1
            if r.status_code == 404:
                return None
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt)                    # back off: 1, 2, 4, 8, 16 s
                continue
            r.raise_for_status()
            data = r.json()
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            with gzip.open(tmp, "wt", encoding="utf-8") as f:
                json.dump(data, f)
            tmp.replace(p)                                   # never a half-written cache
            return data
        raise RuntimeError(f"HTTP {r.status_code} after 5 attempts: {url}")


def ticker_cik_map(client: SecClient) -> dict[str, int]:
    """SEC's current ticker -> CIK map (refreshed daily). Tickers are normalised so
    'BRK.B' and 'BRK-B' both match. Current tickers only: reused tickers resolve to the
    company that holds them today, which is the company in today's IWM universe."""
    data = client.get_json(TICKERS_URL, max_age_days=1) or {}
    out = {}
    for row in data.values():
        out[_norm(row["ticker"])] = int(row["cik_str"])
    return out


def _norm(t: str) -> str:
    return t.upper().replace(".", "-").strip()


# ── parsing ─────────────────────────────────────────────────────────────────────

def parse_facts(doc: dict, ticker: str, cik: int) -> pd.DataFrame:
    """The selected concepts from one companyfacts document, one row per reported
    value. Only the first candidate tag present for a concept is used, so two tags
    never mix within one concept."""
    rows = []
    facts = doc.get("facts", {})
    for concept, candidates in CONCEPTS.items():
        for tax, tag, unit in candidates:
            vals = facts.get(tax, {}).get(tag, {}).get("units", {}).get(unit)
            if not vals:
                continue
            for v in vals:
                if v.get("form") not in FORMS or v.get("val") is None:
                    continue
                if concept in DURATION and not v.get("start"):
                    continue
                rows.append({"cik": cik, "ticker": ticker, "concept": concept, "tag": tag,
                             "start": v.get("start"), "end": v["end"], "val": float(v["val"]),
                             "fy": v.get("fy"), "fp": v.get("fp"), "form": v.get("form"),
                             "filed": v["filed"], "accn": v["accn"]})
            break                                             # first tag present wins
    return pd.DataFrame(rows)


# ── storage ─────────────────────────────────────────────────────────────────────

def connect(db: Path = DB_PATH) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db)
    c.executescript(SCHEMA)
    return c


def store(c: sqlite3.Connection, ticker: str, cik: int | None, facts: pd.DataFrame,
          status: str, detail: str = "") -> None:
    """Idempotent: facts are INSERT OR IGNORE on their natural key, the log row is
    replaced. A crash between the two leaves facts without an 'ok' log, so the ticker
    is simply fetched (from cache) again."""
    if len(facts):
        c.executemany(
            'INSERT OR IGNORE INTO sec_facts (cik, ticker, concept, tag, start, "end", val, '
            "fy, fp, form, filed, accn) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            facts[["cik", "ticker", "concept", "tag", "start", "end", "val", "fy", "fp",
                   "form", "filed", "accn"]].itertuples(index=False, name=None))
    c.execute("INSERT OR REPLACE INTO sec_fetch_log VALUES (?,?,?,?,?,?)",
              (ticker, cik, status, detail[:500], len(facts),
               datetime.now().isoformat(timespec="seconds")))
    c.commit()


def fetch_universe(tickers: list[str], client: SecClient, db: Path = DB_PATH,
                   refetch: bool = False, progress_every: int = 50) -> dict:
    """Fetch and store every ticker. Returns status counts. Never raises for a single
    ticker: every failure is logged with its reason."""
    counts: dict[str, int] = {}
    with closing(connect(db)) as c:
        done = set() if refetch else {r[0] for r in c.execute(
            "SELECT ticker FROM sec_fetch_log WHERE status = 'ok'")}
        cikmap = ticker_cik_map(client)
        todo = [t for t in tickers if t not in done]
        print(f"{len(tickers)} tickers, {len(done)} already fetched, {len(todo)} to do")
        t0 = time.time()
        for i, t in enumerate(todo, 1):
            cik = cikmap.get(_norm(t))
            try:
                if cik is None:
                    status, facts, detail = "no_cik", pd.DataFrame(), "ticker not in SEC map"
                else:
                    doc = client.get_json(FACTS_URL.format(cik=cik))
                    if doc is None:
                        status, facts, detail = "no_facts", pd.DataFrame(), "404: no XBRL facts"
                    else:
                        facts = parse_facts(doc, t, cik)
                        status = "ok" if len(facts) else "no_facts"
                        detail = "" if len(facts) else "none of the selected tags present"
            except Exception as e:                                     # noqa: BLE001
                status, facts, detail = "error", pd.DataFrame(), f"{type(e).__name__}: {e}"
            store(c, t, cik, facts, status, detail)
            counts[status] = counts.get(status, 0) + 1
            if i % progress_every == 0 or i == len(todo):
                rate = i / max(time.time() - t0, 1e-9)
                print(f"  {i}/{len(todo)}  {counts}  ({rate:.1f}/s, "
                      f"~{(len(todo) - i) / max(rate, 1e-9) / 60:.0f} min left)", flush=True)
    return counts


# ── point-in-time panel ─────────────────────────────────────────────────────────

def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _first_reported(df: pd.DataFrame) -> pd.DataFrame:
    """For each (concept, start, end) keep the EARLIEST filing: as first reported."""
    return (df.sort_values("filed")
              .drop_duplicates(["concept", "start", "end"], keep="first"))


def _near(series_end: pd.Series, target: date, tol: int) -> pd.Series:
    return (pd.to_datetime(series_end) - pd.Timestamp(target)).abs() <= pd.Timedelta(days=tol)


def _ttm(dur: pd.DataFrame, end: str) -> tuple[float, str] | None:
    """(12-month value ending `end`, latest filed among its parts), or None.

    A 12-month fact ending at `end` is used directly. Otherwise
    TTM = prior fiscal-year annual + year-to-date - prior-year year-to-date, all
    first-reported."""
    e = date.fromisoformat(end)
    d = dur.assign(days=[_days(s, x) for s, x in zip(dur["start"], dur["end"])])
    annual = d[(d["end"] == end) & d["days"].between(350, 380)]
    if len(annual):
        r = annual.iloc[0]
        return r["val"], r["filed"]
    ytd = d[(d["end"] == end) & d["days"].between(80, 290)]
    if ytd.empty:
        return None
    y = ytd.sort_values("days").iloc[-1]                     # the longest YTD ending here
    s = date.fromisoformat(y["start"])
    prior_annual = d[_near(d["end"], s - timedelta(days=1), 7) & d["days"].between(350, 380)]
    prior_ytd = d[_near(d["end"], e - timedelta(days=365), 10)
                  & (d["days"] - y["days"]).abs().le(10)]
    if prior_annual.empty or prior_ytd.empty:
        return None
    pa, py = prior_annual.iloc[0], prior_ytd.iloc[0]
    return (pa["val"] + y["val"] - py["val"],
            max(y["filed"], pa["filed"], py["filed"]))


def _quarter(dur: pd.DataFrame, end: str) -> float | None:
    """The 3-month value ending `end`: a 3-month fact, or YTD minus the previous YTD
    with the same start (how a Q4 is recovered from annual minus 9-month)."""
    d = dur.assign(days=[_days(s, x) for s, x in zip(dur["start"], dur["end"])])
    q = d[(d["end"] == end) & d["days"].between(80, 100)]
    if len(q):
        return float(q.iloc[0]["val"])
    here = d[(d["end"] == end) & d["days"].between(170, 380)]
    for r in here.itertuples():
        prev = d[(d["start"] == r.start) & (d["days"] - (r.days - 91)).abs().le(15)]
        if len(prev):
            return float(r.val - prev.iloc[0]["val"])
    return None


def build_panel(facts: pd.DataFrame, progress_every: int | None = None) -> pd.DataFrame:
    """One row per (ticker, fiscal period end): TTM flows, balance-sheet items at that
    end, shares from the same filing, and available_date = the latest `filed` among
    every fact used. Columns are what research/strategy_race.scores_on reads.
    About 1 s per company; `progress_every` prints a counter with the time left."""
    out = []
    n_all, t0 = facts["ticker"].nunique() if len(facts) else 0, time.time()
    for i, (t, g) in enumerate(facts.groupby("ticker"), 1):
        if progress_every and (i % progress_every == 0 or i == n_all):
            rate = i / max(time.time() - t0, 1e-9)
            print(f"  SEC panel: {i}/{n_all} companies (~{(n_all - i) / rate / 60:.0f} min "
                  f"left)", flush=True)
        g = _first_reported(g)
        inst = {k: v for k, v in g[g["start"].isna()].groupby("concept")}
        ni = g[g["concept"] == "net_income"]
        ocf = g[g["concept"] == "ocf"]
        assets = inst.get("assets")
        if assets is None or ni.empty:
            continue
        for a in assets.itertuples():
            end = a.end
            ttm = _ttm(ni, end)
            if ttm is None:
                continue
            filed = [ttm[1], a.filed]
            o = _ttm(ocf, end) if len(ocf) else None
            if o:
                filed.append(o[1])

            def at(concept):
                df = inst.get(concept)
                if df is None:
                    return None
                hit = df[df["end"] == end]
                if hit.empty:
                    return None
                filed.append(hit.iloc[0]["filed"])
                return float(hit.iloc[0]["val"])

            liab, eq, cash = at("liabilities"), at("equity"), at("cash")
            sh = inst.get("shares")
            shares = None
            if sh is not None:
                same = sh[sh["accn"] == a.accn]                 # the cover-page count
                if same.empty:
                    # else the latest count ALREADY filed by then -- never a later
                    # filing's, which would push this period's availability forward
                    same = (sh[sh["filed"] <= a.filed]
                            .sort_values(["filed", "end"]).iloc[::-1])
                if len(same):
                    shares = float(same.iloc[0]["val"])
                    filed.append(same.iloc[0]["filed"])
            qs = []
            for k in range(4):
                qe = (pd.Timestamp(end) - pd.DateOffset(months=3 * k))
                cand = ni[_near(ni["end"], qe.date(), 6)]["end"]
                qv = _quarter(ni, cand.iloc[0]) if len(cand) else None
                if qv is None:
                    break
                qs.append(qv)
            out.append({"ticker": t, "available_date": pd.Timestamp(max(filed)),
                        "period_end_date": pd.Timestamp(end), "source": "sec",
                        "ni_ttm": ttm[0], "ocf_ttm": o[0] if o else float("nan"),
                        "ni_q": qs if len(qs) == 4 else [],
                        "total_assets": a.val,
                        "total_equity": eq if eq is not None else float("nan"),
                        "total_liabilities": liab if liab is not None else float("nan"),
                        "cash": cash if cash is not None else float("nan"),
                        "shares": shares if shares is not None else float("nan")})
    return pd.DataFrame(out)


def load_facts(db: Path | None = None) -> pd.DataFrame:
    db = db or DB_PATH
    with closing(sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)) as c:
        return pd.read_sql('SELECT * FROM sec_facts', c)


def cached_panel(db: Path | None = None) -> pd.DataFrame:
    """build_panel(load_facts()) once per version of the SEC database. The result is
    saved next to it (panel_cache.pkl) and reused while the database file is unchanged
    (same size and modification time), so a second study does not spend another
    30-50 minutes rebuilding it. Any fetch that writes the database invalidates it."""
    db = db or DB_PATH
    cache, meta = db.with_name("panel_cache.pkl"), db.with_name("panel_cache.json")
    st = db.stat()
    key = {"db": db.name, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
    if cache.exists() and meta.exists():
        try:
            if json.loads(meta.read_text(encoding="utf-8")) == key:
                print("SEC panel: reusing the saved build (the SEC database is unchanged)",
                      flush=True)
                return pd.read_pickle(cache)
        except (ValueError, OSError):
            pass                                       # unreadable: rebuild
    facts = load_facts(db)
    n = facts["ticker"].nunique() if len(facts) else 0
    print(f"SEC panel: building for {n} companies (about 1 s each; saved for next time)",
          flush=True)
    panel = build_panel(facts, progress_every=100)
    tmp = cache.with_suffix(".tmp")
    panel.to_pickle(tmp)
    tmp.replace(cache)                                 # never a half-written cache
    meta.write_text(json.dumps(key), encoding="utf-8")
    return panel


def status(db: Path = DB_PATH) -> str:
    if not db.exists():
        return f"no SEC database yet at {db} -- run: python -m data.sec_xbrl"
    with closing(sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)) as c:
        log = pd.read_sql("SELECT status, COUNT(*) n FROM sec_fetch_log GROUP BY status", c)
        yrs = pd.read_sql(
            "SELECT substr(\"end\",1,4) y, COUNT(DISTINCT ticker) n FROM sec_facts "
            "WHERE concept='net_income' GROUP BY y ORDER BY y", c)
    return ("fetch log:\n" + log.to_string(index=False)
            + "\n\ntickers with net income facts, by period-end year:\n"
            + yrs.to_string(index=False))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch SEC XBRL fundamentals (research).")
    ap.add_argument("--tickers", nargs="+", default=None)
    ap.add_argument("--refetch", action="store_true", help="ignore the 'ok' log")
    ap.add_argument("--status", action="store_true", help="print coverage and exit")
    args = ap.parse_args(argv)
    if args.status:
        print(status())
        return 0
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    try:
        client = SecClient()
    except ValueError as e:
        print(f"FAILED: {e}")
        return 1
    if args.tickers:
        tickers = args.tickers
    else:
        from data.universe_builder import get_universe
        tickers = get_universe()
    counts = fetch_universe(tickers, client)
    print(f"done: {counts}; {client.requests_made} requests; database {DB_PATH}")
    if not counts:
        print("nothing to do: every ticker was already fetched (--refetch to redo)")
        return 0
    if not counts.get("ok"):
        print("FAILED: no ticker was fetched successfully -- see sec_fetch_log")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
