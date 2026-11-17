# Result — distress (CHS) and accruals as value-trap filters

**Run 2026-10-11 on the PC's database**, exactly as pre-registered in
`distress_accruals_PREREG.md` (committed before any result). No live change made.

> **Code removed 2026-10-12** (finished study, cleanup). It is in git history: `git show aaa4116:research/distress_accruals.py`.

## Verdict: REJECT both — not testable on the data we have (not "proven useless")

The run reported **UNRELIABLE** (the mixed-source consistency gate failed). That gate
does not decide the outcome: under the pre-registered rule both scores **fail the
coverage condition regardless** (0 of 22 years at ≥ 60% of the universe), and neither
t-stat reaches the bar. So the verdict is REJECT for both, by the rule as written.

| | mean IC (cheap, 3m) | years negative | t (non-overlap) | eff. n | coverage years ≥ 60% |
|---|---|---|---|---|---|
| CHS distress | −0.025 | 5/6 | −0.65 | 18 | 0/22 |
| Accruals | −0.039 | 5/6 | −1.39 | 18 | 1/22 |

## What the data showed

- **Fundamentals exist only from 2021.** Coverage is 0% for 2005–2020 and peaks at 47%
  (2024). The study covers ~4.5 years with effective n = 18: too short to confirm
  or refute either effect.
- **CHS:** wrong-signed in the quintile spread (most distressed +7.3% vs least +0.9%,
  forward 3m, pooled) and IC near zero. Consistent with a junk rally in this
  window; no evidence for the filter.
- **Accruals:** the right sign in 5 of 6 years and a large quintile spread (high accruals
  +3.3% vs low +13.9%), but t = −1.39 is not significant and coverage fails. The only
  one worth retesting if longer history becomes available. NOT a finding.
- **Consistency gate:** yfinance-row medians differ from SimFin's (e.g. 2023: 0.057 vs
  0.006; 2022: −0.118 vs 0.012). The two medians are over different sets of companies
  near zero NI, so this is not proof of a scale bug; it is unresolved. A same-company,
  same-year comparison would settle it.
- **CHS coverage falls in 2025–26 (15–17%) while accruals' does not:** the newest
  yfinance rows for many names are the "prior-year" rows, which store no shares or
  cash, so market value cannot be computed.

## What would make this testable

Point-in-time fundamentals with a longer history. SEC XBRL `companyfacts` (free, one
request per company) carries net income, operating cash flow, assets, liabilities,
equity, cash and shares back to ~2009, each with its `filed` date — point-in-time
by construction and one consistent source. That would also settle the SimFin/yfinance
scale question. It requires SEC access from the PC.

No action taken.

---

# Run 2 — SEC XBRL fundamentals (Amendment 1), 2026-10-11 13:41

`python -m research.distress_accruals --source sec`: 1,824/1,925 tickers fetched,
76,259 point-in-time ticker-periods for 1,777 tickers, 259 monthly rebalance dates.
Same scores, thresholds and decision rule as pre-registered.

## Verdict: REJECT both — and CHS is significantly WRONG-signed

| | mean IC (cheap, 3m) | years negative | t (non-overlap) | eff. n | coverage years ≥ 60% | quintile spread (most − least, 3m) |
|---|---|---|---|---|---|---|
| CHS distress | **+0.042** | 3/16 | **+2.46** | 62 | 7/22 | **+8.8%** (12.0% vs 3.2%) |
| Accruals | +0.006 | 8/16 | +0.32 | 62 | 6/22 | −6.1% (4.5% vs 10.6%) |

Coverage is now adequate from 2020 (61% → 85%) and thin before 2012; the coverage
clause fails on the early years. The verdict does not depend on that clause: CHS fails
the sign condition and accruals fails both sign and significance.

## Reading

- **Accruals: a wash.** IC ≈ 0, negative in exactly half the years, t = +0.32. The
  pooled quintile spread (−6.1%) is a mean, driven by a few large outcomes, and the
  rank IC does not support it. No effect.
- **CHS: distressed cheap stocks did BETTER, significantly (t = +2.46, 13 of 16 years).**
  This must NOT be read as "buy distress". The universe is **today's** IWM: every
  name in it survived to 2026. A firm that was distressed in, say, 2015 is in this
  sample only because it recovered; the distressed firms that failed are absent.
  Conditioning on survival turns past distress into a predictor of later recovery.
  That is survivorship bias concentrated exactly where this score looks, and it can
  flip the sign. The broad-universe measurement (~±1%/yr,
  `docs/survivorship_findings.md`) does not bound it, because that is an average
  over all names, not over the distressed tail conditional on survival.
- The pre-registration expected this bias to work "against finding the effect". It
  did, strongly enough to reverse the sign. So this test cannot say whether distress
  matters in small caps; it can only say the current-constituent universe cannot test it.

## Conclusion

- Neither filter enters the system. No live change.
- Distress-style signals cannot be tested honestly on a current-constituent universe.
  Doing so would need a survivorship-free universe WITH prices for dead names, which
  free sources do not provide (0/15 hit rate on yfinance, `survivorship_findings.md` §6).
  Closed unless such data becomes available.
- Kept: `data/sec/` (point-in-time SEC fundamentals, 2010+, 1,777 tickers) for future
  research. Any study on it inherits the same survivorship caveat for distress-related
  questions; it matters much less for questions that do not condition on failure.

No action taken.
