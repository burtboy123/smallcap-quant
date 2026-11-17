# Result — is the small-cap stock picking real? (out-of-sample years)

**Run 2026-10-12 on the PC**, exactly as pre-registered in `oos_picking_PREREG.md`
(merged before the run), on real past prices (`research/price_store.py`) and
point-in-time SEC fundamentals. Report: `research/output/oos_picking/REPORT.md` on the
PC. No live change made.

## Verdict: NOT CONFIRMED (all three clauses fail)

| Clause (pre-registered) | needed | result |
|---|---|---|
| stock picking: picks CAGR − pool CAGR | ≥ +2.0%/yr | **−2.2%/yr** |
| t on monthly (picks − pool) | ≥ 2.0 | **−0.55** |
| periods where the picks out-compounded the pool | ≥ 3 of 4 | **2 of 4** |

Window **2012-04 to 2019-02**: 83 monthly returns (~6.9 years), from the first month
with ≥ 150 scored names. Monthly returns do not overlap, so effective n = nominal n = 83
for the t; the four periods are consecutive blocks of ~21 months. Costs included (0.4%
per side). t = −0.55 is inside the noise: **no detectable picking effect**, not proof
that picking hurt.

Pre-registered consequence: *the 2019–2026 picking result is most likely luck or
specific to that period. The hedged paper test still runs to its own date under its own
rules, but the realistic expectation is low, and an S&P 500 index fund stays the
benchmark for any real money.*

## The same window, in money (descriptive)

| 2012-04 to 2019-02 | CAGR | Sharpe | max DD |
|---|---|---|---|
| your picks (15) | +8.4% | 0.52 | −34.8% |
| the small-cap pool, equal-weight | +10.6% | 0.67 | −21.2% |
| IWM | +11.2% | 0.71 | −21.4% |
| **S&P 500 (SPY)** | **+12.6%** | **1.03** | **−13.7%** |
| hedged: SPY + picks − IWM − 2% | +7.4% | 0.56 | −28.7% |

The hedged structure trailed SPY by 5.2%/yr, at about half its return per unit of risk
and twice its drawdown.

## The reproduction check failed: the +5.0% does not survive a lower liquidity floor

The pre-registration also ran the same rules on 2019-03 to 2026-09, "a check that the
two changes did not make a different strategy". It did not pass that check:

| 2019-03 to 2026-09 (91 months) | picking vs own pool | hedged CAGR | SPY CAGR |
|---|---|---|---|
| universe study (`small`, $5m floor) | +5.0%/yr | +18.4% | +15.9% |
| this study ($2m floor, run continuously from 2012) | **−0.0%/yr** | +13.9% | +15.9% |

**Not a code difference.** With the $5m floor and the same start, this study's code
reproduces the universe study's picks, pool and hedged returns exactly (synthetic
market, `tests/test_oos_picking.py::test_with_the_race_settings_it_reproduces_the_universe_study`).
What differs are the two pre-registered changes: the **$2m dollar-volume floor** (adds
small caps trading $2–5m a day) and **holdings carried in** from before 2019-03 (a
held name stays while in the top 30, so this fades within months). The floor is the
likely main cause; the two were not separated, because no further variant was run after
seeing the result.

**Reading:** a real stock-picking skill should not vanish when the same pool, in the
same years, is widened to slightly less-traded companies. The +5.0% depends on one
liquidity floor in one 7.5-year window. That is fragility, the mark of a result that
was found rather than an edge that exists.

## Sign stability across every picking measurement so far

| Measurement | window | picking vs its own pool |
|---|---|---|
| universe study, small pool, $5m | 2019-03 to 2026-09 | **+5.0%/yr** (the one positive) |
| universe study, wide pool | 2019-03 to 2026-09 | 0.0%/yr |
| universe study, 1,000 largest | 2019-03 to 2026-09 | −0.8%/yr |
| universe study, wide and large, longer history | 2011-03 to 2026-09 | about −2%/yr |
| **this study, small pool, $2m** | **2012-04 to 2019-02** | **−2.2%/yr** |
| this study, small pool, $2m | 2019-03 to 2026-09 | −0.0%/yr |

One positive result in six, and it is the configuration and window the edge was found
in. The sign does not hold.

## What this CAN and CANNOT establish

- **CAN:** in 2012-04 to 2019-02 the incumbent-like picking did not beat its own
  small-cap pool, and the hedged structure lost clearly to SPY on return and on risk.
  With a $2m floor the picking also showed nothing in 2019–2026.
- **CANNOT:** test the exact $5m version before 2019: there were too few such names
  (the universe study's window, which needed 300 scored names, began only in 2019-03).
  An edge confined to small caps
  trading over $5m a day cannot be ruled out, but it would rest on one in-sample
  window with no mechanism for why the floor should matter, so it is not evidence.
  One history.

## Limits

- **Survivorship:** today's small caps traced back, i.e. companies that stayed small
  (yesterday's winners that grew out of the index are missing). The broad bias was
  measured at ~±1%/yr, sign uncertain (`docs/survivorship_findings.md`), and it applies
  to the picks and the pool alike. It cannot turn t = −0.55 into a confirmation.
- **Thinner early coverage:** 166–216 scored names a month in 2012–2017 (439 in 2019,
  1,010 in 2026), against the 300 the original rule needed.
- **Noise:** 15-name portfolios (picks max drawdown −34.8% vs the pool's −21.2%).
- The 150-name start and $2m floor were fixed in advance, but they make this a slightly
  different strategy from the one measured at +5.0% (see the reproduction check).
- The 2%/yr hedge financing is an estimate.

## What it means for the goal (beating SPY)

Nothing tested so far beats an S&P 500 index fund reliably. The one picking result
that did is not confirmed in other years and does not survive a small change of
liquidity floor in its own years. For any real money, **an S&P 500 index fund is the
benchmark** (`docs/real_money_pilot_plan.md` remains the gate).

- The **hedged paper test** keeps running to 2027-09-30 under its own unchanged rules
  (`hedged_paper_PREREG.md`); the realistic expectation is now low.
- The **Q4b forward test** is evaluated on or after 2026-12-29 under its own unchanged
  criteria.

No action taken.

---

## Recomputation (2026-11-10, on the PC)

The verdict numbers (edge, t, periods ahead), the money table and the 2019-on check were
recomputed from `research/output/oos_picking/monthly_returns.csv` and agree with the
report to the printed precision; the hedged series rebuilds from its components to 1e-8.
Two figures the report does not print, from the same file:

| 2012-04 to 2019-02 | vol | worst month |
|---|---|---|
| your picks (15) | 18.6% | −9.0% |
| small-cap pool, equal-weight | 17.2% | −12.3% |
| IWM | 17.0% | −12.3% |
| S&P 500 (SPY) | 12.2% | −9.9% |
| hedged | 14.9% | −9.6% |

Picks minus pool by calendar year: 2012 +0.6 pp, 2013 +1.6, 2014 −10.8, 2015 −11.3,
2016 −4.2, 2017 −1.3, 2018 +9.5, January–February 2019 +3.3. Two of the seven full
years ahead: the sign is not stable.
