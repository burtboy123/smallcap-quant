# Real-Money Pilot Plan (GATED — authorizes nothing)

**Status:** PLANNING DOCUMENT ONLY. Written 2026-08, while the system is still in paper
forward test.

> ## THIS DOCUMENT DOES NOT AUTHORIZE DEPLOYMENT
> It exists so that *if and when* real capital is deployed, the decision is
> **structured and pre-committed rather than impulsive**. Writing this plan is not a
> decision to deploy. **The preconditions in §1 are the gates; this document only
> defines them.** No capital moves until every gate in §1 is independently satisfied
> and the user makes an explicit, separate decision to proceed.

---

## 1. PRECONDITIONS — ALL must be true before any capital moves

No capital is deployed unless **every** item below is satisfied. Any single failure
means **do not deploy** — not "deploy smaller."

| # | Gate | Standard |
|---|---|---|
| 1.1 | **Forward test completed** | The 2026Q3 parallel paper test has run to **2026-09-14**. Not stopped early, not judged mid-flight. |
| 1.2 | **Formally evaluated** | Evaluated strictly per the pre-committed `forward_test/2026Q3_parallel/EVAL_CRITERIA.md`, including the tail-risk/survival dimension and the value-consistency check. Thresholds were locked before results were known; **they are not renegotiated after seeing them.** |
| 1.3 | **A clear book decision made** | The evaluation produced ADOPT / KEEP / INCONCLUSIVE, and §2 below is resolved. "Inconclusive" means **stay on paper**, not "deploy whichever looks better." |
| 1.4 | **Monitoring confirmed working** | The daily snapshot has run without gaps (`forward_test/check_snapshot_fresh.py` reports FRESH), the scheduled task fires unattended, and the equity-curve/drawdown record is complete. **If we cannot measure it on paper, we do not fund it.** |
| 1.5 | **Broker path tested on PAPER first** | Full execution path exercised end-to-end against a **paper** IBKR account per §8 — order placement, fills, reconciliation. Zero live orders before this passes. |
| 1.6 | **Kill-switches implemented and tested** | The §5 halts exist as code/process using the guard-assertion approach already used in this project, and have been **tested by deliberately tripping them** in paper. An untested kill-switch is not a kill-switch. |
| 1.7 | **Loss is genuinely affordable** | The §3 pilot amount is money whose **total loss** changes nothing material. Confirmed honestly, not optimistically. |

---

## 2. WHICH BOOK — pilot the INCUMBENT first

**Recommendation: pilot the Incumbent (gate + quality) book. Keep the value-tilt
Challenger on paper until it earns promotion.**

**Reasoning — the current evidence points this way on both legs:**

1. **It is winning on return.** At ~8 weeks: Incumbent **+3.58%** vs Challenger
   **−1.75%** vs SPY **+3.43%**. The Challenger is not only behind the Incumbent, it is
   behind the benchmark.
2. **It has lower tail risk.** The tail audit (`sandbox/tail_audit/FINDINGS.md`) found
   the Challenger worse on **every** measured tail dimension — a **−32.1%** drawdown in
   the 2022 rate-shock analog (vs Incumbent −23.7%, SPY −24.5%), worse CVaR99 (−5.03%
   vs −4.25%), worse worst-month (−13.1% vs −9.4%) — with its left-tail risk
   concentrated in an illiquid + leveraged + deep-value cluster.
3. **The locked tie-breaker already points here.** `EVAL_CRITERIA.md` §2(b)4 pre-commits
   that a near-tie on return resolves to the Incumbent *because* of its lower left tail.
   The Challenger is currently not even at a tie.
4. **First real money should ride the more survivable book.** A pilot's purpose is to
   learn whether the machinery works in live conditions — not to take maximum factor
   risk on day one. Pilot the lower-variance option; the goal is to survive to iterate.

**What would later justify promoting the Challenger to real money — ALL of:**

- It **satisfies the ADOPT branch** of `EVAL_CRITERIA.md` §2(a) in a formal evaluation:
  return ≥ **+3.0pp** over the Incumbent; **positive value-consistency check** (the win
  traces to cheap-beats-expensive broadly, not 1–2 lucky names); drawdown not >5pp
  deeper and no −25% breach; alpha vs SPY ≥ −3pp; **and** the tail-acceptability test —
  the return margin must be large enough to *pay for* the extra tail risk it carries.
