"""
broker/execution.py — execution quality layer for the PAPER broker path.

Three measured problems from 2026-09-29, three mechanisms:

  1. VOLATILITY-SIZED LIMIT BUFFER  (fixes the 43.3% "limit never reached")
     A flat 30bps buffer could not bridge a delayed quote that understated the true
     price by 42-62bps. The buffer is now sized to a 1-sigma move over the quote-delay
     horizon, per name, with a HARD CEILING so a limit can never become a de facto
     market order.

  2. TWAP SLICING                   (fixes part of the 56.7% "reached but short")
     Orders were ~66% (median) of the liquidity available in the 4 minutes they were
     given, and several exceeded 100%. Orders above a participation threshold are
     split into child orders spread over the execution horizon.

  3. BOUNDED REPRICING              (helps both)
     Unfilled children are cancelled and re-submitted higher, bounded in attempts AND
     cumulative drift. Never converts to a market order.

SAFETY — unchanged and non-negotiable. Every child order and every reprice is
submitted through `IBKRPaperBroker.submit_orders()`, which runs the kill-switch gate
and `assert_gate_enforced()` internally. This module NEVER calls `ib.placeOrder`
itself, so slicing and repricing cannot route around the gate. Dry-run remains the
default; the port allowlist, DU assertion, LIMIT-only construction and book guard all
still apply.

PAPER ONLY.
"""
from __future__ import annotations

import copy
import json
import math
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd
from loguru import logger

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import EXECUTION            # noqa: E402  single source of truth
from broker.killswitch import OrderIntent, PortfolioState  # noqa: E402

EXEC_LOG_DIR = ROOT / "broker" / "execution_log"
SESSION_MINUTES = 390.0


# ── volatility input ────────────────────────────────────────────────────────
def daily_sigma_bps(tickers: list, lookback: int = 20) -> dict:
    """Stdev of daily returns, in bps, from local price history."""
    if not tickers:
        return {}
    conn = sqlite3.connect(f"file:{(ROOT/'quant_system.db').as_posix()}?mode=ro", uri=True)
    q = ",".join("?" * len(tickers))
    df = pd.read_sql(
        f"SELECT ticker, date, close FROM prices WHERE ticker IN ({q})",
        conn, params=list(tickers))
    conn.close()
    out = {}
    for t, g in df.groupby("ticker"):
        c = g.sort_values("date")["close"].tail(lookback + 1)
        if len(c) >= 5:
            r = c.pct_change().dropna()
            if len(r) >= 4 and r.std() == r.std():
                out[t] = float(r.std() * 1e4)
    return out


def limit_buffer_bps(ticker: str, sigma_map: dict, cfg: dict = EXECUTION) -> tuple:
    """Per-name buffer in bps, clamped. Returns (bps, explanation)."""
    sigma = sigma_map.get(ticker) or cfg["fallback_sigma_daily_bps"]
    horizon = math.sqrt(cfg["quote_delay_minutes"] / SESSION_MINUTES)
    raw = cfg["buffer_vol_mult"] * sigma * horizon
    bps = min(max(raw, cfg["buffer_min_bps"]), cfg["buffer_max_bps"])
    why = (f"sigma_d={sigma:.0f}bps x sqrt({cfg['quote_delay_minutes']}/390)"
           f"={horizon:.3f} x{cfg['buffer_vol_mult']} = {raw:.0f}bps"
           f"{' -> CLAMPED' if abs(bps - raw) > 0.5 else ''}")
    return bps, why


# ── slicing ─────────────────────────────────────────────────────────────────
def plan_slices(notional: float, adv_usd: float | None, cfg: dict = EXECUTION) -> int:
    """How many TWAP children this order needs. 1 == no slicing."""
    if not adv_usd or adv_usd <= 0:
        return 1
    available = adv_usd * (cfg["exec_horizon_minutes"] / SESSION_MINUTES)
    budget = cfg["slice_max_participation"] * available
    if budget <= 0 or notional <= budget:
        return 1
    return int(min(math.ceil(notional / budget), cfg["slice_max_children"]))


