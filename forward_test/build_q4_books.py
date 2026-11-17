"""
forward_test/build_q4_books.py — build the 2026Q4 forward-test books. READ-ONLY
against production: reads factor_scores / stocks, writes ONLY under
forward_test/2026Q4/. Does NOT touch portfolio_holdings, the model, or config.

INCUMBENT-minus-ML (the tracked book): runs the FULL production path --
    f_score>=5 gate (applied upstream in factor calc; composite_score NOT NULL)
    -> apply_regime_sector_bias(+/-5)
    -> ensemble_rank()
    -> TIER1(20) + TIER2(10) -> top 25                     [SELECTION]
    -> select_under_limits(): §4 limits applied DURING selection, with backfill
    -> run_risk_check() + enforce_sector_limits()           [production risk stage]
    -> assert_book_investable()                             [hard guard, raises]
with ML_OVERLAY_ENABLED=False throughout (hence "-minus-ML").

WHY THE RISK STAGE IS HERE (regression, 2026-09-29)
---------------------------------------------------
The first version of this script stopped after `-> top 15` and skipped the risk
stage entirely. The book it produced was UN-INVESTABLE under the system's own rules
— Energy 46.69% against a 30% cap, and WEYS/HBB/ATNI below the $5m/day ADV floor —
and it was tracked live for 7 days before the broker's kill-switch gate blocked all
15 of 15 orders. That test had to be retired (see forward_test/2026Q4_retired/).

Note that simply calling production's risk stage would NOT have prevented it:
`enforce_sector_limits()` is a post-hoc trimmer capped at 3 removals per sector with
no backfill, `run_risk_check()` only REPORTS concentration, and neither applies any
liquidity floor. So the limits are applied DURING selection (select_under_limits,
which backfills from the next-best candidate), production's stage is still run for
its flags/trim, and finally assert_book_investable() RAISES if anything is still
out of limits. Limits are never relaxed to make a book fit.

CHALLENGER (paper reference only, no capital, not the production construction):
value-tilt 0.70V/0.30Q on f>=3, direct ranking, no regime bias -- rebuilt as of the
same date so the Q3 tension (multi-year walk-forward vs one-quarter result) keeps
accumulating evidence.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from agents.orchestrator import apply_regime_sector_bias, enforce_sector_limits  # noqa: E402
from agents.risk_agent import run_risk_check                     # noqa: E402
from broker.book_guard import (                                  # noqa: E402
    assert_book_investable, load_adv, select_under_limits,
)
from factors.composite import ensemble_rank                      # noqa: E402
from config.settings import FACTOR_WEIGHTS, QUALITY_GATES, ML_OVERLAY_ENABLED  # noqa: E402

OUT = Path(__file__).parent / "2026Q4b"
N_BOOK = 15
CHALLENGER_VW, CHALLENGER_QW, CHALLENGER_FMIN = 0.70, 0.30, 3


def load_scores(conn, calc_date: str) -> tuple:
    scores = pd.read_sql("""
        SELECT ticker, composite_score, value_score, quality_score,
               momentum_score, f_score, roic
        FROM factor_scores
        WHERE calculation_date = ? AND composite_score IS NOT NULL
    """, conn, params=[calc_date])
    sectors = pd.read_sql(
        "SELECT ticker, sector FROM stocks WHERE sector IS NOT NULL", conn)
    return scores, sectors


def build_incumbent(scores: pd.DataFrame, sectors: pd.DataFrame, regime: dict,
                    adv_map: dict) -> pd.DataFrame:
    """FULL production path: selection -> §4 limits -> risk stage -> hard guard."""
    sec_map = sectors.set_index("ticker")["sector"].to_dict()

    # ── SELECTION (mirrors run_monthly_workflow) ────────────────────────────
    s = apply_regime_sector_bias(scores.copy(), sectors, regime=regime["regime"],
                                 bias_strength=5.0)
    s["passed_quality_gate"] = True
    ranked = ensemble_rank(s)
    tier1 = ranked[ranked["conviction_tier"] == "TIER1_STRONG_BUY"].head(20)
    tier2 = ranked[ranked["conviction_tier"] == "TIER2_VALUE_BUY"].head(10)
    cand = pd.concat([tier1, tier2])
    if len(cand) < 10:
        cand = ranked
    lookup = s.set_index("ticker")["composite_score"].to_dict()
    cand = cand.copy()
    cand["composite_score"] = cand["ticker"].map(lookup).fillna(0.0)
    cand["sector"] = cand["ticker"].map(sec_map)
    cand = cand.sort_values("composite_score", ascending=False)

    # The TIER pool can be too shallow to fill 15 names once the §4 limits bite.
    # Widen it with the rest of the ranked universe (still best-first, same gate)
    # rather than relaxing a limit — the guard refuses a short book outright.
    tail = ranked[~ranked["ticker"].isin(cand["ticker"])].copy()
    tail["composite_score"] = tail["ticker"].map(lookup).fillna(0.0)
    tail["sector"] = tail["ticker"].map(sec_map)
    pool = pd.concat([cand, tail.sort_values("composite_score", ascending=False)],
                     ignore_index=True)
    print(f"  candidate pool: {len(cand)} TIER1/TIER2 + {len(tail)} ranked tail")

    # ── §4 LIMITS APPLIED DURING SELECTION (with backfill) ──────────────────
    book = select_under_limits(pool, N_BOOK, adv_map=adv_map)
    skipped = book.attrs.get("skipped", {})
    print(f"  limits during selection: skipped {len(skipped.get('illiquid', []))} "
          f"illiquid, {len(skipped.get('sector_full', []))} sector-full")

    # ── PRODUCTION RISK STAGE (flags + post-hoc trim), as orchestrator runs it ──
    rc = book[["ticker", "composite_score"]].copy()
    rc["weight"] = 1 / len(book)
    try:
        report = run_risk_check(rc, regime)
        print(f"  run_risk_check: {report['status']}"
              + (f" flags={report['flags']}" if report.get("flags") else ""))
    except Exception as e:                       # never let reporting break the build
        print(f"  run_risk_check skipped ({type(e).__name__}: {e})")
    before = set(book["ticker"])
    # enforce_sector_limits() merges sector_data on ticker; passing a frame that
    # already carries `sector` yields sector_x/sector_y and a KeyError. Drop it for
    # the call and restore after (the same column-collision class of bug seen before).
    trimmed_book = enforce_sector_limits(
        book.drop(columns=["sector"], errors="ignore"), sectors, max_sector_pct=0.30)
    book = book[book["ticker"].isin(trimmed_book["ticker"])].reset_index(drop=True)
    trimmed = before - set(book["ticker"])
    if trimmed:
        print(f"  enforce_sector_limits trimmed: {sorted(trimmed)}")
        book["weight_equal"] = round(1 / len(book), 4)

    # ── HARD GUARD — raises if anything is still out of limits ──────────────
    assert_book_investable(book, label="2026Q4b incumbent-minus-ML", adv_map=adv_map)
    return book


def build_challenger(conn, calc_date: str) -> pd.DataFrame:
    """Paper reference: value-tilt 0.70V/0.30Q on f>=3, direct rank, no regime bias."""
    raw = pd.read_sql("""
        SELECT ticker, value_score, quality_score, f_score
        FROM factor_scores
        WHERE calculation_date = ? AND value_score IS NOT NULL AND quality_score IS NOT NULL
    """, conn, params=[calc_date])
    raw = raw[raw["f_score"] >= CHALLENGER_FMIN].copy()
    raw["composite_score"] = CHALLENGER_VW * raw["value_score"] + CHALLENGER_QW * raw["quality_score"]
    return raw.sort_values("composite_score", ascending=False).head(N_BOOK)


def main() -> int:
    calc_date = sys.argv[1] if len(sys.argv) > 1 else None
    start = sys.argv[2] if len(sys.argv) > 2 else None
    conn = sqlite3.connect(f"file:{(ROOT/'quant_system.db').as_posix()}?mode=ro", uri=True)
    if calc_date is None:
        calc_date = pd.read_sql(
            "SELECT MAX(calculation_date) d FROM factor_scores", conn).d.iloc[0]
    scores, sectors = load_scores(conn, calc_date)

    assert ML_OVERLAY_ENABLED is False, "ML overlay must be disabled for this book"
    assert QUALITY_GATES["f_score_min"] == 5, "expected the restored f>=5 Incumbent gate"
    assert scores["f_score"].min() >= 5, "gate not applied upstream in factor calc"

    from agents.macro_agent import get_current_regime
    try:
        regime = get_current_regime()
    except Exception as e:
        regime = {"regime": "NEUTRAL"}
        print(f"  regime detection failed ({e}) — defaulting NEUTRAL", flush=True)

    adv_map = load_adv(scores["ticker"].tolist())
    print(f"  ADV loaded for {len(adv_map)}/{len(scores)} gate-passed names")

    inc = build_incumbent(scores, sectors, regime, adv_map)
    ch = build_challenger(conn, calc_date)
    conn.close()

    OUT.mkdir(parents=True, exist_ok=True)
    sec_map = sectors.set_index("ticker")["sector"].to_dict()
    inc_out = inc[["ticker", "composite_score"]].copy()
    inc_out["f_score"] = inc["ticker"].map(scores.set_index("ticker")["f_score"])
    inc_out["sector"] = inc_out["ticker"].map(sec_map)
    inc_out["adv_usd"] = inc_out["ticker"].map(adv_map)
    inc_out["weight_equal"] = round(1 / len(inc_out), 4)
    inc_out["book"] = "incumbent_minus_ml"
    # Final guard on the EXACT frame that gets written to disk.
    assert_book_investable(inc_out, label="2026Q4b incumbent (as written)",
                           adv_map=adv_map)
    inc_out.to_csv(OUT / "incumbent_book.csv", index=False)

    ch_out = ch[["ticker", "composite_score", "f_score"]].copy()
    ch_out["sector"] = ch_out["ticker"].map(sec_map)
    ch_out["weight_equal"] = round(1 / len(ch_out), 4)
    ch_out["book"] = "challenger_value_tilt_paper_reference"
    ch_out.to_csv(OUT / "value_tilt_book.csv", index=False)

    meta = {
        "start": start or "TBD (first trading day after approval)",
        "end": "~90 days after start",
        "benchmark": "SPY",
        "baseline_mode": "first_close",
        "_baseline_note": "baselines = each name's close on the first recorded "
                          "trading day >= start, captured by snapshot.py. Avoids the "
                          "Q3 intraday/close mismatch that made day 1 read -1.9%.",
        "calc_date": calc_date,
        "regime": regime["regime"],
        "incumbent_n": len(inc_out),
        "challenger_n": len(ch_out),
        "weighting": "equal-weight (isolates selection, not sizing)",
        "ml_overlay_enabled": ML_OVERLAY_ENABLED,
        "incumbent_construction": {
            "label": "Incumbent-minus-ML",
            "f_score_min": QUALITY_GATES["f_score_min"],
            "factor_weights": FACTOR_WEIGHTS,
            "ranking": "ensemble_rank TIER1(20)+TIER2(10) -> select_under_limits "
                       "-> run_risk_check + enforce_sector_limits -> assert_book_investable",
            "regime_sector_bias": "applied (+/-5)",
            "risk_limits_enforced": {
                "sector_max": 0.30, "adv_min_usd": 5_000_000,
                "position_max": 0.15, "min_positions": 10,
                "applied": "DURING selection, with backfill; never relaxed",
            },
        },
        "challenger_construction": {
            "label": "value-tilt (PAPER REFERENCE ONLY — no capital)",
            "f_score_min": CHALLENGER_FMIN,
            "weights": {"value": CHALLENGER_VW, "quality": CHALLENGER_QW},
            "ranking": "direct composite sort", "regime_sector_bias": "off",
        },
        "overlap": sorted(set(inc_out.ticker) & set(ch_out.ticker)),
    }
    (OUT / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print(f"calc_date={calc_date}  regime={regime['regime']}  ML={ML_OVERLAY_ENABLED}")
    print(f"\nINCUMBENT-minus-ML ({len(inc_out)}): {sorted(inc_out.ticker)}")
    print(f"CHALLENGER ref     ({len(ch_out)}): {sorted(ch_out.ticker)}")
    print(f"overlap: {meta['overlap']}")

    print("\n=== §4 COMPLIANCE OF THE WRITTEN BOOK ===")
    w = inc_out.groupby("sector").size() / len(inc_out)
    for sec, x in w.sort_values(ascending=False).items():
        print(f"  {sec:<24} {x:6.1%}{'  <- at cap' if x > 0.26 else ''}")
    print(f"  min ADV ${inc_out.adv_usd.min()/1e6:.1f}m/day | "
          f"position {1/len(inc_out):.2%} | names {len(inc_out)}")

    prev = Path(__file__).parent / "2026Q4_retired" / "incumbent_book.csv"
    if prev.exists():
        old = set(pd.read_csv(prev).ticker)
        kept = old & set(inc_out.ticker)
        print(f"\nvs the RETIRED (un-investable) 2026Q4 book: kept {len(kept)}/{len(old)}")
        print(f"  kept   : {sorted(kept)}")
        print(f"  dropped: {sorted(old - set(inc_out.ticker))}")
        print(f"  new    : {sorted(set(inc_out.ticker) - old)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
