# 2026Q4 — RETIRED after 5 sessions (superseded by 2026Q4b)

**Ran:** 2026-09-22 → 2026-09-28 (5 recorded sessions) · **Retired:** 2026-09-29

> **This test tracked a book that could never have been held.** It is preserved, not
> deleted: the 5 days of recorded prices and equity curve are kept as history. Do not
> resume it, and do not cite its results as evidence about the strategy — the book was
> never investable, so its returns measure something that could not be traded.

## Why it was retired

The tracked Incumbent book breached three of the system's own §4 hard limits
(`docs/real_money_pilot_plan.md`, enforced by `broker/killswitch.py`):

| Violation | Measured | Limit |
|---|---|---|
| Energy sector concentration | **46.69%** | 30% |
| WEYS average daily dollar volume | **$0.90m/day** | $5m/day |
| HBB average daily dollar volume | **$1.69m/day** | $5m/day |
| ATNI average daily dollar volume | **$2.17m/day** | $5m/day |

Removing the three illiquid names and trimming Energy to the cap leaves only 8 names —
below the §4 minimum of 10. **No compliant subset of this book exists.**

Confirmed at the broker boundary on 2026-09-29: a dry run against paper account
DUR222564 had the kill-switch gate block **all 15 of 15 orders** (7 on the sector cap,
3 on the ADV floor, then the remaining 5 on the minimum-name-count cascade).

## Root cause

`forward_test/build_q4_books.py` replicated only the **selection** path
(`ensemble_rank` → TIER1/TIER2 → top 15) and skipped the downstream risk stage
entirely.

Worth recording: calling production's risk stage would **not** have prevented this
either. `enforce_sector_limits()` is a post-hoc trimmer capped at 3 removals per
sector with no backfill, `run_risk_check()` only *reports* concentration in its flags,
and **neither applies any liquidity floor** — the $5m/day rule existed in the pilot
plan and in the kill-switch, but was absent from book construction. The kill-switch
caught it only at submission; nothing stopped the book being written or tracked.

## What was fixed (2026-09-29)

1. **`broker/book_guard.py`** — one guard every book must pass before being tracked or
   traded. Raises `BookRiskViolation` rather than warning, with thresholds imported
   from `broker/killswitch.py` so they cannot drift. Missing sector or ADV data is a
   violation, not a pass.
2. **`build_q4_books.py`** now runs the full path: selection → `select_under_limits()`
   (limits applied *during* selection, with backfill) → `run_risk_check()` +
   `enforce_sector_limits()` → `assert_book_investable()`. Limits are never relaxed to
   make a book fit; if the pool cannot fill the book, it raises.
3. **`forward_test/2026Q4b/`** supersedes this test with a compliant book.

## The 5 sessions of history (kept)

| Date | Incumbent | Challenger | SPY |
|---|---|---|---|
| 2026-09-22 | −0.42% | −0.42% | −0.10% |
| 2026-09-23 | −1.03% | −3.37% | −0.82% |
| 2026-09-24 | +0.76% | −2.73% | −0.90% |
| 2026-09-25 | +0.66% | −2.83% | −0.37% |
| 2026-09-28 | +0.47% | −3.72% | −1.11% |

Files preserved as-is: `price_snapshots.csv` (135 rows, 27 tickers),
`equity_curves.csv`, both book CSVs, `meta.json`, `snapshot_log.txt`, and the
byte-identical `EVAL_CRITERIA.md`.

**The scheduled snapshot no longer writes here.** It records `2026Q4b`.
