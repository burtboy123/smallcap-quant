# Broker Runbook — IBKR **PAPER** path

**PAPER ONLY.** Nothing in this runbook touches real money. The module cannot reach a
live account: a live port raises before a socket opens, and a non-`DU` account raises
immediately after connecting. There is no flag that relaxes either check.

Account in use: **DU1234567** (paper / "Individual (Demo)", base currency **EUR**, opened
2026-09-30 at EUR 1,000,000). The previous paper account **DUR222564** is retired: from the
morning of 2026-09-30 its login could get no market data at all (IB 10197 / 162 on every
request, with nothing else logged in; Client Portal reported the application for that
username as no longer active). It still holds the Phase-2 book and is not traded any more.
Execution records carry the account, so its runs stay distinguishable from DU1234567's.

---

## 0. The six safety properties (what protects you)

| # | Property | Where | Failure mode |
|---|---|---|---|
| 1 | Paper-port allowlist `{4002, 7497}` | `_assert_paper_port()` — in `__init__` *and* `connect()` | `LivePortRefused` **before any socket opens** |
| 2 | Account must start with `DU` | `connect()` | disconnects, `NonPaperAccountRefused` |
| 3 | LIMIT orders only | `OrderIntent.__post_init__` + `_to_ib_order()` | `ValueError`; a market order cannot be constructed |
| 4 | Dry-run default | `submit_orders(execute=False)` | prints, sends nothing without `--paper-execute` |
| 5 | Kill-switches inside the order path | `submit_orders()` → `OrderGate.authorize()` → `assert_gate_enforced()` | halt raises / intents dropped **before** `placeOrder()` |

Property 5 matters most: the gate is called *inside the only method that can place an
order*, so a caller cannot bypass it by forgetting to check.

**Property 6, added 2026-09-30 — the pipeline health gate.** `main()` calls
`assert_pipeline_healthy()` *before it reads the book*, and refuses on any CRITICAL
failure: a book must never be sized from a pipeline that is not recording. It gates the
**dry run too**, because the dry run is what you read before executing; `--read-only` is
exempt because it sizes nothing. No override flag, same as the kill-switches. Kill-switch
trips, blocked rebalances and refusals now also raise **alerts** — see
`monitoring/README.md`. This exists because `forward_test/2026Q4b` recorded zero sessions
for nine days and nothing said so.

**5.1 drawdown fixed, 2026-10-05.** `main()` used to pass the current NAV as the
high-water mark, so drawdown read 0% on every run and the -15% halt could never fire.
Every broker connection (read-only included) now records NetLiquidation in the base
currency to `monitoring/state/nav_history.jsonl` (`broker/nav_history.py`); the gate's
high-water mark is the highest NAV recorded for the account, and `--read-only` prints
the current drawdown. Measured in EUR, so FX moves neither fake nor hide a drawdown.

**This holds for the execution engine too.** Slices and reprices are not a side channel:
every child order goes through the same `submit_orders()`, so it faces the same gate,
the same book guard, the same `DU` assertion and the same LIMIT-only construction.
Proven by `python -m broker.test_killswitch_with_execution` (§5C).

> A bug caught by exactly that test is worth knowing about: the first version of
> `plan()` gave each child `target_weight = parent / n`, so a 50% parent split into 6
> children of 8.33% each would have **passed the 15% position cap on every child** and
> still left a 50% position. Every child now carries the **parent's** resulting weight.
> Slicing changes *how* an order reaches the market, never *what the limits allow*.

---

## 1. Start IB Gateway

1. Launch **IB Gateway** and log in with the **PAPER** username (different from live).
2. Confirm the title bar/status shows a paper session.
3. **Configure → Settings → API → Settings**
   - ☐ Read-Only API — **unchecked** (otherwise orders are rejected)
   - Socket port **4002**
   - ☑ Allow connections from localhost only (covers "trusted IP 127.0.0.1")
   - IB **Gateway** has no "Enable ActiveX and Socket Clients" box — its API is always
     on. That checkbox exists only in TWS.
