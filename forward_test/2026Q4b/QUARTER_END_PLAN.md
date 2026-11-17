# 2026Q4b — Quarter-End Plan (written 2026-10-04, BEFORE results are known)

**Purpose.** Fix *how* Q4b is evaluated and *what happens next* while the outcome is
still unknown, so December's decision is mechanical rather than argued after the fact.
Nothing here changes a threshold: `EVAL_CRITERIA.md` stays byte-identical, as committed.
This file only supplies the **Q4b-specific window and inputs** that the carried-forward
criteria left at their Q3 values (evaluation date 2026-09-14, start 2026-06-17, SPY
baseline 750.57, 13-name Incumbent).

> **PAPER ONLY.** Completing Q4b authorizes nothing. Real money stays gated by
> `docs/real_money_pilot_plan.md` §1 and a separate, explicit decision.

---

## 1. The Q4b window (substitutions into EVAL_CRITERIA.md)

| Criteria field (Q3 value) | Q4b value | Source |
|---|---|---|
| Common start (2026-06-17) | **2026-09-30**, first closes | `meta.json` `baseline_date` |
| SPY baseline (750.57) | **762.63** (SPY close 2026-09-30) | `meta.json` `spy_baseline` |
| Evaluation date (2026-09-14) | **2026-12-29** (Tue): the README's ~90 days | `README_2026Q4b.md` |
| Books | Incumbent-minus-ML **15**, Challenger value-tilt **15** | `incumbent_book.csv`, `value_tilt_book.csv` |
| Entry value_score (`factor_scores` @ 2026-06-01) | `factor_scores` @ **2026-09-21** | `meta.json` `calc_date`, frozen in `entry_scores.csv` (§2) |

**Sessions recorded after 2026-12-29 are excluded**, exactly as Q3 excluded the days
after 2026-09-14. The evaluation date is fixed here *now*; moving it after seeing results
is the goal-posting §5 of the criteria warns about.

## 2. Do now (October): freeze the inputs

```powershell
python forward_test/freeze_entry_scores.py --period 2026Q4b
git add forward_test/2026Q4b/entry_scores.csv
git commit -m "Q4b: freeze entry factor scores for the value-consistency check"
git push
```

The value-consistency check (a **hard** prerequisite, §2 of the criteria) needs each
name's entry `value_score`. Those rows live only in the database and can be recalculated
or pruned before December; the script copies them into the period folder once and
refuses to overwrite. Check that the printed `value_score coverage` is complete.

## 3. During the quarter (unattended)

- Snapshots every trading day 23:30; the Monday email reports the curve, drawdown,
  account drift and the week's executions.
- **The forward test tracks intended weights, not fills.** Paper-account trades do not
  change Q4b's numbers. Keep the paper account aligned anyway (§6 uses its record).
- If a Monday email shows a snapshot problem, fix it that week. A gap that reaches the
  evaluation date undermines the window (criteria §1: "the equal-weight daily equity
  curve over the window").

## 4. Evaluate (on or after 2026-12-30)

1. **Data complete.** `python -m monitoring.health_check -v` is clean and
   `price_snapshots.csv` has every session 2026-09-30 → 2026-12-29 for all 26 names + SPY.
   Note any carried-forward cells (delistings): criteria §1 counts a delisted name as a
   realized loss to its last traded price.
2. **Compute §1 of the criteria** for the window, using only rows dated ≤ 2026-12-29:
   total return per book, alpha vs SPY (762.63 base), relative spread, max drawdown,
   hit rates, and the **value-consistency rank IC** from `entry_scores.csv`.
   **Automated:** `python forward_test/evaluate.py --final --write` computes every §1
   number from the frozen inputs, runs the drop-one-name test and marks each §2 clause.
   It refuses if the data does not reach the evaluation date, and it never marks the
   judgement clause (a)5, so an ADOPT is always a human decision. Its output is the
   input to step 4, not the result itself.
3. **Apply §2 exactly as written.** ADOPT needs *all* of (a)1–5; KEEP if *any* of
   (b)1–4; otherwise INCONCLUSIVE, which **means stay on the Incumbent**. Run the
   drop-top/bottom-contributor test from (c)3.
4. **Write `EVALUATION_RESULT.md`** in this folder, in the Q3 format
   (`2026Q3_parallel/EVALUATION_RESULT.md`): verdict first, every clause with its number,
   and "no action taken" stated. Commit it.

No threshold, metric definition or date may change in step 2 or 3. If something truly
cannot be computed as defined, record that in the result and treat the affected clause
as **not satisfied**. That fails safe: toward KEEP, never toward ADOPT.

## 5. Next quarter (2027Q1), after the verdict

The verdict decides *which* construction is built. Either way, build it through the full
production path (`forward_test/build_q4_books.py` pattern: gate → regime bias → ensemble
rank → `select_under_limits` → risk stage → `assert_book_investable`), into a **new**
folder; nothing in `2026Q4b/` is edited after evaluation.

The active period is named in one setting, `config/settings.py` `ACTIVE_FT_PERIOD`
(since 2026-10-05). The snapshot default, the broker's kill-switch snapshot (5.3), the
dashboard and every printed `--book` hint read it. Change it together with:

| File | Setting |
|---|---|
| `config/settings.py` | `ACTIVE_FT_PERIOD` |
| `forward_test/snapshot.py` | add the new period to the `PERIODS` registry (start date, baseline mode) |
| `forward_test/run_snapshot.bat` | default `PERIOD` (batch cannot read the setting) |
| `forward_test/build_q4_books.py` | `OUT` (copy to a new builder per quarter) |

`tests/test_silent_failure_rules.py` fails until these agree, and fails if any other
code names a period. The monitoring was built after a period mismatch hid nine silent
days.

Then: freeze the new period's entry scores (§2), run the health check, dry-run the paper
rebalance from the Q4b book to the new one (it must show the exits as SELLs), and execute
only during US hours.

## 6. The execution side (separate from the selection verdict)

The paper account's execution record answers a different question: can the book
actually be traded? At quarter end, summarise from `broker/execution_log/` and the
reconciliation files:

- fill rate and unfilled names per run, and **cost vs reference price** (bps,
  notional-weighted). That is the input to the real-time market-data decision: if delayed
  quotes caused unfills or a cost above ~10–15 bps, a subscription likely pays for itself.
- gate blocks and halts during the quarter, and whether each was correct.

## 7. What this does NOT decide

Real money. Q4b completing satisfies, at most, the "forward test completed and formally
evaluated" style gates in `docs/real_money_pilot_plan.md` §1. Every other gate there
still stands, and the plan itself says it authorizes nothing.
