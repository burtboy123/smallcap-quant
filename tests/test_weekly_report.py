"""
tests/test_weekly_report.py

The weekly summary (monitoring/weekly_report.py) and `health_check --test-email`.
Temp files only; SMTP is faked, so nothing is sent.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from monitoring import alerting as al          # noqa: E402
from monitoring import health_check as hc      # noqa: E402
from monitoring import weekly_report as wr     # noqa: E402

BOOK_NAMES = ["SM", "NUTX", "AMN", "BVS", "TALO", "TK", "MOV", "REPX", "MYE", "SSRM",
              "CCSI", "CDE", "GPRE", "SENEA", "KODK"]


@pytest.fixture
def period(tmp_path, monkeypatch):
    d = tmp_path / "2026Q4b"
    d.mkdir()
    pd.DataFrame({"ticker": BOOK_NAMES, "weight_equal": 1 / 15,
                  "sector": ["Energy"] * 5 + ["Healthcare"] * 10}).to_csv(
        d / "incumbent_book.csv", index=False)
    (d / "meta.json").write_text(json.dumps(
        {"incumbent_baselines": {t: 10.0 for t in BOOK_NAMES}}), encoding="utf-8")
    days = pd.bdate_range("2026-09-30", periods=8)
    pd.DataFrame({
        "date": days.strftime("%Y-%m-%d"),
        "incumbent_index": [1.0, 1.01, 1.02, 1.0, 0.99, 1.03, 1.04, 1.05],
        "challenger_index": 1.0, "spy_index": [1.0 + 0.002 * i for i in range(8)],
        "incumbent_ret": [x - 1 for x in [1.0, 1.01, 1.02, 1.0, 0.99, 1.03, 1.04, 1.05]],
        "incumbent_drawdown": [0, 0, 0, -0.0196, -0.0294, 0, 0, 0],
        "challenger_ret": 0.0, "challenger_drawdown": 0.0,
        "spy_ret": [0.002 * i for i in range(8)], "spy_drawdown": 0.0,
    }).to_csv(d / "equity_curves.csv", index=False)
    snap = d / "price_snapshots.csv"
    pd.DataFrame({"date": "2026-10-09", "ticker": BOOK_NAMES, "close": 10.0,
                  "is_carried": 0}).to_csv(snap, index=False)
    recon = tmp_path / "recon"
    recon.mkdir()
    monkeypatch.setattr(wr, "EQUITY", d / "equity_curves.csv")
    monkeypatch.setattr(wr, "BOOK", d / "incumbent_book.csv")
    monkeypatch.setattr(wr, "RECON_DIR", recon)
    monkeypatch.setattr(wr, "REPORT_DIR", tmp_path / "state")
    monkeypatch.setattr(wr.ck, "SNAP", snap)
    # No network in tests: IWM is "unavailable" unless a test supplies closes.
    monkeypatch.setattr(wr, "_benchmark_closes", lambda *a: pd.Series(dtype=float))
    monkeypatch.setattr(wr, "_hedged_update", _no_network)
    return recon


def _no_network():
    raise RuntimeError("no network in tests")


def _recon(recon_dir, positions, name="recon_20261001_201839.json"):
    (recon_dir / name).write_text(json.dumps({
        "timestamp": "2026-10-01T20:18:39", "account": "DU_TEST",
        "positions_after": positions}), encoding="utf-8")


def test_forward_test_section_reports_week_and_since_start(period):
    body = "\n".join(wr._forward_test(date(2026, 10, 9)))
    assert "+5.00%" in body                         # incumbent since start
    assert "-2.94%" in body                         # max drawdown
    assert "Incumbent vs SPY since start" in body


def test_forward_test_section_adds_iwm_as_information_only(period, monkeypatch):
    days = pd.bdate_range("2026-09-30", periods=8)
    monkeypatch.setattr(wr, "_benchmark_closes", lambda *a: pd.Series(
        [200.0, 202, 204, 198, 200, 202, 206, 210], index=days))
    body = "\n".join(wr._forward_test(date(2026, 10, 9)))
    iwm = next(l for l in body.splitlines() if l.strip().startswith("IWM"))
    assert "+5.00%" in iwm and "-2.94%" in iwm      # since start, drawdown 198/204
    assert "0.00%" in body.split("vs IWM:")[1].split()[0] and "information only" in body


def test_forward_test_section_survives_a_failed_iwm_download(period, monkeypatch):
    def boom(*a):
        raise ConnectionError("offline")
    monkeypatch.setattr(wr, "_benchmark_closes", boom)
    body = "\n".join(wr._forward_test(date(2026, 10, 9)))
    assert "IWM unavailable (ConnectionError: offline)" in body
    assert "Incumbent vs SPY since start" in body


def test_attribution_sums_to_the_book_and_ranks_names(period):
    rows = []
    for day, px in (("2026-09-30", 10.0), ("2026-10-01", 10.0)):
        rows += [{"date": day, "ticker": t, "close": px} for t in BOOK_NAMES]
    last = {t: 10.0 for t in BOOK_NAMES} | {"SM": 13.0, "KODK": 7.0}
    rows += [{"date": "2026-10-09", "ticker": t, "close": c} for t, c in last.items()]
    pd.DataFrame(rows).to_csv(wr.ck.SNAP, index=False)
    body = "\n".join(wr._attribution(date(2026, 10, 9)))
    # SM +30% / 15 names = +2.00 pp (Energy); KODK -30% / 15 = -2.00 pp (Healthcare)
    assert "Energy" in body and "+2.00%" in body and "-2.00%" in body
    total = next(l for l in body.splitlines() if l.strip().startswith("total"))
    assert total.split()[-2:] == ["+0.00%", "+0.00%"]
    assert "best this week : SM +2.00%" in body
    assert "worst this week: KODK -2.00%" in body


def _trades_and_events(monkeypatch, trades, events):
    from broker import execution_store as xs
    df = pd.DataFrame(trades)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    monkeypatch.setattr(xs, "load_trades", lambda: df)
    monkeypatch.setattr(xs, "load_events", lambda: pd.DataFrame(events))


def test_cost_section_weights_by_notional_and_reconstructs_old_refs(monkeypatch):
    base = {"timestamp": "2026-10-05T17:00:00", "account": "DU_TEST", "dry_run": False}
    _trades_and_events(monkeypatch, [
        # stored ref: bought 100 @ 10.10 vs ref 10.00 -> +100 bps on 1,010
        {**base, "run_id": "new", "ticker": "AAA", "side": "BUY", "ordered": 100,
         "filled": 100, "avg_fill": 10.10, "ref_price": 10.0},
        # older run, no ref stored: PLANNED limit 20.10 with a 50 bps buffer -> ref 20.00
        # sold 100 @ 19.90 -> +50 bps on 1,990
        {**base, "timestamp": "2026-10-01T17:00:00", "run_id": "old", "ticker": "BBB",
         "side": "SELL", "ordered": 100, "filled": 100, "avg_fill": 19.90},
        {**base, "timestamp": "2026-10-01T17:00:00", "run_id": "old", "ticker": "CCC",
         "side": "BUY", "ordered": 50, "filled": 0, "avg_fill": None},
        {**base, "dry_run": True, "run_id": "dry", "ticker": "AAA", "side": "BUY",
         "ordered": 1, "filled": 1, "avg_fill": 99.0, "ref_price": 1.0},
    ], [{"run_id": "old", "event": "PLANNED", "ticker": "BBB",
         "limit": 20.0 * (1 - 50 / 1e4), "buffer_bps": 50.0}])
    body = "\n".join(wr._cost(date(2026, 10, 9)))
    assert "+100.0" in body and "+50.0" in body
    # (100 * 1010 + 50 * 1990) / 3000 = 66.8
    assert "all runs: +66.8 bps" in body and "1 order(s) never filled" in body
    assert "weigh real-time market data" in body
    assert "+9800" not in body                         # dry runs excluded


def test_cost_section_clips_fill_rate_of_old_records(monkeypatch):
    base = {"timestamp": "2026-10-05T17:00:00", "account": "DU_TEST", "dry_run": False,
            "run_id": "r", "side": "BUY", "ref_price": 10.0}
    _trades_and_events(monkeypatch, [
        {**base, "ticker": "AAA", "ordered": 50, "filled": 100, "avg_fill": 10.0},
        {**base, "ticker": "BBB", "ordered": 100, "filled": 100, "avg_fill": 10.0},
    ], [])
    body = "\n".join(wr._cost(date(2026, 10, 9)))
    assert "100%" in body and "200%" not in body and "150%" not in body
    assert "+0.0 bps" in body and "delayed data is not costing much" in body


def test_account_section_flags_missing_names_and_drift(period):
    held = [{"ticker": t, "position": 1000, "avg_cost": 10.0}
            for t in BOOK_NAMES if t not in ("NUTX", "REPX")]
    held[0]["position"] = 3000                      # SM overweight
    _recon(period, held)
    body = "\n".join(wr._account(date(2026, 10, 9)))
    assert "13/15 target names held" in body
    assert "NUTX, REPX" in body
    assert "SM " in body.split("off target")[1]
    assert "finish the book" in body


def test_stale_account_view_says_so_and_asks_for_a_fresh_look(period):
    """2026-10-01 positions read on 2026-11-16: the email said "NUTX missing, finish the
    book" for seven weeks, while a 2026-10-06 read-only had shown 15/15."""
    held = [{"ticker": t, "position": 1000, "avg_cost": 10.0}
            for t in BOOK_NAMES if t != "NUTX"]
    _recon(period, held)
    body = "\n".join(wr._account(date(2026, 11, 16)))
    assert "46d ago" in body and "may not be what the account holds" in body
    assert "--read-only" in body
    assert "finish the book" not in body


