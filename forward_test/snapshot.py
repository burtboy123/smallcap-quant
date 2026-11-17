"""
forward_test/snapshot.py — daily price snapshot for a forward-test period (default:
config.settings.ACTIVE_FT_PERIOD). Records the daily close of every book constituent +
SPY into the period's price_snapshots.csv and computes the equal-weight, buy-and-hold
equity curve for each book + SPY, so returns and MAX DRAWDOWN are reconstructable.

ISOLATION:
  * Reads the period's book CSVs and meta.json; reads only ACTIVE_FT_PERIOD from config.
  * Writes only inside forward_test/<period>/: price_snapshots.csv, equity_curves.csv,
    meta.json (once, when baselines are first locked) and corporate_actions.json
    (when a reported split is recorded; see forward_test/baselines.py).
  * Does NOT touch the prod DB, the main pipeline or any model. Prices from yfinance.

WHAT IS STORED:
  Each run refetches the whole window, and a fresh close REPLACES the stored one for the
  same day. That is deliberate: yfinance restates a name's whole history when it splits,
  and mixing old- and new-share prices in one series would be wrong. Every replaced
  close that moved by more than REVISION_TOL is printed, so a rewrite is never silent,
  and the locked baselines are checked against the series each run (reconcile_splits):
  a mismatch is accepted only when a split yfinance reports explains it; otherwise the
  equity curve is not written and the run exits 1.

DELISTING RESILIENCE:
  A ticker yfinance no longer returns keeps its stored history, and its last valid close
  is CARRIED FORWARD and flagged (is_carried=1), so a delisting freezes the last known
  value instead of erasing the name.

Usage:
    python forward_test/snapshot.py          # backfill start..today
Run daily (or any cadence); it is idempotent — same day updates in place, no dupes.
A session that has not closed yet is never recorded (drop_open_session): a run during
US hours records through the previous close and the open day waits for the next run.
"""
from __future__ import annotations
import warnings; warnings.filterwarnings("ignore")
import os, sys, json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd, yfinance as yf

HERE = Path(__file__).parent
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))
from config.settings import ACTIVE_FT_PERIOD  # noqa: E402
from forward_test import baselines as bl  # noqa: E402

REVISION_TOL = 0.005      # a stored close replaced by one >0.5% different is reported

# Period registry. Each forward test is an isolated directory; the scheduled task
# passes --period so the SAME machinery records whichever test is active.
#   baseline_mode "explicit"    -> baselines read from a column in the book CSVs (Q3)
#   baseline_mode "first_close" -> baselines = each name's close on the first recorded
#                                  trading day >= start (Q4). Avoids the Q3 intraday/
#                                  close mismatch that made Q3 day 1 read -1.9%.
PERIODS = {
    "2026Q3_parallel": {
        "start": "2026-06-17",
        "spy_baseline": 750.57,          # locked at test start
        "baseline_mode": "explicit",
        "baseline_col": "baseline_price_2026_06_17",
    },
    # RETIRED 2026-09-29: tracked an un-investable book (Energy 46.69% vs a 30% cap;
    # WEYS/HBB/ATNI under the $5m/day ADV floor). Preserved as 2026Q4_retired with its
    # 5 sessions of history. Kept here only so the directory stays readable — the
    # scheduled task no longer records it. See 2026Q4_retired/README_RETIRED.md.
    "2026Q4_retired": {
        "start": "2026-09-22",
        "spy_baseline": None,
        "baseline_mode": "first_close",
        "baseline_col": None,
    },
    # ACTIVE (config/settings.py ACTIVE_FT_PERIOD)
    "2026Q4b": {
        "start": "2026-09-30",           # next trading day after the rebuild
        "spy_baseline": None,            # resolved from the first recorded close
        "baseline_mode": "first_close",
        "baseline_col": None,
    },
}

PERIOD = os.getenv("FT_PERIOD", ACTIVE_FT_PERIOD)
if "--period" in sys.argv:
    PERIOD = sys.argv[sys.argv.index("--period") + 1]
if PERIOD not in PERIODS:
    raise SystemExit(f"unknown period {PERIOD!r}; known: {list(PERIODS)}")

