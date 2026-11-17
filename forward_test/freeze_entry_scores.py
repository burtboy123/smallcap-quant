"""
forward_test/freeze_entry_scores.py — copy each book name's ENTRY factor scores into the
period folder, so the end-of-quarter value-consistency check (EVAL_CRITERIA.md §1) reads a
frozen file instead of a database that may have been recalculated by then.

The check is a Spearman rank correlation between each name's entry `value_score` and its
realised quarter return, and it is a HARD prerequisite in the decision rule. The entry
scores live only in `factor_scores` at the book's calc_date (meta.json); the books
themselves do not carry `value_score`. Freezing them at the start of the window is part
of pre-committing the evaluation.

    python forward_test/freeze_entry_scores.py                  # active period
    python forward_test/freeze_entry_scores.py --period 2026Q4b

Writes <period>/entry_scores.csv. Refuses to overwrite an existing file (exit 1): the
whole point is that it does not change after the window opens.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

COLUMNS = ["value_score", "quality_score", "momentum_score", "composite_score", "f_score"]


def _db() -> Path:
    from config.settings import DATABASE_URL
    p = Path(DATABASE_URL.replace("sqlite:///", ""))
    return p if p.is_absolute() else ROOT / p


def freeze(period_dir: Path, db: Path | None = None) -> Path:
    out = period_dir / "entry_scores.csv"
    if out.exists():
        raise FileExistsError(f"{out} already exists -- entry scores are frozen once")
    meta = json.loads((period_dir / "meta.json").read_text(encoding="utf-8"))
    calc_date = meta["calc_date"]
    books = []
    for name in ("incumbent_book.csv", "value_tilt_book.csv"):
        if (period_dir / name).exists():
            b = pd.read_csv(period_dir / name)[["ticker"]]
            b["book"] = name.replace("_book.csv", "")
            books.append(b)
    tickers = pd.concat(books, ignore_index=True)
    with closing(sqlite3.connect(db or _db())) as c:   # closes; `with connect()` does not
        have = {r[1] for r in c.execute("PRAGMA table_info(factor_scores)")}
        cols = [x for x in COLUMNS if x in have]
        q = (f"SELECT ticker, {', '.join(cols)} FROM factor_scores "
             f"WHERE calculation_date = ? AND ticker IN "
             f"({','.join('?' * tickers['ticker'].nunique())})")
        scores = pd.read_sql(q, c, params=[calc_date, *tickers["ticker"].unique()])
    if scores.empty:
        raise ValueError(f"no factor_scores rows at calc_date {calc_date}")
    df = tickers.merge(scores.drop_duplicates("ticker"), on="ticker", how="left")
    df.insert(0, "calc_date", calc_date)
    df.to_csv(out, index=False)
    return out


def main(argv: list[str] | None = None) -> int:
    from forward_test.snapshot import PERIOD
    ap = argparse.ArgumentParser(description="Freeze entry factor scores for a period.")
    ap.add_argument("--period", default=PERIOD)
    args = ap.parse_args(argv)
    try:
        out = freeze(ROOT / "forward_test" / args.period)
    except Exception as e:                                          # noqa: BLE001
        print(f"NOT WRITTEN: {type(e).__name__}: {e}")
        return 1
    df = pd.read_csv(out)
    missing = df[df["value_score"].isna()]["ticker"].tolist() if "value_score" in df else []
    print(f"wrote {out.relative_to(ROOT)}: {len(df)} rows "
          f"(value_score coverage {len(df) - len(missing)}/{len(df)})")
    if missing:
        print(f"  no value_score for: {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
