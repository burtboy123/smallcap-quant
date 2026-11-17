# Survivorship Bias — Measured (canonical reference)

**Status: MEASURED, 2026-09-06.** This document supersedes the project's former standing
assumption. Other documents should **link here rather than restate these numbers.**

Full method, tables and reconciliation: `sandbox/survivorship/DEATH_RATE_FINDINGS.md`.
Feasibility probe that preceded it: `sandbox/survivorship/FINDINGS.md`.

---

## The correction in one line

> ~~"Backtest returns are inflated by survivorship bias; every tail number is an
> under-estimate."~~ **(previously assumed — now DISPROVEN)**
>
> **Measured: net survivorship bias is ~±1%/yr with an uncertain sign, possibly mildly
> pessimistic.** Acquisitions outnumber bankruptcies **~6:1**, and their premia roughly
> offset the bankruptcy losses.

The old assumption was not merely imprecise — it had **the wrong dominant term**. It
implicitly modelled "companies that leave the index" as "companies that went to zero."
In fact **most exits are acquisitions, which complete at a premium — a positive return
the backtest also never sees.**

## How it was measured

EDGAR Form 25 / 25-NSE / Form 15 filings 2020–2024, deduplicated to company level and
false-positive-filtered, cross-referenced against 8-K item codes; universe rebuilt
survivorship-free from XBRL `dei:EntityPublicFloat` frames. **CIK-level throughout** — no
ticker mapping anywhere, because SEC drops tickers on delisting and reused tickers return
another company's data (the SBNY trap).

- 12,908 raw exit filings → 4,077 distinct companies → 1,877 in scope ($50M–$10B float)
- 561 (30%) were **false positives** — a Form 25 filed for one *security class* (a bond,
  preferred, warrant) by a company that is still filing 10-Ks
- **1,289 true deaths**, against a pooled panel of **16,516 company-years**

## 1. Exit-type mix (n = 1,289)

| Exit type | Share | Return implication |
|---|---|---|
| **ACQUISITION** | **50.4%** | **Positive** — completes at a premium |
| DEREG / GO-PRIVATE | 33.7% | **Ambiguous** — buyout premium vs going dark |
| **BANKRUPTCY** | **8.6%** | ≈ **−100%** |
| UNCLEAR | 7.2% | unknown |

**Acquisitions outnumber bankruptcies ~6:1.** This is the finding that reverses the
assumption.

## 2. Fragility gradients — what actually predicts death

Bankruptcy rate (the ≈−100% event), per year:

| Cut | Gradient | Range |
|---|---|---|
| **Public float (size)** | **~30×** | 1.19%/yr at $50–150M → 0.04%/yr at $3–10B |
| **Profitability (ROA)** | **~14×** | 1.29%/yr at ROA<−20% → 0.09%/yr at ROA>5% |
| **Leverage (liab/assets)** | **~5×** | 0.15%/yr at <0.30 → 0.81%/yr at >0.70 |
| Deep value (P/B proxy <0.5) | only ~2–4× | 0.59%/yr vs 0.16% at P/B 0.5–1 |

**Smallness, unprofitability and leverage are the death drivers. Cheapness is NOT** — deep
value is elevated only ~2–4×, far weaker than the other three, and deep-value names also
have the *highest* acquisition rate (4.35%/yr), which partly compensates.

*Practical read: a quality/profitability floor is a better-targeted bankruptcy hedge than a
valuation guard, because profitability carries a 14× gradient and cheapness only ~2–4×.*

## 3. The trap: never rank risk by TOTAL death rate

Total death rate **peaks in the middle**, not at the bottom:

| Float bucket | Total death %/yr | of which bankruptcy | of which go-private |
|---|---|---|---|
| $50–150M | 8.22% | **1.19%** | 2.14% |
| **$150–400M** | **11.56%** ← peak | 0.51% | **6.55%** ← why |
| $400M–1B | 6.33% | 0.32% | 1.92% |
| $1–3B | 3.73% | 0.20% | 0.12% |
| $3–10B | 2.51% | 0.04% | 0.11% |

The mid-cap peak is a **going-private phenomenon** (the PE take-private sweet spot), not a
failure phenomenon. **Bankruptcy must always be separated from acquisition and go-private
before any risk statement is made.** Ranking buckets by total exit rate inverts the actual
risk ordering.

## 4. The bounded bias