CFG   = PERIODS[PERIOD]
DIR   = HERE / PERIOD
SNAP  = DIR / "price_snapshots.csv"
CURVE = DIR / "equity_curves.csv"
START = CFG["start"]
SPY_BASELINE = CFG["spy_baseline"]

# A daily bar is final only after the regular NYSE close. The scheduled task fires at
# 23:30 Finland (16:30 ET), but it is set to run as soon as possible after a missed
# start, so it also fires whenever the PC wakes: 2026-11-05 16:48 and 2026-11-09 18:03
# Finland were both DURING the US session, and yfinance then returns the session's
# partial bar as if it were a close. The merge healed each on the next run, but the
# equity curve printed, the risk-watch email sent and the hedged-paper IWM cache written
# in between were built on an intraday print. So a bar for a session that has not
# closed is not recorded at all; the next run picks it up, one session behind at most,
# which is inside the 5.3 kill-switch's grace. Early closes (13:00 ET, the day after
# Thanksgiving and Christmas Eve) are not special-cased: that bar waits for the next
# run too. Conservative, and one rule to reason about.
US_CLOSE_ET = time(16, 15)     # 16:00 close, plus time for the closing print to settle
_ET = ZoneInfo("America/New_York")


def _now_et() -> datetime:
    return datetime.now(_ET)


def drop_open_session(fetched: pd.DataFrame, now_et: datetime) -> tuple[pd.DataFrame, list]:
    """Remove the rows of any session that has not closed: today's before US_CLOSE_ET,
    and anything dated later than today in New York. Returns (kept, dropped dates)."""
    last_final = now_et.date()
    if now_et.time() < US_CLOSE_ET:
        last_final -= timedelta(days=1)
    keep = [pd.Timestamp(d).date() <= last_final for d in fetched.index]
    dropped = [d for d, k in zip(fetched.index, keep) if not k]
    return fetched[keep], dropped


def _resolve_baselines(books: dict, wide: pd.DataFrame) -> dict:
    """first_close mode: baseline = close on the first recorded trading day."""
    first = wide.index.min()
    return {t: float(wide.loc[first, t]) for t in books if t in wide.columns
            and pd.notna(wide.loc[first, t])}


def load_books():
    inc = pd.read_csv(DIR / "incumbent_book.csv")
    ch  = pd.read_csv(DIR / "value_tilt_book.csv")
    if CFG["baseline_mode"] == "explicit":
        col = CFG["baseline_col"]
        inc_base = dict(zip(inc["ticker"], inc[col]))
        ch_base  = dict(zip(ch["ticker"],  ch[col]))
    else:
        # placeholders; filled from the first recorded close in equity_curves()
        inc_base = {t: None for t in inc["ticker"]}
        ch_base  = {t: None for t in ch["ticker"]}
    tickers = sorted(set(inc["ticker"]) | set(ch["ticker"]))
    return inc_base, ch_base, tickers


def fetch(tickers, start, end):
    """Daily Close, not adjusted for dividends (auto_adjust=False) but, like every
    yfinance price, adjusted for splits: after a split the whole series is restated."""
    df = yf.download(tickers + ["SPY"], start=start, end=end, auto_adjust=False,
                     progress=False)["Close"]
    if isinstance(df, pd.Series):   # single ticker edge case
        df = df.to_frame()
    df.index = pd.to_datetime(df.index).date
    return df   # index=date, columns=tickers


def fetch_splits(tickers, start, end) -> dict:
    """{ticker: [(date, ratio), ...]} for the splits yfinance reports in [start, end).

    Read from the download's "Stock Splits" column, not Ticker.splits: on 2026-11-17
    Ticker("SENEA").splits came back EMPTY while the download carried its 5.0 row.
    Only called when a baseline disagrees with the series, so a normal run makes no
    extra request."""
    tickers = list(tickers)
    df = yf.download(tickers, start=start, end=end, auto_adjust=False, actions=True,
                     progress=False)
    if df is None or df.empty or "Stock Splits" not in df.columns.get_level_values(0):
        return {}
    s = df["Stock Splits"]
    if isinstance(s, pd.Series):
        s = s.to_frame(tickers[0])
    out = {}
    for t in s.columns:
        col = s[t].dropna()
        col = col[col != 0]
        out[str(t)] = [(str(pd.Timestamp(d).date()), float(r)) for d, r in col.items()]
    return out


