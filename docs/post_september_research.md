# Post-September Research Backlog

Research items to pursue **after** the 2026-09-14 forward-test evaluation. Every item
here is a **hypothesis to TEST, not a decision** — nothing is added live on intuition.
The standing gate (see bottom) applies to all of them: each is a new decision rule,
each can be curve-fit, and none ships without walk-forward validation showing it
improves **risk-adjusted** return by a margin that survives the small effective-n.

Current system for reference: monthly-rebalanced value-tilt (0.70V / 0.30Q, f>=3,
no ML), **no stop-losses, no per-stock holding-period rules, no take-profit** — a name
is held while it keeps ranking and sold when it drops out at the next monthly rebalance.

---

## Exit-rule research (post-September, walk-forward gated)

Two user-raised ideas about *when to sell*, captured as hypotheses to validate.

### (a) Stop-loss variants — test RISK-ADJUSTED impact, not just return

**Hypothesis:** some stop-loss rule improves the book's **risk-adjusted** return
(Sharpe / Sortino / drawdown-adjusted), not merely raw return.

**Known prior / reason for skepticism:** stop-losses tend to **HURT mean-reverting
value strategies.** A value tilt buys cheap names precisely because they are down; a
price-based stop systematically **sells them at the bottom, locking in the loss right
before the reversion the strategy is designed to capture.** So the naive expectation is
that a conventional trailing/percentage stop *degrades* a value book. The test must
therefore measure risk-adjusted return, because a stop can lower volatility and drawdown
while also lowering return — the question is the *ratio*, not either leg alone.

**What to test (separately — they are different rules):**
- **Conventional stop** (e.g. -15% / -20% / trailing) — expected to hurt; test to
  confirm the magnitude, don't assume.
- **Tail-only / catastrophic stop** — a stop that fires ONLY on a
  catastrophic/going-to-zero move (very large gap-down, or a distress trigger), not on
  ordinary value drawdown. This is the only variant with a plausible edge: it targets
  the **left-tail / bankruptcy case the tail audit flagged** (illiquid + leveraged +
  deep-value cluster that can go to zero) while leaving normal mean-reversion alone.
  Connects directly to `sandbox/tail_audit/FINDINGS.md`.
- Report each variant's effect on return, vol, max drawdown, CVaR, and the risk-adjusted
  ratios — walk-forward, with the effective-n honesty.

**Overfitting risk:** the stop threshold is a free parameter; it is trivially tunable to
the sample. Must clear walk-forward on out-of-sample folds, not a single split.

### (b) Holding-period rules — test vs the current monthly-until-drops-out logic

**Hypothesis:** a variable or fixed holding period beats the current
"rebalance monthly, hold while ranking" rule on risk-adjusted return.

**Reason for strong skepticism:** **per-stock holding times require predicting WHEN a
stock pays off** — a much harder prediction than *whether* it is cheap. The feature
research (`sandbox/feature_research/FINDINGS.md`) showed the factors **barely predict
*whether* a name outperforms (sign-stable IC ~0.12–0.13) and give no reliable signal
about *when*** (IC is flat-to-noisy across horizons once effective-n is respected; the
"better at long horizons" result was an eff-n<6 mirage). A per-stock timing rule is
therefore predicting a quantity the factors demonstrably cannot estimate → **high
overfitting risk.**

**What to test (least-overfit-prone first):**
- **Fixed minimum/maximum hold** (e.g. force a min 1–2 rebalance hold, or a max hold
  cap) — few parameters, testable.
- **Value-decay exit** (sell when a name's *value_score* has mean-reverted back to
  neutral, i.e. it is no longer cheap) — this is closest to the strategy's own logic and
  is roughly what "drops out of the ranking" already does; test whether an explicit
  version adds anything over the implicit one.
- **Per-stock variable timing** (predict-the-payoff-date) — flagged as **highest
  overfitting risk**; only pursue if the simpler variants show a real, walk-forward-
  surviving effect first.

**Standard to clear:** same effective-n honesty as everything else — a holding rule that
helps on one split but not across walk-forward folds is noise, not signal.

---

## Cross-reference: existing agenda items addressing the SAME fear

The underlying concern behind both ideas is **"we hold fragile names too long / eat their
blowups."** The tail audit already proposed answers to that fear that are **more
strategy-appropriate than reactive selling**, because they prevent the fragile exposure
from entering the book in the first place rather than trying to exit it after damage:

- **Liquidity floor** — exclude sub-~**$5m/day** average dollar volume (drops the names
  you cannot exit in a fire-sale anyway; `sandbox/tail_audit/FINDINGS.md`).
- **Deep-distress P/B guard** — flag/cap P/B below ~0.5 (keeps "cheapest" from meaning
  "most likely a value trap / going to zero").
- **Fragility-aware sizing** — down-weight the illiquid + leveraged + deep-value cluster
  instead of equal-weighting it beside defensive names.

**These are a plausibly BETTER-FITTING answer to the same concern than stops/holding
rules**, and are prevention (entry-side) rather than cure (exit-side), which suits a
mean-reverting value book that is penalized by premature selling. **The exit-rule tests
(a)/(b) should be evaluated *head-to-head against* these entry-side controls** — i.e. does
a tail-only stop add anything *on top of* a liquidity floor + distress guard + fragility
sizing, or do the entry-side controls already capture the benefit at lower cost and lower
overfitting risk? Do not test the exit rules in isolation.

---

## Standing rule for ALL items in this backlog

Each item above is a **new decision rule**. Every new decision rule in this project has
been vetted the same way, and these are no exception:

1. **Walk-forward validation, not a single split** — strict time-split with embargo,
   report **effective (non-overlapping) n**, treat long-horizon / small-n results as
   directional only.
2. **Risk-adjusted, not raw return** — a rule that changes return AND risk is judged on
   the ratio (Sharpe/Sortino/drawdown-adjusted), not on either leg alone.
3. **Must beat the current construction by a margin that survives the small sample** —
   a marginal or one-split improvement is treated as noise. Ties default to the simpler
   existing rule.
4. **Survivorship-aware** — ~~the universe is current constituents, so backtested tail/exit
   benefits are optimistic; state the caveat.~~ *(revised 2026-09-06)* the net bias is
   **measured at ~±1%/yr with an uncertain sign** (`docs/survivorship_findings.md`), so a
   claimed benefit **larger than ~1%/yr is not explainable by survivorship** and must be
   judged on its own evidence. Cite the measured number, not the old blanket caveat. The
   exception: results specific to **unprofitable or bankruptcy-band names** are still
   mildly optimistic (−0.29%/yr at ROA<−20%), and the measured bankruptcy rate is a floor.
5. **Nothing ships without clearing all of the above** — consistent with how the regime
   rules (`docs/regime_rules_validation.md`), the ML overlay, and the gate/weight changes
   were each vetted.

_Backlog capture only. No implementation, backtest, or live change is made by this
document. The frozen model, config, pipeline, and the running forward test are untouched._