- It clears that bar **over a longer window than one quarter** — at minimum a second
  full quarter of paper results consistent with the walk-forward thesis, since one
  quarter (~3 rebalances) cannot confirm an edge.
- Promotion is **ADOPT-WITH-HARDENING, never adopt-as-is**: the §4 limits plus the
  tail-audit entry-side controls (liquidity floor, deep-distress P/B guard,
  fragility-aware sizing) are applied *before* it receives any capital.
- The Challenger keeps running on **paper in parallel** throughout. It is promoted by
  evidence, never by impatience.

**The ML overlay stays frozen and disabled in any pilot.** Its leakage-free
out-of-sample IC is ≈0 (+0.015, sign-unstable across folds). It has not earned capital.
The pilot is a gate-and-value-tilt book with **no ML**.

---

## 3. HOW MUCH — a named, small, losable number

**The pilot size is written down before deploying, not decided in the moment.**

### Formula

```
PILOT_$ = X% * EVENTUAL_ALLOCATION
```

- `EVENTUAL_ALLOCATION` = the total capital you would *eventually* commit to this
  strategy if it proved itself. **Write this number down too** — sizing a pilot against
  a vague "someday" number is how pilots get oversized.
- `X` = **user-set, single digit.** Recommended starting range: **2%–5%**.
- **`X` is set BEFORE seeing any live results, and is not revised upward mid-pilot.**

### Guidance for picking X conservatively

- **The correct frame is tuition, not investment.** Assume the pilot goes to zero. If
  that outcome would change your behavior, your sleep, or your finances, **X is too
  high.** Lower it until total loss is genuinely boring.
- **Size for the tail you measured, not the return you hope for.** These books have
  shown ~−24% to −32% drawdowns in crash analogs. ~~and survivorship means the true tail
  is **worse than measured**.~~ *(Survivorship is now measured at ~±1%/yr and does not
  systematically understate the tail — `docs/survivorship_findings.md`.)* **Assume
  something considerably worse than anything in the record anyway** — the drawdown record
  rests on a **single crash regime (2022)**, which is the real reason to distrust it.
- **Fixed costs matter at small size.** Too small and commissions/spreads dominate and
  you learn nothing about the strategy. There is a floor below which a pilot is not
  informative — resolve that tension by **raising your bar for what counts as
  affordable, not by raising X**.
- **The pilot's job is to test the machinery, not to make money.** Execution, slippage,
  reconciliation, monitoring, and *your own behavior under live P&L* are what is being
  tested. None of that requires size.

### Recorded values (fill in at deployment time)

| Field | Value |
|---|---|
| `EVENTUAL_ALLOCATION` | ______ |
| `X` (%) | ______ |
| `PILOT_$` | ______ |
| Max-drawdown halt level (§5.1) | ______ |
| Date set | ______ |
| "Total loss is affordable" confirmed | ______ |

---

## 4. POSITION & CONCENTRATION LIMITS — hard limits, enforced in code

These are **hard limits**, enforced with the guard/assertion pattern already used in
this project (e.g. the Kelly cap fix, which raises rather than silently shipping a
violation). A limit that is only a comment is not a limit.

| Limit | Value | Rationale |
|---|---|---|
| **Max position** | **15%** of pilot capital | Already exists (`KELLY["max_position"] = 0.15`) and was recently fixed — the old normalize→clamp→renormalize sequence inflated capped positions back **above** the cap (produced 17.3%). Now enforced by iterative water-filling plus an assertion. **Re-verify the assertion fires before funding.** |
| **Max sector** | **30%** of book | Prevents one sector shock taking the book. Already applied at construction. |
| **Minimum liquidity floor** | exclude names below **~$5m/day** average dollar volume | From the tail audit: these are names you **cannot exit in a panic** — it found holdings as illiquid as **$0.9m/day ADV**. The single highest-value risk control identified. |
| **Sizing method** | equal-weight, **or** Kelly-capped with the 15% cap verified | Equal-weight is the honest default; Kelly only with the cap assertion active. |
| **Min positions** | **≥ 10** | Avoids concentration by accident when few names pass the gate. |