def _report_revisions(merged: pd.DataFrame) -> None:
    """Print every ticker whose stored closes were replaced by materially different ones."""
    both = merged.dropna(subset=["close_new", "close_old"])
    ratio = both["close_new"] / both["close_old"]
    rev = both[(ratio - 1).abs() > REVISION_TOL].assign(ratio=ratio)
    for t, g in rev.groupby("ticker"):
        print(f"  revised {len(g)} stored close(s) of {t} ({g['date'].min()} .. "
              f"{g['date'].max()}), new/old x{g['ratio'].median():.4f}")


def merge_persist(fetched: pd.DataFrame) -> pd.DataFrame:
    """Merge fetched closes into the persisted long table without ever erasing
    previously-recorded prices; carry forward + flag on missing days."""
    all_tickers = list(fetched.columns)
    trading_days = list(fetched.index)   # calendar = days yfinance returned

    # long fetched
    flong = fetched.reset_index().melt(id_vars="index", var_name="ticker",
                                       value_name="close").rename(columns={"index": "date"})
    flong["date"] = flong["date"].astype(str)

    # existing persisted (kept wherever the fetch has no value for that day and ticker)
    if SNAP.exists():
        existing = pd.read_csv(SNAP)[["date", "ticker", "close"]]
        existing["date"] = existing["date"].astype(str)
    else:
        existing = pd.DataFrame(columns=["date", "ticker", "close"])

    # prefer a fresh valid fetched close (a split restates the whole series); else fall
    # back to the persisted value. Material replacements are printed, never silent.
    merged = flong.merge(existing, on=["date", "ticker"], how="outer",
                         suffixes=("_new", "_old"))
    _report_revisions(merged)
    merged["close"] = merged["close_new"].where(merged["close_new"].notna(),
                                                merged["close_old"])
    merged = merged[["date", "ticker", "close"]]

    # build full grid (all trading days x all tickers) and carry-forward per ticker
    days = sorted(set(merged["date"]) | {str(d) for d in trading_days})
    days = [d for d in days if d >= START]
    grid = pd.MultiIndex.from_product([days, all_tickers], names=["date", "ticker"]).to_frame(index=False)
    g = grid.merge(merged, on=["date", "ticker"], how="left")
    g = g.sort_values(["ticker", "date"])
    # Forward-fill within each ticker; flag the filled cells (delisting-safe freeze).
    # Vectorised rather than groupby("ticker", group_keys=False).apply(_ffill): because
    # _ffill returned a frame still containing the grouping column, pandas (2.3.3 here)
    # gave back 'ticker' as BOTH an index level and a column, so the sort_values below
    # raised "'ticker' is both an index level and a column label, which is ambiguous"
    # and the snapshot silently failed every run from 2026-09-21 on -- 7 missed
    # sessions, and price_snapshots.csv was never written at all. groupby().ffill()
    # respects group boundaries identically, so the carry-forward semantics are
    # unchanged: a close is only ever carried from the same ticker's own history.
    was_missing = g["close"].isna()
    g["close"] = g.groupby("ticker")["close"].ffill()
    g["is_carried"] = (was_missing & g["close"].notna()).astype(int)
    g = g.dropna(subset=["close"]).sort_values(["date", "ticker"]).reset_index(drop=True)
    g.to_csv(SNAP, index=False)
    return g


