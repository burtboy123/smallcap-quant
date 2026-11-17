---
name: research-discipline
description: Methodology for any quant_system research, backtest, factor/rule study or forward-test evaluation. Use whenever testing a hypothesis (a new factor, gate, exit rule, regime rule, weighting, sizing), reading a backtest or forward-test result, writing a FINDINGS or EVALUATION_RESULT document, or deciding whether a result justifies a change. Enforces effective-n honesty, walk-forward validation, costs, measured survivorship, sign stability, and reporting a wash as a wash.
---

# Research discipline

This project has run many studies and most came back negative or inconclusive. That is
the system working, not failing. The rules below are how those studies were done; the
sources are `docs/regime_rules_validation.md`, `docs/post_september_research.md`,
`docs/survivorship_findings.md` and `forward_test/2026Q3_parallel/EVALUATION_RESULT.md`.
(`sandbox/` holds the study code but is gitignored, so it exists only on the user's PC.)

## 0. Research changes nothing

A study is a **hypothesis to test, not a decision.** Research runs read-only against
the database, writes only under `sandbox/` or `docs/`, and changes no model, config,
gate, threshold, book or forward test. Adopting a result is a separate, explicit step
the user decides. Every report ends with "No action taken".

## 1. Decide the rule before seeing the result

- Write down the metric, the comparison, the threshold, the window and the
  ADOPT / KEEP / INCONCLUSIVE rule **before** running it (the `EVAL_CRITERIA.md` pattern).
- Never move a threshold, window or evaluation date after results are known. If a
  criterion truly cannot be computed as defined, say so and treat it as **not
  satisfied** (fails toward keeping the incumbent, never toward adopting).
- Ties and near-ties go to the simpler, existing rule.

## 2. Out-of-sample, walk-forward, several folds

- Strict time split with an embargo; no information from after the decision date
  (fundamentals use the 90-day `available_date` lag).
- **Several folds, not one split.** A result that helps on one split and not across
  folds is noise. The one rule here that generalized (3x recency weight) improved OOS
  IC in *both* folds.
- In-sample 2005-2018, out-of-sample 2019-2024 (`config/settings.py` BACKTEST). Do not
  tune on the OOS period and then report it as OOS.

## 3. Effective n, always

- Count **non-overlapping, independent** observations. Overlapping forward windows on
  monthly sampling (63-day returns sampled monthly) inflate nominal n about 3x.
- Report nominal n **and** effective n next to every effect size.
- Effective n below ~6 is **directional only**. The "factors work better at long
  horizons" result was an eff-n<6 mirage.
- **n = 1 is structurally unvalidatable.** A rule built to fix one episode (the XLK
  oil-shock override fired once in 21 years) "works" on that episode by construction.
  Label it an unvalidated guard until it has fired across several independent periods.
- A forward-test quarter (~3 rebalances, 15 names) cannot confirm or refute an edge.
  It is one draw.

## 4. Does it discriminate?

- Compare **when the rule fires vs when it is quiet**, not just "it worked when it
  fired". The XLK trigger fired 57% of months with identical forward results either way:
  no discrimination.
- Check the base rate. If something is true 60% of the time anyway, a 59% hit rate
  is nothing.

## 5. Risk-adjusted and after costs

- Judge on the **ratio** (Sharpe, Sortino, drawdown-adjusted), not on return or risk
  alone. Report return, vol, max drawdown, CVaR and worst month.
- Apply the project's costs (~0.8% round trip per position change,
  `TRANSACTION_COSTS`) and the turnover the rule implies. An edge that does not survive
  costs is not an edge.
- Middle-of-distribution metrics (IC, long-short spread) are blind to the left tail.
  Look at the tail separately.

## 6. Sign stability and concentration

- Does the sign hold across folds, sub-periods and month-ends, or flip?
- **Drop the top and bottom contributor.** If the result flips sign or collapses,
  1-2 names drove it: inconclusive.
- A result must trace to its **mechanism** (a value win needs a positive
  value-consistency IC). A win against the thesis is luck, not evidence.
- Test a new rule **head-to-head against the simpler alternative** that addresses the
  same concern (exit rules vs entry-side liquidity/distress controls), never in isolation.

## 7. Survivorship: cite the measurement

- Use: "net survivorship bias ~±1%/yr, sign uncertain, possibly mildly pessimistic
  (`docs/survivorship_findings.md`)". Do **not** write that backtests are "inflated by
  survivorship" or that tails are "under-estimates": that was measured and disproven.
- A claimed effect larger than ~1%/yr cannot be explained away by survivorship; judge
  it on its own evidence.
- Exceptions: unprofitable / bankruptcy-band names are mildly optimistic (−0.29%/yr at
  ROA<−20%), and the measured bankruptcy rate is a **floor**.
- Never rank risk by total exit rate. Separate bankruptcy from acquisition and
  go-private first (the mid-cap "death peak" is take-privates).
- Identify companies by CIK, not ticker: tickers get reused (the SBNY trap).

## 8. Absence of evidence

- A calm window says nothing about the tail. "No drawdown happened" is not "the book
  is safe": the fragility was simply not triggered.
- Expecting ~0 events (bankruptcies in a 15-name quarter) and observing 0 confirms
  nothing.

## 9. Data integrity before any metric

Check coverage (every session, every ticker), gaps against the exchange holiday
calendar, carried-forward cells, delistings, nulls, and revised (not just appended)
rows. **Recompute key numbers independently from raw data** and state the difference.

## 10. Report honestly

Structure (as in `EVALUATION_RESULT.md`):

1. **Verdict first**, in one line: ADOPT / KEEP / INCONCLUSIVE / CURVE-FIT / NEUTRAL /
   PRINCIPLED, and which pre-committed clause triggered it.
2. Method and data checks.
3. Every number with its n, effective n and window.
4. **What this CAN and CANNOT establish.**
5. **Limitations, all of them.** Load-bearing caveats are not optional.
6. "No action taken." Next steps are the user's decision.

Language rules:

- **A wash is reported as a wash.** "No detectable effect", not "promising" or "a
  modest improvement" for something inside the noise band.
- Never present an in-sample, single-split or eff-n<6 result as a finding.
- When a result supersedes an earlier claim, ~~strike it through~~ and link the new
  source; do not silently delete the history.
- Label unvalidated rules **in the code** where they live (UNVALIDATED / CURVE-FIT /
  NEUTRAL / PRINCIPLED), so nothing downstream treats them as a confirmed edge.

## Checklist before calling something a finding

- [ ] Rule and threshold written down before the result was seen
- [ ] Walk-forward, several folds, consistent sign across them
- [ ] Nominal n and effective n reported; eff-n ≥ ~6, or labeled directional
- [ ] Fires-vs-quiet (or treated-vs-control) comparison, against the base rate
- [ ] Risk-adjusted, after ~0.8% round-trip costs and the implied turnover
- [ ] Drop-top/bottom-contributor test passed
- [ ] Traces to the mechanism; beats the simpler alternative head-to-head
- [ ] Survivorship cited as measured; effect compared with the ~1%/yr bound
- [ ] Data integrity checked; key numbers recomputed from raw data
- [ ] CAN/CANNOT and all limitations written; "No action taken"