**Guard requirement:** every limit is checked **at order-generation time**; a violation
**blocks the orders and raises** — it does not warn and proceed.

---

## 5. KILL-SWITCHES — pre-committed, portfolio-level

Each kill-switch specifies **the action that follows**, not just a threshold. The action
is always **halt and review** — never "hope through it."

### 5.1 Max-drawdown halt

- **Trigger:** pilot equity drawdown from its high-water mark reaches **−15%**
  (conservative) to **−20%** (outer limit). **Pick one number before funding** and
  record it in §3. Recommended for a first pilot: **−15%**.
- **Action:** **HALT all new orders immediately. Do not rebalance into the drawdown.**
  Whether to liquidate or hold existing positions is a separate, deliberate decision —
  but no new capital and no new entries. Conduct a **written review**: is this within
  the distribution the backtest and tail audit predicted, or is something *broken*
  (data, execution, logic)? Resuming requires the §1 gates to be re-checked.
- **Why pre-commit:** a drawdown is exactly when judgment is worst. The number is chosen
  now, in calm conditions, precisely so it cannot be renegotiated in the moment.

### 5.2 Single-name catastrophic-loss rule

- **Trigger:** any single holding loses **≥50%** from entry, **or** is halted, delisted,
  or files for bankruptcy protection.
- **Action:** **HALT further additions to that name** and flag it for immediate manual
  review (at the next rebalance, or sooner for a distress event). Investigate whether
  the gate should have excluded it — this is the **value-trap / going-to-zero case** the
  tail audit warned about, concentrated in the deep-value + illiquid cluster.
- **This is NOT a general stop-loss.** Stop-losses tend to **hurt** mean-reverting value
  strategies by selling cheap names at the bottom, right before the reversion the
  strategy exists to capture (see `docs/post_september_research.md`). This rule is
  deliberately scoped to the **catastrophic tail case only**, and remains an unvalidated
  safety brake — not a claimed edge.

### 5.3 Data / monitoring-failure halt

- **Trigger:** the daily snapshot is **stale by more than one trading session**
  (`check_snapshot_fresh.py` reports STALE); the price feed fails; factor data is stale
  or trips its freshness guard; or broker reconciliation (§8) mismatches.
- **Action:** **HALT trading. Do not place orders on stale or unverified data.**
  Diagnose, repair, verify a clean run, then resume. **Never trade blind** — the f-score
  truncation incident is the precedent: a silent data defect propagated into live
  scoring and stayed invisible until specifically audited.

---

## 6. REVIEW CADENCE — scaling requires evidence, not vibes

### Schedule

| Cadence | Activity |
|---|---|
| **Daily (automated)** | Snapshot runs; freshness check. Any STALE → §5.3 halt. |
| **Weekly (~10 min)** | Monitoring only: equity curve, drawdown vs the §5.1 level, any §5.2 trigger, reconciliation clean. **No decisions at weekly reviews** except firing a kill-switch. |
| **Monthly (formal, written)** | Rebalance review + performance vs SPY and vs the paper books. Written go / no-go / scale decision, **including the reasoning**, so it can be audited later. |
| **Quarterly (deep)** | Full review against the walk-forward thesis: is live behavior consistent with a ~0.12 IC value signal after costs, or diverging? Re-run the tail audit against the live book. |

### Monthly checkpoint — three outcomes only

- **GO (continue at current size):** no kill-switch tripped, execution clean,
  reconciliation matches, results within the expected distribution — **including being
  down**; a modest loss is fully consistent with the thesis.
- **NO-GO (halt / reduce):** a kill-switch tripped, execution or data problems, or
  behavior inconsistent with the thesis in a way suggesting a **defect** rather than
  variance.
- **SCALE (increase size):** only per the ladder below.

### Scale-up ladder

Capital increases only on evidence, and only stepwise. **No step may be skipped.**

