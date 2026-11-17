"""
broker/test_killswitch_trips.py — DELIBERATE KILL-SWITCH TRIP TESTS.

Pilot plan §1.6: "An untested kill-switch is not a kill-switch."

Each test deliberately creates the failure condition and asserts that the ORDER PATH
WAS BLOCKED — not merely that a warning was emitted. A `submit()` stub stands in for
the (not-yet-built) broker order path and records everything that reached it; every
test asserts that the distressed/oversized/stale order never arrived.

Run:  python -m pytest broker/test_killswitch_trips.py -v
  or: python broker/test_killswitch_trips.py     (standalone report)

PAPER ONLY — no broker connection exists in this file.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

# `python broker/test_killswitch_trips.py` (documented above) puts broker/ on sys.path,
# not the repo root, so `from broker...` died with ModuleNotFoundError before any test
# ran. Same defect prices_updater.py had.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from broker.killswitch import (  # noqa: E402
    MAX_DRAWDOWN_HALT, MAX_POSITION_WEIGHT, GateDecision, KillSwitchTripped, OrderGate,
    OrderIntent, PortfolioState, Switch, assert_gate_enforced,
)

SUBMITTED: list = []


def submit(decision: GateDecision) -> list:
    """Stand-in for the broker order path. Only approved intents may pass."""
    assert_gate_enforced(decision, decision.approved)
    SUBMITTED.extend(decision.approved)
    return decision.approved


@pytest.fixture(autouse=True)
def _clear():
    SUBMITTED.clear()
    yield
    SUBMITTED.clear()


def healthy_portfolio(**kw) -> PortfolioState:
    base = dict(
        equity=100_000.0,
        high_water_mark=100_000.0,
        positions={f"T{i}": {"entry_price": 10.0, "last_price": 10.5} for i in range(12)},
    )
    base.update(kw)
    return PortfolioState(**base)


def fresh_snapshot(tmp_path: Path, latest: date) -> Path:
    p = tmp_path / "price_snapshots.csv"
    p.write_text("date,ticker,close,is_carried\n"
                 f"{latest.isoformat()},AAA,10.0,0\n", encoding="utf-8")
    return p


# ── 5.1 MAX DRAWDOWN ────────────────────────────────────────────────────────
def test_trip_5_1_max_drawdown_halts_all_orders(tmp_path):
    """Simulated -16% drawdown (past the -15% halt) must stop EVERY order."""
    gate = OrderGate(audit_log=tmp_path / "audit.log")
    pf = healthy_portfolio(equity=84_000.0, high_water_mark=100_000.0)   # -16%
    assert pf.drawdown <= MAX_DRAWDOWN_HALT

    intents = [OrderIntent("AAA", "BUY", 100, 10.0, sector="Tech", target_weight=0.01)]
    with pytest.raises(KillSwitchTripped) as exc:
        gate.authorize(intents, pf)

    assert exc.value.switch is Switch.MAX_DRAWDOWN
    assert SUBMITTED == [], "ORDER PATH NOT BLOCKED — orders reached the broker stub"


def test_5_1_does_not_trip_just_above_threshold(tmp_path):
    """-14% must NOT halt: the switch must be precise, not trigger-happy."""
    gate = OrderGate(audit_log=tmp_path / "audit.log")
    pf = healthy_portfolio(equity=86_000.0, high_water_mark=100_000.0)   # -14%
    d = gate.authorize([OrderIntent("AAA", "BUY", 10, 10.0, target_weight=0.01)], pf)
    assert len(submit(d)) == 1


# ── 5.2 SINGLE-NAME CATASTROPHIC ────────────────────────────────────────────
def test_trip_5_2_catastrophic_loss_blocks_additions(tmp_path):
    """A name -60% from entry must not be added to; healthy names still trade."""
    gate = OrderGate(audit_log=tmp_path / "audit.log")
    pf = healthy_portfolio()
    pf.positions["BAD"] = {"entry_price": 100.0, "last_price": 40.0}     # -60%

    intents = [
        OrderIntent("BAD", "BUY", 50, 40.0, target_weight=0.02),
        OrderIntent("T1", "BUY", 50, 10.0, target_weight=0.02),
    ]
    d = gate.authorize(intents, pf)
    submit(d)

    blocked = {i.ticker for i, _, _ in d.blocked}
    assert "BAD" in blocked, "catastrophic-loss name was NOT blocked"
    assert all(s.ticker != "BAD" for s in SUBMITTED), "ORDER PATH NOT BLOCKED for BAD"
    assert any(s.ticker == "T1" for s in SUBMITTED), "healthy name wrongly blocked"


def test_trip_5_2_delisting_blocks_additions(tmp_path):
    gate = OrderGate(audit_log=tmp_path / "audit.log")
    pf = healthy_portfolio()
    pf.positions["DEAD"] = {"entry_price": 10.0, "last_price": 9.8, "delisted": True}
    d = gate.authorize([OrderIntent("DEAD", "BUY", 10, 9.8, target_weight=0.01)], pf)
    submit(d)
    assert [i.ticker for i, _, _ in d.blocked] == ["DEAD"]
    assert SUBMITTED == [], "ORDER PATH NOT BLOCKED for a delisted name"


def test_5_2_exit_from_distressed_name_is_allowed(tmp_path):
    """The rule blocks ADDITIONS only — it must never trap you in a position."""
    gate = OrderGate(audit_log=tmp_path / "audit.log")
    pf = healthy_portfolio()
    pf.positions["BAD"] = {"entry_price": 100.0, "last_price": 30.0, "halted": True}
    d = gate.authorize([OrderIntent("BAD", "SELL", 50, 30.0)], pf)
    assert len(submit(d)) == 1, "exit from a distressed name was wrongly blocked"


# ── 5.3 STALE DATA ──────────────────────────────────────────────────────────
def test_trip_5_3_stale_snapshot_halts_all_orders(tmp_path):
    """Snapshot 6 sessions behind must stop EVERY order."""
    snap = fresh_snapshot(tmp_path, date(2026, 9, 1))
    gate = OrderGate(snap, today=date(2026, 9, 10), audit_log=tmp_path / "audit.log")
    with pytest.raises(KillSwitchTripped) as exc:
        gate.authorize([OrderIntent("AAA", "BUY", 10, 10.0, target_weight=0.01)],
                       healthy_portfolio())
    assert exc.value.switch is Switch.STALE_DATA
    assert SUBMITTED == [], "ORDER PATH NOT BLOCKED on stale data"


def test_trip_5_3_missing_snapshot_halts(tmp_path):
    gate = OrderGate(tmp_path / "nope.csv", audit_log=tmp_path / "audit.log")
    with pytest.raises(KillSwitchTripped):
        gate.authorize([OrderIntent("AAA", "BUY", 10, 10.0)], healthy_portfolio())
    assert SUBMITTED == []


def test_5_3_fresh_snapshot_within_grace_passes(tmp_path):
    """1 session behind is within grace — must NOT halt."""
    snap = fresh_snapshot(tmp_path, date(2026, 9, 10))       # Thu
    gate = OrderGate(snap, today=date(2026, 9, 11),          # Fri
                     audit_log=tmp_path / "audit.log")
    d = gate.authorize([OrderIntent("AAA", "BUY", 10, 10.0, target_weight=0.01)],
                       healthy_portfolio())
    assert len(submit(d)) == 1


# ── 4.x HARD LIMITS ─────────────────────────────────────────────────────────
def test_trip_4_position_cap_blocks_oversized_order(tmp_path):
    """A 20% target position must be blocked by the 15% cap."""
    gate = OrderGate(audit_log=tmp_path / "audit.log")
    d = gate.authorize(
        [OrderIntent("BIG", "BUY", 2000, 10.0, sector="Tech", target_weight=0.20)],
        healthy_portfolio())
    submit(d)
    assert d.blocked and d.blocked[0][1] is Switch.HARD_LIMITS
    assert f"{MAX_POSITION_WEIGHT:.0%}" in d.blocked[0][2]
    assert SUBMITTED == [], "ORDER PATH NOT BLOCKED for an oversized position"


def test_trip_4_sector_cap_blocks_concentrated_orders(tmp_path):
    """Four 10% Energy buys = 40% sector, past the 30% cap."""
    gate = OrderGate(audit_log=tmp_path / "audit.log")
    pf = healthy_portfolio()
    intents = [OrderIntent(f"E{i}", "BUY", 1000, 10.0, sector="Energy",
                           target_weight=0.10) for i in range(4)]
    d = gate.authorize(intents, pf, book_value=100_000.0)
    submit(d)
    assert any(s is Switch.HARD_LIMITS and "sector" in r for _, s, r in d.blocked), \
        "sector concentration was NOT blocked"
    assert len(SUBMITTED) < 4, "ORDER PATH NOT BLOCKED for sector breach"


def test_trip_4_liquidity_floor_blocks_illiquid_name(tmp_path):
    """$0.9m/day ADV — the exact tail-audit case — must be blocked."""
    gate = OrderGate(audit_log=tmp_path / "audit.log")
    d = gate.authorize(
        [OrderIntent("ILLQ", "BUY", 100, 10.0, sector="Real Estate",
                     adv_usd=900_000, target_weight=0.02)],
        healthy_portfolio())
    submit(d)
    assert d.blocked and "ADV" in d.blocked[0][2]
    assert SUBMITTED == [], "ORDER PATH NOT BLOCKED for an illiquid name"


def test_trip_4_min_positions_blocks_overconcentrated_book(tmp_path):
    """A book that would hold < 10 names must not be built."""
    gate = OrderGate(audit_log=tmp_path / "audit.log")
    pf = PortfolioState(equity=100_000.0, high_water_mark=100_000.0,
                        positions={"A": {"entry_price": 10.0, "last_price": 10.0}})
    d = gate.authorize([OrderIntent("B", "BUY", 10, 10.0, target_weight=0.05)], pf)
    submit(d)
    assert d.blocked, "min-positions floor did not block"
    assert SUBMITTED == [], "ORDER PATH NOT BLOCKED below min positions"


# ── the backstop assertion itself ───────────────────────────────────────────
def test_assertion_backstop_catches_bypass_attempt(tmp_path):
    """Even if calling code ignores the decision, the assertion must stop it."""
    gate = OrderGate(audit_log=tmp_path / "audit.log")
    d = gate.authorize(
        [OrderIntent("BIG", "BUY", 2000, 10.0, target_weight=0.20)],
        healthy_portfolio())
    rogue = [i for i, _, _ in d.blocked]          # caller tries to submit blocked orders
    with pytest.raises(AssertionError, match="KILL-SWITCH VIOLATION"):
        assert_gate_enforced(d, rogue)


def test_limit_price_is_mandatory():
    """No market orders may be constructed at all."""
    with pytest.raises(ValueError, match="LIMIT price required"):
        OrderIntent("AAA", "BUY", 10, 0)


if __name__ == "__main__":
    import subprocess, sys
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v",
                              "--no-header", "-p", "no:cacheprovider"]))
