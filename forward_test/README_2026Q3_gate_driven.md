# Forward Test — Gate-Driven Book (2026 Q3)

**Created:** 2026-06-17
**Book selected at rebalance:** 2026-06-14
**Window:** 2026-06-17 → **2026-09-14** (≈ one quarter)
**Benchmark:** SPY (primary). IWM noted as a small/mid-cap reference since the
universe is US small/mid-cap.

## What is being tested

The **gate-driven portfolio** — stocks selected by the corrected fundamental
quality pipeline, gated at **f_score ≥ 5** on the repaired 0–9 Piotroski scale
(`config/settings.py: QUALITY_GATES["f_score_min"] = 5`). This is the component
that carries demonstrated value: all 13 holdings clear the gate.

## What is NOT being tested

The **ML overlay is frozen and explicitly UNPROVEN.** Leakage-free walk-forward
out-of-sample IC was sign-unstable and indistinguishable from zero:

| Test year | rank IC |
|---|---|
| 2022 | +0.025 |
| 2023 | +0.083 |
| 2024 | −0.040 |
| walk-forward mean | +0.023 |

(The production model's previously-cited +0.4575 was in-sample leakage, not skill.)
The production model `.pkl` files are **frozen** — not promoted, not unfrozen, not
retrained into production. Any predictive outcome of this forward test must be
attributed to the **gate**, not the model.

## Holdings (13) — all pass f ≥ 5

| Ticker | f_score | ROIC | quality_score |
|---|---|---|---|
| MCRI | 9 | 0.255 | 89.8 |
| MOV  | 9 | 0.074 | 76.5 |
| PRDO | 8 | 0.439 | 93.5 |
| FLXS | 8 | 0.188 | 69.5 |
| APEI | 8 | 0.270 | 83.8 |
| OII  | 7 | 0.138 | 61.8 |
| IDCC | 7 | 0.620 | 91.2 |
| CDE  | 7 | 0.085 | 73.9 |
| NATR | 6 | 0.309 | 81.3 |
| KMT  | 6 | 0.120 | 61.4 |
| UTMD | 6 | 0.319 | 76.6 |
| INSW | 5 | 0.197 | 71.5 |
| HRMY | 5 | 0.283 | 76.0 |

Weakest at the bar: INSW, HRMY (f = 5). Position sizing per the 2026-06-14
rebalance (Kelly-fractional, capped); the ML conviction overlay in sizing is
treated as cosmetic, not a validated signal.

## Rebalance cadence

Monthly (first trading day): expected rebalances ~2026-07-01, ~2026-08-01,
~2026-09-01 — i.e. **~3 rebalances** over the window. The book is refreshed by the
same gate-driven pipeline each month; it is not a static buy-and-hold of these 13.

## What a 2026-09-14 result WILL and WILL NOT prove

**WILL show (execution / plumbing sanity):**
- The end-to-end pipeline runs unattended over a real quarter (fetch → gate →
  construct → rebalance → track) without silent regressions (the f-score
  truncation guard and fetch-before-calculate ordering hold in production).
- That the gate produces an investable, liquid, sector-diversified book and that
  realized returns/turnover/costs are in a sane range vs SPY.
- Whether anything operationally breaks (data gaps, stale prices, gate misfires).

**WILL NOT prove (no edge confirmation):**
- That the strategy has **alpha.** One quarter with ~3 monthly rebalances is a
  single, tiny, noise-dominated sample. Beating or trailing SPY over ~90 days is
  statistically indistinguishable from luck — it does not validate or refute the
  factor edge.
- Anything about the **ML model**, which is disabled/unproven by construction.
- Out-of-sample factor IC — that requires many independent periods
  (walk-forward across years), not one forward quarter.

**Bottom line:** this is an **execution shakedown**, not an edge test. Treat a
favorable Sep 14 number as "the machine runs," never as "the strategy works."
