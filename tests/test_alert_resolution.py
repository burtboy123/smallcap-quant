"""
tests/test_alert_resolution.py

Kill-switch and rebalance alerts are EVENTS raised by the broker path. Until
2026-10-06 nothing closed them, so a 5.3 halt from 09-30 still showed "CRITICAL,
active" on the dashboard a week later, after the switch had passed on every run.
A gate run without a halt now closes kill-switch alerts; one that also blocks
nothing closes the rebalance alerts. Health and risk alerts are never touched.

Temp state only, email off; no broker connection, nothing is placed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from broker.ibkr_paper import IBKRPaperBroker  # noqa: E402
from broker.killswitch import (  # noqa: E402
    GateDecision, KillSwitchTripped, OrderIntent, PortfolioState, Switch,
)
from monitoring import alerting as al  # noqa: E402


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(al, "ALERT_LOG", tmp_path / "alert_log.jsonl")
    monkeypatch.setattr(al, "ALERT_STATE", tmp_path / "alert_state.json")
    monkeypatch.setenv("ALERT_EMAIL_ENABLED", "false")
    al.save_alert_state({
        "killswitch:5.3_STALE_DATA": {"since": "2026-09-30T14:38:00", "occurrences": 1,
                                      "summary": "5.3 halted", "severity": "CRITICAL"},
        "rebalance:blocked": {"since": "2026-10-01T17:18:00", "occurrences": 4,
                              "summary": "9 of 15 blocked", "severity": "CRITICAL"},
        "health:snapshot_fresh": {"since": "2026-10-05T15:30:00", "occurrences": 1,
                                  "summary": "stale", "severity": "WARNING"},
        "risk:name:KODK": {"since": "2026-10-05T23:40:00", "occurrences": 1,
                           "summary": "down", "severity": "WARNING"},
    })
    return al


def test_clean_gate_clears_killswitch_and_rebalance_only(state):
    cleared = state.resolve_after_clean_gate(blocked=False)
    assert sorted(cleared) == ["killswitch:5.3_STALE_DATA", "rebalance:blocked"]
    assert set(state.load_alert_state()) == {"health:snapshot_fresh", "risk:name:KODK"}


def test_gate_that_blocked_keeps_the_rebalance_alert(state):
    assert state.resolve_after_clean_gate(blocked=True) == ["killswitch:5.3_STALE_DATA"]
    assert "rebalance:blocked" in state.load_alert_state()


def test_resolution_never_raises(state, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(state, "dispatch", boom)
    assert state.resolve_after_clean_gate(blocked=False) == []


# ── wired into the broker's one submission path ─────────────────────────────────

class _Gate:
    def __init__(self, decision=None, halt=None):
        self.decision, self.halt = decision, halt

    def authorize(self, intents, pf, book_value=None):
        if self.halt:
            raise self.halt
        return self.decision


PF = PortfolioState(equity=1e6, high_water_mark=1e6, positions={})
BUY = OrderIntent(ticker="AAA", action="BUY", quantity=10, limit_price=10.0,
                  sector="X", adv_usd=50e6, target_weight=0.01)


def test_broker_clears_stale_alerts_after_a_clean_dry_run(state):
    br = IBKRPaperBroker(gate=_Gate(GateDecision(approved=[BUY])))
    br.submit_orders([BUY], PF, execute=False)
    assert set(state.load_alert_state()) == {"health:snapshot_fresh", "risk:name:KODK"}


def test_broker_zero_intent_gate_run_also_clears(state):
    br = IBKRPaperBroker(gate=_Gate(GateDecision()))
    br.submit_orders([], PF, execute=False)
    assert "killswitch:5.3_STALE_DATA" not in state.load_alert_state()


def test_broker_keeps_alerts_when_the_gate_halts(state):
    halt = KillSwitchTripped(Switch.STALE_DATA, "snapshot 3 sessions old")
    br = IBKRPaperBroker(gate=_Gate(halt=halt))
    result = br.submit_orders([BUY], PF, execute=False)
    assert result["halted"]
    st = state.load_alert_state()
    assert "killswitch:5.3_STALE_DATA" in st and "rebalance:blocked" in st


def test_broker_keeps_rebalance_alert_when_orders_are_blocked(state):
    dec = GateDecision(approved=[], blocked=[(BUY, Switch.HARD_LIMITS, "ADV too low")])
    IBKRPaperBroker(gate=_Gate(dec)).submit_orders([BUY], PF, execute=False)
    st = state.load_alert_state()
    assert "killswitch:5.3_STALE_DATA" not in st
    # Still the SAME ongoing alert: not resolved and re-raised as new (which would
    # send a false RESOLVED email and reset its start date).
    assert st["rebalance:blocked"]["since"] == "2026-10-01T17:18:00"
    assert st["rebalance:blocked"]["occurrences"] == 5
