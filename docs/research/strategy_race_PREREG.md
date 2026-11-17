# Pre-registration — strategy race vs the S&P 500

**Written 2026-10-11, before any result exists.** Implemented by
`research/strategy_race.py`. Nothing below may change after the first run; if a rule
cannot be computed as written, that contestant is treated as **not passing**.

## Question

The end goal is a portfolio that earns more than the S&P 500 (SPY). Two things decide
that, and they need different fixes:

1. **Stock picking:** does a strategy beat the other stocks it could have chosen from?
2. **The pond:** the system fishes only in small caps (Russell 2000). If small caps as a
   whole trail SPY by more than picking adds, no factor formula beats SPY, and the
   lever is the universe, not the weights.

So every contestant's gap to SPY is split into three parts that add up exactly:

    strategy − SPY  =  (IWM − SPY)               small caps vs large caps (real)
                     + (universe EW − IWM)       survivorship + equal weighting (artifact)
                     + (strategy − universe EW)  stock picking, after costs

`universe EW` = equal weight of every name that passes the same filters on each date.
Because it has the same survivorship as the strategies, **the third term is the clean
measure of skill**; the second term measures how much the current-constituent universe
itself flatters (or hurts) the comparison.

## Data (read-only)

- Prices: `prices` table (2005 →), adjusted close for returns, raw close × volume for
  liquidity and size. Sectors: `stocks` table (today's sector, a small bias).
- Fundamentals: the SEC XBRL panel (`data/sec/sec_fundamentals.db`, point in time by
  filing date: a period is used only from the latest `filed` date of the facts it needs).
- Benchmarks: SPY and IWM, dividend-adjusted (yfinance on the PC, cached).
- Universe: today's IWM constituents (current-constituent survivorship; measured net
  bias ~±1%/yr, sign uncertain, `docs/survivorship_findings.md`).

## Factors (fixed definitions; percentile ranks across eligible names on each date)

- **Value:** mean rank of earnings yield (TTM net income / market cap), book-to-market
  (equity / market cap), cash-flow yield (TTM operating cash flow / market cap).
- **Quality:** mean rank of ROA (TTM NI / assets), cash flow / assets, low leverage
  (−liabilities / assets), low accruals (−(NI − OCF) / assets).
- **Momentum:** price return from 12 months to 1 month ago.
- **Low volatility:** −(standard deviation of daily returns, past 252 days).
- **Health checks (F-score-lite, 6 of Piotroski's 9 that SEC facts allow):** ROA > 0;
  OCF > 0; ROA up vs a year earlier; OCF > NI; leverage down vs a year earlier; shares
  not increased vs a year earlier. (Gross margin, asset turnover and current ratio need
  facts the panel does not hold.)

These approximate the production styles; they are **not** the production code (which
also uses EV/EBITDA, FCF, ROIC, sentiment, short interest and more, none of which exist
point-in-time this far back). This race compares **styles**.

## Contestants (5, fixed)

| Name | Score | Health gate |
|---|---|---|
| `incumbent_like` | 0.415 quality + 0.338 value + 0.246 momentum (production 0.27/0.22/0.16 rescaled) | ≥ 4 of 6 |
| `value` | 0.70 value + 0.30 quality (the current comparison portfolio's style) | ≥ 2 of 6 |
| `quality_defensive` | 0.60 quality + 0.40 low volatility | ≥ 4 of 6 |
| `momentum` | momentum | ≥ 2 of 6 |
| `blend` | average of the four portfolios above (fixed 25% each) | — |

## Portfolio rules (all contestants)

- Eligible on a date: close ≥ $3, median daily dollar volume (63 days) ≥ $5m, market
  cap $50m–$10bn, a full score, and the health gate.
- Hold **15** names, equal weight, **at most 4 per sector** (≤ 26.7%, the 30% cap).
  Fewer than 15 eligible: hold those, rest in cash at 0%.
- **Holding rule:** a current holding stays while it still passes the health gate and
  ranks in the **top 30**; free slots are filled best-first. *(Added 2026-10-11, before
  any real-data run: on synthetic data a plain monthly top-15 turned over ~11x a year,
  ~4%/yr in costs. The live system damps turnover the same way, so the race would
  otherwise penalize every contestant for churn the real system does not have.)*
- Rebalance on the first trading day of each month. Costs **0.4% per side** on every
  weight traded (0.8% round trip, `TRANSACTION_COSTS`), including the first buy.
- Window: from the first month (2010 or later) with **≥ 300 fully scored names**, to the
  last complete month. The start month is reported.
- Descriptive only (no verdict): the same contestants rebalanced quarterly, and every
  contestant's average monthly result in months when SPY was above vs below its
  200-day average (the "environment" question).

## Metrics

Monthly net returns; CAGR, volatility, Sharpe (no risk-free rate; comparable across
contestants), Sortino, max drawdown, best and worst month, CVaR 95% (mean of the worst
5% of months), annual turnover, cost drag; and against SPY and against universe EW:
annualized active return, tracking error, information ratio, t-stat of monthly active
returns (n = months; effective n reported), sign in each of 4 equal consecutive folds.

## Verdicts (per contestant, separately vs SPY and vs universe EW)

- **BEATS** — annualized active return > 0 **and** t ≥ **2.6** (2.6 not 2.0: five
  contestants, Bonferroni) **and** positive in **≥ 3 of 4 folds** **and**, vs SPY only,
  max drawdown no more than **10 pp** deeper than SPY's.
- **PROMISING (directional)** — active > 0 and positive in ≥ 3 of 4 folds, but t < 2.6.
- **DOES NOT BEAT** — anything else.

## What the result is allowed to lead to

- If no contestant BEATS SPY while one or more shows picking skill (vs universe EW) and
  `IWM − SPY` is larger than that skill: the report says **the small-cap universe is the
  main obstacle**; the next study is the universe (mid caps, or a core S&P holding plus a
  factor sleeve), not new weights.
- A contestant that BEATS SPY and has a higher information ratio than `incumbent_like`
  becomes a candidate for a **paper** slot in the next forward-test quarter. Nothing
  goes live from a backtest; the Q4b evaluation and its criteria are untouched.
- Ties and near-ties go to `incumbent_like` (the existing construction).

## Known limits, stated now

- Current-constituent survivorship (term 2 measures its size in this sample).
- Thin SEC coverage before ~2012; the coverage per year is reported.
- Today's sectors; shares from the filing (a split between filing and date distorts
  size until the next filing).
- Style approximations, not the production model.
- One history: a strategy that wins here can still lose the next decade. A backtest is
  evidence, not a forecast.

No action is taken by this study.

## Run 2 (2026-10-11, before run 2 has any result)

Run 1 read the main `prices` table for "raw close", but that column is adjusted for
later splits and dividends (`data/prices.py` stores yfinance's auto-adjusted close in
both close and adj_close). That contradicts the data section above, so run 1 did not
implement this pre-registration. Run 2 implements it as written, with prices from
`research/price_store.py`: real price = split-adjusted close × later split ratios (size,
$3 floor), real dollar volume, total-return close for returns; SPY and IWM from the same
store. Nothing else changes: same contestants, rules, window rule and verdicts.

