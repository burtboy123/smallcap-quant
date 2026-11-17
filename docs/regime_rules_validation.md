# Regime & Weighting Rules — Walk-Forward Validation

**Date:** 2026-06-17
**Sandbox evidence:** `sandbox/validate_regime_rules.py`, `sandbox/validate_3x_weight.py`
**Scope:** three live-tuned rules that were never out-of-sample tested. No logic was
changed as a result of this validation — the rules were only **labeled** in-code so
nothing downstream silently treats an unvalidated rule as a confirmed edge. The
production ML model remains **frozen**.

## Method

Regime rules cannot be tested on pre-2020 *factor* returns (no historical factor
data exists before the SimFin window). They were instead tested by **forward
alignment** on price data 2005–2026: a regime call is "correct" if the factor tilt
it induces actually pays off over the next quarter, measured as
`growth_minus_value = IWF_fwd63d − IWD_fwd63d` (>0 = growth won = RISK_ON_GROWTH
correct; <0 = value won = INFLATION_SHOCK correct). The 3× weight was tested on the
real leakage-free walk-forward IC harness.

**Caveat:** these rules fire on rare events; overlapping 63-day forward windows on
monthly sampling inflate nominal n. Treat effect sizes as directional, not
statistically significant.

## Verdicts

### 1. XLK override → force RISK_ON_GROWTH — **CURVE-FIT**

Code: `get_tech_sector_leading()` + the override branch in
`classify_enhanced_regime()` (`xlk_3m > spy_3m * 1.1` and `above_200ma`).

| | n | fwd growth−value | hit |
|---|---|---|---|
| Rule FIRES | 149 | +0.935% | 59% |
| Rule QUIET | 114 | +0.801% | 60% |
| FIRES pre-2020 | 107 | +0.825% | 58% |
| FIRES 2020–2025 | 42 | +1.213% | 62% |

- The broad trigger fires **~57% of all months** and FIRES-vs-QUIET forward growth
  is **identical** (+0.935% vs +0.801%) → no discrimination.
- The specific oil-shock override it feeds fired **exactly once in 21 years
  (2025–2026)** → fit to its origin window (n=1), structurally unvalidatable.

**Action:** kept as a low-frequency guard against the Mar–Jun 2026 INFLATION_SHOCK
misfire, labeled UNVALIDATED/CURVE-FIT in-code. Do not treat tech leadership as
general regime logic.

### 2. 3× live-outcome training weight — **PRINCIPLED (currently immaterial)**

Code: `retrain_with_outcomes()` in `models/factor_model.py`.

Walk-forward proxy (up-weight most-recent training year 1× vs 3×, test next year):

| Fold | 1× OOS IC | 3× OOS IC | Δ |
|---|---|---|---|
| train 2021–22 → test 2023 | +0.0410 | +0.0626 | +0.0216 |
| train 2021–23 → test 2024 | −0.0400 | −0.0212 | +0.0188 |
| mean | | | **+0.0202** |

Improved OOS IC in **both** folds (consistent). But the real 45 live outcomes are
only **1.0% (1×) → 2.9% (3×)** of training, so the production effect is negligible
today; it will grow favorably as live outcomes accumulate.

**Action:** kept as-is, labeled PRINCIPLED in-code.

### 3. INFLATION_SHOCK requires XLE>SPY — **NEUTRAL (unvalidated, harmless)**

Code: `get_energy_sector_outperforming()` + the INFLATION_SHOCK branch.

| Oil-shock subset | n | fwd growth−value | fwd XLE−SPY |
|---|---|---|---|
| XLE leads (rule ALLOWS) | 23 | mean −0.49% / median +0.59% | −1.17% |
| XLE lags (rule SUPPRESSES) | **4** | +0.73% | −6.26% |

Only 27 oil-shock months in 21 years; the decisive "suppress" branch has **n=4**.
The premise (XLE leadership ⇒ durable energy/value outperformance) is **not
supported** — energy mean-reverts after oil shocks (forward XLE−SPY negative in
both branches). Faint directional support: XLE-lagging shocks fare worse forward
(−6.3% vs −1.2%).

**Action:** kept as a harmless sanity guard, labeled NEUTRAL/UNVALIDATED in-code.

## Meta-finding

Two of the three rules fire on events so rare (override **n=1**, XLE-suppress
**n=4**) that they are **structurally unvalidatable** — which is itself the
signature of curve-fitting: a rule tuned on a handful of observations will always
"work" on those observations. The only rule resting on many observations and a
testable mechanism — the 3× recency weight — is the one that cleanly generalized.
The practical rule going forward: a regime/weighting tweak introduced to fix one
observed episode should be treated as an unvalidated guard until it has fired and
been forward-checked across multiple independent periods.
