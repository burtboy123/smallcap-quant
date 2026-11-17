# Result — universe study (wider pools vs the S&P 500)

**Run 2026-10-12 on the PC**, exactly as pre-registered in `universe_study_PREREG.md`
(committed before any result), on real past prices (`research/price_store.py`; split
check: 1,158 of 1,224 splits line up). Report: `research/output/universe_study/REPORT.md`
on the PC. No live change made.

> **Update 2026-10-12:** the small-cap picking edge below (+5.0%/yr) was **not
> confirmed** in the years before 2019, and it did not survive a lower liquidity floor
> in these same years (`oos_picking_RESULT.md`).

## Verdict: nothing BEATS SPY; one structure is PROMISING, and it is mostly extra risk

| Contestant | verdict | CAGR | vs SPY /yr | t | periods ahead | Sharpe | max DD |
|---|---|---|---|---|---|---|---|
| small (today's system) | DOES NOT BEAT | +13.7% | −2.2% | 0.12 | 2 of 4 | 0.61 | −47.9% |
| wide (all caps) | DOES NOT BEAT | +13.2% | −2.7% | 0.05 | 2 of 4 | 0.59 | −49.3% |
| large (1,000 largest) | DOES NOT BEAT | +10.3% | −5.5% | −0.47 | 0 of 4 | 0.52 | −41.7% |
| core70 (70% SPY + 30% small) | DOES NOT BEAT | +15.6% | −0.3% | 0.10 | 2 of 4 | 0.85 | −30.2% |
| **hedged** (SPY + small − IWM) | **PROMISING** | **+18.4%** | **+2.5%** | 0.70 | 3 of 4 | 0.84 | −33.6% |
| SPY | | +15.9% | | | | **0.95** | −23.8% |

Window 2019-03 to 2026-09 (91 monthly returns, ~7.6 years; first month with ≥ 300
scored names in both the small and the large pool). Costs included.

**By the pre-registered reading rule (2):** `hedged` is PROMISING and `small` beats its
own pool (+5.0%/yr), so the next step allowed is a **paper** forward test of the hedged
structure. Nothing goes live from this.

## Reading

1. **The hedged structure earned more, not better.** +2.5%/yr over SPY, but with 23.5%
   volatility against 17.1% and a −34% drawdown against −24%. Per unit of risk it did
   *worse* than SPY (Sharpe 0.84 vs 0.95). As a rough estimate (not a tested
   contestant): holding ~1.37x SPY on margin, the same volatility, would have earned
   about 18–20%/yr, i.e. about the same. So the extra return is mostly the extra risk
   of leverage, not proven skill. And t = 0.70: about a 1-in-4 chance that luck alone
   gives this much.
2. **Stock picking showed up only among small caps.** Picking added +5.0%/yr inside
   the small-cap pool, but the pool itself trailed SPY by 7.2%/yr. In the wide pool
   picking added **0.0%**, among the 1,000 largest **−0.8%**. Over the longer history
   (2011-03 to 2026-09, descriptive only) picking trailed its own pool by about
   **2%/yr** in both the wide and the large pool.
3. **Large caps were the worst pool for this method.** An equal-weighted pool of the
   1,000 largest trailed the cap-weighted S&P 500 by 4.8%/yr: in this period a handful
   of giant companies carried the index, and equal-weighting 15 large names cannot hold
   them all.
4. **70/30 is SPY with a bumpier ride:** −0.3%/yr, a deeper drawdown (−30% vs −24%).

## What this CAN and CANNOT establish

- **CAN:** over 2019-03 to 2026-09, none of these pools or mixes out-compounded SPY on
  its own, and the only structure that did so took more risk to do it.
- **CANNOT:** confirm a picking edge (small-cap picking +5.0%/yr is directional; the
  hedged t is 0.70). Cannot say the next 7 years look the same: small caps and
  equal-weighting have led in other decades.

## Limits

Current-constituent survivorship (larger for the large pool: today's large caps include
yesterday's small-cap winners); 66 of 1,224 splits (5%) did not line up in the split
check; window set by SEC coverage; today's sectors; the 2%/yr financing is an estimate;
the leveraged-SPY comparison is a back-of-envelope estimate, not a tested contestant;
one history.

## What it means for the goal (beating SPY)

Over this period, **an S&P 500 index fund was the best risk-adjusted choice** among
everything tested. The only route that out-earned it did so by borrowing, and borrowing
SPY itself would have done about as well. That is a wash for skill, reported as a wash.
The pre-registered next step, if wanted, is a paper forward test of the hedged
structure; real money stays gated.

No action taken.
