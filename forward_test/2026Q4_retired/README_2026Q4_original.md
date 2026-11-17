# Forward Test — 2026 Q4 (second paper quarter)

**Created:** 2026-09-21 · **Start:** **2026-09-22** (first trading day after approval)
**End:** ~2026-12-21 (≈90 days) · **Benchmark:** SPY
**Weighting:** equal-weight both books (isolates *selection*, not sizing)

> **PAPER ONLY. No capital is committed in this quarter.** The real-money pilot
> (`docs/real_money_pilot_plan.md`) remains gated and unauthorized.

## Why this quarter exists

The 2026Q3 parallel test returned **KEEP INCUMBENT**
(`2026Q3_parallel/EVALUATION_RESULT.md`, commit `b69c444`) — but on a **+2.53 pp margin
that sat inside the ±3 pp noise band**, with a **negative** value-consistency check, and
in a window containing **no crash regime**. The evaluation explicitly recorded that the
Incumbent was kept *by rule, not proven better*.

Q4 therefore keeps accumulating evidence on the tension that verdict left open: a
**multi-year walk-forward study** (which favoured the value tilt) versus a
**one-quarter live result** (which did not).

## Tracked book — "Incumbent-minus-ML"

| | |
|---|---|
| Label | **Incumbent-minus-ML** |
| Gate | `f_score >= 5` (restored, commit `42a6daf`) |
| Composite | 9-factor: 0.22V / 0.27Q / 0.16M / 0.11 sent / 0.07 SI / 0.04 ins / 0.03 ES / 0.05 52w / 0.05 rev |
| Ranking | `ensemble_rank()` → TIER1(20) + TIER2(10) → top 25 → top 15 |
| Regime sector bias | **applied (±5)** — regime at construction: `RISK_ON_GROWTH` |
| **ML overlay** | **DISABLED** (`ML_OVERLAY_ENABLED = False`) — hence *minus-ML* |
| Holdings | 15 |

### Why "minus-ML" — an honest label, not a cosmetic one

The historical record disagrees with itself about whether the Q3 Incumbent's *selection*
used the ML overlay. `README_2026Q3_parallel.md` lists "ML overlay" among its selection
inputs; `README_2026Q3_gate_driven.md` (the book-lock doc, same day) says the overlay is
"frozen and explicitly UNPROVEN" and that *"any predictive outcome of this forward test
must be attributed to the **gate**, not the model."*

The code record is decisive on one point: `ML_OVERLAY_ENABLED` **did not exist** when the
Q3 Incumbent was selected on 2026-06-14 — it was introduced days later as part of the
disable. Before it existed, `rank_universe()` attempted ML scoring unconditionally and,
on success, **overwrote `composite_score` outright**. So ML may well have influenced
which 13 names were picked. Sizing is moot (Q3 was equal-weighted by design).

This book is built with **ML off**, by decision. It is therefore labelled
**Incumbent-minus-ML** rather than "Incumbent", because it is not provably the same
construction that won.

## ⚠️ The rebuilt book reproduces only **2 of the original 13** names

| | |
|---|---|
| Reproduced (2/13) | **INSW, MOV** |
| Dropped (11) | APEI, CDE, FLXS, HRMY, IDCC, KMT, MCRI, NATR, OII, PRDO, UTMD |
| New (13) | AMN, ATNI, BVS, HBB, NBR, NUTX, REPX, SENEA, SM, TALO, TK, TNK, WEYS |

**Read this carefully: Q4 tests the Incumbent *construction*, not the winning *book*.**
Four things moved at once, and they are not separable from one quarter of data:

1. **~3 months and ~3 rebalance cycles of natural turnover.** The Q3 gate-driven README
   said so itself: *"the book is refreshed by the same gate-driven pipeline each month;
   it is not a static buy-and-hold of these 13."*
2. **The ML overlay is now off** (see above) — it may have shaped the original selection.
3. **The regime changed** to `RISK_ON_GROWTH`, which applies +5 to Consumer Cyclical /
   Healthcare / Industrials / Technology and −5 to Consumer Defensive.
4. **Fundamentals and prices were refreshed** after being 3.5 months stale (see below).

