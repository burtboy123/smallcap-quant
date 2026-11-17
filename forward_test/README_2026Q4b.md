# Forward Test — 2026Q4b (replaces the retired 2026Q4)

**Created:** 2026-09-29 · **Start:** **2026-09-30** (next trading day) ·
**End:** ~2026-12-29 (≈90 days) · **Benchmark:** SPY · **Weighting:** equal-weight

> **PAPER ONLY.** No capital is committed. The real-money pilot
> (`docs/real_money_pilot_plan.md`) remains gated and unauthorized.

## Why this test exists

It replaces **2026Q4**, retired on 2026-09-29 after 5 sessions because it tracked a
book that could never have been held — Energy 46.69% against a 30% cap and three
names below the $5m/day ADV floor. The broker's kill-switch blocked **all 15 of 15**
of its orders. Full account: `2026Q4_retired/README_RETIRED.md`.

Losing ~1 week is better than spending a quarter measuring an unholdable book.

## Tracked book — Incumbent-minus-ML, risk-compliant

| | |
|---|---|
| Gate | `f_score >= 5` |
| Composite | 9-factor (0.22V / 0.27Q / 0.16M / …) |
| Ranking | `ensemble_rank` TIER1(20)+TIER2(10), widened with the ranked tail |
| Regime sector bias | applied (±5) — regime at construction `RISK_ON_GROWTH` |
| **ML overlay** | **DISABLED** (`ML_OVERLAY_ENABLED=False`) — hence *minus-ML* |
| Risk limits | applied **during** selection, then re-asserted |
| Holdings | 15, equal-weight 6.67% |

**SM, NUTX, AMN, BVS, TALO, TK, MOV, REPX, MYE, SSRM, CCSI, CDE, GPRE, SENEA, KODK**

### §4 compliance (verified, and asserted at build time)

| Sector | Weight | | Limit | Measured |
|---|---|---|---|---|
| Energy | 26.7% ← at cap | | sector ≤ 30% | **26.7%** ✅ |
| Healthcare | 20.0% | | ADV ≥ $5m/day | **$5.3m** min ✅ |
| Basic Materials | 20.0% | | position ≤ 15% | **6.67%** ✅ |
| Consumer Cyclical | 13.3% | | names ≥ 10 | **15** ✅ |
| Cons. Defensive / Industrials / Technology | 6.7% each | | | |

## How it is built — the durable fix

`build_q4_books.py` now runs the **full production path**:

```
f>=5 gate  →  regime sector bias (±5)  →  ensemble_rank  →  TIER1+TIER2
     →  select_under_limits()        §4 limits DURING selection, with backfill
     →  run_risk_check()             production risk stage (flags)
     →  enforce_sector_limits()      production post-hoc trim
     →  assert_book_investable()     hard guard — RAISES
```

The old version stopped after the selection step. Note that simply calling
production's risk stage would **not** have been enough either: `enforce_sector_limits()`
trims at most 3 per sector with no backfill, `run_risk_check()` only *reports*
concentration, and **neither applies a liquidity floor** — the $5m/day rule lived in
the pilot plan and the kill-switch but was absent from book construction. Hence the
limits are applied during selection and then re-asserted.

The TIER1/TIER2 pool (30 names) proved too shallow to fill 15 once the limits bite, so
it is widened with the ranked tail — still best-first under the same gate — rather than
relaxing any limit. 9 illiquid and 9 sector-full candidates were skipped.

## ⚠️ Intended weights vs actual paper fills — read this

**This forward test tracks INTENDED WEIGHTS (equal-weight 6.67%), not actual fills.**
That is deliberate: it makes this a clean test of **selection**, undistorted by
execution luck, partial fills, or the delayed-market-data limitation on the paper
account.

**The paper account is a separate EXECUTION record.** The two will not always agree,
and that is by design. As of 2026-09-29 they differ:

| | |
|---|---|
| Tracked (Q4b) | SM NUTX AMN BVS TALO TK MOV REPX MYE SSRM **CCSI CDE GPRE** SENEA KODK |
| Paper account holds | SM TALO TK NUTX* AMN BVS SSRM **ADAM MCS ZVRA** MOV* KODK MYE SENEA* REPX |

`*` MOV, NUTX and SENEA show **zero** position — unfilled on 2026-09-29 because their
limits were derived from ~15-minute-delayed quotes (no real-time subscription) and the
true market sat above them.

Cause of the name differences: the paper account still holds the **Phase-2** compliant
book, which was ranked on plain composite score with **no** regime sector bias and
**no** TIER structure. Q4b is the full production path, so the two constructions
legitimately differ on 3 names.

**Resolution:** the paper account is realigned to Q4b on **2026-09-30**, once the first
Q4b snapshot exists. It was *not* forced through on 09-29: the kill-switch's 5.3
staleness check correctly refused (`snapshot does not exist — never ran`), and the gate
was neither weakened nor repointed at the retired test's snapshot to obtain a pass. The
16 pending orders (including exits of ADAM, ZVRA, MCS) are recorded in commit
`4289eaa`.

> **Update 2026-10-04.** The realignment above did not happen on DUR222564: that
> account lost all market data on 2026-09-30 and was retired. The paper account is now
> **DU1234567**, built fresh from flat on 2026-10-01 (see `broker/RUNBOOK.md`). The
> quarter-end evaluation steps are in `2026Q4b/QUARTER_END_PLAN.md`.

## Challenger — PAPER REFERENCE ONLY

Value-tilt 0.70V/0.30Q on f≥3, direct ranking, no regime bias. 15 names, **no capital**.
Overlap with the tracked book: REPX, SM, TALO, TK.

> The Challenger reference book is **not** risk-constrained — it is a pure selection
> reference and is never traded, so the §4 limits do not apply to it. Do not send it
> to the broker; `assert_book_investable()` would reject it.

## Evaluation criteria

`2026Q4b/EVAL_CRITERIA.md` is **byte-identical** to Q3's and the retired Q4's
(sha256 `63f962fc3b5a2862…`), carried forward unchanged as instructed.

## Machinery

- `snapshot.py --period 2026Q4b` (also the `FT_PERIOD` default).
- **first-close baselines**, locked into `meta.json` on the first recorded session.
- Scheduled task `QuantForwardTestSnapshot` → `run_snapshot.bat` (defaults to
  `2026Q4b`). Verified 2026-09-29: exit 0, wrote `2026Q4b/snapshot_log.txt`, read 26
  unique constituents + SPY, correctly reported NOT STARTED. Row-writing separately
  proven against these exact books on a throwaway period (6 sessions, 27 tickers, 0
  carried-forward cells), which was then removed.
- `2026Q3_parallel` and `2026Q4_retired` are frozen and no longer snapshotted.

## Limits of what this can establish

Unchanged: ~3 rebalances, tiny effective n — **directional only**. It cannot confirm or
refute the value edge, cannot resolve bankruptcy/value-trap risk at a ~1%/yr base rate
in 15 names, and cannot assess tail behaviour unless a crash occurs. It can establish
execution sanity, a directional read, and — because it is now actually *investable* —
a comparison that could in principle be acted on.
