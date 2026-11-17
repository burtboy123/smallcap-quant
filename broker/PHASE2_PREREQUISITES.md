# Phase 2 — IBKR Paper Integration: **BLOCKED ON PREREQUISITES**

**Checked:** 2026-09-21 · **Status: STOPPED at the prerequisite gate, as instructed.**

> The task specified: *"Check prerequisites first… If not, STOP and tell me exactly what
> to install/configure — **do not work around it**."* No broker module was written and no
> paper rebalance was attempted. Building an untestable module, or substituting a mock
> for a real paper account, would be working around the gate.

---

## What was checked and what was found

| Check | Result |
|---|---|
| TWS **paper** port 7497 listening | ❌ no |
| IB Gateway **paper** port 4002 listening | ❌ no |
| TWS **live** port 7496 listening | ❌ no *(correct — must stay closed)* |
| IB Gateway **live** port 4001 listening | ❌ no *(correct — must stay closed)* |
| Running TWS / Gateway / Java process | ❌ none |
| `%USERPROFILE%\Jts` | ❌ absent |
| `C:\Jts` | ❌ absent |
| `%LOCALAPPDATA%\IBGateway` | ❌ absent |
| `C:\Program Files\Trader Workstation` | ❌ absent |

**Conclusion: Interactive Brokers software is not installed on this machine at all.**
This is not a configuration problem — there is nothing to configure yet.

---

## Exactly what to install and configure

### 1. An IBKR account with paper trading enabled

A **paper trading account** must exist and be enabled. In IBKR Client Portal:
**Settings → Account Settings → Paper Trading Account**. Note the paper username —
it is **different** from the live username (IBKR issues a separate one, and logging in
with the live credentials is what puts you on a live port).

*If there is no funded live account yet, IBKR still offers a standalone paper account —
that is sufficient for everything in Phase 2, and is the right choice here.*

### 2. Install IB Gateway (recommended) — not full TWS

Download: <https://www.interactivebrokers.com/en/trading/ibgateway-stable.php>

**IB Gateway is the better choice for this project:** it is a thin API-only bridge with
no charting UI, uses far less memory, is more stable for unattended/scheduled use, and
has a smaller surface area for accidental manual order entry. Full TWS is only worth it
if you want the trading GUI.

- **Paper port: `4002`** (Gateway). Live is `4001` — the module will hard-refuse it.
- If you install full TWS instead: **paper `7497`**, live `7496`.

### 3. Configure the API (must be done inside the app, once)

Launch IB Gateway and **log in with the PAPER username**. Confirm the UI shows a paper
session before continuing. Then:

**Configure → Settings → API → Settings**
- ☑ **Enable ActiveX and Socket Clients**
- ☐ **Read-Only API** — must be **UNCHECKED** (otherwise orders are rejected)
- **Socket port:** `4002`
- **Trusted IPs:** add `127.0.0.1`
- ☑ **Allow connections from localhost only** (keep this on — do not expose the socket)
- ☐ Leave *"Create API message log file"* off unless debugging

**Configure → Settings → API → Precautions** — for automated LIMIT orders, either tick
the relevant *"Bypass … for API orders"* boxes or expect confirmation dialogs to block
submissions. Prefer bypassing **only** the price/size precautions you actually hit.

### 4. Keep it running during any paper session

Gateway auto-logs-out daily by default (**Configure → Lock and Exit → Auto restart**).
For scheduled use, set auto-restart rather than auto-logout.

### 5. Verify before telling me to proceed

```powershell
Test-NetConnection -ComputerName 127.0.0.1 -Port 4002 -InformationLevel Quiet   # must be True
Test-NetConnection -ComputerName 127.0.0.1 -Port 4001 -InformationLevel Quiet   # must be False
```

Expect **`True` on 4002** (paper open) and **`False` on 4001** (live closed).

---

## What I will build once the gate is open

**Library: `ib_async`.** It is the maintained continuation of the widely used `ib_insync`
(whose original author stopped maintaining it), and it is the right choice over the
official `ibapi` because `ibapi` is a bare callback/threading interface that requires
hundreds of lines of boilerplate for request/response correlation, while `ib_async`
provides a synchronous, typed, well-documented API over the same socket protocol. Less
of our own code in the order path is a safety property, not just convenience.

`broker/ibkr_paper.py` will:
1. **Hard-refuse any non-paper port** — a module-level allowlist `{4002, 7497}`, raising
   before a socket is opened if a live port (4001/7496) is passed. Not a warning.
2. Additionally assert the connected account id starts with the paper prefix (`DU`),
   disconnecting immediately if it does not — a second, independent check, because a
   port number alone is not proof of a paper session.
3. Read account summary and current positions.
4. Translate a target book into orders (diff vs current positions).
5. **LIMIT orders only.** `OrderIntent` already makes a market order unconstructible.
6. **Default to DRY-RUN** — print the order table, send nothing. Sending requires an
   explicit `--paper-execute` flag.
7. **Route every order through `broker/killswitch.py: OrderGate.authorize()`** (already
   built and trip-tested — Phase 3), with `assert_gate_enforced()` immediately before
   submission.

Then Phase 2 task 5: dry-run → paper-execute → reconcile (intended vs actual fills,
slippage vs limit, unfilled/partial flags).

---

## What is NOT blocked

**Phase 3 is complete.** The kill-switches do not need a broker — they are pure decision
logic that runs *before* any connection. All four are implemented and each has been
deliberately tripped in test (14 tests passing). `OrderGate` is broker-agnostic by design
and is the exact integration point the future `ibkr_paper.py` will call, so the safety
layer is in place *before* connectivity — the correct order.

See `broker/killswitch.py` and `broker/test_killswitch_trips.py`.

---

## Pilot-plan status (unchanged by this task)

`docs/real_money_pilot_plan.md` §1 gates, for real money:

| Gate | Status |
|---|---|
| 1.6 Kill-switches implemented **and tested** | ✅ **done** (Phase 3) |
| Broker path paper-tested end to end | ❌ **blocked** — no IBKR software installed |
| Remaining §1 gates | unchanged, not addressed by this task |

**Real money remains unauthorized and no capital step has been taken.**
