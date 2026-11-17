# September Evaluation Criteria — Incumbent vs Challenger (PRE-COMMITTED)

**Written:** 2026-06-17 (BEFORE any results are known — fixed upfront to prevent
hindsight goal-posting.)
**Evaluation date:** 2026-09-14
**Books:** Incumbent (gate+quality, 13) vs Challenger (value-tilt 0.70V/0.30Q, f≥3,
no ML, no regime bias, 15). Both **equal-weighted**, common start 2026-06-17,
SPY baseline 750.57.

---

## 1. Metrics computed on 2026-09-14 (exact definitions)

All measured from the 2026-06-17 baseline prices in `incumbent_book.csv` /
`value_tilt_book.csv`. Delisted names count as realized loss to last traded price.

| Metric | Definition |
|---|---|
| **Total return** (per book) | equal-weight mean of `price_0914 / baseline - 1` |
| **Alpha vs SPY** | book total return − SPY return (SPY: 0914 close / 750.57 − 1) |
| **Relative spread** | Challenger return − Incumbent return |
| **Max drawdown** (per book) | worst peak-to-trough of the equal-weight daily equity curve over the window |
| **Hit rate** | % of holdings with positive return; and % beating SPY |
| **Value-consistency check** | Spearman rank corr between each name's entry `value_score` and its realized quarter return, across the combined gated universe (does cheap-beats-expensive actually show?) |

## 2. Decision rule (pre-committed)

> **Hard prerequisite for ANY adoption:** the relative spread alone is never
> sufficient. Because one quarter ≈ 3 rebalances with tiny effective n, a Challenger
> win is only credited if it is **consistent with the walk-forward thesis** — i.e.
> the value-consistency check is **positive** (cheap names outperformed expensive over
> the quarter), so the result traces to the value factor broadly, not 1–2 lucky names.

> **Second hard prerequisite (added 2026-07-02):** the decision weighs **survival, not
> just return.** The Challenger has a **strictly worse left tail than the Incumbent on
> every measured dimension** (see §3). A Challenger return win **inside the noise band
> does NOT justify adopting a strictly-worse-tail book** — when return is a near-tie,
> the Incumbent's lower left-tail exposure makes it the better **risk-adjusted** choice
> and it wins by default. A return win only earns adoption if it is *large and thesis-
> consistent enough* to pay for the extra tail risk being taken on.

### (a) ADOPT Challenger — requires ALL of:
1. Challenger return ≥ Incumbent return by **≥ +3.0 percentage points** (margin above
   the quarter-noise band), **and**
2. Value-consistency check **positive** (rank IC of entry value_score vs realized
   return > 0 over the window) — the win comes from the thesis, not noise, **and**
3. Challenger max drawdown **not materially worse**: no more than **+5 pp** deeper than
   Incumbent's, and no hard breach below **−25%**, **and**
4. Challenger does not badly trail SPY (alpha vs SPY ≥ **−3 pp**; a long book in a
   normal-to-up market should be near or above SPY), **and**
5. **Tail acceptable (added 2026-07-02).** The extra left-tail risk the Challenger
   carries (§3) is judged acceptable *given the size of the return win* — i.e. the
   return margin is large and thesis-consistent enough to compensate for a book with
   ~1.2× the daily expected shortfall and a materially worse worst-month than the
   Incumbent. A marginal (just-over-3pp) win does **not** clear this bar on its own.
   → Adoption is **ADOPT-WITH-HARDENING, not adopt-as-is**: deploy **Kelly-sized**
   (cap fix `d261439` re-validated), apply the §3 robustness agenda (liquidity floor,
   deep-distress P/B guard, fragility-aware sizing, defensive ballast/tail hedge), and
   keep a reduced parallel track for one more quarter before full capital.

### (b) KEEP Incumbent — if ANY of:
1. Incumbent return ≥ Challenger by **≥ +3.0 pp**, **or**
2. Value-consistency check **negative** (value tilt *lost* — cheap underperformed):
   the walk-forward edge did not show, so do not adopt regardless of headline return
   (a Challenger win on a *negative* value IC would be luck, not the thesis), **or**
3. Challenger max drawdown worse by **> +5 pp** than Incumbent or breaches **−25%**, **or**
4. **Tail tie-breaker (added 2026-07-02).** Returns are a **near-tie** (spread within
   the ±3 pp noise band). Because the Challenger has a strictly worse left tail (§3),
   the Incumbent is the better risk-adjusted book and is kept by default — a coin-flip
   on return is decided *against* the higher-tail book, not left as merely "inconclusive."

### (c) INCONCLUSIVE — extend the parallel test another quarter (NO capital move) — if:
1. Relative spread within **±3.0 pp** (inside the noise band), **or**
2. Mixed signal — e.g. Challenger wins return but with materially worse drawdown, or
   wins return while the value-consistency check is flat/negative, **or**
3. The return difference is driven by **1–2 idiosyncratic names** (drop the top/bottom
   contributor; if the spread flips sign or collapses below 3 pp, treat as inconclusive).

> **Tail resolution of a near-tie (added 2026-07-02):** extending the trial is the
> default when no capital decision is forced. But the book is **never flipped to the
> Challenger on a noise-band result** — if a decision *is* forced, a near-tie resolves
> to **KEEP Incumbent** per (b)4, because the Challenger's worse left tail (§3) loses
> the risk-adjusted coin-flip. "Inconclusive" therefore means *stay on the Incumbent
> and keep watching*, not "hold the Challenger."