def reconcile_splits(wide: pd.DataFrame, meta: dict) -> None:
    """Check every locked baseline against the series' close on the baseline date.

    A mismatch beyond baselines.TOLERANCE is accepted only when splits yfinance reports
    after the baseline date explain it; those are appended to corporate_actions.json.
    Anything else exits 1 before the equity curve is written: a return computed against
    a baseline in different units is wrong by the split ratio (SENEA, 2026-11-17)."""
    bdate = str(meta.get("baseline_date"))
    if bdate not in wide.index:
        print(f"  FAILED: the baseline date {bdate} is not in {SNAP.name}, so the locked "
              f"baselines cannot be checked against the prices. Equity curve not written.")
        raise SystemExit(1)
    locked = {**(meta.get("challenger_baselines") or {}),
              **(meta.get("incumbent_baselines") or {}), "SPY": meta.get("spy_baseline")}
    splits = bl.read_splits(DIR)
    off = bl.mismatches(locked, wide.loc[bdate].to_dict(), splits)
    if not off:
        return
    recorded = {(s["ticker"], s["date"]) for s in splits}
    end = (date.today() + timedelta(days=1)).isoformat()
    reported = fetch_splits(sorted(off), bdate, end)
    new, unexplained = [], {}
    for t, r in sorted(off.items()):
        cands = [(d, x) for d, x in reported.get(t, []) if d > bdate and (t, d) not in recorded]
        if bl.explain(r, cands):
            new += [{"ticker": t, "date": d, "ratio": x, "source": "yfinance Stock Splits"}
                    for d, x in cands]
        else:
            unexplained[t] = r
    if unexplained:
        for t, r in unexplained.items():
            print(f"  FAILED: {t} closed {r:.4f}x its locked baseline on {bdate}, and no split "
                  f"yfinance reports explains it (a merger, a bad print or a revision?).")
        print(f"  Equity curve NOT written: returns against a baseline in other units would "
              f"be wrong. A real split is recorded in {DIR.name}/{bl.CA_NAME} "
              f"(see forward_test/baselines.py); then rerun.")
        raise SystemExit(1)
    bl.append_splits(DIR, new)
    for s in new:
        print(f"  split recorded: {s['ticker']} x{s['ratio']:g} on {s['date']} -- yfinance "
              f"restated its prices; baseline restated in {bl.CA_NAME}, meta.json unchanged")


def equity_curves(snap: pd.DataFrame, inc_base, ch_base):
    wide = snap.pivot(index="date", columns="ticker", values="close").sort_index()

    global SPY_BASELINE
    if CFG["baseline_mode"] == "first_close":
        inc_base = _resolve_baselines(inc_base, wide)
        ch_base  = _resolve_baselines(ch_base, wide)
        if SPY_BASELINE is None:
            SPY_BASELINE = float(wide.loc[wide.index.min(), "SPY"])
        # persist the resolved baselines once, so they are locked for the test
        mp = DIR / "meta.json"
        if mp.exists():
            m = json.loads(mp.read_text(encoding="utf-8"))
            if not m.get("baselines_locked"):
                m["baselines_locked"] = True
                m["baseline_date"] = str(wide.index.min())
                m["spy_baseline"] = SPY_BASELINE
                m["incumbent_baselines"] = inc_base
                m["challenger_baselines"] = ch_base
                mp.write_text(json.dumps(m, indent=2), encoding="utf-8")
                print(f"  locked baselines as of {wide.index.min()} (SPY {SPY_BASELINE})")
            else:
                # locked: restate for recorded splits, after checking them against the
                # series (exits 1 on an unexplained mismatch)
                reconcile_splits(wide, m)
                cb = bl.current_baselines(DIR)
                inc_base, ch_base, SPY_BASELINE = cb["incumbent"], cb["challenger"], cb["spy"]

    def book_index(base: dict):
        cols = [t for t in base if t in wide.columns]
        rel = wide[cols].divide(pd.Series(base))   # close / baseline per name
        return rel.mean(axis=1)                     # equal-weight index (=1.0 at baseline)

    out = pd.DataFrame(index=wide.index)
    out["incumbent_index"]  = book_index(inc_base)
    out["challenger_index"] = book_index(ch_base)
    out["spy_index"]        = wide["SPY"] / SPY_BASELINE
    for b in ["incumbent", "challenger", "spy"]:
        out[f"{b}_ret"] = out[f"{b}_index"] - 1.0
        run_max = out[f"{b}_index"].cummax()
        out[f"{b}_drawdown"] = out[f"{b}_index"] / run_max - 1.0
    out = out.reset_index().rename(columns={"index": "date"})
    out.to_csv(CURVE, index=False)
    return out


