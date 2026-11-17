# Findings — quarterly vs 12-month fundamentals in the live factor inputs

**Audited 2026-10-11** with `scripts/audit_fundamentals_mix.py` (read-only), against
SEC XBRL 12-month net income as ground truth (`data/sec_xbrl.py`).

> **Code removed 2026-10-12** (finished study, cleanup). It is in git history: `git show aaa4116:scripts/audit_fundamentals_mix.py`.

## The issue

`fundamentals` holds SimFin **quarterly** rows (`filing_date` set) and yfinance
**12-month** rows (`filing_date` NULL) with no source column. Found by the distress study.

## What the live engine actually uses

- **Current rows: fine.** On 2026-09-21 (Q4b build) and 2026-10-11, 1,915 of 1,917
  tickers use a 12-month yfinance row. Compared with SEC's 12 months for the period those
  figures really cover, 1,602 of 1,773 agree within 0.7–1.4×, median 1.00.
- **The live yfinance row is labelled with an older quarter-end** (to pass the 90-day
  storage rule) but holds current trailing figures. Harmless for live use; it is why
  comparing it with SEC at its label looked wrong.
- **All 15 held Q4b names are unaffected:** current and prior-year rows are 12-month;
  11/15 match SEC exactly, KODK/MYE within 1.4×, TK/SENEA only have SEC annual figures
  to compare with, SSRM (2.38×) is a definition difference with the same sign.
- **The F-score's prior-year row was a SimFin quarter for 129 of 1,869 tickers
  (6.9%).** For those, YoY ROA (F3) and asset turnover (F9) compared a quarter with a
  year and were awarded too easily, which could move names across the f>=5 gate.

## Fix (2026-10-11, for the 2027Q1 build; the Q4b book is unchanged)

`factors/calculator.twelve_month_prior`: a quarterly prior-year row's flow fields become
the sum of four consecutive point-in-time quarters ending at it; balance-sheet fields
unchanged; without four quarters the row is kept as before, so coverage never drops.
Tested, including that no quarter filed after the prior-year date is summed.

## Still open (not acted on)

- 55 of 1,773 live rows (3%) have the opposite sign to SEC; 14 look quarter-scaled.
  Small; worth a look before relying on single names.
- The current-row label mismatch above (cosmetic for live use).
