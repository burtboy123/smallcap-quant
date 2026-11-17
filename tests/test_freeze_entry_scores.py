"""tests/test_freeze_entry_scores.py — the entry-score freeze for the value-consistency check."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from forward_test.freeze_entry_scores import freeze  # noqa: E402


@pytest.fixture
def period(tmp_path):
    d = tmp_path / "2026Q4b"
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"calc_date": "2026-09-21"}))
    pd.DataFrame({"ticker": ["SM", "AMN"]}).to_csv(d / "incumbent_book.csv", index=False)
    pd.DataFrame({"ticker": ["SM", "XYZ"]}).to_csv(d / "value_tilt_book.csv", index=False)
    db = tmp_path / "q.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE factor_scores (ticker TEXT, calculation_date TEXT, "
                  "value_score REAL, quality_score REAL, composite_score REAL)")
        c.executemany("INSERT INTO factor_scores VALUES (?,?,?,?,?)", [
            ("SM", "2026-09-21", 80, 60, 78), ("AMN", "2026-09-21", 40, 70, 65),
            ("SM", "2026-10-21", 10, 10, 10)])                 # a later recalculation
    return d, db


def test_freezes_the_calc_date_scores_for_both_books(period):
    d, db = period
    df = pd.read_csv(freeze(d, db))
    assert set(df["calc_date"]) == {"2026-09-21"}
    sm = df[df.ticker == "SM"]
    assert list(sm["book"]) == ["incumbent", "value_tilt"]
    assert (sm["value_score"] == 80).all()                    # not the later 10
    assert df[df.ticker == "XYZ"]["value_score"].isna().all()  # missing stays visible


def test_refuses_to_overwrite(period):
    d, db = period
    freeze(d, db)
    with pytest.raises(FileExistsError):
        freeze(d, db)


def test_no_rows_at_calc_date_is_an_error(period):
    d, db = period
    (d / "meta.json").write_text(json.dumps({"calc_date": "2025-01-01"}))
    with pytest.raises(ValueError):
        freeze(d, db)