def _recorded_sessions() -> int:
    if not SNAP.exists():
        return 0
    try:
        return int(pd.read_csv(SNAP)["date"].nunique())
    except Exception:                                   # unreadable counts as recorded:
        return 1                                        # never call an outage "not started"


def main():
    inc_base, ch_base, tickers = load_books()
    end = (date.today() + timedelta(days=1)).isoformat()   # yf end is exclusive
    print(f"Snapshot: {len(tickers)} constituents + SPY, {START} -> {date.today()}")
    fetched = fetch(tickers, START, end)
    # yfinance does NOT honour the start bound when the requested range has no
    # sessions yet: asked for start=2026-09-30 on the morning of 2026-09-30 (before
    # the US open) it returns the 2026-09-29 close instead. Those rows predate the
    # test's own start, so recording them would backdate the book to a session it was
    # not yet holding. They also defeated the guard below -- non-empty here, filtered
    # to nothing by the `d >= START` grid filter in merge_persist, which then wrote a
    # 0-row price_snapshots.csv and crashed in equity_curves on a missing SPY column.
    if fetched is not None and not fetched.empty:
        fetched = fetched[[str(d) >= START for d in fetched.index]]
        # A row with no close at all is not a session: yfinance adds one for a split's
        # ex-date before that day has traded (SENEA, 2026-11-17). Kept, it would become
        # a recorded session with every name carried forward.
        fetched = fetched.dropna(how="all")
    if fetched is None or fetched.empty or fetched.dropna(how="all").empty:
        recorded = _recorded_sessions()
        if recorded:
            # The fetch covers START..today, so once sessions are on file an empty
            # result is a data outage, not "not started". Exiting 0 here would let the
            # scheduled task report success on a night that recorded nothing.
            print(f"  FAILED: yfinance returned no data for {START} -> {date.today()} "
                  f"although {recorded} session(s) are already recorded. Nothing "
                  f"written. Check connectivity and rerun.")
            raise SystemExit(1)
        print(f"  NOT STARTED: no trading data on/after start {START} yet "
              f"(today {date.today()}). Nothing recorded; this is not an error.")
        return None
    now_et = _now_et()
    fetched, dropped = drop_open_session(fetched, now_et)
    if dropped:
        print(f"  not recording {', '.join(str(d) for d in dropped)}: the US session has "
              f"not closed yet ({now_et:%H:%M} ET), so that bar is partial. The next run "
              f"after the close records it.")
    if fetched.dropna(how="all").empty:
        recorded = _recorded_sessions()
        if recorded:
            # Sessions are on file, the fetch covers START..today, yet nothing final came
            # back: an outage, not the open day. Exit 1 so the task does not report success.
            print(f"  FAILED: yfinance returned no closed session for {START} -> "
                  f"{date.today()} although {recorded} session(s) are already recorded. "
                  f"Nothing written. Check connectivity and rerun.")
            raise SystemExit(1)
        print(f"  NOT STARTED: the only session on/after {START} is still open. Nothing "
              f"recorded; this is not an error. Rerun after the US close.")
        return None
    snap = merge_persist(fetched)
    curve = equity_curves(snap, inc_base, ch_base)

    n_carried = int(snap["is_carried"].sum())
    print(f"  recorded {snap['date'].nunique()} trading days, {snap['ticker'].nunique()} tickers "
          f"({n_carried} carried-forward cells{' — CHECK for delistings' if n_carried else ''})")
    print(f"  wrote {SNAP.name} and {CURVE.name}")

    last = curve.iloc[-1]
    print(f"\n  EQUITY CURVE as of {last['date']}:")
    for b, lbl in [("incumbent","Incumbent"),("challenger","Challenger"),("spy","SPY")]:
        print(f"    {lbl:<11} return {last[f'{b}_ret']:+.2%}   maxDD {curve[f'{b}_drawdown'].min():+.2%}")
    print(f"    spread (Challenger - Incumbent): {last['challenger_ret']-last['incumbent_ret']:+.2%}")
    return curve


if __name__ == "__main__":
    main()
