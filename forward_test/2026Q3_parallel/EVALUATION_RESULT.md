# September Evaluation Result — Incumbent vs Challenger

**Evaluated:** 2026-09-21 · **Window:** 2026-06-17 → **2026-09-14** (the locked evaluation
date; later recorded days exist in the snapshot and were **excluded**)
**Criteria applied:** `EVAL_CRITERIA.md`, locked 2026-06-17 (return criteria) and
2026-07-02 (§3 tail/survival dimension). **No threshold was reinterpreted or softened.**

> **EVALUATION ONLY. No action taken.** Production model remains frozen. No book change,
> no promotion, no capital move, no hardening applied. The verdict below is a reading of
> the locked rule, not an instruction that has been executed.

---

## VERDICT: **KEEP INCUMBENT**

Triggered by **two independent** clauses of §2(b) — not a single marginal call:

- **(b)2 — value-consistency check NEGATIVE** (rank IC **−0.164**). The value thesis did
  not show this quarter. This is a *hard* trigger: "do not adopt regardless of headline
  return." It would have blocked adoption even if the Challenger had won on return.
- **(b)4 — tail tie-breaker.** Spread is **−2.53 pp**, inside the ±3 pp noise band; per
  the pre-committed rule a near-tie resolves *against* the strictly-worse-tail book.

§2(c)1 (inconclusive: spread within ±3 pp) also triggers. The criteria anticipated this
overlap and resolve it explicitly: *"'Inconclusive' therefore means stay on the Incumbent
and keep watching, not 'hold the Challenger.'"* Both paths converge on the same
operational outcome: **the book stays Incumbent.**

---

## 1. Data integrity (checked before any metric was computed)

| Check | Result |
|---|---|
| Window coverage | **61 trading days**, 2026-06-17 → 2026-09-14 ✅ |
| Calendar gaps | **None.** 64 business days − 3 = 61; the 3 absences are exactly US market holidays: **Jun 19** (Juneteenth), **Jul 3** (Independence Day observed), **Sep 7** (Labor Day) ✅ |
| Ticker coverage | **28/28 present on every single day** (1,708 rows = 61 × 28); 13 Incumbent + 15 Challenger + SPY, PRDO overlapping ✅ |
| Carried-forward cells | **0** across the entire run (`is_carried` = 0 for every row; snapshot log confirms "0 carried-forward cells" on all 65 runs) ✅ |
| Delistings | **None.** A delisting would freeze a price and set `is_carried=1`; that never occurred ✅ |
| Null / non-positive prices | 0 / 0 ✅ |
| **Backfill needed?** | **No.** Data already ran through 2026-09-18, past the eval date. `snapshot.py` was **not** re-run. |
| One revised trading day | **2026-07-01 only** — see below. Disclosed, benign, does not affect the verdict ⚠️ |

**Disclosed data revision (2026-07-01).** The uncommitted snapshot data contained one
*revised* — not merely appended — trading day. Comparing the previously committed file
against the current one, exactly **28 cells changed, all on 2026-07-01** (every ticker,
including SPY); **no other date in the entire overlap differs.** Changes are small and
mixed-sign (mean |Δ| 0.64%, max 1.66%), which is the signature of a snapshot run that
captured **provisional intraday prices** and was later corrected to the **official
closes** by an idempotent re-run — the mechanism working as designed, not corruption.
The evaluation uses the corrected (settled-close) data. Impact on the verdict is nil:
2026-07-01 is a mid-window day whose drawdown (−0.26%) is nowhere near either book's
maximum, and endpoint returns are computed from 2026-09-14 prices against entry
baselines, which are untouched.

**Independent recomputation cross-check.** Rather than trusting `equity_curves.csv`, all
metrics were recomputed from raw `price_snapshots.csv` against the book baseline prices.
They match the recorded curve to **0.00 bp** on all three return series and both drawdowns:

| Series | Recomputed | Recorded | Diff |
|---|---|---|---|
| Incumbent return | +3.7303% | +3.7303% | 0.00 bp |
| Challenger return | +1.2034% | +1.2034% | 0.00 bp |
| SPY return | +1.3736% | +1.3736% | 0.00 bp |
| Incumbent maxDD | −4.2433% | −4.2433% | — |
| Challenger maxDD | −5.2825% | −5.2825% | — |

Continuity with the stated Aug-14 interim reading is exact (Incumbent +3.58%, Challenger
−1.75%, SPY +3.43%).

