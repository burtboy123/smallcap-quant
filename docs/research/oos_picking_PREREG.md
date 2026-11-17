# Pre-registration — is the small-cap stock picking real? (out-of-sample years)

**Written 2026-10-12, before any result for these years exists.** Implemented by
`research/oos_picking.py`. Nothing below may change after the first run; a rule that
cannot be computed as written counts as **not passing**.

## Why

The only measured skill so far: the incumbent-like picking beat its own small-cap pool
by **+5.0%/yr** over 2019-03 to 2026-09 (`universe_study_RESULT.md`), unproven. The
hedged paper test (`hedged_paper_PREREG.md`) only makes sense if that skill is real.
Those 7.5 years have now been looked at; the honest check is years the method has
**never** been measured on: everything before 2019-03.

## What is tested (one test)

Exactly the universe study's `small` contestant (incumbent-like score 0.415 quality +
0.338 value + 0.246 momentum, health gate ≥ 4 of 6, 15 names, ≤ 4 per sector, keep while
in the top 30, monthly, 0.4% per side, IWM list, market cap $50m–$10bn, real past
prices), with two changes needed to reach back before 2019, fixed now:

- **dollar-volume floor $2m** (not $5m): small caps traded less a decade ago;
- **≥ 150 scored names** to start (not 300).

**Window:** from the first month (2010 or later) with ≥ 150 scored names in the small
pool, through **2019-02**. Fewer than **36 months** → **INSUFFICIENT DATA**.

**Measure:** stock picking = the 15 picks' CAGR (after costs) minus the CAGR of the
pool's equal-weight average (no costs), over the window.

## Verdict

- **CONFIRMED:** picking ≥ **+2.0%/yr** **and** t ≥ **2.0** on monthly (picks − pool)
  returns **and** the picks out-compound the pool in **≥ 3 of 4** equal consecutive
  periods.
- **CONSISTENT (directional):** picking > 0 and ahead in ≥ 3 of 4 periods, without the
  rest.
- **NOT CONFIRMED:** anything else.

## Also reported (descriptive, no verdict)

- The hedged structure over the same window: SPY + picks − IWM − 2%/yr vs SPY (CAGR,
  Sharpe, max drawdown).
- The same rules continued over 2019-03 to the latest month, to compare with the
  universe study's +5.0% (a check that the two changes above did not make a different
  strategy).
- Scored names per year.

## What the result may lead to (fixed now)

- **CONFIRMED:** the picking edge has support outside the years it was found in; the
  hedged paper test continues with a real prior. Still paper; real money stays gated.
- **CONSISTENT:** directional only; the hedged paper test continues.
- **NOT CONFIRMED / INSUFFICIENT DATA:** the 2019–2026 picking result is most likely
  luck or specific to that period. The hedged paper test still runs to its own date
  under its own rules, but the realistic expectation is low, and an S&P 500 index fund
  stays the benchmark for any real money.

## Known limits, stated now

Current-constituent survivorship is stronger the further back you go (today's small
caps traced back to 2010, i.e. companies that stayed small); the picks-vs-pool measure
shares it on both sides, and the broad bias was measured at ~±1%/yr
(`docs/survivorship_findings.md`). SEC coverage is thinner early. 15-name portfolios
are noisy. One history.

No action is taken by this study.