## 3. Tail-risk / survival dimension (added 2026-07-02 — from the read-only tail audit)

Source: `sandbox/tail_audit/` (`crash_regime_drawdowns.csv`, `fragility_rank.csv`,
`FINDINGS.md`). The validation metrics (rank IC +0.124, long-short +6.78%) measure the
**middle** of the return distribution and are **structurally blind to the left tail**.
The audit found the **Challenger is return-optimized, not survival-optimized.** These
numbers sit here so the September reviewer weighs them *next to* the return figures.

**Reference tail facts (Challenger vs Incumbent):**

| Dimension | Incumbent | Challenger |
|---|---|---|
| 2022 rate-shock drawdown | −23.7% | **−32.1%** (SPY −24.5%; 8.4 pp worse than Incumbent) |
| 2018Q4 liquidity drawdown | −25.9% | −27.9% |
| CVaR99 (daily expected shortfall) | −4.25% | **−5.03%** |
| Worst month | −9.4% | **−13.1%** |
| Fragile cluster | — | illiquid + leveraged + deep-value cyclicals: **SUNS $0.9m/day ADV, NUS P/B 0.34**, CMRE (shipping), CMCL (mining), NLOP/SUNS (real-estate credit/office), REPX/CNX (energy) — co-move in liquidity events |

The Challenger is **worse on every measured tail dimension.** Its extra return premium
is paid for in left-tail exposure the headline metrics cannot see.

**Caveats on reading the tail numbers:**
1. **Survivorship — REFRAMED 2026-09-06, no longer a one-directional understatement.**
   > ~~The universe is current constituents, so the worst historical value names (those
   > that went bankrupt) are **absent**. Every tail number above is an **under-estimate**
   > of the true left tail.~~ *(previously assumed — now measured)*

   Bankruptcy frequency is now **measured and low**: ~1.19%/yr at the smallest float,
   ~0.47%/yr in-scope; bankruptcies are 8.6% of exits and acquisitions outnumber them
   ~6:1. Net survivorship bias is **~±1%/yr, sign uncertain**
   (`docs/survivorship_findings.md`). **The Challenger's tail exposure above is therefore
   primarily CORRELATED-DRAWDOWN risk — the fragile-cluster co-movement named in the table
   — not bankruptcy-wipeout risk.** That does not make the Challenger's worse tail benign;
   it identifies *which* risk it is. **Caveat:** the measured bankruptcy rate is a
   **floor** (detection depends on 8-K item 1.03 filings), so this bounds the term rather
   than eliminating it.
2. **The window likely contains no crash** — a calm 2026-06→09 quarter is the base case.
   **Absence of a large drawdown by September is NOT evidence the tail risk isn't there**
   — it just means the fragility wasn't triggered in this sample. Do not read "no
   drawdown happened" as "the book is safe."

**Post-September robustness agenda (the documented ADOPT-WITH-HARDENING path, if the
Challenger construction is otherwise adopted per §2a — NOT applied now):**
- **Liquidity floor** — exclude sub-**$5m/day** average dollar volume (drops SUNS, CMCL,
  NLOP; names that cannot be exited in a fire-sale). Highest-value single change.
- **Deep-distress P/B guard** — flag/cap P/B below ~0.5 (e.g. NUS 0.34) so "cheapest"
  is not "most likely a value trap."
- **Fragility-aware sizing** — down-weight/cap the top-fragility cluster instead of
  equal-weighting it beside the defensive names.
- **Quality-defensive ballast / tail hedge** — barbell the cheap-value sleeve with a
  quality-defensive sleeve or a small IWM/SPY put hedge, and cap combined
  shipping/mining/energy/real-estate-credit exposure (shared liquidity-event failure mode).

Adoption is therefore **adopt-with-hardening, never adopt-as-is.**

## 4. What this quarter CAN and CANNOT establish (honest limits)

- **CANNOT confirm the value edge.** ~3 rebalances, overlapping windows, effective
  n is tiny. A single quarter beating the incumbent is **necessary but not sufficient**;
  it cannot validate or refute the +0.124 walk-forward IC on its own.
- **CANNOT see bankruptcy/value-trap risk** the f≥3 relaxation might carry — one quarter
  of 15 names cannot resolve a ~1%/yr event. *(The reason has changed: this is now a
  **sample-size** limit, not a survivorship-blindness limit. Base rates are measured —
  ~1.19%/yr bankruptcy at the smallest float, and cheapness is only a ~2–4× multiplier
  while unprofitability is ~14× — see `docs/survivorship_findings.md`. Expect ~0 bankruptcies
  in a 15-name quarter either way, so **observing none confirms nothing.**)*
- **Adoption is gated on consistency, not a standalone win** (§2 prerequisite): the
  result must agree with the walk-forward evidence (value factor working) to count.
  This guards against promoting a noise win that happens to favor the challenger.
- **What it CAN establish:** execution sanity (both pipelines run a real quarter
  cleanly), a directional read on whether the value tilt behaves as the IC study
  predicts, and whether the f≥3 book's drawdown is tolerable in live conditions.

## 5. Sign-off

These thresholds are fixed as of 2026-06-17 (return criteria) and 2026-07-02 (the §3
tail-risk / survival dimension, which ADDED to — did not weaken — the return criteria).
Any change to them after results are known must be recorded with rationale in this
file's git history and is itself a red
flag. Production model remains frozen throughout; no capital is flipped during the trial.
