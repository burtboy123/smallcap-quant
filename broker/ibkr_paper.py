"""
broker/ibkr_paper.py — IBKR **PAPER** broker path.

PAPER ONLY. This module is structurally incapable of reaching a live account:
a live port raises before a socket is opened, and a non-"DU" account id raises
immediately after connecting. There is no flag, env var, or argument that relaxes
either check.

Safety properties (each enforced in code, not documentation)
------------------------------------------------------------
  1. HARD PAPER-PORT ALLOWLIST   connect() refuses any port not in PAPER_PORTS.
                                 Live ports (4001/7496) are named explicitly so the
                                 error is unambiguous. Raises — never warns, never
                                 falls back.
  2. ACCOUNT-PREFIX ASSERTION    after connecting, the managed account id must start
                                 with "DU" (IBKR's paper prefix). Otherwise the client
                                 DISCONNECTS and raises. A port number alone is not
                                 proof of a paper session, so this is an independent
                                 second check.
  3. LIMIT ORDERS ONLY           market orders are rejected at the API boundary:
                                 OrderIntent cannot be constructed without a positive
                                 limit price, and _to_ib_order() refuses any order
                                 type other than "LMT".
  4. DRY-RUN BY DEFAULT          submit_orders() prints the intended orders and sends
                                 nothing unless execute=True, which the CLI sets only
                                 for the explicit --paper-execute flag.
  5. KILL-SWITCHES IN THE PATH   submit_orders() calls OrderGate.authorize() itself and
                                 places only what the gate approved, with
                                 assert_gate_enforced() immediately before the first
                                 placeOrder(). The gate is not a caller-side courtesy
                                 check that could be skipped — it is inside the only
                                 method that can place an order.

Library: ib_async (maintained continuation of ib_insync). Chosen over the official
`ibapi` because ibapi is a bare callback/threading interface requiring substantial
boilerplate for request/response correlation; less of our own code in the order path
is a safety property.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from loguru import logger

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from broker.killswitch import (  # noqa: E402
    GateDecision, KillSwitchTripped, OrderGate, OrderIntent, PortfolioState,
    assert_gate_enforced,
)

# ── Safety property 1: the allowlist ────────────────────────────────────────
PAPER_PORTS = {4002: "IB Gateway (paper)", 7497: "TWS (paper)"}
LIVE_PORTS = {4001: "IB Gateway (LIVE)", 7496: "TWS (LIVE)"}
PAPER_ACCOUNT_PREFIX = "DU"          # IBKR paper accounts; live are "U"/"DF"/etc.

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4002
DEFAULT_CLIENT_ID = 17

# Marketable-limit buffer: BUY limit = ref * (1 + BUFFER), SELL = ref * (1 - BUFFER).
# Bounded so a limit can never chase an arbitrarily bad price. Reported in
# reconciliation so slippage-vs-limit is interpretable.
LIMIT_BUFFER = 0.003                  # 30 bps

# Minimum share of the universe that must actually be priced before a book is
# sized. 0.95 not 1.00: one delisted or halted name should not block a
# rebalance, but a data outage must. See main()'s coverage refusal.
MIN_PRICE_COVERAGE = 0.95

# ACTIVE forward test. 2026Q4 was retired 2026-09-29 (it tracked an un-investable
# book) and lives on as forward_test/2026Q4_retired/. The kill-switch reads this
# directory's snapshot for its 5.3 staleness check, so pointing at a retired or
# missing period correctly HALTS the order path rather than trading blind.
from config.settings import ACTIVE_FT_PERIOD as ACTIVE_PERIOD  # noqa: E402
Q4_DIR = ROOT / "forward_test" / ACTIVE_PERIOD
RECON_DIR = ROOT / "broker" / "reconciliation"


class LivePortRefused(RuntimeError):
    """Raised before any socket is opened when a non-paper port is requested."""


class NonPaperAccountRefused(RuntimeError):
    """Raised after connecting when the account id is not a paper (DU) account."""


def _assert_paper_port(port: int) -> None:
    """Safety property 1. Called BEFORE any connection attempt."""
    if port in LIVE_PORTS:
        raise LivePortRefused(
            f"REFUSED: port {port} is {LIVE_PORTS[port]} — a LIVE trading port. "
            f"This module is paper-only and will not connect to it. "
            f"Permitted: {sorted(PAPER_PORTS)}"
        )
    if port not in PAPER_PORTS:
        raise LivePortRefused(
            f"REFUSED: port {port} is not a known paper port. "
            f"Permitted: {sorted(PAPER_PORTS)} "
            f"({', '.join(f'{p}={n}' for p, n in PAPER_PORTS.items())})"
        )


class IBKRPaperBroker:
    """Thin, safety-gated wrapper over ib_async for the PAPER account."""

    def __init__(
        self,
        port: int = DEFAULT_PORT,
        host: str = DEFAULT_HOST,
        client_id: int = DEFAULT_CLIENT_ID,
        gate: OrderGate | None = None,
    ):
        _assert_paper_port(port)                       # ← before anything else
        self.host, self.port, self.client_id = host, port, client_id
        self.gate = gate or OrderGate(
            snapshot_csv=Q4_DIR / "price_snapshots.csv")
        self.ib = None
        self.account: str | None = None

    # ── connection ──────────────────────────────────────────────────────────
    def connect(self):
        from ib_async import IB

        _assert_paper_port(self.port)                  # re-assert at call site
        self.ib = IB()
        logger.info(f"connecting to {self.host}:{self.port} "
                    f"({PAPER_PORTS[self.port]}) clientId={self.client_id}")
        self.ib.connect(self.host, self.port, clientId=self.client_id, timeout=20)

        # ── Safety property 2: account-prefix assertion ─────────────────────
        accounts = list(self.ib.managedAccounts())
        logger.info(f"managed accounts reported by Gateway: {accounts}")
        if not accounts:
            self.ib.disconnect()
            raise NonPaperAccountRefused("REFUSED: Gateway reported no managed accounts.")
        bad = [a for a in accounts if not a.startswith(PAPER_ACCOUNT_PREFIX)]
        if bad:
            self.ib.disconnect()
            raise NonPaperAccountRefused(
                f"REFUSED: account(s) {bad} do not start with "
                f"'{PAPER_ACCOUNT_PREFIX}' — not a paper account. Disconnected."
            )
        self.account = accounts[0]
        logger.success(
            f"PAPER ACCOUNT CONFIRMED: {self.account} "
            f"(prefix '{PAPER_ACCOUNT_PREFIX}' assertion PASSED)")
        return self

    def disconnect(self):
        if self.ib is not None and self.ib.isConnected():
            self.ib.disconnect()

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.disconnect()
        return False

    # ── read path ───────────────────────────────────────────────────────────
    def account_summary(self) -> dict:
        """Account summary, CURRENCY-AWARE.

        The paper account's base currency is NOT assumed to be USD. An earlier
        version filtered on `currency in ("USD","")` and silently returned an EMPTY
        dict against this EUR-based account — which would have sized a USD book off
        a zero/foreign NAV. Currency is now surfaced explicitly, and a USD-equivalent
        NAV is computed via the broker's own FX rate when the base is not USD.
        """
        rows = self.ib.accountValues(self.account)
        want = {"NetLiquidation", "TotalCashValue", "BuyingPower",
                "GrossPositionValue", "AvailableFunds"}
        vals, base_ccy = {}, None
        for r in rows:
            if r.tag in want and r.currency not in ("BASE", ""):
                vals[r.tag] = float(r.value)
                base_ccy = base_ccy or r.currency
        base_ccy = base_ccy or "USD"
        out = {"_base_currency": base_ccy, **vals}

        nav = vals.get("NetLiquidation", 0.0)
        if base_ccy == "USD":
            out["_fx_to_usd"] = 1.0
            out["_nav_usd"] = nav
        else:
            fx = self.fx_rate_to_usd(base_ccy)
            out["_fx_to_usd"] = fx
            out["_nav_usd"] = nav * fx
            logger.warning(
                f"account base currency is {base_ccy}, not USD. "
                f"{base_ccy}USD={fx:.5f} -> NAV {nav:,.2f} {base_ccy} "
                f"= ${out['_nav_usd']:,.2f}. USD book sized off the USD-equivalent NAV.")
        return out

    def fx_rate_to_usd(self, ccy: str) -> float:
        """FX rate from the BROKER's own quote, else its historical close.

        Falls back to the historical daily close (the historical farm needs no
        market-data subscription) because a live quote can be unavailable for reasons
        unrelated to us — e.g. IB error 10197 "No market data during competing live
        session" when another client holds the data line. If BOTH fail this RAISES:
        an FX rate is never guessed, because the whole USD book is sized off it.
        """
        from ib_async import Forex

        c = self.ib.qualifyContracts(Forex(f"{ccy}USD"))[0]
        try:
            self.ib.reqMarketDataType(3)          # delayed is fine for an FX rate
            t = self.ib.reqTickers(c)[0]
            if t.bid and t.ask and t.bid > 0 and t.ask > 0:
                return float((t.bid + t.ask) / 2)
            for cand in (t.last, t.close):
                if cand and cand == cand and cand > 0:
                    return float(cand)
        except Exception as e:
            logger.warning(f"{ccy}USD live quote failed ({e}) — trying historical")

        try:
            bars = self.ib.reqHistoricalData(
                c, endDateTime="", durationStr="5 D", barSizeSetting="1 day",
                whatToShow="MIDPOINT", useRTH=False, formatDate=1)
            if bars and bars[-1].close and bars[-1].close > 0:
                rate = float(bars[-1].close)
                logger.warning(f"{ccy}USD from HISTORICAL close: {rate:.5f} "
                               f"(no live quote available)")
                return rate
        except Exception as e:
            logger.error(f"{ccy}USD historical fallback failed — {e}")

        raise RuntimeError(f"REFUSED: no usable {ccy}USD rate — cannot size a USD book.")

    def positions(self) -> pd.DataFrame:
        pos = self.ib.positions(self.account)
        if not pos:
            return pd.DataFrame(columns=["ticker", "position", "avg_cost"])
        return pd.DataFrame([{
            "ticker": p.contract.symbol,
            "position": float(p.position),
            "avg_cost": float(p.avgCost),
        } for p in pos])

    # ── pricing ─────────────────────────────────────────────────────────────
    def market_data_preflight(self, probe: str = "SPY") -> dict:
        """Probe whether ANY pricing path works before a book is sized off one.

        Both pricing paths can be refused by IB for reasons that have nothing to do
        with this code: another client logged into the same credentials from another
        IP takes the market-data line, and then every quote comes back NaN (error
        10197 "No market data during competing live session") AND the historical
        farm refuses too (error 162 "Trading TWS session is connected from a
        different IP address"). Account values keep reading fine, so the connection
        looks healthy while nothing can actually be priced.

        Without this probe the failure surfaces late and quietly: reference_prices()
        returns {} and build_intents() skips every name, producing a run that exits
        0 having submitted nothing. Fail loudly and early instead.
        """
        from ib_async import Stock

        out = {"delayed_quote": False, "historical": False, "probe": probe}
        try:
            c = self.ib.qualifyContracts(Stock(probe, "SMART", "USD"))[0]
        except Exception as e:
            out["detail"] = f"could not even qualify {probe}: {e}"
            return out

        try:
            self.ib.reqMarketDataType(3)
            t = self.ib.reqTickers(c)[0]
            out["delayed_quote"] = any(
                v and v == v and v > 0 for v in (_f(t.bid), _f(t.last), _f(t.close)))
        except Exception as e:
            logger.debug(f"preflight delayed quote failed: {e}")

        try:
            bars = self.ib.reqHistoricalData(
                c, endDateTime="", durationStr="5 D", barSizeSetting="1 day",
                whatToShow="TRADES", useRTH=True, formatDate=1)
            out["historical"] = bool(bars) and bars[-1].close > 0
        except Exception as e:
            logger.debug(f"preflight historical failed: {e}")

        if out["delayed_quote"] or out["historical"]:
            paths = [k for k in ("delayed_quote", "historical") if out[k]]
            out["detail"] = f"pricing available via {', '.join(paths)}"
        else:
            out["detail"] = (
                "NO pricing path works: both the delayed quote and the historical "
                "farm returned nothing for " + probe + ". The usual cause is another "
                "TWS/Gateway client logged into these same credentials from a "
                "different IP, which holds the market-data line (IB errors 10197 / "
                "162). Log that other session out, then retry.")
        return out

    def reference_prices(self, tickers: list) -> dict:
        """Reference price per ticker: midpoint, else last, else close.

        This paper account has NO real-time US market-data subscription (IB error
        10089 on every symbol: "Requested market data requires additional
        subscription for API… Delayed market data is available."). Rather than
        fabricate prices, we explicitly request DELAYED data (reqMarketDataType(3),
        falling back to 4 = delayed-frozen), and fall back again to the last
        historical daily close for any symbol still unpriced.

        CONSEQUENCE, stated plainly: limit prices are derived from data that may be
        ~15 minutes stale. That is acceptable for a paper shakedown but it widens
        the gap between the limit and the true touch, and it must be read as a
        caveat on any slippage figure. `source` records which path priced each name.
        """
        from ib_async import Stock

        contracts = [Stock(t, "SMART", "USD") for t in tickers]
        qualified = self.ib.qualifyContracts(*contracts)

        for mkt_type, label in ((3, "DELAYED"), (4, "DELAYED_FROZEN")):
            self.ib.reqMarketDataType(mkt_type)
            tick = self.ib.reqTickers(*qualified)
            out = {}
            for t in tick:
                sym = t.contract.symbol
                bid, ask, last, close = _f(t.bid), _f(t.ask), _f(t.last), _f(t.close)
                if bid and ask:
                    px, src = (bid + ask) / 2, f"{label}_MID"
                elif last:
                    px, src = last, f"{label}_LAST"
                elif close:
                    px, src = close, f"{label}_CLOSE"
                else:
                    continue
                out[sym] = {"price": float(px), "bid": bid, "ask": ask,
                            "last": last, "close": close, "source": src}
            if out:
                logger.info(f"priced {len(out)}/{len(tickers)} via {label} market data")
                break

        missing = [t for t in tickers if t not in out]
        if missing:
            logger.warning(f"no market data for {missing} — falling back to "
                           f"last historical daily close")
            out.update(self._historical_closes(missing))
        return out

    def _historical_closes(self, tickers: list) -> dict:
        """Last daily close from IB's historical farm (no subscription needed)."""
        from ib_async import Stock

        out = {}
        for t in tickers:
            try:
                c = self.ib.qualifyContracts(Stock(t, "SMART", "USD"))[0]
                bars = self.ib.reqHistoricalData(
                    c, endDateTime="", durationStr="5 D", barSizeSetting="1 day",
                    whatToShow="TRADES", useRTH=True, formatDate=1)
                if bars:
                    px = float(bars[-1].close)
                    out[t] = {"price": px, "bid": None, "ask": None, "last": None,
                              "close": px, "source": "HIST_CLOSE"}
            except Exception as e:
                logger.warning(f"{t}: historical close failed — {e}")
        return out

    # ── order construction ──────────────────────────────────────────────────
    @staticmethod
    def _to_ib_order(intent: OrderIntent):
        """Safety property 3: LIMIT only, enforced at the API boundary."""
        from ib_async import LimitOrder

        if intent.limit_price is None or intent.limit_price <= 0:
            raise ValueError(
                f"REFUSED: {intent.ticker} has no positive limit price — this module "
                f"never emits market orders.")
        o = LimitOrder(intent.action, intent.quantity, round(intent.limit_price, 2))
        if o.orderType != "LMT":
            raise ValueError(f"REFUSED: order type {o.orderType!r} is not LMT.")
        o.tif = "DAY"
        o.outsideRth = False
        return o

    # ── the ONLY method that can place an order ─────────────────────────────
    def submit_orders(
        self,
        intents: list,
        pf: PortfolioState,
        *,
        execute: bool = False,          # safety property 4: dry-run default
        book_value: float | None = None,
    ) -> dict:
        """Authorize through the kill-switch gate, then (optionally) place.

        The gate is called HERE, inside the submission path — callers cannot
        bypass it by forgetting to check. A portfolio-wide halt raises and
        nothing is placed.
        """
        result = {"halted": False, "halt_switch": None, "halt_detail": None,
                  "approved": [], "blocked": [], "placed": [], "executed": execute}

        # ── Safety property 5: kill-switches IN the path ────────────────────
        try:
            decision: GateDecision = self.gate.authorize(
                intents, pf, book_value=book_value)
        except KillSwitchTripped as e:
            logger.error(f"ORDER PATH HALTED by {e.switch.value}: {e.detail}")
            result.update(halted=True, halt_switch=e.switch.value,
                          halt_detail=e.detail, blocked=[(i, "HALT", e.detail)
                                                          for i in intents])
            # Alert on the HALT itself. A kill-switch that trips into a log nobody
            # reads is the silent-failure pattern this monitoring exists to end.
            # _safe_dispatch never raises, so alerting cannot affect the refusal.
            from monitoring.alerting import alert_killswitch_trip
            alert_killswitch_trip(
                e.switch.value, e.detail,
                context=f"{len(intents)} intent(s) dropped; execute={execute}")
            return result

        result["approved"] = decision.approved
        result["blocked"] = [(i, s.value, r) for i, s, r in decision.blocked]

        # No halt this time: close kill-switch alerts from earlier runs, and the
        # blocked-rebalance alert too if nothing was blocked. Never raises.
        from monitoring.alerting import resolve_after_clean_gate
        resolve_after_clean_gate(blocked=bool(decision.blocked))

        if decision.blocked:
            from monitoring.alerting import alert_rebalance_blocked
            alert_rebalance_blocked(
                len(decision.blocked), len(decision.approved),
                [f"{i.ticker} {i.action}: {r}" for i, _s, r in decision.blocked])

        if not execute:
            logger.warning("DRY-RUN — nothing sent. Pass --paper-execute to submit.")
            return result

        # Backstop: fires if anything not approved reaches this point.
        assert_gate_enforced(decision, decision.approved)

        for intent in decision.approved:
            from ib_async import Stock
            contract = self.ib.qualifyContracts(
                Stock(intent.ticker, "SMART", "USD"))[0]
            order = self._to_ib_order(intent)
            trade = self.ib.placeOrder(contract, order)
            result["placed"].append(trade)
            logger.info(f"PLACED {intent.action} {intent.quantity} {intent.ticker} "
                        f"LMT {intent.limit_price:.2f}")
        return result

    def await_fills(self, trades: list, timeout_s: int = 60) -> list:
        """Wait for terminal state (or timeout) so reconciliation can read fills."""
        import time
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            self.ib.waitOnUpdate(timeout=1.0)
            if all(t.isDone() for t in trades):
                break
        return trades


def _f(x):
    try:
        v = float(x)
        return None if v != v or v <= 0 else v      # NaN / non-positive -> None
    except (TypeError, ValueError):
        return None


# ── ADV (liquidity limit input) ─────────────────────────────────────────────
def load_adv(tickers: list, lookback: int = 60) -> dict:
    """Median daily dollar volume from the local price history."""
    conn = sqlite3.connect(f"file:{(ROOT/'quant_system.db').as_posix()}?mode=ro", uri=True)
    q = ",".join("?" * len(tickers))
    df = pd.read_sql(
        f"SELECT ticker, date, close, volume FROM prices WHERE ticker IN ({q})",
        conn, params=tickers)
    conn.close()
    out = {}
    for t, g in df.groupby("ticker"):
        g = g.sort_values("date").tail(lookback)
        dv = (g["close"] * g["volume"]).dropna()
        if len(dv):
            out[t] = float(dv.median())
    return out


# ── target-book translation ─────────────────────────────────────────────────
def build_intents(
    book: pd.DataFrame,
    positions: pd.DataFrame,
    nav: float,
    prices: dict,
    adv: dict,
    limit_buffer: float = LIMIT_BUFFER,
    weights: dict | None = None,
) -> list:
    """Diff the target book against actual positions into LIMIT OrderIntents.

    Covers BOTH directions. An earlier version iterated only the target book, so a
    position held but no longer in the book was silently left on -- a rebalance that
    never exits anything. Names that dropped out are liquidated first.

    Sizing (PORTFOLIO in config/settings.py):
      - weights are scaled down if they sum above 1 (15 x 0.0667 = 100.05%);
      - cash_reserve of NAV stays uninvested, covering BUY limits above reference;
      - an adjustment to a held name smaller than min_trade_weight of NAV is skipped.
        Exits and new positions are never skipped.
    BUY target_weight is the RESULTING position weight -- what the gate's position
    cap must judge -- not the size of this order. For a fresh build they are equal;
    for a top-up the order alone understated the position (14% held + 3% bought
    passed the 15% cap as "3%").

    `weights` (ticker -> weight) overrides the book's flat `weight_equal`. main()
    passes the buy-and-hold weights (broker/targets.py) for a forward-test book, so
    a mid-quarter top-up does not trim winners and add to losers back to equal weight.
    """
    from config.settings import PORTFOLIO
    reserve = float(PORTFOLIO.get("cash_reserve", 0.0))
    min_trade = float(PORTFOLIO.get("min_trade_weight", 0.0))
    w_of = {t: float(w) for t, w in zip(book["ticker"], book["weight_equal"])}
    if weights is not None:
        w_of.update({t: float(w) for t, w in dict(weights).items() if t in w_of})
    total_w = sum(w_of.values())
    scale = (1.0 / total_w) if total_w > 1.0 else 1.0
    investable = nav * (1.0 - reserve)
    held = dict(zip(positions["ticker"], positions["position"])) if len(positions) else {}
    intents = []

    # ── exits: held but NOT in the target book ──────────────────────────────
    target_names = set(book["ticker"])
    for t, qty in held.items():
        if t in target_names or qty == 0:
            continue
        if t not in prices:
            logger.warning(f"{t}: held but no reference price — CANNOT EXIT, skipped")
            continue
        ref = prices[t]["price"]
        action = "SELL" if qty > 0 else "BUY"        # covers a short, if one existed
        lim = ref * (1 - limit_buffer) if action == "SELL" else ref * (1 + limit_buffer)
        intents.append(OrderIntent(
            ticker=t, action=action, quantity=int(abs(qty)), limit_price=round(lim, 2),
            sector=None, adv_usd=adv.get(t), target_weight=None))
        logger.info(f"{t}: exiting {qty:.0f} shares — not in the target book")

    for _, r in book.iterrows():
        t = r["ticker"]
        if t not in prices:
            logger.warning(f"{t}: no reference price — skipped")
            continue
        ref = prices[t]["price"]
        target_notional = investable * w_of[t] * scale
        target_shares = int(target_notional // ref)
        have = int(held.get(t, 0))
        delta = target_shares - have
        if delta == 0:
            continue
        if have and target_shares and abs(delta) * ref < min_trade * nav:
            logger.debug(f"{t}: {delta:+d} sh (${abs(delta) * ref:,.0f}) below the "
                         f"{min_trade:.2%} min trade -- skipped")
            continue
        action = "BUY" if delta > 0 else "SELL"
        # marketable limit, bounded
        lim = ref * (1 + limit_buffer) if action == "BUY" else ref * (1 - limit_buffer)
        intents.append(OrderIntent(
            ticker=t, action=action, quantity=abs(delta), limit_price=round(lim, 2),
            sector=r.get("sector"), adv_usd=adv.get(t),
            target_weight=(target_shares * ref) / nav if action == "BUY" else None,
        ))
    return intents


def size_book(book_path, book: pd.DataFrame, positions: pd.DataFrame, nav: float,
              prices: dict, adv: dict) -> tuple[list, str]:
    """Order intents for `book`, sized to where the forward test's book IS today.

    A forward-test book is buy-and-hold, so its weights drift with prices; sizing a
    mid-quarter top-up to the flat entry weights would trim every winner and add to
    every loser (broker/targets.py). Books with no recorded baselines (a fresh build)
    get their entry weights. Returns (intents, a one-line description of the basis).
    """
    from broker.targets import baselines_for, buy_and_hold_weights
    weights, basis = buy_and_hold_weights(
        book, baselines_for(book_path), {t: p["price"] for t, p in prices.items()})
    return build_intents(book, positions, nav, prices, adv, weights=weights), basis


def print_orders(intents: list, prices: dict, title: str = "INTENDED ORDERS"):
    print(f"\n=== {title} ({len(intents)}) ===")
    if not intents:
        print("  (none)")
        return
    print(f"  {'TICKER':<8}{'SIDE':<6}{'QTY':>8}{'REF':>10}{'LIMIT':>10}"
          f"{'NOTIONAL':>12}  {'SECTOR':<24}{'ADV($m)':>9}  {'PX SOURCE':<16}")
    for i in sorted(intents, key=lambda x: -x.notional):
        ref = prices.get(i.ticker, {}).get("price", float("nan"))
        adv = f"{i.adv_usd/1e6:8.1f}" if i.adv_usd else "     n/a"
        src = prices.get(i.ticker, {}).get("source", "?")
        print(f"  {i.ticker:<8}{i.action:<6}{i.quantity:>8}{ref:>10.2f}"
              f"{i.limit_price:>10.2f}{i.notional:>12,.0f}  {str(i.sector):<24}{adv}"
              f"  {src:<16}")
    print(f"  {'TOTAL':<8}{'':<6}{'':>8}{'':>10}{'':>10}"
          f"{sum(i.notional for i in intents):>12,.0f}")


def print_gate_result(result: dict):
    if result["halted"]:
        print(f"\n*** ORDER PATH HALTED — {result['halt_switch']} ***")
        print(f"    {result['halt_detail']}")
        print("    NOTHING was submitted.")
        return
    print(f"\n=== KILL-SWITCH GATE ===")
    print(f"  approved: {len(result['approved'])}")
    print(f"  blocked : {len(result['blocked'])}")
    for intent, switch, reason in result["blocked"]:
        print(f"    BLOCKED {intent.ticker:<6} {intent.action:<5} — [{switch}] {reason}")


# ── CLI ─────────────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="IBKR PAPER broker path (paper only).")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--client-id", type=int, default=DEFAULT_CLIENT_ID)
    ap.add_argument("--read-only", action="store_true",
                    help="connect, report account + positions, place nothing")
    ap.add_argument("--paper-execute", action="store_true",
                    help="ACTUALLY SUBMIT to the PAPER account (default is dry-run)")
    ap.add_argument("--book", default=str(Q4_DIR / "incumbent_book.csv"))
    ap.add_argument("--execution-engine", action="store_true",
                    help="volatility-sized limits + TWAP slicing + bounded repricing")
    ap.add_argument("--slice-pause", type=int, default=None,
                    help="override EXECUTION['slice_interval_s'] between TWAP waves")
    ap.add_argument("--nav-override", type=float, default=None,
                    help="use this NAV instead of the account's net liquidation")
    ap.add_argument("--skip-health-tests", action="store_true",
                    help="health gate skips the pytest run (the data checks still "
                         "run and still block; this only trades ~3s for less "
                         "regression cover)")
    args = ap.parse_args(argv)

    with IBKRPaperBroker(port=args.port, client_id=args.client_id) as br:
        pf_chk = br.market_data_preflight()
        ok = pf_chk["delayed_quote"] or pf_chk["historical"]
        print(f"\n=== MARKET DATA PREFLIGHT ({pf_chk['probe']}) ===")
        print(f"  delayed quote : {'OK' if pf_chk['delayed_quote'] else 'UNAVAILABLE'}")
        print(f"  historical    : {'OK' if pf_chk['historical'] else 'UNAVAILABLE'}")
        print(f"  {pf_chk['detail']}")
        if not ok and not args.read_only:
            raise RuntimeError("REFUSED: no pricing path available — "
                               + pf_chk["detail"])
        summ = br.account_summary()
        pos = br.positions()
        ccy = summ.get("_base_currency", "USD")
        print(f"\n=== ACCOUNT {br.account} (PAPER) — base currency {ccy} ===")
        for k in ["NetLiquidation", "TotalCashValue", "BuyingPower",
                  "GrossPositionValue", "AvailableFunds"]:
            if k in summ:
                print(f"  {k:<20} {summ[k]:>15,.2f} {ccy}")
        if ccy != "USD":
            print(f"  {'FX ' + ccy + 'USD':<20} {summ['_fx_to_usd']:>15,.5f}")
            print(f"  {'NAV (USD equiv)':<20} {summ['_nav_usd']:>15,.2f} USD"
                  f"   <- the USD book is sized off this")
        print(f"\n=== POSITIONS ({len(pos)}) ===")
        print("  (flat)" if pos.empty else pos.to_string(index=False))

        # Drawdown vs the account's recorded high-water mark, in its base currency
        # (broker/nav_history.py). Recorded on every connection, read-only included,
        # so the mark keeps up even between trading runs.
        from broker import nav_history
        from broker.killswitch import MAX_DRAWDOWN_HALT
        nav_base = float(summ.get("NetLiquidation", 0.0) or 0.0)
        hw = nav_history.high_water_mark(br.account, nav_base)
        nav_history.record(br.account, nav_base, ccy)
        print(f"\n=== DRAWDOWN ({ccy}) ===")
        print(f"  high-water mark {hw['hwm']:,.2f} ({hw['hwm_ts']})   now "
              f"{nav_base:,.2f}   drawdown {hw['drawdown']:+.2%}   "
              f"(5.1 halts at {MAX_DRAWDOWN_HALT:.0%})")

        # Save what the account holds on every connection, read-only included. The
        # weekly report and risk watch compare the book with the newest look at the
        # account; until 2026-11-17 they read only the last EXECUTION's file, so a
        # 2026-10-06 read-only that showed 15/15 never reached them and the Monday email
        # said "NUTX missing, finish the book" for seven weeks.
        save_positions(br.account, pos)

        if args.read_only:
            return 0

        # ── PIPELINE HEALTH GATE ────────────────────────────────────────────
        # A book must never be sized from a pipeline that is not recording. The
        # forward test silently wrote 0-row snapshots for nine days; expressed as
        # orders, that failure is a rebalance against data that does not exist.
        # Applies to the DRY RUN too, deliberately: the dry run is what you read
        # before executing, so it must not present numbers derived from a broken
        # pipeline. --read-only is exempt because it sizes nothing.
        # No override flag, following the kill-switch precedent.
        from monitoring.alerting import alert_rebalance_refused
        from monitoring.health_check import PipelineUnhealthy, assert_pipeline_healthy
        try:
            assert_pipeline_healthy(skip_tests=args.skip_health_tests)
        except PipelineUnhealthy as e:
            alert_rebalance_refused("pipeline health gate refused the rebalance",
                                    detail=str(e))
            raise

        book = pd.read_csv(args.book)
        # Same guard as the forward-test builders: a book that breaches a §4 hard
        # limit must never reach the broker, dry-run or not. Raises, never warns.
        from broker.book_guard import assert_book_investable
        assert_book_investable(book, label=f"target book {Path(args.book).name}")
        # Size the USD book off the USD-EQUIVALENT NAV — never the raw base-currency
        # figure (see account_summary docstring: this account is EUR-based).
        nav = args.nav_override or summ.get("_nav_usd", 0.0)
        if not nav:
            raise RuntimeError("REFUSED: NAV resolved to 0 — refusing to size a book.")
        # Union of target and held names — exits need prices/ADV too, or a dropped
        # position can never be liquidated.
        universe = sorted(set(book["ticker"]) | set(pos["ticker"] if len(pos) else []))
        prices = br.reference_prices(universe)
        # A book sized off a handful of prices is not the book. Without this,
        # build_intents() merely WARNS per unpriced name and returns a short list, so
        # a data outage looks like a clean run that happened to trade little.
        cover = len(prices) / max(len(universe), 1)
        if cover < MIN_PRICE_COVERAGE:
            raise RuntimeError(
                f"REFUSED: priced only {len(prices)}/{len(universe)} names "
                f"({cover:.0%} < {MIN_PRICE_COVERAGE:.0%} required). Sizing a book "
                f"off this would silently deploy a fraction of it. "
                f"{br.market_data_preflight()['detail']}")
        adv = load_adv(universe)
        intents, basis = size_book(args.book, book, pos, nav, prices, adv)
        print(f"\n=== TARGET WEIGHTS ===\n  {basis}")
        print_orders(intents, prices)

        pf = PortfolioState(
            equity=nav,
            # The recorded high-water mark, expressed in the same USD units as `nav`
            # so equity / HWM - 1 equals the base-currency drawdown. It used to be
            # `nav` itself, which made the 5.1 MAX_DRAWDOWN halt unreachable.
            high_water_mark=nav / (1.0 + hw["drawdown"]) if nav else nav,
            positions={r.ticker: {"entry_price": r.avg_cost, "qty": r.position,
                                  "last_price":
                                  prices.get(r.ticker, {}).get("price", r.avg_cost)}
                       for r in pos.itertuples()} if len(pos) else {},
        )
        if not intents:
            # Book already matches: nothing to send. Still run the gate once (it
            # submits nothing with zero intents) so a portfolio-wide halt is reported
            # now rather than at the next trade, and stale halt alerts are cleared.
            print("  book already at target -- nothing to trade")
            print_gate_result(br.submit_orders([], pf, execute=False, book_value=nav))
            return 0
        if args.execution_engine:
            # Volatility-sized limits + TWAP slicing + bounded repricing. Every child
            # and every reprice still goes through br.submit_orders(), so the
            # kill-switch gate runs on each one — slicing cannot route around it.
            from broker.execution import (
                ExecutionEngine, daily_sigma_bps, limit_buffer_bps, persist,
            )
            sigma = daily_sigma_bps(universe)
            eng = ExecutionEngine(br, dry_run=not args.paper_execute)
            children = eng.plan(intents, prices, adv, sigma)
            print(f"\n=== EXECUTION PLAN ({len(children)} child orders from "
                  f"{len(intents)} parents) ===")
            print(f"  {'TICKER':<8}{'SEQ':>5}{'QTY':>8}{'LIMIT':>9}{'BUF bp':>8}  WHY")
            for c in children:
                bps, why = limit_buffer_bps(c.intent.ticker, sigma)
                print(f"  {c.intent.ticker:<8}{c.seq:>5}{c.intent.quantity:>8}"
                      f"{c.intent.limit_price:>9.2f}{bps:>8.0f}  {why}")
            summary = eng.run(pf, nav, wave_pause_s=args.slice_pause)
            if summary["halted"]:
                print(f"\n*** ORDER PATH HALTED — {summary['halted']['switch']} ***")
                print(f"    {summary['halted']['detail']}")
                print("    NOTHING was submitted.")
            persist(summary, extra={"account": br.account, "nav_usd": nav,
                                    "book": args.book, "dry_run": not args.paper_execute})
            print_execution_summary(summary)
            if args.paper_execute:
                recon = reconcile_children(summary, prices, nav, br)
                print_reconciliation(recon)
            return 0

        result = br.submit_orders(intents, pf, execute=args.paper_execute,
                                  book_value=nav)
        print_gate_result(result)

        if args.paper_execute and result["placed"]:
            br.await_fills(result["placed"])
            recon = reconcile(result, prices, nav, br)
            RECON_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            (RECON_DIR / f"recon_{stamp}.json").write_text(
                json.dumps(recon, indent=2, default=str), encoding="utf-8")
            print_reconciliation(recon)
    return 0


def save_positions(account: str, pos: pd.DataFrame) -> Path:
    """Write positions_<stamp>.json (same `positions_after` shape as a recon file)."""
    RECON_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    p = RECON_DIR / f"positions_{now:%Y%m%d_%H%M%S}.json"
    p.write_text(json.dumps({"timestamp": now.isoformat(timespec="seconds"),
                             "account": account, "source": "connection",
                             "positions_after": pos.to_dict("records")},
                            indent=2, default=str), encoding="utf-8")
    return p


def print_execution_summary(summary: dict):
    ch = pd.DataFrame(summary["children"])
    print(f"\n=== EXECUTION SUMMARY ({len(ch)} child orders) ===")
    if ch.empty:
        print("  (none)")
        return
    print(f"  {'TICKER':<8}{'SEQ':>4}{'ORD':>8}{'FILL':>8}{'LIMIT0':>9}{'LIMIT':>9}"
          f"{'RPX':>5}  STATUS")
    for r in ch.sort_values(["ticker", "seq"]).itertuples():
        print(f"  {r.ticker:<8}{r.seq:>4}{r.ordered:>8.0f}{r.filled:>8.0f}"
              f"{r.original_limit:>9.2f}{r.limit:>9.2f}{r.reprices:>5}  {r.status}")
    print(f"\n  reprices total : {int(ch.reprices.sum())}")
    print(f"  status counts  : {ch.status.value_counts().to_dict()}")


def reconcile_children(summary: dict, prices: dict, nav: float,
                       br: IBKRPaperBroker) -> dict:
    """Aggregate child orders back to parent level for reporting."""
    ch = pd.DataFrame(summary["children"])
    rows = []
    if not ch.empty:
        for tk, g in ch.groupby("ticker"):
            ordered, filled = g.ordered.sum(), g.filled.sum()
            fills = g[g.filled > 0]
            avg = ((fills.avg_fill * fills.filled).sum() / fills.filled.sum()
                   if fills.filled.sum() else 0.0)
            lim = float(g.limit.mean())
            slip = (avg - lim) if (avg and g.side.iloc[0] == "BUY") else None
            # Cost vs the price the order was sized off (positive = paid more / sold
            # for less). Unlike slippage vs LIMIT, this is not flattered by the buffer.
            ref = (float(g.ref_price.dropna().mean())
                   if "ref_price" in g and g.ref_price.notna().any() else None)
            sign = 1 if g.side.iloc[0] == "BUY" else -1
            cost_ref = (round(sign * (avg - ref) / ref * 1e4, 1)
                        if (avg and ref) else None)
            rows.append({
                "ticker": tk, "side": g.side.iloc[0], "ordered": float(ordered),
                "filled": float(filled), "remaining": float(ordered - filled),
                "limit": lim, "avg_fill": avg or None,
                "slippage_vs_limit_bps": round(slip / lim * 1e4, 1)
                                          if (slip is not None and lim) else None,
                "ref": ref, "cost_vs_ref_bps": cost_ref,
                "status": ",".join(sorted(set(g.status))),
                "reprices": int(g.reprices.sum()), "children": len(g),
                "fully_filled": ordered - filled == 0 and filled > 0,
                "partial": 0 < filled < ordered, "unfilled": filled == 0,
            })
    after = br.positions()
    recon = {"timestamp": datetime.now().isoformat(timespec="seconds"),
             "account": br.account, "nav_at_build": nav, "orders": rows,
             "positions_after": after.to_dict("records")}
    RECON_DIR.mkdir(parents=True, exist_ok=True)
    (RECON_DIR / f"recon_{datetime.now():%Y%m%d_%H%M%S}.json").write_text(
        json.dumps(recon, indent=2, default=str), encoding="utf-8")
    return recon


def reconcile(result: dict, prices: dict, nav: float, br: IBKRPaperBroker) -> dict:
    rows = []
    for trade in result["placed"]:
        o, c = trade.order, trade.contract
        filled = float(trade.orderStatus.filled)
        remaining = float(trade.orderStatus.remaining)
        avg = float(trade.orderStatus.avgFillPrice or 0)
        lim = float(o.lmtPrice)
        ref = prices.get(c.symbol, {}).get("price")
        # slippage vs limit: negative = filled better than the limit
        slip_vs_limit = (avg - lim) if (avg and o.action == "BUY") else (
            (lim - avg) if avg else None)
        rows.append({
            "ticker": c.symbol, "side": o.action, "ordered": float(o.totalQuantity),
            "filled": filled, "remaining": remaining,
            "limit": lim, "avg_fill": avg or None, "ref_at_build": ref,
            "slippage_vs_limit": round(slip_vs_limit, 4) if slip_vs_limit is not None else None,
            "slippage_vs_limit_bps": round(slip_vs_limit / lim * 1e4, 1)
                                      if (slip_vs_limit is not None and lim) else None,
            "status": trade.orderStatus.status,
            "fully_filled": remaining == 0 and filled > 0,
            "partial": 0 < filled < float(o.totalQuantity),
            "unfilled": filled == 0,
        })
    after = br.positions()
    return {"timestamp": datetime.now().isoformat(timespec="seconds"),
            "account": br.account, "nav_at_build": nav, "orders": rows,
            "positions_after": after.to_dict("records")}


def print_reconciliation(recon: dict):
    print(f"\n=== RECONCILIATION ({recon['account']}) ===")
    r = pd.DataFrame(recon["orders"])
    if r.empty:
        print("  (no orders placed)")
        return
    has_ref = "cost_vs_ref_bps" in r
    print(f"  {'TICKER':<8}{'SIDE':<6}{'ORD':>7}{'FILL':>7}{'REM':>6}"
          f"{'LIMIT':>9}{'AVGFILL':>9}{'SLIP(bp)':>10}"
          f"{'vsREF(bp)':>11}  STATUS")
    for _, x in r.iterrows():
        af = f"{x.avg_fill:9.2f}" if x.avg_fill else "      n/a"
        sb = (f"{x.slippage_vs_limit_bps:10.1f}"
              if pd.notna(x.slippage_vs_limit_bps) else "       n/a")
        cr = (f"{x.cost_vs_ref_bps:11.1f}"
              if has_ref and pd.notna(x.cost_vs_ref_bps) else "        n/a")
        print(f"  {x.ticker:<8}{x.side:<6}{x.ordered:>7.0f}{x.filled:>7.0f}"
              f"{x.remaining:>6.0f}{x.limit:>9.2f}{af}{sb}{cr}  {x.status}")
    print(f"\n  fully filled : {int(r.fully_filled.sum())}/{len(r)}")
    print(f"  partial      : {int(r.partial.sum())}")
    print(f"  unfilled     : {int(r.unfilled.sum())}")
    filled = r[r.avg_fill.notna()]
    if len(filled):
        print(f"  mean slippage vs limit: {filled.slippage_vs_limit_bps.mean():+.1f} bps "
              f"(negative = filled better than limit)")
        if has_ref and filled.cost_vs_ref_bps.notna().any():
            w = filled[filled.cost_vs_ref_bps.notna()]
            wt = (w.filled * w.avg_fill)
            print(f"  cost vs reference price: {(w.cost_vs_ref_bps * wt).sum() / wt.sum():+.1f}"
                  f" bps notional-weighted (positive = paid above the delayed mid "
                  f"the order was sized off)")


if __name__ == "__main__":
    raise SystemExit(main())
