"""
broker/killswitch.py — PRE-ORDER KILL-SWITCH GATE.

Implements the pre-committed kill-switches in docs/real_money_pilot_plan.md §4/§5 as
code that **BLOCKS order submission**, not code that warns.

Design contract
---------------
Every order path MUST call `OrderGate.authorize(...)` and may submit **only** the
intents it returns. The gate raises `KillSwitchTripped` for portfolio-wide halts and
drops/annotates individual intents for per-name blocks. There is deliberately no
"force" / "override" parameter: a kill-switch you can pass a flag to is not a
kill-switch. Resuming after a halt is a human decision per the pilot plan
("HALT and review" — §5), re-checking the §1 gates.

Switches implemented
--------------------
  5.1  MAX_DRAWDOWN        portfolio equity DD from high-water mark <= -15%  -> HALT ALL
  5.2  SINGLE_NAME_CATASTROPHIC  name -50% from entry, or halted/delisted/bankrupt
                                 -> BLOCK ADDITIONS to that name (exits still allowed)
  5.3  STALE_DATA          snapshot stale > 1 trading session, or feed failure
                           -> HALT ALL (never trade on unverified prices)
  4.x  HARD_LIMITS         position > 15%, sector > 30%, ADV < $5m, min positions < 10
                           -> BLOCK the offending intent(s), assertion-style

Thresholds are sourced from config.settings where a single source of truth already
exists (KELLY["max_position"], PORTFOLIO["max_sector_weight"]) so they cannot drift
from production. Pilot-plan-only numbers are module constants, documented inline.

PAPER ONLY. This module never connects to anything; it is pure decision logic.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from pathlib import Path

import pandas as pd
from loguru import logger

from config.settings import KELLY, PORTFOLIO, PROJECT_ROOT
# The ONE NYSE calendar (forward_test/market_calendar.py), shared with the snapshot
# freshness check and the health check. This module kept a private copy until
# 2026-11-10; it ended at 2026 while the other was told to extend, and two calendars
# disagree about the same Monday: a holiday missing on one side is a "missed session"
# that halts here or cries stale there.
from forward_test.market_calendar import (
    is_trading_day as _is_trading_day,
    most_recent_trading_day as _most_recent_trading_day,
    sessions_behind,
)

# ── Thresholds ──────────────────────────────────────────────────────────────
# §5.1 pilot plan: "-15% (conservative) to -20% (outer limit). Pick one number
# before funding. Recommended for a first pilot: -15%."  -> we pick -15%.
MAX_DRAWDOWN_HALT = -0.15
# §5.2 pilot plan: any single holding loses >= 50% from entry.
SINGLE_NAME_CATASTROPHIC_LOSS = -0.50
# §5.3 pilot plan: stale by MORE THAN one trading session (1-session grace).
MAX_STALE_SESSIONS = 1
# §4 limits — position/sector come from production config (single source of truth).
MAX_POSITION_WEIGHT = KELLY["max_position"]            # 0.15
MAX_SECTOR_WEIGHT = PORTFOLIO["max_sector_weight"]      # 0.30
MIN_LIQUIDITY_ADV_USD = 5_000_000                       # §4 "~$5m/day"
MIN_POSITIONS = 10                                      # §4 "min positions >= 10"

AUDIT_LOG = PROJECT_ROOT / "broker" / "killswitch_audit.log"

class Switch(str, Enum):
    MAX_DRAWDOWN = "5.1_MAX_DRAWDOWN"
    SINGLE_NAME = "5.2_SINGLE_NAME_CATASTROPHIC"
    STALE_DATA = "5.3_STALE_DATA"
    HARD_LIMITS = "4.x_HARD_LIMITS"


class KillSwitchTripped(Exception):
    """Portfolio-wide halt. No order may be submitted after this is raised."""

    def __init__(self, switch: Switch, detail: str):
        self.switch, self.detail = switch, detail
        super().__init__(f"[{switch.value}] HALT — {detail}")


@dataclass
class OrderIntent:
    ticker: str
    action: str            # "BUY" | "SELL"
    quantity: int
    limit_price: float     # LIMIT orders only; market orders are never produced
    sector: str | None = None
    adv_usd: float | None = None          # average daily dollar volume
    target_weight: float | None = None    # intended post-trade weight of the book

    def __post_init__(self):
        if self.action not in ("BUY", "SELL"):
            raise ValueError(f"{self.ticker}: action must be BUY or SELL, got {self.action!r}")
        if self.quantity <= 0:
            raise ValueError(f"{self.ticker}: quantity must be positive, got {self.quantity}")
        if self.limit_price is None or self.limit_price <= 0:
            raise ValueError(f"{self.ticker}: LIMIT price required and must be > 0")

    @property
    def notional(self) -> float:
        return self.quantity * self.limit_price


@dataclass
class PortfolioState:
    """What the gate needs to know about the live book."""
    equity: float
    high_water_mark: float
    # ticker -> {"entry_price":…, "last_price":…, "qty":…, "halted":bool,
    #            "delisted":bool, "bankrupt":bool}
    positions: dict = field(default_factory=dict)

    @property
    def drawdown(self) -> float:
        if not self.high_water_mark:
            return 0.0
        return self.equity / self.high_water_mark - 1.0


@dataclass
class GateDecision:
    approved: list = field(default_factory=list)
    blocked: list = field(default_factory=list)   # (intent, switch, reason)

    @property
    def all_clear(self) -> bool:
        return not self.blocked


# ── staleness: _is_trading_day / _most_recent_trading_day / sessions_behind are the
# forward test's own (imported above), so this switch and the freshness check can never
# read the same snapshot against two different calendars. ───────────────────────────


class OrderGate:
    """The single choke point every order must pass through."""

    def __init__(
        self,
        snapshot_csv: Path | str | None = None,
        *,
        max_drawdown: float = MAX_DRAWDOWN_HALT,
        today: date | None = None,
        audit_log: Path | None = AUDIT_LOG,
    ):
        self.snapshot_csv = Path(snapshot_csv) if snapshot_csv else None
        self.max_drawdown = max_drawdown
        self.today = today
        self.audit_log = Path(audit_log) if audit_log else None

    # ── audit ───────────────────────────────────────────────────────────────
    def _audit(self, event: str, switch: Switch | None, detail: str, extra: dict | None = None):
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "event": event,
            "switch": switch.value if switch else None,
            "detail": detail,
        }
        if extra:
            rec.update(extra)
        line = json.dumps(rec)
        lvl = logger.error if event == "HALT" else (logger.warning if event == "BLOCK" else logger.info)
        lvl(f"KILLSWITCH {line}")
        if self.audit_log:
            self.audit_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.audit_log, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    # ── 5.1 ─────────────────────────────────────────────────────────────────
    def _check_drawdown(self, pf: PortfolioState):
        dd = pf.drawdown
        if dd <= self.max_drawdown:
            detail = (f"equity {pf.equity:,.2f} vs HWM {pf.high_water_mark:,.2f} "
                      f"= {dd:.2%} drawdown, at/through the {self.max_drawdown:.0%} halt")
            self._audit("HALT", Switch.MAX_DRAWDOWN, detail, {"drawdown": round(dd, 6)})
            raise KillSwitchTripped(Switch.MAX_DRAWDOWN, detail)

    # ── 5.3 ─────────────────────────────────────────────────────────────────
    def _check_stale(self):
        if self.snapshot_csv is None:
            return
        if not self.snapshot_csv.exists():
            detail = f"snapshot {self.snapshot_csv.name} does not exist — never ran"
            self._audit("HALT", Switch.STALE_DATA, detail)
            raise KillSwitchTripped(Switch.STALE_DATA, detail)
        try:
            s = pd.read_csv(self.snapshot_csv)
            latest = pd.to_datetime(s["date"]).dt.date.max()
        except Exception as e:                                   # feed/parse failure
            detail = f"snapshot unreadable ({type(e).__name__}: {e}) — treating as stale"
            self._audit("HALT", Switch.STALE_DATA, detail)
            raise KillSwitchTripped(Switch.STALE_DATA, detail)
        behind = sessions_behind(latest, self.today)
        if behind > MAX_STALE_SESSIONS:
            detail = (f"latest recorded {latest}; {behind} trading sessions behind "
                      f"(grace is {MAX_STALE_SESSIONS}) — refusing to trade on stale prices")
            self._audit("HALT", Switch.STALE_DATA, detail, {"sessions_behind": behind})
            raise KillSwitchTripped(Switch.STALE_DATA, detail)

    # ── 5.2 ─────────────────────────────────────────────────────────────────
    def _distressed(self, pf: PortfolioState) -> dict:
        out = {}
        for tk, p in pf.positions.items():
            reasons = []
            if p.get("halted"):
                reasons.append("halted")
            if p.get("delisted"):
                reasons.append("delisted")
            if p.get("bankrupt"):
                reasons.append("bankruptcy filing")
            entry, last = p.get("entry_price"), p.get("last_price")
            if entry and last:
                r = last / entry - 1.0
                if r <= SINGLE_NAME_CATASTROPHIC_LOSS:
                    reasons.append(f"{r:.1%} from entry (<= {SINGLE_NAME_CATASTROPHIC_LOSS:.0%})")
            if reasons:
                out[tk] = "; ".join(reasons)
        return out

    # ── main entry point ────────────────────────────────────────────────────
    def authorize(
        self,
        intents: list,
        pf: PortfolioState,
        *,
        book_value: float | None = None,
    ) -> GateDecision:
        """Return the subset of `intents` that may be submitted.

        Raises KillSwitchTripped on any portfolio-wide halt (5.1, 5.3) — in that case
        NOTHING may be submitted. Per-name and limit breaches (5.2, 4.x) block only
        the offending intents.
        """
        # Portfolio-wide halts first — these stop everything.
        self._check_stale()
        self._check_drawdown(pf)

        decision = GateDecision()
        distressed = self._distressed(pf)
        nav = book_value or pf.equity

        # sector exposure that would result from the BUY intents
        sector_notional: dict = {}
        for i in intents:
            if i.action == "BUY" and i.sector:
                sector_notional[i.sector] = sector_notional.get(i.sector, 0.0) + i.notional

        for intent in intents:
            blocked_by = None
            reason = ""

            # 5.2 — additions to a distressed name (exits always permitted)
            if intent.action == "BUY" and intent.ticker in distressed:
                blocked_by, reason = Switch.SINGLE_NAME, (
                    f"addition to distressed name: {distressed[intent.ticker]}")

            # 4.x — hard limits (BUY side only; reducing risk is never blocked)
            elif intent.action == "BUY":
                w = (intent.target_weight
                     if intent.target_weight is not None
                     else (intent.notional / nav if nav else 0.0))
                if w > MAX_POSITION_WEIGHT + 1e-9:
                    blocked_by, reason = Switch.HARD_LIMITS, (
                        f"position weight {w:.2%} exceeds {MAX_POSITION_WEIGHT:.0%} cap")
                elif intent.sector and nav and (
                        sector_notional.get(intent.sector, 0.0) / nav) > MAX_SECTOR_WEIGHT + 1e-9:
                    blocked_by, reason = Switch.HARD_LIMITS, (
                        f"sector '{intent.sector}' exposure "
                        f"{sector_notional[intent.sector]/nav:.2%} exceeds "
                        f"{MAX_SECTOR_WEIGHT:.0%} cap")
                elif intent.adv_usd is not None and intent.adv_usd < MIN_LIQUIDITY_ADV_USD:
                    blocked_by, reason = Switch.HARD_LIMITS, (
                        f"ADV ${intent.adv_usd:,.0f}/day below the "
                        f"${MIN_LIQUIDITY_ADV_USD:,.0f}/day liquidity floor")

            if blocked_by:
                decision.blocked.append((intent, blocked_by, reason))
                self._audit("BLOCK", blocked_by, f"{intent.ticker} {intent.action}: {reason}")
            else:
                decision.approved.append(intent)

        # §4 min positions — a *shape* check on the resulting book, not per intent.
        # A SELL removes a name only if it closes the position. A trim does not:
        # treating every SELL as an exit blocked a whole top-up on 2026-10-01
        # ("13 held + 9 buys - 6 trims = 9 names"). When the held quantity is
        # unknown, the SELL is still assumed to be an exit (the conservative read).
        def _closes(i) -> bool:
            held = pf.positions.get(i.ticker, {}).get("qty")
            return held is None or i.quantity >= abs(held)
        resulting = set(pf.positions) | {i.ticker for i in decision.approved if i.action == "BUY"}
        resulting -= {i.ticker for i in decision.approved
                      if i.action == "SELL" and _closes(i)}
        if decision.approved and len(resulting) < MIN_POSITIONS:
            detail = (f"resulting book would hold {len(resulting)} names, below the "
                      f"§4 minimum of {MIN_POSITIONS}")
            for intent in list(decision.approved):
                if intent.action == "BUY":
                    decision.approved.remove(intent)
                    decision.blocked.append((intent, Switch.HARD_LIMITS, detail))
            self._audit("BLOCK", Switch.HARD_LIMITS, detail)

        self._audit("PASS", None,
                    f"{len(decision.approved)} approved, {len(decision.blocked)} blocked")
        return decision


def assert_gate_enforced(decision: GateDecision, submitted: list) -> None:
    """Assertion-pattern backstop (same spirit as the Kelly cap assertion).

    Call immediately before submission. Guarantees nothing the gate blocked can reach
    the broker even if calling code mishandles the decision object.
    """
    approved = {(i.ticker, i.action, i.quantity) for i in decision.approved}
    for s in submitted:
        key = (s.ticker, s.action, s.quantity)
        assert key in approved, (
            f"KILL-SWITCH VIOLATION: {key} was not approved by the gate. "
            f"Blocked: {[(b[0].ticker, b[1].value) for b in decision.blocked]}"
        )