| Step | Requirement | Size |
|---|---|---|
| **0 — Pilot** | §1 gates all pass | `PILOT_$` (§3) |
| **1** | **≥ 6 months** live; no kill-switch tripped; execution + reconciliation clean; results consistent with the walk-forward thesis | up to **2×** pilot |
| **2** | **≥ 12 months** live cumulative, including **at least one meaningful market drawdown survived** within expectations | up to **4×** pilot |
| **3+** | **≥ 24 months** live, results still consistent with a ~0.12 IC signal **after costs** | stepwise toward `EVENTUAL_ALLOCATION` |

**Ladder rules:** scaling is never automatic — each step needs an explicit written
decision. **Any kill-switch trip resets you to the previous step at minimum.** Never
scale *into* a drawdown. Never scale on a single strong month.

---

## 7. WHAT CANNOT BE CONCLUDED — read before interpreting any result

- **A few months of good live results is NOT proof of edge.** With monthly rebalancing,
  a 6-month pilot is ~6 rebalances — a handful of independent observations. A win over
  that span is **entirely consistent with luck.**
- **A losing pilot is not proof the strategy is broken, either.** A ~0.12 IC signal
  produces long stretches of underperformance. This cuts both ways — which is exactly
  why the §5 kill-switches are about **risk control and defect detection**, not about
  judging the edge.
- **The tail is under-sampled — but not for the reason previously stated.**
  > ~~Every backtested tail number is **survivorship-biased** — the universe is current
  > constituents, so names that went bankrupt are absent. Assume the true left tail is
  > worse than the −32% on record.~~ *(previously assumed — now measured and disproven)*

  Bankruptcy frequency is measured and low (~1.19%/yr at the smallest float; net
  survivorship bias ~±1%/yr, sign uncertain — `docs/survivorship_findings.md`). **The real
  limit is that the record contains ONE crash regime (2022)**, so −32% is one draw from a
  distribution whose shape is unknown. **Still assume the true left tail is worse than the
  −32% on record** — the justification is small-sample, not survivorship. The book's tail
  exposure is primarily **correlated drawdown**, not bankruptcy wipeout.
- **A calm pilot period proves nothing about tail risk.** If no crash occurs, the
  fragility simply was not triggered. **"No drawdown happened" ≠ "the book is safe."**
- **Therefore: keep scaling proportional to evidence.** The §6 ladder exists because the
  natural human error is to scale *after* a good run — precisely when confidence is
  highest and evidence is weakest. Evidence accumulates slowly; capital should too.

---

## 8. BROKER / EXECUTION SAFETY

**Intended broker: Interactive Brokers (TWS / IB Gateway)** — available in Finland,
supports US small/mid-cap equities, and provides a paper-trading environment that
mirrors the live API.

### Required before any live order

1. **Paper-trade the complete execution path first.** Not just an API connection test —
   the **full** loop: factor calculation → book construction → order generation →
   submission → fills → positions → reconciliation, on the IBKR **paper** account, for
   **at least one complete monthly rebalance cycle**.
2. **Order-type and slippage checks.** These are small/mid-caps with real spreads. Use
   **limit orders**, not market orders, on illiquid names. Measure realized slippage vs
   decision price in paper and confirm it does not swamp the signal — at a ~0.12 IC,
   **slippage is a first-order risk, not a detail.**
3. **Liquidity sanity per order.** Cross-check order size against the §4 ADV floor. An
   order that is a large fraction of a name's daily volume moves the price against you
   and cannot be exited quickly.
4. **Reconciliation before and after every live rebalance — mandatory.** Compare broker
   positions and cash against the system's expected book. **Any mismatch halts trading**
   (§5.3) until explained. Never let the system's view of the world drift from the
   broker's.
5. **Dry-run default stays on.** Order submission defaults to **dry run**; live
   submission requires an explicit, deliberate flag — consistent with the existing
   broker-agent design.
6. **First live rebalance is manual.** A human reviews every order before submission.
   Automating live submission is earned only after several clean manual cycles.

---

## Sign-off

This plan **defines** the gates. **It does not open them.** Deployment requires:

1. Every §1 precondition independently verified,
2. The §3 numbers written down and confirmed affordable,
3. The §4 limits and §5 kill-switches implemented **and tested**,
4. An explicit, separate decision by the user to proceed.

Until all four are true, the system remains in **paper** and the production model
remains **frozen**.

_No model, config, pipeline, broker, or forward-test changes were made by this
document._