A 2/13 carryover is *not* evidence the restore failed — the gate, weights, ranking and
bias all verify as restored. But it does mean **Q4 cannot be read as "the winner
continued."** It is a fresh book from the restored construction.

## Challenger — PAPER REFERENCE ONLY

| | |
|---|---|
| Purpose | Reference series only. **No capital. Not the production construction.** |
| Gate | `f_score >= 3` |
| Composite | value-tilt 0.70V / 0.30Q, direct ranking |
| Regime sector bias | off |
| Holdings | 15 |

Rebuilt as of the same date so the Q3 tension keeps gathering evidence rather than going
cold. Overlap with the tracked book: **REPX, SM, TALO, TK** (4 names).

## Evaluation criteria

`2026Q4/EVAL_CRITERIA.md` is a **byte-identical copy** of the Q3 criteria
(sha256 `63f962fc3b5a2862…`), carried forward **unchanged** as instructed. Its §2 decision
rule, §3 tail/survival prerequisite and noise band all apply as written. Note that its
Incumbent/Challenger framing is inherited from Q3; the tracked book here is
Incumbent-**minus-ML**.

## Data integrity — two defects found and fixed before this book was built

Both would have silently corrupted the Q4 selection:

1. **Prices were 3.5 months stale** (latest 2026-06-01). A factor calculation dated
   2026-09-21 was already running against them. Value factors use the *latest* price, and
   **momentum 12-1 coverage was 0.0%** — with the restored 9-factor weights that silently
   renormalizes a 0.16-weight factor out of the composite entirely. The run was
   **killed before it stored anything**, prices refreshed (502,485 rows, now through
   2026-09-18), and the calculation re-run. Momentum coverage went **0.0% → 95.2%**.
2. **Prior-year fundamentals were too sparse** for a full 0–9 Piotroski score (64% < the
   80% floor). The `953e6cf` truncation guard **refused to compute** — correctly, and
   doubly important now that `f>=5` is the gate. Fundamentals were refreshed (3,713 rows)
   and the guard then passed.

This is the pilot plan's §5.3 concern in miniature, and the f-score truncation precedent
repeating: *never trade on stale or unverified data.*

## Verification of the restored construction

| Check | Result |
|---|---|
| `f_score_min` | **5** ✅ |
| Min f_score among gate-passed names | **5** (0 names with f<5 carry a composite) ✅ |
| Universe scored / gate-passed | 1,296 / **524** |
| `FACTOR_WEIGHTS` | 9-factor, sums to **1.0** ✅ |
| Coverage of all 9 factors among gate-passed | **100%** each ✅ |
| `ensemble_rank` TIER1/TIER2 | restored ✅ |
| Regime sector bias | applied, `RISK_ON_GROWTH` ✅ |
| `ML_OVERLAY_ENABLED` | **False** ✅ |

## Machinery

`snapshot.py` is now **period-parameterized** (`--period`, or `FT_PERIOD`), with a
`PERIODS` registry. Q3 behaviour is unchanged and byte-compatible.

- **Baselines:** Q4 uses `baseline_mode = "first_close"` — each name's close on the first
  recorded trading day, locked into `meta.json` on first run. This fixes a real Q3 flaw:
  Q3's baselines were captured intraday, so its equity curve read **−1.9% on day one**
  before any market move. Q4 starts at exactly 1.000.
- **Scheduled task:** `QuantForwardTestSnapshot` runs `run_snapshot.bat`, which now
  defaults to `2026Q4`. Verified by firing the task: exit 0, wrote to
  `2026Q4/snapshot_log.txt`, read 26 unique constituents + SPY.
- **2026Q3_parallel is concluded and frozen** — no longer snapshotted.

## What this quarter can and cannot establish

Unchanged from Q3, and worth restating: ~3 rebalances and a tiny effective n make this
**directional only**. It cannot confirm or refute the value edge, cannot resolve
bankruptcy/value-trap risk at a ~1%/yr base rate in 15 names, and cannot assess tail
behaviour unless a crash actually occurs. What it *can* establish is execution sanity, a
second directional read, and — because the Challenger runs alongside — whether Q3's
negative value-consistency repeats or reverses.