4. **Configure → Lock and Exit → Auto restart** (not auto-logout) for unattended use.

Gateway must stay running for the whole cycle.

## 2. Verify the port before anything else

```powershell
Test-NetConnection -ComputerName 127.0.0.1 -Port 4002 -InformationLevel Quiet   # expect True
Test-NetConnection -ComputerName 127.0.0.1 -Port 4001 -InformationLevel Quiet   # expect False
```

**`4002` True and `4001` False.** If 4001 is open, stop and close the live Gateway —
the module will refuse it, but a live session should not be running at all.

## 2A. Market-data preflight (the failure that looks like success)

A run refuses up front if nothing can be priced:

```
=== MARKET DATA PREFLIGHT (SPY) ===
  delayed quote : UNAVAILABLE
  historical    : UNAVAILABLE
  NO pricing path works ...
```

**Why this check exists.** If another TWS/Gateway client is logged into the *same*
credentials from a different IP, IB gives that session the market-data line and refuses
this one — quotes return `NaN` (**error 10197** "No market data during competing live
session") *and* the historical farm refuses (**error 162** "Trading TWS session is
connected from a different IP address"). Account values keep reading normally, so the
connection looks perfectly healthy.

Before the preflight existed, that outage was silent: `reference_prices()` returned
`{}`, `build_intents()` logged one warning per name, and the run **exited 0 having
submitted nothing** — indistinguishable from a legitimately empty rebalance.

**An open IBKR browser tab counts.** Client Portal logged in with the same username is
"another platform": close every IBKR tab (and log out of IBKR Mobile) before a run.
Its banner "You are logged in without Trading/Market Data permissions ... You may be
logged in on another platform" means the reverse: Gateway holds the session.

If 10197 / 162 persist with **nothing** else logged in and Gateway restarted, suspect the
account itself (see the account note at the top) rather than a session.

**Fix:** log the other session out, then retry. Do not work around it — a book sized off
prices you could not fetch is not the book.

Two related refusals, both deliberate:
- `MIN_PRICE_COVERAGE = 0.95` — under 95% of the universe priced and the run refuses.
  Not 100%, so one halted or delisted name cannot block a rebalance; an outage must.
- `fx_rate_to_usd()` tries the live quote, then the historical close, then **raises**.
  The FX rate is never guessed: this account is EUR-based and the whole USD book is
  sized off it.

## 3. Read-path check (always do this first)

```powershell
python -m broker.ibkr_paper --read-only
```

Expect:
- `PAPER ACCOUNT CONFIRMED: DU1234567 (prefix 'DU' assertion PASSED)`
- account summary **with its base currency**, plus the USD-equivalent NAV
- current positions

> **Currency:** this account is **EUR**-based. The USD book is sized off `NAV (USD
> equiv)` using IB's own EURUSD quote. If you ever see the USD-equivalent line missing
> or NAV resolving to 0, **stop** — the run will refuse rather than size a zero book.

## 4. Dry run (sends nothing)

```powershell
python -m broker.ibkr_paper --book broker/compliant_book.csv                      # flat limits
python -m broker.ibkr_paper --book broker/compliant_book.csv --execution-engine   # §5A
```

Read the two sections:
- **INTENDED ORDERS** — side, qty, reference price, limit, notional, sector, ADV, and
  the **price source** (`DELAYED_MID`, `HIST_CLOSE`, …).
- **KILL-SWITCH GATE** — `approved` / `blocked`, with the reason for each block.

**A dry run showing `approved: 0` is a legitimate result, not a bug.** It means the
book violates a hard limit. See §7.

> **Market data:** this account has **no real-time US subscription** (IB error 10089).
> The module requests **delayed** data (`reqMarketDataType(3)`, then `4`), falling back
> to the last historical daily close. Limits can therefore be derived from data ~15
> minutes stale — a real cost: it was the likely cause of 3 unfilled orders on
> 2026-09-29. Treat slippage figures with that caveat.

## 5. Paper execute

Only after a dry run you have actually read.

```powershell
python -m broker.ibkr_paper --book broker/compliant_book.csv --paper-execute
```

Orders are **DAY LIMIT**, `outsideRth=False`. They rest until filled, cancelled, or the
close.

### Market hours (Finland ↔ US)

| | Finland (EEST, UTC+3) | US Eastern (EDT, UTC-4) |
|---|---|---|
| US regular session | **16:30 – 23:00** | 09:30 – 16:00 |
| After the Oct/Nov DST shifts | 16:30 – 23:00 (EET/EST, UTC+2/-5) | unchanged |

- **Run inside the session.** Fills are then representative.
- **Outside the session**, `outsideRth=False` means orders simply do not execute; they
  rest until the next open. A run started after 23:00 Finland is an **after-hours test
  whose fills are unrepresentative** — label it as such in any report, or wait.
- DST: the US and EU shift on different dates (late Oct / early Nov), so the Finnish
  window can move by an hour for ~1 week. Re-derive it rather than assuming.

### 5A. The execution engine (`--execution-engine`)

The flat 30 bps buffer left the book **69.96% deployed** on 2026-09-29 (9/15 filled,
82.1% of shares, −21.6 bps). `--execution-engine` replaces it with three mechanisms.
All parameters live in `EXECUTION` in `config/settings.py` — **no magic numbers in the
order path**.

**1. Volatility-sized limit buffer.** Instead of one flat offset:

```
buffer_bps = clamp(vol_mult × sigma_daily_bps × sqrt(quote_delay_min / 390),
                   buffer_min_bps, buffer_max_bps)
```

| key | default | why |
|---|---|---|
| `quote_delay_minutes` | `15` | measured staleness of IB delayed data |
| `buffer_vol_mult` | `1.0` | one standard deviation of drift over the delay — not a guess, the actual scale of what the buffer must bridge |
| `buffer_min_bps` | `15` | a quiet name still needs to cross the spread |
| `buffer_max_bps` | `75` | **hard ceiling.** Keeps a LIMIT from becoming a de facto market order |
| `fallback_sigma_daily_bps` | `250` | used only when local history is too short |

Sizing the offset by the *name's own* volatility is the point: a quiet name gets 21 bps
(MOV), a jumpy one 64 (CCSI). A single flat number is either too tight for the second or
needlessly generous to the first.

**2. Bounded repricing.** Unfilled after `reprice_interval_s` (**90 s**) → reprice by
`reprice_step_bps` (**20**), at most `reprice_max_attempts` (**3**) times, and never past
a cumulative `reprice_total_drift_cap_bps` (**60**). Then it gives up and reports.
Every reprice is logged to the execution store. **It never converts to market.**

**3. TWAP slicing for thin names.** If an order would exceed
`slice_max_participation` (**20%**) of the liquidity expected in
`exec_horizon_minutes` (**20**) — i.e. `ADV × 20/390` — it is split into up to
`slice_max_children` (**6**) children, `slice_interval_s` (**120 s**) apart.
20% is a conventional participation ceiling; above it you are the price.

> **What the diagnosis actually found** (`broker/diagnose_underfill.py`, run against IB
> 1-minute TRADES bars). Stale quotes were **not** the main cause: mean staleness was
> only **+3.0 bps**. Two orders (MCS, ZVRA, **43%** of the shortfall) were stale enough
> to matter (+42, +62 bps) and the flat 30 bps could not bridge them. But four orders
> (KODK, MOV, NUTX, SENEA, **57%**) had limits the market *did* reach and still filled
> short — MOV's limit sat **51 bps above** the true low and filled **zero**.
> **The dominant lever is time in market:** orders were cancelled after ~4 minutes,
> about 1% of a session's liquidity, while the median order was ~66% of the liquidity
> available in that window. That is what slicing and repricing address.
>
> Caveat recorded at the time: IB's historical *volume* is unusable without a
> subscription (100 shares in 6 minutes for a $22m/day name), so participation is
> computed against **local ADV**, not IB window volume.

### 5B. Comparing against the baseline

```powershell
python -m broker.compare_runs
```

Prints baseline vs newest engine run and states **IMPROVED / WORSE / A WASH** against a
5 pp share-fill threshold *fixed in the code before the numbers arrive*. It also
cross-checks that the store still reproduces the measured baseline and refuses to
compare if it drifted. A wash is reported as a wash.

### 5C. Proving the safety properties still hold

```powershell
python -m broker.test_killswitch_with_execution
```

Trips four kill-switches with repricing and slicing **active** and asserts
`placeOrder()` is never reached — it is monkeypatched to raise if it is. Expect all four
PASS and `placeOrder() reached 0 times`.

> The test writes a **fresh** snapshot first for trips 2–4. Without it, 5.3 STALE_DATA
> fires first every time and masks the switch actually under test — a green run that
> proves only that staleness works.

---

## 6. Reconcile (always)

```powershell
python -m broker.reconcile --book broker/compliant_book.csv
python -m broker.reconcile --book broker/compliant_book.csv --cancel-open   # tidy finish
```

Reports fill rate, slippage vs limit, partial/unfilled, and **positions vs target
weights**. `--cancel-open` cancels still-working orders so the account is left in a
defined state; it never places anything. JSON written to `broker/reconciliation/`.

**How to read it:**
- *Slippage negative* = filled **better** than the limit. Expected with marketable limits.
- *Partial fills* are normal in small caps. Either re-run to top up, or accept the
  underweight and record it.
- *Unfilled* usually means the limit was not marketable — most likely the delayed-data
  caveat in §4, or a genuinely thin name.
- *Deployed % below target* is the honest state of the book. Do not "fix" it by
  widening limits without deciding that deliberately.

### 6.1 Execution history on disk

Every run is written to `broker/execution_log/` as JSON (`broker/execution_store.py`,
`SCHEMA_VERSION = 1`), so history survives Gateway restarts and the dashboard has
something to read with Gateway **closed**. The 2026-09-29 baseline is imported as
`run_20260929_182912_baseline.json` (`engine="simple_flat_30bps"`).

Records come from execution/fill records, **never `ib.trades()` totals** — re-reading a
completed trade zeroes `totalQuantity` and `filled`, which would silently report a
filled order as a zero-share one.

### 6.2 Dashboard page

```powershell
streamlit run dashboard/app.py      # then pick "Execution" (Portfolio section)
```

Per-order ticker/side/shares/limit/fill/timestamp/status, slippage per trade and in
aggregate, % of the target book deployed, unfilled and partial names joined to their
diagnosed reason, and **current paper positions beside the Q4b intended book** with the
differences highlighted.

**Read-only by construction** — it places and modifies nothing. It is labelled as the
**PAPER account (DU1234567, simulated)** and as distinct from the Q4b intended-weights
selection test, because those two are easy to conflate and mean different things.

---

## 7. If a kill-switch trips

The gate raises **or** drops intents *before* `placeOrder()`. Every decision is written
to `broker/killswitch_audit.log` (append-only JSON).

| Switch | Meaning | Do this |
|---|---|---|
| **5.1 MAX_DRAWDOWN** | equity ≤ −15% from high-water mark | **HALT. Do not rebalance into the drawdown.** Written review: is this inside the distribution the backtest/tail audit predicted, or is something *broken*? Resuming requires re-checking the §1 pilot gates. |
| **5.2 SINGLE_NAME** | a holding ≤ −50% from entry, or halted/delisted/bankrupt | **No additions to that name.** Flag for manual review — the value-trap case. **Exits are still allowed**; this is not a stop-loss and must never trap you in a position. |
| **5.3 STALE_DATA** | snapshot > 1 session behind, missing, or unreadable | **HALT — never trade on unverified prices.** Run `python forward_test/check_snapshot_fresh.py`, fix the scheduled task, verify a clean snapshot, then retry. Precedent: the f-score truncation incident. |
| **4.x HARD_LIMITS** | position > 15%, sector > 30%, ADV < $5m/day, or book < 10 names | Not an emergency — **the book is wrong, not the market.** Rebuild it under the limits (§8). Do **not** relax the limit to make the order fit. |

**There is no override flag, by design.** A kill-switch you can pass a flag to is not a
kill-switch. Resuming after a halt is a deliberate human decision.

---

## 8. Rebuilding a limits-compliant book

```powershell
python -m broker.build_compliant_book
```

Same gate (f≥5) and composite ranking as production, with §4 applied *during*
selection: ADV ≥ $5m/day, sector ≤ 30%, ≥ 10 names. It asserts all three before
writing `broker/compliant_book.csv`.

> **Resolved 2026-09-29 (history kept — the cause recurs).** The tracked
> `forward_test/2026Q4/incumbent_book.csv` was **not investable**: Energy 46.8%, 3 names
> below the ADV floor, largest compliant subset 8 names against a §4 minimum of 10. The
> gate blocked all 15 of its orders. Cause: `forward_test/build_q4_books.py` replicated
> only the *selection* path and skipped production's downstream `run_risk_check()` /
> `enforce_sector_limits()`.
>
> **Decision taken:** Q4 was retired to `forward_test/2026Q4_retired/` and the test
> restarted as **`2026Q4b`** tracking the risk-compliant book — losing ~1 week beats
> spending a quarter measuring a book that could never be held. `ACTIVE_PERIOD` in
> `broker/ibkr_paper.py` points at `2026Q4b`, and the book guard
> (`assert_book_investable()`) now runs on the **broker path** as well as the builder,
> so an un-investable book cannot reach the broker even in a dry run.
>
> **The lesson that generalises:** a builder that reuses the selection path but not the
> risk path will produce a book that ranks well and cannot be held. Any new builder must
> call the guard.

---

## 9. Daily checklist

1. Gateway running, paper session, port 4002 open / 4001 closed.
2. `--read-only` → `DU` assertion passes, NAV sane.
3. **Preflight prices** — both paths UNAVAILABLE means another session holds the
   market-data line (§2A). Log it out; do not work around it.
4. **Pipeline health** — `python -m monitoring.health_check -v`. This supersedes the
   bare snapshot check: it covers freshness *and* 0-row files, row growth, the
   scheduled task's real exit code, ticker coverage, prices-DB staleness and new test
   failures. Exit 1 means something needs a human. See `monitoring/README.md`.
   The dry run in step 5 runs the same gate and **refuses** on any CRITICAL failure, so
   a broken pipeline stops you here rather than producing a book from data that does
   not exist.
5. Dry run; **read** the blocked list.
6. Inside market hours (16:30–23:00 Finland) → `--paper-execute`. Outside it,
   `outsideRth=False` orders do not fill and any slippage number is meaningless.
7. Reconcile with `--cancel-open`; archive the JSON.
8. `python -m broker.compare_runs` — and accept the verdict it prints.
9. Any kill-switch trip → §7, and **stop**.

## 10. Escalation — stop and get a human

- Account id does not start with `DU`, or the DU assertion fails.
- Port 4001/7496 open, or any attempt to connect to one.
- `assert_gate_enforced` raises **KILL-SWITCH VIOLATION** (an order reached submission
  without approval — a code defect, not a market event).
- Reconciliation cannot account for a position (held but never ordered, or vice versa).
- Real money is proposed. **It remains unauthorized**: the pilot plan requires all §1
  gates plus an explicit, separate decision by the user.
