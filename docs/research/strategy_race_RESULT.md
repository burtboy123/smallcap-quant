# Result — strategy race vs the S&P 500

## Run 2 (2026-10-12): real past prices — the verdict stands; value's edge was look-ahead

Same pre-registered rules, prices from `research/price_store.py` (the real price on each
past day; split check 1,158 of 1,224 splits line up). As printed on the PC (the full
run-2 report table was not transcribed here):

| Contestant | vs SPY | picking (vs universe) | CAGR run 2 | gap to SPY | CAGR run 1 |
|---|---|---|---|---|---|
| incumbent_like | DOES NOT BEAT | PROMISING | +14.5% | −1.4% | +14.4% |
| value | DOES NOT BEAT | DOES NOT BEAT | **+6.1%** | −9.8% | **+13.0%** |
| quality_defensive | DOES NOT BEAT | DOES NOT BEAT | +1.5% | −14.3% | +2.0% |
| momentum | DOES NOT BEAT | PROMISING | +15.0% | −0.9% | +12.8% |
| blend | DOES NOT BEAT | PROMISING | +10.6% | −5.3% | +11.9% |

SPY over the window: +15.9%/yr.

- **Nothing beats SPY**, as in run 1; now no contestant is even PROMISING vs SPY.
- **The value style collapsed from +13.0% to +6.1%/yr** once past prices were real.
  Only value's scores use market cap (earnings, book and cash-flow yield), and that is
  exactly where later splits leaked the future in. So run 1's value result was mostly
  look-ahead. The incumbent-like style barely moved (+14.4% → +14.5%).
- **Consequence beyond this study (not measured, flagged):** the value-tilt evidence
  that motivated the comparison portfolio (the walk-forward IC studies in `sandbox/`)
  was computed with the same adjusted prices and may be inflated the same way. The
  Q4b forward test itself is clean (prices as they happened); its pre-committed
  criteria are unchanged, but whoever evaluates it should know this.

---