def test_newest_look_wins_over_an_older_execution(period):
    """A read-only connection's positions file, newer than the last execution, is used."""
    _recon(period, [{"ticker": t, "position": 1000, "avg_cost": 10.0}
                    for t in BOOK_NAMES if t != "NUTX"])
    from broker import ibkr_paper as bp
    bp.RECON_DIR = period                       # conftest restores it after the test
    bp.save_positions("DU_TEST", pd.DataFrame(
        [{"ticker": t, "position": 1000, "avg_cost": 10.0} for t in BOOK_NAMES]))
    path, rec = wr._latest_recon()
    assert path.name.startswith("positions_") and len(rec["positions_after"]) == 15
    body = "\n".join(wr._account(date.today()))
    assert "15/15 target names held" in body and "from a broker connection" in body
    assert "missing from book : none" in body


def test_read_only_saves_the_positions_it_saw(monkeypatch):
    import broker.ibkr_paper as bp

    class StubBroker:
        account = "DU_TEST"

        def __init__(self, **_k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def market_data_preflight(self, probe="SPY"):
            return {"delayed_quote": True, "historical": True, "probe": probe, "detail": ""}

        def account_summary(self):
            return {"NetLiquidation": 1e6, "_nav_usd": 1e6, "_base_currency": "USD"}

        def positions(self):
            return pd.DataFrame([{"ticker": "SM", "position": 10, "avg_cost": 30.0}])

    monkeypatch.setattr(bp, "IBKRPaperBroker", StubBroker)
    assert bp.main(["--read-only"]) == 0
    files = list(bp.RECON_DIR.glob("positions_*.json"))
    assert len(files) == 1
    rec = json.loads(files[0].read_text(encoding="utf-8"))
    assert rec["account"] == "DU_TEST" and rec["positions_after"][0]["ticker"] == "SM"


def _closes(prices: dict):
    pd.DataFrame({"date": "2026-10-09", "ticker": list(prices),
                  "close": list(prices.values()), "is_carried": 0}).to_csv(
        wr.ck.SNAP, index=False)


def test_price_moves_alone_are_not_drift(period):
    # Bought equal-weight at the 10.00 baselines and never rebalanced: SM doubled and
    # KODK halved, so their weights moved a lot -- exactly as the tracked book's did.
    _closes({t: 10.0 for t in BOOK_NAMES} | {"SM": 20.0, "KODK": 5.0})
    _recon(period, [{"ticker": t, "position": 1000, "avg_cost": 10.0}
                    for t in BOOK_NAMES])
    body = "\n".join(wr._account(date(2026, 10, 9)))
    assert "off target > 2%  : none" in body
    assert "buy-and-hold weights" in body


def test_wrong_size_is_still_flagged_after_price_moves(period):
    _closes({t: 10.0 for t in BOOK_NAMES} | {"SM": 20.0})
    held = [{"ticker": t, "position": 1000, "avg_cost": 10.0} for t in BOOK_NAMES]
    held[BOOK_NAMES.index("AMN")]["position"] = 400          # only 40% of its shares
    _recon(period, held)
    off = "\n".join(wr._account(date(2026, 10, 9))).split("off target")[1]
    assert "AMN" in off.splitlines()[0] and "SM" not in off.splitlines()[0]


def test_missing_names_do_not_make_the_rest_look_overweight(period):
    # 10 of 15 held: each is 10% of the account vs a 6.67% book weight. That is a
    # missing-name problem (listed), not 10 sizing errors.
    gone = BOOK_NAMES[:5]
    _recon(period, [{"ticker": t, "position": 1000, "avg_cost": 10.0}
                    for t in BOOK_NAMES if t not in gone])
    body = "\n".join(wr._account(date(2026, 10, 9)))
    assert f"missing from book : {', '.join(sorted(gone))}" in body
    assert "off target > 2%  : none" in body


def test_without_baselines_targets_fall_back_to_entry_weights(period):
    (wr.BOOK.parent / "meta.json").write_text("{}", encoding="utf-8")
    _recon(period, [{"ticker": t, "position": 1000, "avg_cost": 10.0}
                    for t in BOOK_NAMES])
    assert "entry weights (no baselines" in "\n".join(wr._account(date(2026, 10, 9)))


def test_account_section_without_any_reconciliation(period):
    assert "no reconciliation on file yet" in "\n".join(wr._account(date(2026, 10, 9)))


def test_a_broken_section_does_not_break_the_report(period, monkeypatch):
    monkeypatch.setattr(wr, "_forward_test", lambda today: 1 / 0)
    subject, body = wr.build_report([], date(2026, 10, 9))
    assert "unavailable: ZeroDivisionError" in body
    assert "4. Paper account vs target" in body and "all checks clean" in subject


def test_report_is_written_to_state_dir(period):
    subject, body = wr.build_report([], date(2026, 10, 12))
    p = wr.write_report(subject, body, date(2026, 10, 12))
    assert p.name == "weekly_report_2026-10-12.txt" and subject in p.read_text()


# ── email ───────────────────────────────────────────────────────────────────────

class _FakeSMTP:
    sent: list = []

    def __init__(self, host, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        pass

    def login(self, user, pw):
        pass

    def send_message(self, msg):
        _FakeSMTP.sent.append(msg)


@pytest.fixture
def email_on(monkeypatch):
    for k, v in {"ALERT_EMAIL_ENABLED": "true", "ALERT_SMTP_HOST": "smtp.example",
                 "ALERT_SMTP_PORT": "587", "ALERT_SMTP_USER": "me@example.com",
                 "ALERT_SMTP_PASSWORD": "x" * 16,
                 "ALERT_EMAIL_TO": "me@example.com"}.items():
        monkeypatch.setenv(k, v)
    _FakeSMTP.sent = []
    monkeypatch.setattr(al.smtplib, "SMTP", _FakeSMTP)


def test_test_email_refuses_when_disabled(monkeypatch, capsys):
    monkeypatch.setenv("ALERT_EMAIL_ENABLED", "false")
    assert hc.main(["--test-email"]) == 1
    assert "NOT SENT" in capsys.readouterr().out


def test_test_email_sends_one_message(email_on, capsys):
    assert hc.main(["--test-email"]) == 0
    assert len(_FakeSMTP.sent) == 1
    assert _FakeSMTP.sent[0]["Subject"] == "[quant-system] test email"


def test_weekly_report_is_emailed_but_not_with_no_alert(email_on, period, monkeypatch):
    monkeypatch.setattr(hc, "run", lambda **kw: ([], {"sent": [], "suppressed": [],
                                                       "resolved": [],
                                                       "email_message": ""}))
    hc.main(["--fast", "--weekly-report"])
    assert [m["Subject"].startswith("[quant-system weekly]") for m in _FakeSMTP.sent] == [True]
    hc.main(["--fast", "--no-alert", "--weekly-report"])
    assert len(_FakeSMTP.sent) == 1


def test_execution_store_records_cost_vs_reference(tmp_path, monkeypatch):
    from broker import execution_store as xs
    monkeypatch.setattr(xs, "EXEC_LOG_DIR", tmp_path)
    child = {"ticker": "AAA", "side": "BUY", "ordered": 100, "filled": 100,
             "avg_fill": 10.05, "limit": 10.08, "original_limit": 10.08,
             "status": "Filled", "reprices": 0, "ref_price": 10.0, "events": []}
    p = xs.from_engine_summary({"children": [child]}, account="DU_TEST",
                               nav_usd=1e6, dry_run=False, book=None, config={})
    t = json.loads(p.read_text())["trades"][0]
    assert t["ref_price"] == 10.0 and t["cost_vs_ref_bps"] == 50.0


# ── coming up, and the hedged paper test (section 7) ────────────────────────────

def _reading(days=7, verdict="interim (no verdict)"):
    return None, {"days": days, "last": date(2026, 10, 9), "verdict": verdict,
                  "hedged": {"total": 0.0324, "max_dd": -0.002},
                  "spy": {"total": 0.0209, "max_dd": -0.007}}


def test_the_hedged_test_is_updated_and_reported_every_week(period, monkeypatch):
    calls = []
    monkeypatch.setattr(wr, "_hedged_update", lambda: calls.append(1) or _reading())
    _subject, body = wr.build_report([], date(2026, 10, 12))
    assert calls == [1]                                    # the Monday run updates it
    sec = body.split("7. Hedged paper test")[1]
    assert "7 trading days to 2026-10-09: hedged +3.24% vs S&P 500 +2.09%" in sec
    assert "interim (no verdict); evaluation on/after 2027-09-30" in sec


def test_a_failed_hedged_update_is_one_line_not_a_broken_report(period):
    _subject, body = wr.build_report([], date(2026, 10, 12))     # fixture: no network
    sec = body.split("7. Hedged paper test")[1]
    assert "unavailable: RuntimeError: no network in tests" in sec
    assert "2. Forward test" in body and body.rstrip().endswith("health_check -v")


def test_coming_up_leads_the_report(period):
    _subject, body = wr.build_report([], date(2026, 10, 12))
    assert body.index("Coming up") < body.index("1. Pipeline health")