*One note on 12 tickers showing a repeated close on consecutive days (max 3 occurrences,
NLOP): these are genuine unchanged closes in thin small caps, not stale fills —
`is_carried=0` and the snapshot fetched fresh data on each of those days.*

---

## 2. Final metrics — full window to 2026-09-14

Computed per §1 of the criteria: equal-weight mean of `price_0914 / baseline − 1`, using
the baseline prices in the book CSVs and SPY baseline **750.57**.

| Metric | Incumbent (13) | Challenger (15) | SPY |
|---|---|---|---|
| **Total return** | **+3.73%** | **+1.20%** | +1.37% |
| **Alpha vs SPY** | **+2.36 pp** | **−0.17 pp** | — |
| **Max drawdown** (daily path) | **−4.24%** | **−5.28%** | −3.38% |
| **Hit rate — positive** | 8/13 = **61.5%** | 6/15 = **40.0%** | — |
| **Hit rate — beat SPY** | 8/13 = **61.5%** | 6/15 = **40.0%** | — |
| Worst 1-day move | −2.05% | −2.34% | −1.52% |
| Annualized daily vol | 15.4% | 16.0% | 11.5% |

- **Relative spread (Challenger − Incumbent): −2.53 pp** (Incumbent ahead)
- **Max-drawdown gap: Challenger 1.04 pp deeper** (−5.28% vs −4.24%)

**The spread was stable all quarter — it never left the noise band in either direction:**

| Through | Incumbent | Challenger | SPY | Spread |
|---|---|---|---|---|
| 2026-06-30 | +1.70% | −0.73% | −0.51% | −2.43 pp |
| 2026-07-31 | +2.76% | +0.14% | −0.47% | −2.62 pp |
| 2026-08-31 | +4.07% | +1.64% | +2.20% | −2.43 pp |
| **2026-09-14** | **+3.73%** | **+1.20%** | **+1.37%** | **−2.53 pp** |

**Per-name returns**

- **Incumbent:** INSW +30.7%, OII +27.3%, FLXS +26.2%, HRMY +21.2%, IDCC +17.6%,
  UTMD +5.1%, CDE +3.4%, PRDO +3.3%, MCRI −4.0%, MOV −11.7%, APEI −16.2%, KMT −20.9%,
  NATR −33.6%
- **Challenger:** RIGL +33.1%, REPX +27.3%, CMCL +16.8%, NRDS +11.3%, CNX +6.7%,
  PRDO +3.4%, CMRE −1.9%, YELP −3.6%, CALM −4.8%, NUS −5.0%, NLOP −6.9%, CRMD −10.3%,
  STNE −12.0%, SUNS −13.6%, EBS −22.4%

---

## 3. Value-consistency check — the decisive measurement

Per §1: Spearman rank correlation between each name's **entry `value_score`** (from
`factor_scores` at 2026-06-01, the book-construction snapshot) and its **realized quarter
return**, across the combined gated universe (the union of both gated books, n = 27;
`value_score` coverage 27/27).

> **Rank IC = −0.1642** (p = 0.413, n = 27) → **NEGATIVE. Cheap underperformed.**

Mean realized return by entry-`value_score` tercile:

| Entry value_score tercile | Mean return | n |
|---|---|---|
| Lowest (most expensive) | +3.48% | 9 |
| Middle | +8.41% | 9 |
| **Highest (cheapest)** | **−4.87%** | 9 |

Within each book separately:

| Sub-sample | n | Rank IC | p |
|---|---|---|---|
| Incumbent-only | 13 | +0.247 | 0.415 |
| **Challenger-only** | 15 | **−0.632** | **0.011** |

**The cheapest names in the Challenger book were systematically its worst performers this
quarter** — the one statistically significant number in this evaluation. The Challenger's
value tilt did not merely fail to pay; it ran backwards over the window.

*Scope note: the criteria's "combined gated universe" is computed on the 27 union-of-books
names because that is the only set with recorded prices over the window — the production
DB holds no prices after 2026-06-10 (pipeline frozen), so the broader ~600-name gated
universe is not computable. This is the criteria's intended reading, not a substitution.*

---

## 4. Criterion-by-criterion — applied exactly as written, in order

### §2 Hard prerequisites (gate everything below)