**Run 1 (superseded).** Kept for the record. It used prices adjusted for later splits
(the main `prices` table's close is auto-adjusted, `data/prices.py`), contrary to the
pre-registration's "real close"; past valuations leaked the future. Its small-caps-vs-SPY
term (−6.2%/yr) does not depend on this; its picking numbers do.

**Run 2026-10-11 on the PC's database**, exactly as pre-registered in
`strategy_race_PREREG.md` (committed before any result). Report:
`research/output/strategy_race/REPORT.md` on the PC. No live change made.

## Verdict: nothing BEATS the S&P 500; the small-cap pond is the main obstacle

- **vs SPY:** no contestant passes BEATS. `incumbent_like` and `value` are PROMISING by
  the rule as written; the other three DOES NOT BEAT.
- **Stock picking (vs the universe):** `incumbent_like` and `value` PROMISING
  (directional, t ≈ 1); the others DOES NOT BEAT.
- **By the pre-registered reading rule:** no contestant beats SPY, and the small-cap
  gap (IWM − SPY, **−6.2%/yr**) is larger than the best picking result (**+5.3%/yr**).
  So the report's conclusion is that **the universe is the main obstacle**; the next
  study is the universe (mid caps, or an S&P core plus a factor sleeve), not new weights.

**Read the PROMISING labels with care.** The rule tests the *average monthly* active
return. Every contestant is far more volatile than SPY (vol 21–41% vs 17%), and a
volatile series' monthly average overstates what it compounds to. In compounded terms
(CAGR, what an account actually earns) **every contestant trails SPY**, by 1.3 to
13.7 pp a year. The verdict stands as pre-registered; this is a weakness of the rule,
noted so the next pre-registration judges on CAGR.

## Data and window

- 1,915 universe tickers with prices, 1,777 with SEC fundamentals.
- **Window 2019-04 to 2026-09: 90 months (~7.5 years).** The pre-registered start
  rule (first month with ≥ 300 fully scored names) was not met until 2019-04. *Which*
  filter binds before then (SEC coverage, the $5m dollar-volume floor, the size range)
  was not measured; with distorted past prices the size and $3 filters were also off. So this is **not** a 2010+ test,
  as hoped when it was designed; it is one ~7.5-year period that includes the 2020
  crash and the 2022 rate shock.
- Checks done here: the decomposition adds up exactly for every contestant;
  CAGR − SPY and CAGR − universe EW recompute to the reported gaps; SPY +15.7%/yr and
  IWM +9.5%/yr for 2019-04 to 2026-09 are in line with the public index history.
  Not recomputed independently from the raw price data (it is on the PC only).

## Numbers (net of 0.8% round-trip costs; n = 90 monthly returns each)

| | CAGR | vol | Sharpe | max DD | worst month | picking vs EW (CAGR) | t (vs EW) | folds + |
|---|---|---|---|---|---|---|---|---|
| incumbent_like | +14.4% | 27.9% | 0.63 | −50.3% | −38.5% | **+5.3%** | 1.07 | 3/4 |
| value | +13.0% | 32.6% | 0.56 | −56.8% | −49.3% | +3.9% | 0.99 | 3/4 |
| momentum | +12.8% | 41.3% | 0.49 | −57.5% | −32.9% | +3.7% | 0.80 | 2/4 |
| blend | +11.9% | 27.1% | 0.56 | −42.2% | −36.7% | +2.8% | 0.87 | 2/4 |
| quality_defensive | +2.0% | 21.5% | 0.20 | −34.3% | −26.1% | −7.1% | −1.74 | 1/4 |
| **SPY** | **+15.7%** | 17.2% | **0.94** | **−23.8%** | −19.9% | | | |
| IWM | +9.5% | 23.4% | 0.51 | −35.4% | −29.1% | | | |
| universe EW | +9.1% | 26.5% | 0.47 | −39.0% | −32.3% | | | |

Where the gap to SPY comes from (CAGR, adds up exactly):

| | gap to SPY | small caps vs large | universe artifact | stock picking |
|---|---|---|---|---|
| incumbent_like | −1.3% | −6.2% | −0.4% | +5.3% |
| value | −2.7% | −6.2% | −0.4% | +3.9% |
| momentum | −2.9% | −6.2% | −0.4% | +3.7% |
| blend | −3.8% | −6.2% | −0.4% | +2.8% |
| quality_defensive | −13.7% | −6.2% | −0.4% | −7.1% |

## Reading

1. **The pond cost more than any strategy choice.** Small caps (IWM) trailed SPY by
   6.2%/yr over these 7.5 years. That single term is larger than the whole spread
   between the best and the second-worst contestant.
2. ~~**The current style picked best within the pond, but it is not proven.** +5.3%/yr
   over the universe, positive in 3 of 4 folds, but t = 1.07.~~ *(run 1, look-ahead
   prices. On real prices: +5.0%/yr inside the small-cap pool, still unproven; see
   Run 2 above and `universe_study_RESULT.md`.)*
3. **Risk is the other half of the gap.** The best contestant had a −50% drawdown and a
   −38% month (March 2020) against SPY's −24% and −20%; Sharpe 0.63 vs 0.94. Holding
   SPY would have earned more with about half the drawdown.
4. **The survivorship artifact is small here:** universe EW trailed IWM by 0.4%/yr,
   inside the measured ~±1%/yr band (`docs/survivorship_findings.md`).
5. **Costs and cadence:** holding-rule turnover 3–7x a year, 1.2–2.7%/yr in costs.
   Quarterly rebalancing was *worse* for every contestant here (descriptive only).
6. **Environments (descriptive, n = 15 months below the 200-day average):** every
   picking contestant did better vs SPY in the 15 months when SPY was below its 200-day
   average, and about even or worse in the 75 months above it. Fifteen months, mostly
   two episodes (2020, 2022): far too few to build a switching rule on.
7. **quality_defensive lagged by 7.1%/yr.** Not investigated: it fails every test,
   so no decision depends on why.

## What this CAN and CANNOT establish

- **CAN:** in 2019-04 to 2026-09, on today's small caps, none of these five styles
  out-compounded SPY after costs, and the small-cap/large-cap gap was the largest
  single reason.
- **CANNOT:** confirm (or refute) a stock-picking edge: t ≈ 1 on ~7.5 years. Cannot say
  small caps will keep trailing; the period is one draw, and small caps have led in
  other decades. Cannot judge the production model itself (styles, not the live code).

## Limits

Current-constituent survivorship (measured here at −0.4%/yr); window limited by SEC
coverage to 7.5 years; today's sectors; style approximations; arithmetic-mean verdict
rule (see above); one history; 15-name portfolios are noisy.

## What would move the goal (each a new pre-registered study, nothing live)

1. **The pond:** run the same picking on a wider pool (mid and large caps), and an
   S&P core plus a small-cap picking sleeve, judged on **CAGR** and drawdown vs SPY.
2. **More history for the picking signal:** a separate pre-registration with a lower
   coverage bar to start earlier than 2019, so the t ≈ 1 result is retested on years it
   has not seen.

No action taken.