@dataclass
class ChildOrder:
    parent: str
    seq: int
    intent: OrderIntent
    original_limit: float
    # The child's TOTAL planned quantity and the reference price its limit came from.
    # intent.quantity is replaced by the remainder on every reprice, so it cannot be
    # used to judge "filled" or to report what was ordered (2026-10-01: AMN reported
    # "ordered 32, remaining -2089" after a reprice of the last 32 shares).
    planned_qty: float = 0.0
    ref_price: float | None = None
    reprices: int = 0
    filled: float = 0.0
    avg_fill: float = 0.0
    status: str = "PENDING"
    trades: list = field(default_factory=list)
    events: list = field(default_factory=list)

    def log(self, kind: str, **kw):
        e = {"ts": datetime.now().isoformat(timespec="seconds"), "event": kind,
             "ticker": self.intent.ticker, "seq": self.seq, **kw}
        self.events.append(e)
        logger.info(f"EXEC {json.dumps(e)}")


class ExecutionEngine:
    """Slices, submits, waits, reprices — always via broker.submit_orders()."""

    def __init__(self, broker, cfg: dict = EXECUTION, dry_run: bool = True):
        self.br = broker
        self.cfg = cfg
        self.dry_run = dry_run
        self.children: list = []
        self.halted = None

    # ── planning ────────────────────────────────────────────────────────────
    def plan(self, intents: list, prices: dict, adv: dict, sigma: dict) -> list:
        """Turn parent intents into (possibly sliced) child orders with vol-sized limits."""
        children = []
        for it in intents:
            ref = prices.get(it.ticker, {}).get("price")
            if not ref:
                logger.warning(f"{it.ticker}: no reference price — skipped")
                continue
            bps, why = limit_buffer_bps(it.ticker, sigma, self.cfg)
            sign = 1 if it.action == "BUY" else -1
            lim = round(ref * (1 + sign * bps / 1e4), 2)

            n = plan_slices(it.quantity * ref, adv.get(it.ticker), self.cfg)
            base, rem = divmod(int(it.quantity), n)
            for s in range(n):
                qty = base + (1 if s < rem else 0)
                if qty <= 0:
                    continue
                child = ChildOrder(
                    parent=it.ticker, seq=s + 1,
                    intent=OrderIntent(
                        ticker=it.ticker, action=it.action, quantity=qty,
                        limit_price=lim, sector=it.sector, adv_usd=it.adv_usd,
                        # target_weight carries the PARENT's resulting position
                        # weight, NOT the per-slice share. Dividing it by n would let
                        # slicing smuggle an oversized position past the 15% cap:
                        # a 50% parent split into 6 children of 8.33% would pass the
                        # gate on every child and still leave a 50% position. The cap
                        # is on the RESULTING POSITION, so every child must be judged
                        # against the full parent weight.
                        target_weight=it.target_weight),
                    original_limit=lim, planned_qty=qty, ref_price=ref)
                child.log("PLANNED", qty=qty, limit=lim, buffer_bps=round(bps, 1),
                          slices=n, why=why)
                children.append(child)
        self.children = children
        return children

    # ── execution ───────────────────────────────────────────────────────────
    def run(self, pf: PortfolioState, book_value: float, *, wave_pause_s: int | None = None):
        """Submit every child through the gate, wait, reprice within bounds."""
        cfg = self.cfg
        pause = cfg["slice_interval_s"] if wave_pause_s is None else wave_pause_s
        # The gate's >=10-names check judges the RESULTING book. Passing the run-start
        # state to every wave made it judge each later wave and reprice batch alone:
        # on 2026-10-01 a fresh build from flat passed wave 1 (15 names) and then
        # blocked wave 2 (5 names) and late reprices (2 names) as "a book of 5 / 2".
        # Work on a copy that accumulates every wave the gate approved.
        pf = copy.deepcopy(pf)
        waves = {}
        for c in self.children:
            waves.setdefault(c.seq, []).append(c)

        for seq in sorted(waves):
            wave = waves[seq]
            logger.info(f"--- WAVE {seq}/{max(waves)} : {len(wave)} child orders ---")
            if not self._submit(wave, pf, book_value, tag=f"wave{seq}"):
                return self._summary()
            if not self.dry_run:
                self._wait_and_reprice(wave, pf, book_value)
            if seq != max(waves) and not self.dry_run and pause:
                logger.info(f"TWAP pause {pause}s before the next wave")
                time.sleep(pause)
        return self._summary()

    @staticmethod
    def _project(pf: PortfolioState, approved: list) -> None:
        """Fold gate-approved wave intents into the projected positions.

        Only WAVE submissions are projected. A reprice re-submits the remainder of an
        order already projected, so applying it again would double-count.
        """
        for i in approved:
            p = pf.positions.setdefault(i.ticker, {})
            held = p.get("qty")
            if i.action == "BUY":
                p["qty"] = (held or 0) + i.quantity
            elif held is None or i.quantity >= held:
                pf.positions.pop(i.ticker, None)        # exit (or unknown: assume exit)
            else:
                p["qty"] = held - i.quantity

    def _submit(self, children: list, pf, book_value, tag: str) -> bool:
        """Submit a wave THROUGH THE GATE. Returns False if the gate halted."""
        intents = [c.intent for c in children]
        res = self.br.submit_orders(intents, pf, execute=not self.dry_run,
                                    book_value=book_value)
        if not res["halted"] and tag.startswith("wave"):
            self._project(pf, res.get("approved", []))
        if res["halted"]:
            self.halted = {"switch": res["halt_switch"], "detail": res["halt_detail"]}
            # Mark EVERY not-yet-working child halted, not just this wave's — a
            # portfolio-wide halt stops the whole order, including waves that were
            # never reached. Leaving them "PENDING" would misreport the outcome.
            for c in self.children:
                if c.status in ("PENDING", "WORKING"):
                    c.status = "HALTED"
                    c.log("GATE_HALT", switch=res["halt_switch"],
                          detail=res["halt_detail"],
                          wave_reached=(c in children))
            logger.error(f"[{tag}] ORDER PATH HALTED — nothing submitted")
            return False

        blocked = {i.ticker for i, _, _ in res["blocked"]}
        for c in children:
            if c.intent.ticker in blocked:
                reason = next(r for i, s, r in res["blocked"] if i.ticker == c.intent.ticker)
                c.status = "BLOCKED"
                c.log("GATE_BLOCK", reason=reason)
        for c, tr in zip([c for c in children if c.intent.ticker not in blocked],
                         res.get("placed", [])):
            c.trades.append(tr)
            c.status = "WORKING"
            c.log("SUBMITTED", limit=c.intent.limit_price, qty=c.intent.quantity)
        return True

    def _wait_and_reprice(self, wave: list, pf, book_value):
        cfg = self.cfg
        for attempt in range(cfg["reprice_max_attempts"] + 1):
            self._sleep_updates(cfg["reprice_interval_s"])
            self._harvest(wave)
            pending = [c for c in wave
                       if c.status == "WORKING" and c.filled < c.planned_qty]
            if not pending:
                return
            if attempt == cfg["reprice_max_attempts"]:
                for c in pending:
                    c.status = "GAVE_UP"
                    c.log("GAVE_UP", filled=c.filled, of=c.intent.quantity,
                          reprices=c.reprices,
                          reason=f"{cfg['reprice_max_attempts']} reprices exhausted")
                return

            reprice_batch = []
            for c in pending:
                sign = 1 if c.intent.action == "BUY" else -1
                new_lim = round(c.intent.limit_price *
                                (1 + sign * cfg["reprice_step_bps"] / 1e4), 2)
                drift = abs(new_lim - c.original_limit) / c.original_limit * 1e4
                if drift > cfg["reprice_total_drift_cap_bps"]:
                    c.status = "DRIFT_CAPPED"
                    c.log("DRIFT_CAP", attempted_limit=new_lim,
                          drift_bps=round(drift, 1),
                          cap_bps=cfg["reprice_total_drift_cap_bps"],
                          reason="cumulative drift cap reached — not repriced further")
                    continue
                # Cancel the resting remainder FIRST, let the cancel land, then re-read
                # fills before sizing the replacement. Sizing before the cancel let
                # fills that arrived in between be ordered again (GPRE, 2026-10-01:
                # 5 shares over plan).
                for tr in c.trades:
                    if not tr.isDone():
                        self.br.ib.cancelOrder(tr.order)
                self.br.ib.sleep(1)
                self._harvest([c])
                remaining = int(c.planned_qty - c.filled)
                if remaining <= 0:
                    continue                    # filled while cancelling: nothing to do
                c.log("REPRICE", frm=c.intent.limit_price, to=new_lim,
                      remaining=remaining, attempt=c.reprices + 1,
                      drift_bps=round(drift, 1))
                c.intent = OrderIntent(
                    ticker=c.intent.ticker, action=c.intent.action,
                    quantity=remaining, limit_price=new_lim,
                    sector=c.intent.sector, adv_usd=c.intent.adv_usd,
                    target_weight=c.intent.target_weight)
                c.reprices += 1
                reprice_batch.append(c)
            if reprice_batch:
                self.br.ib.sleep(1)
                if not self._submit(reprice_batch, pf, book_value, tag="reprice"):
                    return

    def _sleep_updates(self, seconds: int):
        end = time.time() + seconds
        while time.time() < end:
            self.br.ib.waitOnUpdate(timeout=1.0)

    @staticmethod
    def _harvest(wave: list):
        for c in wave:
            fq = sum(float(f.execution.shares) for tr in c.trades for f in tr.fills)
            notional = sum(float(f.execution.shares) * float(f.execution.price)
                           for tr in c.trades for f in tr.fills)
            c.filled = fq
            c.avg_fill = (notional / fq) if fq else 0.0
            if fq >= c.planned_qty and c.status == "WORKING":
                c.status = "FILLED"
                c.log("FILLED", qty=fq, avg=round(c.avg_fill, 4))

    def _summary(self) -> dict:
        self._harvest(self.children)
        return {
            "halted": self.halted,
            "children": [{
                "parent": c.parent, "seq": c.seq, "ticker": c.intent.ticker,
                "side": c.intent.action, "ordered": c.planned_qty,
                "working_qty": c.intent.quantity, "ref_price": c.ref_price,
                "filled": c.filled, "avg_fill": c.avg_fill or None,
                "limit": c.intent.limit_price, "original_limit": c.original_limit,
                "reprices": c.reprices, "status": c.status,
                "events": c.events,
            } for c in self.children],
        }


def persist(summary: dict, extra: dict | None = None) -> Path:
    """Write an execution record that survives Gateway restarts.

    Delegates to broker.execution_store so every run lands in ONE canonical schema
    that the dashboard can read with Gateway closed. Fills come from execution
    records, never from a live order's working quantity (which IB zeroes on
    completion — the bug that undercounted the 2026-09-29 run).
    """
    from broker.execution_store import from_engine_summary

    extra = extra or {}
    p = from_engine_summary(
        summary,
        account=extra.get("account", "unknown"),
        nav_usd=extra.get("nav_usd"),
        dry_run=extra.get("dry_run", True),
        book=extra.get("book"),
        config=EXECUTION,
    )
    logger.info(f"execution record -> {p}")
    return p