| # | Prerequisite | Threshold | Measured | Result |
|---|---|---|---|---|
| P1 | Value-consistency positive — win must trace to the thesis, not 1–2 lucky names | rank IC **> 0** | **−0.164** | ❌ **FAIL** |
| P2 | Survival weighed, not just return; noise-band win cannot justify a worse-tail book | n/a — Challenger did not win | spread **−2.53 pp** | ❌ **N/A — no win to credit** |

### §2(a) ADOPT Challenger — requires **ALL** five

| # | Criterion | Threshold | Measured | Result |
|---|---|---|---|---|
| a1 | Return margin over Incumbent | **≥ +3.0 pp** | **−2.53 pp** | ❌ **FAIL** |
| a2 | Value-consistency positive | rank IC **> 0** | **−0.164** | ❌ **FAIL** |
| a3 | Max drawdown not materially worse | ≤ **+5 pp** deeper **and** no breach below **−25%** | **1.04 pp** deeper; −5.28% (no breach) | ✅ PASS |
| a4 | Does not badly trail SPY | alpha **≥ −3 pp** | **−0.17 pp** | ✅ PASS |
| a5 | Tail acceptable given size of return win ("a marginal win does not clear this bar on its own") | requires large, thesis-consistent win | **no win at all**; thesis negative | ❌ **FAIL** |

→ **ADOPT requires all five. 3 of 5 FAIL. ADOPT is not available.**

### §2(b) KEEP Incumbent — triggers on **ANY** one

| # | Criterion | Threshold | Measured | Result |
|---|---|---|---|---|
| b1 | Incumbent return margin | **≥ +3.0 pp** | **+2.53 pp** | ➖ not triggered (2.53 < 3.0) |
| b2 | **Value-consistency negative** | rank IC **< 0** | **−0.164** | 🔴 **TRIGGERED** |
| b3 | Challenger drawdown worse by > +5 pp, or breaches −25% | > +5 pp / −25% | 1.04 pp deeper; no breach | ➖ not triggered |
| b4 | **Tail tie-breaker** — near-tie spread within ±3 pp, Challenger has strictly worse left tail (§3) | \|spread\| **< 3.0 pp** | **−2.53 pp** | 🔴 **TRIGGERED** |

→ **KEEP INCUMBENT triggered twice, independently.**

### §2(c) INCONCLUSIVE — extend parallel test, no capital move

| # | Criterion | Threshold | Measured | Result |
|---|---|---|---|---|
| c1 | Spread inside noise band | within **±3.0 pp** | **−2.53 pp** | 🟡 TRIGGERED |
| c2 | Mixed signal (Challenger wins return but worse DD / flat-negative value check) | — | Challenger did not win return | ➖ not triggered |
| c3 | Difference driven by 1–2 idiosyncratic names (drop top/bottom; inconclusive if spread flips sign or falls below 3 pp) | sign flip or \|spread\| < 3 pp | see below — **no sign flip** in any perturbation; spread already < 3 pp at baseline | 🟡 TRIGGERED (trivially — baseline already inside band) |

**c3 drop-one sensitivity** (spread stays negative — Incumbent ahead — in every case):

| Perturbation | Spread | Sign flip? |
|---|---|---|
| baseline | −2.53 pp | — |
| drop Incumbent best (INSW +30.7%) | −0.28 pp | no |
| drop Incumbent worst (NATR −33.6%) | −5.64 pp | no |
| drop Challenger best (RIGL +33.1%) | −4.80 pp | no |
| drop Challenger worst (EBS −22.4%) | −0.84 pp | no |
| drop both books' best | −2.55 pp | no |
| drop both books' worst | −3.95 pp | no |

The Incumbent's edge is **not** an artifact of one name: removing the Incumbent's single
best holding still leaves the Challenger behind (−0.28 pp), and removing the Challenger's
worst still leaves it behind (−0.84 pp). The result is directionally robust to
single-name removal — though the *magnitude* swings widely (−0.28 to −5.64 pp), which is
itself a reminder of how little a 13–15 name, one-quarter sample can resolve.

### Resolving the (b)/(c) overlap — as the criteria pre-specify

Both KEEP (b2, b4) and INCONCLUSIVE (c1, c3) trigger. `EVAL_CRITERIA.md` §2 anticipated
this exact case:

> *"extending the trial is the default when no capital decision is forced. But the book is
> **never flipped to the Challenger on a noise-band result** — if a decision is forced, a
> near-tie resolves to **KEEP Incumbent** per (b)4… 'Inconclusive' therefore means stay on
> the Incumbent and keep watching, not 'hold the Challenger.'"*

