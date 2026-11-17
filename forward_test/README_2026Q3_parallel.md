# Forward Test — Parallel Head-to-Head (2026 Q3)

**Created:** 2026-06-17
**Window:** 2026-06-17 → **2026-09-14**
**Benchmark:** SPY (baseline 750.57 on 2026-06-17)
**Weighting for comparison:** equal-weight both books (isolates *selection*, not sizing)

## What this is

A **head-to-head trial of two constructions, run in parallel** — capital is **NOT**
flipped. The incumbent live book continues; the challenger is tracked on paper so
September gives an evidence-based comparison before any capital commitment.

| | Incumbent | Challenger |
|---|---|---|
| Name | gate + quality (legacy) | value-tilt (validated) |
| Selection | f≥5 gate, 9-factor composite, ML overlay, ensemble screens | f≥3 junk floor, 0.70V/0.30Q composite, **no ML**, direct ranking |
| Regime sector bias | applied (±5) | **off** |
| Holdings | 13 | 15 |
| Overlap | **PRDO only** (1 name) — 92% different books |

## Construction rationale (challenger)

Settled by a leakage-free walk-forward study (effective n ~9–13/yr — directional):
- **Value is the engine.** Full-universe rank IC +0.124, top-bottom LS +6.78% at 90d;
  the one independent, sign-stable signal. Quality is a modest secondary; momentum
  is dead; the 5 aux factors had 0% historical coverage.
- **f≥5 gate demoted to f≥3.** f≥5 *halved* the value IC (+0.124→+0.047) and was
  strictly worse on risk (maxDD −17.8% vs −16.0%, Sharpe 0.64 vs 1.60) — signal
  destruction with a risk penalty. f≥3 is a light junk floor (drops the Piotroski
  0–2 distress band) and a deliberate hedge against bankruptcy-band names. *(The hedge
  rationale is now measured rather than assumed, and it holds: unprofitability carries a
  ~14× bankruptcy gradient, the second-strongest driver after size —
  `docs/survivorship_findings.md`.)*
- **ML overlay disabled** (worst config, OOS IC +0.015) — model frozen, `.pkl` retained.
- **Regime sector bias OFF** — it is a curve-fit-labeled overlay (would re-import an
  unvalidated rule and contaminate this test).

## Challenger book (15, value-tilt, equal-weight)
NLOP, RIGL, REPX, CMRE, STNE, NRDS, NUS, CRMD, CNX, CMCL, EBS, SUNS, **PRDO**, CALM, YELP.
Sectors all ≤30% (Consumer Defensive 20%, Healthcare 20%, Energy/Comm/Real Estate 13% each, …).
Sentiment filter removed PAGS, BMBL, REFI, CABO, SMPL — notably the marginal f=4 names
the f≥3 relaxation admitted were screened out downstream anyway.

## Incumbent book (13, equal-weight for comparison)
APEI, CDE, FLXS, HRMY, IDCC, INSW, KMT, MCRI, MOV, NATR, OII, PRDO, UTMD.

## Open items / caveats
- **Kelly sizing without ML produced 4 positions at 17.3%** (> the 15% `max_position`
  cap) in the challenger's native sizing — flagged for review. This forward test uses
  **equal-weight** so sizing is not a confound; the cap-breach must be fixed before any
  Kelly-sized live deployment of the challenger.
- One quarter / ~3 monthly rebalances is **not** an edge test — it is an execution +
  directional comparison. A favorable challenger result is necessary, not sufficient,
  for capital commitment. ~~Survivorship bias still hides bankruptcy-band risk.~~
  *(Revised 2026-09-06: bankruptcy base rates are now measured — ~1.19%/yr at the smallest
  float, `docs/survivorship_findings.md`. A 15-name quarter should see ~0 bankruptcies
  regardless, so this quarter cannot resolve bankruptcy-band risk on **sample-size**
  grounds, not survivorship-blindness.)*

## Files
- `2026Q3_parallel/incumbent_book.csv`, `value_tilt_book.csv`, `meta.json`
- Returns to be computed from `baseline_price_2026_06_17` vs price at evaluation date.

**Status:** challenger ON TRIAL vs incumbent. No capital flipped; production model frozen.
