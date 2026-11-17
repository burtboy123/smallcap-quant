# Pre-registration — choosing from a wider pool (universe study)

**Written 2026-10-11, before any result exists.** Implemented by
`research/universe_study.py`. Nothing below may change after the first run; a rule that
cannot be computed as written counts as **not passing**.

## Why

The strategy race (`strategy_race_RESULT.md`) found that over 2019-04 to 2026-09 small
caps as a group trailed the S&P 500 by 6.2%/yr, more than the best stock picking added
(+5.3%/yr, not proven). That run also used prices adjusted for later splits for market
cap, which leaks the future into past valuations; its picking numbers are being
re-measured (Run 2). This study asks the next question: **does choosing from a wider
pool, or keeping the picking while dropping the small-cap exposure, out-compound SPY?**

## Data (read-only)

- Prices: `research/price_store.py` (`data/research/prices.db`): the real price on
  each past day (split-adjusted close × later split ratios) for the $3 floor, market
  cap and dollar volume; the total-return close for returns, momentum and volatility;
  SPY and IWM total-return closes from the same store.
- Fundamentals: the SEC XBRL panel (point in time by filing date), extended to the
  IWV (Russell 3000) list.
- Lists: today's iShares IWV (Russell 3000) and IWM (Russell 2000) holdings; sectors
  from the iShares files. Current-constituent survivorship applies to every pool.

## Method shared by every pool

Exactly the strategy race's `incumbent_like` rules: score 0.415 quality + 0.338 value
+ 0.246 momentum (same factor definitions), health gate ≥ 4 of 6, close ≥ $3, median
dollar volume ≥ $5m, market cap ≥ $50m, 15 names equal weight, at most 4 per sector,
keep a holding while it ranks in the top 30, monthly, 0.4% per side on every weight
traded. Percentile ranks are computed **within each pool**.

## Contestants (5, fixed)

| Name | What |
|---|---|
| `small` | pool = IWM list, market cap ≤ $10bn (today's system) |
| `wide` | pool = IWV list, no upper size limit |
| `large` | pool = the 1,000 largest IWV names by market cap on each date (≈ Russell 1000) |
| `core70` | 70% SPY + 30% `small`, rebalanced to 70/30 monthly; 0.4%/side on the sleeve's traded weight, 0.05%/side on SPY's |
| `hedged` | 100% SPY + 100% `small` − 100% IWM (keep the picking, drop the small-cap exposure); minus **2.0%/yr** for financing and borrowing. Uses leverage (200% long, 100% short) |

## Window

**Verdict window:** from the first month (2010 or later) in which both the `small` and
the `large` pool have ≥ 300 fully scored names, to the last complete month. All five
contestants on the same months. **Descriptive only:** `large` and `wide` again from
their own first month with ≥ 300 names (longer history, no verdict).

## Verdict vs SPY (judged on compounding, what an account earns)

- **BEATS:** CAGR above SPY's **and** t ≥ **2.6** on monthly active returns (five
  contestants, Bonferroni) **and** CAGR above SPY's in **≥ 3 of 4** equal consecutive
  folds **and** max drawdown no more than **10 pp** deeper than SPY's.
- **PROMISING:** CAGR above SPY's and in ≥ 3 of 4 folds, without the rest.
- **DOES NOT BEAT:** anything else.

(The strategy race tested the average monthly active return, which flatters volatile
portfolios; that is why this study judges on CAGR.)

## Also reported

CAGR, volatility, Sharpe, max drawdown, worst month, growth of $10,000; for the three
pools, the gap to SPY split into (pool EW − SPY) + (contestant − pool EW).

## What the result may lead to (fixed now)

1. `large` or `wide` BEATS → candidate for a **paper** slot in the next forward-test
   quarter.
2. `hedged` BEATS or PROMISING while `small` beats its own pool → the asset is stock
   picking without the small-cap exposure; next step a **paper** forward test of that
   structure. It needs shorting and leverage; real money stays gated
   (`docs/real_money_pilot_plan.md`).
3. Nothing BEATS or is PROMISING → the report says plainly that an S&P 500 index fund
   was not beaten by any of these in this period; no live change.
4. Ties go to `small` (today's system). Nothing goes live from a backtest; the Q4b
   forward test and its criteria are untouched.

## Known limits, stated now

Current-constituent survivorship (larger for `large`: today's large caps include
yesterday's small-cap winners); window set by SEC coverage; iShares sectors as of
today; the 2%/yr financing cost is an estimate; one history.

No action is taken by this study.
