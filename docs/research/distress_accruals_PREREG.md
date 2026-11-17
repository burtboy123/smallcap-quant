# Pre-registration — distress and accruals as value-trap filters

**Written 2026-10-08, BEFORE any result is computed.** This file fixes the hypotheses,
metrics, data rules and the decision rule. Changing any of them after results exist is
the goal-posting `.claude/skills/research-discipline/SKILL.md` §1 forbids; if something
cannot be computed as defined, it is recorded and the clause counts as **not met**.

> Research only. Nothing here changes the live composite, the f>=5 gate, regime logic,
> the Q4b book or the broker. Adoption would be a separate, explicit decision.

## Why

The value thesis keeps losing to **value traps**: in Q3 the Challenger's cheapest names
were its worst (rank IC −0.63). The survivorship study measured what predicts
bankruptcy: unprofitability (~14×), leverage (~5×) and smallness (~30×), not
cheapness (~2–4×). Both scores below target that risk directly, from data already in
the database. They are tested as **filters** (remove names before ranking), not factors.

## Hypotheses

- **H1 (distress):** higher Campbell-Hilscher-Szilagyi (2008) failure score → lower
  forward return, especially among cheap stocks.
- **H2 (accruals):** higher accruals, (TTM net income − TTM operating cash flow) / total
  assets (Sloan 1996) → lower forward return, especially among cheap stocks.

## Scores (fixed; NO coefficient is fitted on our data)

CHS, the published coefficients (CHS 2008, best model):

    score = −20.26·NIMTAAVG + 1.42·TLMTA − 7.13·EXRETAVG + 1.41·SIGMA
            − 0.045·RSIZE − 2.13·CASHMTA + 0.075·MB − 0.058·PRICE   (higher = more distress)

| Input | Definition here |
|---|---|
| ME | price × shares_outstanding |
| TL | total_assets − total_equity |
| MVA | TL + ME |
| NIMTAAVG | quarterly NI / MVA, last 4 quarters, weights ∝ φ^(3(j−1)), φ = 2^(−1/3); TTM-only rows use (NI_TTM / 4) / MVA |
| TLMTA | TL / MVA |
| EXRETAVG | log(1+r_i) − log(1+r_bench), last 12 months, weights ∝ φ^(j−1); bench = equal-weight universe mean (no index series is stored) |
| SIGMA | annualised st.dev. of daily returns, last 63 trading days |
| RSIZE | log(ME / Σ ME of the universe on that date) |
| CASHMTA | cash_and_equivalents / MVA |
| MB | ME / (book + 0.1·(ME − book)); book ≤ 0 → book = 1 |
| PRICE | log(min(price, 15)) |

Inputs winsorised at the 5th/95th percentile cross-sectionally on each date.

## Data rules

- **Point in time:** a fundamentals row is usable on rebalance date *d* only if
  `available_date ≤ d` (Rule 1). Prices only up to *d*. Forward returns from `adj_close`
  (fallback `close`) strictly after *d*.
- **Mixed sources:** rows with `filing_date` (SimFin) hold **quarterly** flows; rows
  without (yfinance) hold **12-month** flows. TTM = sum of the last 4 consecutive SimFin
  quarters; yfinance rows are used as TTM directly. Stock items (assets, equity, cash)
  are taken as-is.
- **Consistency gate:** per year, median TTM NI/total_assets of yfinance rows must be
  within a factor of 1.5 of SimFin-built TTM rows. If not, results are marked
  **UNRELIABLE** and no verdict is issued.
- **Universe:** `get_universe()` (current IWM constituents). Rebalance dates: first
  trading day of each month with ≥ 3 months of forward prices.
- **Cheap subset:** top tercile of book-to-market (total_equity / ME) on each date.

## Metrics

1. **Coverage:** % of universe names with a valid score, per year.
2. **Rank IC:** Spearman correlation between score and forward 1- and 3-month return,
   per rebalance date, in the cheap subset (primary) and the full universe.
3. **Per-year mean IC** and the share of years with the predicted (negative) sign.
4. **t-stat** of the 3-month IC on **non-overlapping** dates (every third month), with
   that effective n reported.
5. **Quintile spread:** mean forward 3-month return of the most- minus least-distressed
   quintile (no costs; costs belong to the filter backtest).

## Decision rule (per hypothesis, cheap subset, 3-month horizon)

**PROCEED to the filter backtest** only if ALL of:
1. coverage ≥ 60% of universe names in at least half the years tested;
2. mean IC < 0, and negative in ≥ 2/3 of calendar years;
3. non-overlapping t-stat ≤ −2.0;
4. the consistency gate passed.

**WATCHLIST** if 1, 2 and 4 hold and −2.0 < t ≤ −1.0.
**REJECT** otherwise. A result inside the noise band is reported as a wash.

The filter backtest (next stage, separately approved) will choose its exclusion
threshold on earlier years only and apply it to later years, with costs, against the
unfiltered strategy.

## Known limitations (stated now)

- **Survivorship:** the universe is today's IWM, so firms that became distressed and
  then died are missing. For a distress signal this biases the test **against** finding
  the effect; the measured ~±1%/yr broad-universe bias may understate it here
  (`docs/survivorship_findings.md`).
- **History depth** depends on SimFin coverage on the PC; measured in metric 1.
- **Benchmark** for EXRET is the universe mean, not the S&P 500 used by CHS.

---

## Amendment 1 — 2026-10-11: data source (rules unchanged)

**After** the first run (`distress_accruals_RESULT.md`: REJECT, coverage 0% before
2021), a second data source is added: SEC XBRL `companyfacts` via `data/sec_xbrl.py`,
run with `python -m research.distress_accruals --source sec`.

What changes: only where fundamentals come from. SEC facts are taken **as first
reported**, a period is usable from the latest SEC `filed` date of the facts it uses
(this replaces the `available_date` / +90-day rule for this source), TTM comes from
annual + year-to-date − prior year-to-date, and `Liabilities` is used directly where
reported. It is one source, so the mixed-source consistency gate is trivially passed.

What does NOT change: the scores, coefficients, cheap subset, horizons, metrics,
every threshold of the decision rule, and the order they are applied in.

Why this is not goal-posting: the first run showed nothing about the signal that this
change could exploit. It failed on coverage, and its IC/t-stats were insignificant in
both directions. The change addresses coverage only. The first run's verdict stands
as recorded; the SEC run is reported as a separate result.