Both branches therefore produce the same operational state: **stay on the Incumbent.**
The distinction is only whether the parallel test continues — and that is the user's
call, not this evaluation's.

---

## 5. Verdict and the tension in the numbers

**The locked rule produces: KEEP INCUMBENT.**

**Do the rule and the raw numbers point different ways? No — they agree in direction.**
The Incumbent finished ahead on every measured dimension: return (+3.73% vs +1.20%),
alpha (+2.36 pp vs −0.17 pp), max drawdown (−4.24% vs −5.28%), and hit rate (61.5% vs
40.0%). There is no case here where the rule overrides a Challenger win.

**But two honest tensions are worth stating, and neither changes the verdict:**

1. **"Incumbent won" is not established either.** Its 2.53 pp lead is *inside* the same
   ±3 pp noise band that denies the Challenger a win. The Incumbent is kept **by rule**
   — as the pre-committed default on a near-tie, and because the value thesis failed —
   **not because it was proven superior.** Reading this as "the Incumbent is the better
   book" over-reads a coin-flip on return.
2. **The Challenger's feared tail did not show up — and that proves nothing.** It passed
   the drawdown criteria comfortably (a3 ✅: 1.04 pp deeper, no −25% breach), and its
   realized vol was only modestly above the Incumbent's (16.0% vs 15.4%). But SPY never
   drew down more than **−3.38%** in this window: **there was no crash regime.** §3's
   caveat 2 is explicit — *"Absence of a large drawdown by September is NOT evidence the
   tail risk isn't there."* The fragile-cluster co-movement risk (SUNS/NLOP real-estate
   credit, CMRE shipping, CMCL mining, REPX/CNX energy) was **never stress-tested.** The
   tail criteria passing this quarter is an untested pass, not a clean bill of health.

The single most decisive finding is **not** the return spread — it is the **negative
value-consistency check**. §2's hard prerequisite exists precisely so that a Challenger
result is only credited when it traces to the value thesis. This quarter the thesis ran
*backwards* inside the Challenger book (rank IC −0.63, p = 0.011): the cheapest names it
selected were its worst performers. Under the locked rule, that alone blocks adoption
regardless of what the headline return had done.

---

## 6. What this result CAN and CANNOT prove

**CANNOT:**

- **Cannot refute the value edge.** One quarter, ~3 rebalances, overlapping windows,
  effective n is tiny. The −0.164 combined (and −0.632 Challenger-only) rank IC is **one
  draw**; it does not overturn the +0.124 walk-forward IC any more than a single positive
  quarter would have confirmed it. Directional only.
- **Cannot establish that the Incumbent is the better book.** Its lead sits inside the
  noise band (§5, tension 1).
- **Cannot see bankruptcy / value-trap risk.** A 15-name quarter cannot resolve a ~1%/yr
  event; measured base rates imply ~0 expected bankruptcies in this sample, so observing
  none confirms nothing (`docs/survivorship_findings.md`).
- **Cannot assess tail behavior.** **No crash regime occurred** — SPY maxDD −3.38%, worst
  single day −1.52%. The Challenger's §3 tail exposure was not exercised.

**CAN:**

- **Execution sanity.** Both books ran a full real quarter cleanly: 61/61 trading days,
  28/28 tickers daily, zero carried-forward cells, zero delistings, recorded path
  reproducible from raw prices to 0.00 bp.
- **A directional read** that the value tilt did **not** behave as the IC study predicts
  over this particular quarter — and that the negative sign was consistent across the
  whole window, not a late-window artifact (spread pinned between −2.4 and −2.6 pp at
  every month-end).
- **That the f≥3 book's live drawdown was tolerable in calm conditions** (−5.28%), with
  the explicit caveat that calm conditions are exactly where it was never going to fail.

---

## 7. Status

**No action taken.** Verdict recorded only. Not done and not to be inferred: no book
change, no promotion of the Challenger, no Kelly sizing, no §3 hardening agenda, no
real-money step, no change to model / config / pipeline. The production model remains
frozen; both books remain as recorded.

Next steps are the user's decision after reading this.

---

*Reproduction: metrics recomputed from `price_snapshots.csv` + book baselines, verified
against `equity_curves.csv`; entry `value_score` from `factor_scores` @ 2026-06-01.
Window strictly 2026-06-17 → 2026-09-14 (snapshot rows through 2026-09-18 excluded).*
