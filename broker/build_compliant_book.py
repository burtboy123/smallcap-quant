"""
broker/build_compliant_book.py — build a §4-COMPLIANT target book.

The 2026Q4 tracked book cannot satisfy the pilot plan's §4 hard limits: 3 of its 15
names sit below the $5m/day ADV floor, and 7 of the remaining 12 are Energy (58%),
so with a 30% sector cap the largest compliant subset is 8 names — below the §4
minimum of 10. No amount of trimming fixes it; replacements must come from the wider
gate-passing universe.

Root cause: forward_test/build_q4_books.py replicated only the SELECTION path
(ensemble_rank -> TIER1/TIER2 -> top 15). Production's run_monthly_workflow() also
runs run_risk_check()/enforce_sector_limits() downstream, which is where sector and
liquidity constraints are applied. The replication skipped that stage.

This builds what production should emit: same gate (f>=5, composite_score NOT NULL)
and same composite ranking, with the §4 limits applied DURING selection —
  * ADV >= $5m/day          (liquidity floor)
  * sector <= 30% of book   (concentration)
  * >= 10 names             (minimum positions)
Limits are ENFORCED here, never relaxed; the kill-switch gate remains the final word.

Writes broker/compliant_book.csv. Does NOT touch forward_test/2026Q4/ — that test is
running with baselines locked on 2026-09-22 and must not be mutated mid-flight.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from broker.ibkr_paper import load_adv                       # noqa: E402
from broker.killswitch import (                               # noqa: E402
    MAX_SECTOR_WEIGHT, MIN_LIQUIDITY_ADV_USD,
)

N_BOOK = 15
OUT = Path(__file__).parent / "compliant_book.csv"


def main() -> int:
    conn = sqlite3.connect(f"file:{(ROOT/'quant_system.db').as_posix()}?mode=ro", uri=True)
    calc = pd.read_sql("SELECT MAX(calculation_date) d FROM factor_scores", conn).d.iloc[0]
    scores = pd.read_sql("""
        SELECT f.ticker, f.composite_score, f.f_score, s.sector
        FROM factor_scores f LEFT JOIN stocks s ON f.ticker = s.ticker
        WHERE f.calculation_date = ? AND f.composite_score IS NOT NULL
        ORDER BY f.composite_score DESC
    """, conn, params=[calc])
    conn.close()
    print(f"gate-passed universe @ {calc}: {len(scores)} names (f>=5)")

    # liquidity floor — ADV from local price history
    top = scores.head(400).copy()               # deep enough to fill 15 after filters
    adv = load_adv(top.ticker.tolist())
    top["adv_usd"] = top.ticker.map(adv)
    liquid = top[top.adv_usd >= MIN_LIQUIDITY_ADV_USD].copy()
    print(f"after ADV >= ${MIN_LIQUIDITY_ADV_USD/1e6:.0f}m/day: {len(liquid)} candidates")

    # greedy selection under the sector cap
    max_per_sector = int(N_BOOK * MAX_SECTOR_WEIGHT)          # 4 of 15 = 26.7% (5 -> 33%)
    picks, per_sector = [], {}
    for r in liquid.itertuples():
        sec = r.sector or "Unknown"
        if per_sector.get(sec, 0) >= max_per_sector:
            continue
        picks.append(r)
        per_sector[sec] = per_sector.get(sec, 0) + 1
        if len(picks) == N_BOOK:
            break

    book = pd.DataFrame([{
        "ticker": p.ticker, "composite_score": p.composite_score,
        "f_score": p.f_score, "sector": p.sector, "adv_usd": p.adv_usd,
    } for p in picks])
    book["weight_equal"] = round(1 / len(book), 4)
    book["book"] = "incumbent_minus_ml_risk_capped"

    # Use the SHARED guard rather than bespoke asserts, so this path cannot drift
    # from what the forward-test builders and the broker enforce.
    from broker.book_guard import assert_book_investable
    assert_book_investable(book, label="compliant_book.csv")
    w = book.groupby("sector").size() / len(book)

    book.to_csv(OUT, index=False)
    print(f"\n=== §4-COMPLIANT BOOK ({len(book)} names) ===")
    print(book.assign(adv_m=(book.adv_usd / 1e6).round(1))
              [["ticker", "composite_score", "f_score", "sector", "adv_m"]]
              .to_string(index=False))
    print(f"\nsector weights (cap {MAX_SECTOR_WEIGHT:.0%}):")
    for s, x in (w * 100).sort_values(ascending=False).items():
        print(f"  {s:<24} {x:5.1f}%")
    print(f"\nmin ADV in book: ${book.adv_usd.min()/1e6:.1f}m/day "
          f"(floor ${MIN_LIQUIDITY_ADV_USD/1e6:.0f}m)")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
