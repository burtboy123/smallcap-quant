# Pre-registration — paper forward test of the hedged structure

**Written 2026-10-12, before any forward result of this structure exists.** Implemented
by `forward_test/hedged_paper.py`. Nothing below may change after the first reading;
a rule that cannot be computed as written counts as **not passing**.

## Why

`universe_study_RESULT.md`: over 2019-03 to 2026-09, holding the S&P 500, adding the
system's small-cap picks and shorting the Russell 2000 (IWM) out-compounded SPY by
2.5%/yr (PROMISING, t = 0.70), but with a lower Sharpe (0.84 vs 0.95): more return,
mostly paid for with more risk. The pre-registered next step was a paper forward test.
This one is judged so that "more return only from more risk" does **not** pass.

## What is tracked (paper only, nothing traded)

Each trading day, from **2026-09-30** (the 2026Q4b start; that book was fixed before the
start, and SPY/IWM closes are facts of each day, so computing from the start is not
look-ahead):

    hedged return = SPY return + (forward-test portfolio return − IWM return) − 2%/yr / 252

- The portfolio is the active forward-test period's incumbent book, exactly as the
  forward test records it (`equity_curves.csv`); later periods continue the chain.
- Same price basis as the forward test: raw closes, price change only (no dividends),
  for SPY, IWM and the portfolio alike.
- 2%/yr covers financing and borrowing IWM (an estimate). It implies 200% long and 100%
  short; margin calls are not modelled.

## Evaluation (on or after 2027-09-30, about 250 trading days)

**PASS** needs all three:
1. Hedged compounded return above SPY's over the whole period;
2. Hedged Sharpe ratio (daily returns, annualized, no risk-free rate) **at least** SPY's;
3. Hedged max drawdown no more than 10 pp deeper than SPY's.

Anything else: **NOT SHOWN**. Readings before the evaluation date are information, not a
verdict, and must not move the date.

## What a PASS may lead to

Only a decision by the user whether to plan a small real-money pilot under
`docs/real_money_pilot_plan.md`, which remains the gate. Nothing is automatic. A
NOT SHOWN ends this structure unless a new, separately pre-registered test is written.

## Known limits, stated now

One year is short (one draw of ~250 days, effective n small); 15-name portfolios are
noisy; price-only basis; the financing figure is an estimate; real shorting can be
recalled or cost more in a squeeze.

No action is taken by this test.
