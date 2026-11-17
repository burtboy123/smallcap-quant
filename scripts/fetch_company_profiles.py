"""
scripts/fetch_company_profiles.py — short company descriptions for the dashboard.

Saves each company's name, sector, industry and yfinance business summary to
data/company_profiles.json; the picks page shows the first sentence. Covers the held
book plus the top of the latest screen by default (about 60 companies, ~1 minute).
Re-running only fetches companies not already saved, unless --refresh.

No AI service is used. Read-only against the database.

    python scripts/fetch_company_profiles.py            # held book + top 60 picks
    python scripts/fetch_company_profiles.py --tickers AMN KODK
    python scripts/fetch_company_profiles.py --refresh  # re-fetch everything listed
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard.picks_view import PROFILES  # noqa: E402


def default_tickers(top_n: int = 60) -> list[str]:
    import pandas as pd
    from config.settings import ACTIVE_FT_PERIOD, DATABASE_URL
    out: list[str] = []
    book = ROOT / "forward_test" / ACTIVE_FT_PERIOD / "incumbent_book.csv"
    if book.exists():
        out += list(pd.read_csv(book)["ticker"])
    db = Path(DATABASE_URL.replace("sqlite:///", ""))
    db = db if db.is_absolute() else ROOT / db
    if db.exists():
        with closing(sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)) as c:
            raw = pd.read_sql(
                "SELECT ticker, composite_score, value_score, quality_score, momentum_score, "
                "f_score, roic, momentum_12_1 FROM factor_scores WHERE calculation_date = "
                "(SELECT MAX(calculation_date) FROM factor_scores)", c)
        if len(raw):
            from factors.composite import ensemble_rank
            raw["passed_quality_gate"] = raw["composite_score"].notna()
            out += list(ensemble_rank(raw).head(top_n)["ticker"])
    return list(dict.fromkeys(out))


def fetch(tickers: list[str], refresh: bool = False, path: Path = PROFILES) -> dict:
    import yfinance as yf
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    todo = [t for t in tickers if refresh or t not in saved]
    print(f"{len(tickers)} companies, {len(tickers) - len(todo)} already saved, "
          f"{len(todo)} to fetch")
    failed = []
    for i, t in enumerate(todo, 1):
        try:
            info = yf.Ticker(t).info or {}
            if not info.get("longBusinessSummary"):
                raise ValueError("no business summary")
            saved[t] = {"name": info.get("longName") or info.get("shortName"),
                        "sector": info.get("sector"), "industry": info.get("industry"),
                        "summary": info.get("longBusinessSummary"),
                        "website": info.get("website")}
        except Exception as e:                                         # noqa: BLE001
            failed.append(f"{t} ({type(e).__name__}: {e})")
        if i % 10 == 0 or i == len(todo):
            print(f"  {i}/{len(todo)}", flush=True)
            path.write_text(json.dumps(saved, indent=1), encoding="utf-8")   # resumable
        time.sleep(0.3)
    path.write_text(json.dumps(saved, indent=1), encoding="utf-8")
    if failed:
        print(f"no description for {len(failed)}: " + "; ".join(failed[:10]))
    return {"saved": len(saved), "failed": failed}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch short company descriptions.")
    ap.add_argument("--tickers", nargs="+", default=None)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args(argv)
    tickers = args.tickers or default_tickers()
    if not tickers:
        print("FAILED: no tickers (no book file and no factor scores found)")
        return 1
    r = fetch(tickers, refresh=args.refresh)
    print(f"done: {r['saved']} companies in {PROFILES}")
    if r["failed"] and len(r["failed"]) == len(tickers):
        print("FAILED: every fetch failed -- check the internet connection")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
