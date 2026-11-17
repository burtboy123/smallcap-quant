# Quant System — Autonomous Small-Cap Factor Investing

A quantitative factor system for the Russell 2000 universe (~1,950 US small/mid-cap stocks): equal-weight 15-name books, built each quarter and paper forward-tested against the S&P 500.

> **Where the evidence stands (2026-11).** Pre-registered research has not found an edge
> over the S&P 500: nothing beat SPY in the strategy race or the universe study, and the
> out-of-sample picking check came back NOT CONFIRMED (`docs/research/*_RESULT.md`). The
> system is run to measure that honestly, not on the assumption that it works.

> **PAPER ONLY.** The system runs a paper forward test and an IBKR **paper** account.
> The broker module refuses live ports and non-`DU` accounts in code. Real money is
> gated by [docs/real_money_pilot_plan.md](docs/real_money_pilot_plan.md), which
> authorizes nothing by itself.

---

## Quick Start

### Prerequisites
- Python 3.12 (the trading machine's version; commands below work in PowerShell and bash)
- No data API key: prices and live fundamentals come from yfinance, research
  fundamentals from SEC XBRL

### 1. Clone and install
```bash
git clone <repo>
cd quant_system
python -m venv venv
venv\Scripts\activate         # bash: source venv/bin/activate
pip install -r requirements.txt
```

> Every package in `requirements.txt` is pinned to the version the trading machine runs.
> Change a pin only after the suite passes on that machine with it.

### 2. Configure environment
```bash
cp .env.example .env
# Edit .env: alert e-mail settings, BACKUP_DIR (a synced folder) and the FINNHUB/FRED keys are optional
```

### 3. Download the universe CSV (manual step, once)
1. Go to https://www.ishares.com/us/products/239710/IWM
2. Click **Download → Holdings**
3. Save the file to `data/iwm_holdings.csv`

### 4. Validate the universe
```bash
python -c "from data.universe_builder import get_universe; t=get_universe(); print(f'Universe: {len(t)} tickers'); print('First 10:', t[:10])"
```
Expected: `Universe: 1900-2050 tickers`

### 5. Run the tests
```bash
python -m pytest tests/ -q
```
All of `tests/` should pass. `monitoring/known_test_failures.txt` is the baseline the
health check compares against, and it is empty: any failure is a regression.

### 6. Load data
```bash
python scripts/bootstrap_data.py                  # one-time: 2y prices + TTM fundamentals (~30-45 min)
python scripts/bootstrap_data.py --prices-only
python -m scripts.run_full_pipeline               # full: prices from 2005, fundamentals, factor scores (hours)
```

---

> **Pending actions with exact commands:** [`NEXT_STEPS.md`](NEXT_STEPS.md)

## Day-to-day operations

| Task | Command |
|---|---|
| Refresh recent prices (whole universe) | `python -m data.prices_updater` (35-day lookback; `--lookback-days 45` for a wider gap, `--dry-run` to preview) |
| Schedule the refresh (once; runs Mondays 15:00) | `powershell -ExecutionPolicy Bypass -File monitoring\register_tasks.ps1 -Job Prices -Elevate` |
| Back up machine-only data (DB, execution log, state) | `python scripts/backup.py` (weekly via the Monday task; every zip is restore-verified before it is kept; `--verify` re-checks the newest; set `BACKUP_DIR` in `.env` to a OneDrive/Drive folder) |
| Forward-test evaluation (interim reading; `--final` in December) | `python forward_test/evaluate.py` — computes EVAL_CRITERIA §1 and marks the §2 clauses |
| Send a test alert email | `python -m monitoring.health_check --test-email` (after filling the `ALERT_*` lines in `.env`) |
| Pipeline health check | `python -m monitoring.health_check -v` — exit 0 = healthy, 1 = a FAIL; each failure prints a `RUN:` line that fixes it |
| Prove the monitor works | `python -m monitoring.break_test` |
| Forward-test snapshot (normally scheduled) | `python forward_test/snapshot.py` |
| Is the snapshot current? | `python forward_test/check_snapshot_fresh.py` |
| Live screener | `python scripts/live_screener.py --portfolio 50000 --update` |
| Monthly run | `python -m scripts.monthly_run` |
| Dashboard | `streamlit run dashboard/app.py` |
| Broker (PAPER), read-only | `python -m broker.ibkr_paper --read-only` — see [broker/RUNBOOK.md](broker/RUNBOOK.md) before anything else |

**Where prices come from.** Two paths write the `prices` table, both taking tickers
from `data.universe_builder.get_universe()`:
- incremental — `data/prices_updater.py:update_recent_prices()`, used by
  `python -m data.prices_updater` and `live_screener.py --update`;
- full history — `data.prices.fetch_prices()` from 2005, used by
  `scripts/run_full_pipeline.py`, the first step of `scripts/monthly_run.py`.

Every command printed by the health check, and every command in this README and the
runbooks, is checked by `tests/test_runbook_commands.py`: the target must exist, have an
entry point, import correctly when run as written, and accept the flags given.

Further reading: [monitoring/README.md](monitoring/README.md) (checks, alerts, what to
do), [broker/RUNBOOK.md](broker/RUNBOOK.md) (paper execution),
[forward_test/README_2026Q4b.md](forward_test/README_2026Q4b.md) (the active forward test).

---

## Build Phases

| Phase | Status | Description |
|-------|--------|-------------|
| **Phase 1** | ✅ Built | Data pipeline: universe, prices, fundamentals |
| **Phase 2** | ✅ Built | Factor engine: value, quality, momentum; F-score gate; 9-weight composite (2 of them constant today, see `factors_live`) + ensemble rank |
| **Phase 3** | ✅ Built | Backtesting engine with point-in-time discipline and transaction costs |
| **Phase 4** | ✅ Built | Sentiment agent; macro/regime, risk and learning agents; ML overlay (currently **disabled**) |
| **Phase 5** | ✅ Built | Orchestrator + monthly run |
| **Forward test** | 🔄 Running | 2026Q3 evaluated: **KEEP INCUMBENT**. 2026Q4b (Incumbent-minus-ML, risk-compliant) started 2026-09-30 |
| **Paper broker** | 🔄 Paper only | IBKR paper path with kill-switches, book guard, execution engine and pipeline health gate |
| **Real money** | ⛔ Gated | Not authorized — see the pilot plan's §1 preconditions |

---

## Architecture

```
Universe (~1,950 tickers from IWM)
    ↓
Data Pipeline (prices and live fundamentals via yfinance; SEC XBRL for research)
    ↓
Factor Engine (F-score gate → value / quality / momentum composite → ensemble rank)
    ↓
Agents (macro regime sector bias, risk limits, sentiment; ML overlay disabled)
    ↓
Book construction under hard limits (sector ≤ 30%, position ≤ 15%, ADV ≥ $5m/day)
    ↓
Forward test (daily snapshot)  ·  Paper broker (kill-switches + health gate)
    ↑
Monitoring (daily health check, deduplicating alerts)
```

---

## Non-Negotiable Rules

1. **No lookahead bias** — fundamental data always uses a 90-day reporting lag (`available_date`)
2. **Survivorship bias measured, not assumed** — free data sources exclude delisted companies, but the bias has been quantified from EDGAR exit filings: **net ~±1%/yr, sign uncertain, possibly mildly pessimistic** (acquisitions outnumber bankruptcies ~6:1 and exit at a premium). See [docs/survivorship_findings.md](docs/survivorship_findings.md). *Previously assumed to make returns materially inflated — that is disproven.* Performance expectations remain conservative on **small-sample** grounds.
3. **Transaction costs mandatory** — ~0.8% round-trip cost per position change always applied
4. **Out-of-sample validation** — training (2005-2018) and validation (2019-2024) periods are strictly separated
5. **Every decision is explainable** — each selected stock has a written rationale
6. **Silent failure is a bug** — a job that does nothing must say so and exit non-zero; a remediation command must actually remediate

---

## Cost to Run

- **Data and backtests:** $0 (yfinance and SEC XBRL are free)
- **Sentiment:** $0. No Claude API is used: the composite reads stored sentiment scores
  only (neutral when there are none); the monthly sentiment step was removed 2026-10-12
- **Infrastructure:** SQLite locally (free), PostgreSQL for production

---

## Project Structure

```
quant_system/
├── config/          # Settings, constants, factor weights (all thresholds live here)
├── data/            # Universe, prices, prices_updater, fundamentals, storage
├── factors/         # Value, quality, momentum, composite ranking
├── models/          # ML factor model (overlay currently disabled)
├── backtesting/     # Engine, portfolio, costs, metrics, reports
├── agents/          # Orchestrator, macro/regime, risk, sentiment, learning, broker agents
├── broker/          # IBKR PAPER path: kill-switches, book guard, execution, RUNBOOK
├── forward_test/    # Daily snapshot, per-period books, evaluation criteria and results
├── monitoring/      # Health checks, alerting, scheduled-task registration
├── dashboard/       # Streamlit management dashboard
├── scripts/         # Entry points: bootstrap, pipeline, screener, monthly run, training
├── database/        # Schema and init
├── docs/            # Research notes, survivorship findings, real-money pilot plan
└── tests/           # Unit and integration tests
```

---

*Russell 2000 universe · quarterly books · equal-weight · benchmark: S&P 500*