At acquisition **+30%** (literature estimate for US takeover premia, ~25–35%; *assumed, not
measured here*) and bankruptcy **−100%**:

| Bucket | Net bias | Direction |
|---|---|---|
| All in-scope | **+0.59%/yr** | PESSIMISTIC |
| Smallest float $50–150M | +0.03%/yr | ~neutral |
| High leverage >0.70 | +0.33%/yr | PESSIMISTIC |
| Unprofitable ROA<−20% | **−0.29%/yr** | OPTIMISTIC |

**The sign is not robust** — across the full sensitivity range for how deregistrations are
valued (+15% to −50%), the sign flips. **The magnitude is robust: ~±1%/yr in every
scenario.** That bounded magnitude, not the sign, is the load-bearing result.

**The backtest is optimistic only for genuinely unprofitable names.** For the broad
small/mid-cap universe it is plausibly mildly *pessimistic*.

## 5. Consequences already settled by this

- **Liquidity floors (`sandbox/entry_hardening/`) — SETTLED, verdict CONFIRMED.** That
  study found floors cost **6–8 points of CAGR** but flagged itself *confounded* pending
  this work. The survivorship correction is **~1 point** — an order of magnitude too small
  to explain it away. The confound was real but immaterial. See §6 of the source study.
- **Tail framing — reweighted.** The dominant tail term, bankruptcy frequency, is now
  measured and **low** (~1.19%/yr even at the smallest float). The books' tail exposure is
  therefore **primarily correlated-drawdown risk** (as the 2022 analog showed), not
  bankruptcy-wipeout risk. *Subject to the floor caveat below.*
- **Short-side reasoning — INVERTED.** `sandbox/short_sleeve/` argued the short leg was
  biased *down* because "the names that went to zero are missing." Missing acquisitions
  (3.54%/yr at ~+30%) hurt a short book far more than missing bankruptcies (0.47%/yr at
  ~+100%) help it. The short leg is biased **UP by ~0.6%/yr** — the mirror of the long
  leg — so measured short contribution is **optimistic, not conservative.**

## 6. Limitations — all of them

These are load-bearing. Do not cite the numbers above without them.

1. **2020 is left-censored.** Death year = year of last periodic filing, so 2020 deaths
   typically last filed in 2019 and fall outside the window (only 4 deaths dated 2020 vs
   190–448 in later years). **The usable window is 2021–2024 (~4 years).**
2. **35% of exit candidates were dropped for missing float** (1,417 of 4,077 — funds,
   trusts, foreign filers, bond-only issuers). These skew small, so **death counts are
   understated**, probably most at the smallest sizes.
3. **The bankruptcy rate is a FLOOR, not a point estimate.** Detection requires an 8-K
   item 1.03 to have been filed and tagged; a company that simply stops filing is
   classified DEREG or UNCLEAR (41% of deaths combined). **True bankruptcy rates are ≥
   those shown**, which would push the bias estimate toward the optimistic side.
4. **Float is a size proxy, NOT ADV.** The liquidity question is answered by proxy. A
   direct average-dollar-volume test remains impossible without prices for dead names.
5. **The top float bucket contains exactly ONE bankruptcy** (counts by bucket: 40 / 19 /
   10 / 7 / 1; 77 total in the whole panel). The **direction** of the size gradient is safe
   — it is monotonic across all five buckets and independently reproduced by the ROA and
   leverage cuts — but **the "30×" multiple itself is imprecise.**
6. **The +30% acquisition premium is assumed, not measured.** Measuring it needs delisted
   price history, which is the part that is not obtainable from free sources (0/15 hit rate
   on yfinance for known dead names).
7. **Frequency is resolved; magnitude is not.** We measured *how often* names die and
   *why*. Terminal values are imputed, not observed.

## 7. How to cite this

Use: **"net survivorship bias ~±1%/yr, sign uncertain, possibly mildly pessimistic
(`docs/survivorship_findings.md`)."**

Do **not** write "backtest returns are inflated by survivorship," "the true tail is worse
because of survivorship," or "tail numbers are under-estimates because bankrupt names are
absent." Those assert a large one-directional inflation that has been measured and
disproven. Where the old claim appears in historical documents it is retained with
strike-through as superseded history.

*Note: this bounds the bias; it does not eliminate it, and it changes no model, gate,
threshold or weight. Anything built on these numbers must still clear the project's normal
walk-forward bar.*
