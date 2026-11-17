"""
monitoring/weekly_report.py — one-page weekly summary, sent after the Monday health run.

The health check moved to weekly (Mondays 15:30), so nobody is reading logs daily any
more. This report is what you read instead: one message a week that answers

  1. Is the pipeline healthy?            (this run's checks)
  2. How is the forward test doing?      (Q4b vs SPY and IWM, week and since start)
  3. Where did the return come from?     (Incumbent by sector; best/worst names)
  4. Does the paper account match?       (last reconciled positions vs the
                                          buy-and-hold weights the test tracks)
  5. Did anything trade this week?       (execution runs, fill rate)
  6. What does trading cost?             (fill price vs the price the order was sized
                                          off, per run and cumulative -- the input to
                                          the real-time market-data decision)
  7. How is the hedged paper test doing? (updated here every Monday: downloads IWM
                                          closes, so no button or command is needed)

Above all of them, "Coming up": the next dated action (a forward-test evaluation, the
hedged test's verdict), marked DUE once its date passes (monitoring/upcoming.py).

It needs no IB Gateway: positions come from the last reconciliation file the broker
wrote, valued at the latest snapshot closes. Every section degrades to a one-line
"unavailable: <why>" instead of failing the report -- a summary that crashes on a
missing file is the silent failure this monitoring exists to prevent.

    python -m monitoring.health_check --fast --weekly-report     # build + email it
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from monitoring import checks as ck

ROOT = Path(__file__).parent.parent
PERIOD_DIR = ck.SNAP.parent
EQUITY = PERIOD_DIR / "equity_curves.csv"
BOOK = PERIOD_DIR / "incumbent_book.csv"
RECON_DIR = ROOT / "broker" / "reconciliation"
REPORT_DIR = ROOT / "monitoring" / "state"
DRIFT_FLAG = 0.02            # flag a name more than 2 pp away from its target weight
POSITIONS_STALE_DAYS = 10    # older than this, the account view may not be the account
SMALL_CAP_BENCH = "IWM"      # information only: EVAL_CRITERIA judges against SPY
COST_FLAG_BPS = 10.0         # QUARTER_END_PLAN §6: above ~10-15 bps, real-time data pays


def _pct(x) -> str:
    return "n/a" if x is None or pd.isna(x) else f"{x:+.2%}"


def _section(title: str, build) -> list[str]:
    try:
        body = build()
    except Exception as e:                                          # noqa: BLE001
        body = [f"  unavailable: {type(e).__name__}: {e}"]
    return [title, "-" * len(title), *body, ""]


# ── 1. health ───────────────────────────────────────────────────────────────────

def _health(results) -> list[str]:
    if not results:
        return ["  no checks were run with this report"]
    failed = [r for r in results if r.failed]
    n = len(results)
    out = [f"  {n - len(failed)}/{n} checks clean"
           + ("" if failed else " -- all good")]
    for r in failed:
        out.append(f"  FAIL [{r.severity}] {r.name}: {r.summary}")
        if r.runbook:
            out.append(f"       fix: {r.runbook}")
    # WARN never emails on its own; this weekly line is where it reaches you.
    for r in (r for r in results if r.status == "WARN"):
        out.append(f"  to do (not urgent) {r.name}: {r.summary}")
    return out


# ── 2. forward test ─────────────────────────────────────────────────────────────

def _forward_test(today: date) -> list[str]:
    if not EQUITY.exists():
        return [f"  no equity curve yet ({EQUITY.relative_to(ROOT)})"]
    eq = pd.read_csv(EQUITY, parse_dates=["date"]).sort_values("date")
    if eq.empty:
        return ["  equity curve is empty"]
    last = eq.iloc[-1]
    week_ago = pd.Timestamp(last["date"]) - timedelta(days=7)
    base = eq[eq["date"] <= week_ago]
    base = base.iloc[-1] if len(base) else eq.iloc[0]

    def wk(col):
        return last[f"{col}_index"] / base[f"{col}_index"] - 1.0

    out = [f"  {ck.PERIOD}, as of {last['date']:%Y-%m-%d} "
           f"({len(eq)} sessions since {eq.iloc[0]['date']:%Y-%m-%d})",
           f"  {'':<12}{'week':>9}{'since start':>13}{'max drawdown':>14}"]
    for col, label in (("incumbent", "Incumbent"), ("challenger", "Challenger"),
                       ("spy", "SPY")):
        if f"{col}_index" not in eq:
            continue
        out.append(f"  {label:<12}{_pct(wk(col)):>9}{_pct(last[f'{col}_ret']):>13}"
                   f"{_pct(eq[f'{col}_drawdown'].min()):>14}")
    spread = last["incumbent_ret"] - last["spy_ret"]
    try:
        bench = _bench_on_curve(eq["date"])
    except Exception as e:                                          # noqa: BLE001
        bench, why = None, f"{type(e).__name__}: {e}"
    else:
        why = "no data"
    if bench is not None and bench.notna().iloc[0] and bench.notna().iloc[-1]:
        idx = bench.ffill() / bench.iloc[0]
        b_wk = idx[eq["date"] <= week_ago]
        b_wk = b_wk.iloc[-1] if len(b_wk) else idx.iloc[0]
        dd = (idx / idx.cummax() - 1).min()
        out.append(f"  {SMALL_CAP_BENCH + ' *':<12}{_pct(idx.iloc[-1] / b_wk - 1):>9}"
                   f"{_pct(idx.iloc[-1] - 1):>13}{_pct(dd):>14}")
        out.append(f"  Incumbent vs SPY since start: {_pct(spread)};  "
                   f"vs {SMALL_CAP_BENCH}: {_pct(last['incumbent_ret'] - (idx.iloc[-1] - 1))}")
        out.append(f"  * {SMALL_CAP_BENCH} (small caps) is information only; the "
                   f"evaluation criteria judge against SPY")
    else:
        out.append(f"  Incumbent vs SPY since start: {_pct(spread)}")
        out.append(f"  {SMALL_CAP_BENCH} unavailable ({why})")
    stale = ck._sessions_between(pd.Timestamp(last["date"]).date(),
                                 ck.most_recent_trading_day(today))
    if stale > 1:
        out.append(f"  WARNING: curve is {stale} sessions behind -- see snapshot checks")
    return out


def _benchmark_closes(symbol: str, start: date, end: date) -> pd.Series:
    """Raw daily closes (auto_adjust=False, like the snapshot's SPY). Separate so
    tests can replace it without touching the network."""
    import yfinance as yf
    df = yf.download(symbol, start=start, end=end + timedelta(days=1),
                     auto_adjust=False, progress=False)["Close"]
    if isinstance(df, pd.DataFrame):
        df = df.iloc[:, 0]
    df.index = pd.to_datetime(df.index).normalize()
    return df.dropna()


def _bench_on_curve(dates: pd.Series) -> pd.Series | None:
    d = pd.to_datetime(dates).dt.normalize()
    closes = _benchmark_closes(SMALL_CAP_BENCH, d.iloc[0].date(), d.iloc[-1].date())
    if closes is None or closes.empty:
        return None
    return pd.Series(closes.reindex(d).to_numpy(), index=dates.index)


# ── 3. attribution ──────────────────────────────────────────────────────────────

def _attribution(today: date) -> list[str]:
    """Each name's contribution to the equal-weight, buy-and-hold Incumbent book.

    The book's index is mean(close / baseline), so a name contributes
    (close_then - close_before) / baseline / n. Contributions sum to the book's move.
    """
    from forward_test.baselines import current_baselines
    base = current_baselines(BOOK.parent)["incumbent"]      # split-restated
    if not base:
        return ["  no Incumbent baselines in meta.json yet"]
    snap = pd.read_csv(ck.SNAP, parse_dates=["date"])
    wide = (snap[snap["ticker"].isin(base)]
            .pivot_table(index="date", columns="ticker", values="close")
            .sort_index().ffill())
    if wide.empty:
        return ["  no snapshot closes for the book yet"]
    last_day = wide.index[-1]
    prior = wide[wide.index <= last_day - timedelta(days=7)]
    before = prior.iloc[-1] if len(prior) else pd.Series(base)
    n = len(base)
    b = pd.Series(base)
    since = (wide.iloc[-1].reindex(b.index) / b - 1) / n
    week = (wide.iloc[-1].reindex(b.index) - before.reindex(b.index)) / b / n
    book = pd.read_csv(BOOK)
    sector = dict(zip(book["ticker"], book["sector"])) if "sector" in book else {}
    df = pd.DataFrame({"week": week, "since": since})
    df["sector"] = df.index.map(lambda t: sector.get(t, "Unknown"))

    out = [f"  Incumbent, as of {last_day:%Y-%m-%d}; percentage points of book return",
           f"  {'sector':<24}{'names':>6}{'week':>9}{'since start':>13}"]
    by = df.groupby("sector").agg(names=("week", "size"), week=("week", "sum"),
                                  since=("since", "sum")).sort_values("since")
    for s, r in by.iloc[::-1].iterrows():
        out.append(f"  {s:<24}{int(r['names']):>6}{_pct(r['week']):>9}{_pct(r['since']):>13}")
    out.append(f"  {'total':<24}{n:>6}{_pct(df['week'].sum()):>9}{_pct(df['since'].sum()):>13}")
    wk = df["week"].dropna().sort_values()
    if len(wk):
        best = ", ".join(f"{t} {_pct(v)}" for t, v in wk.iloc[::-1].head(3).items())
        worst = ", ".join(f"{t} {_pct(v)}" for t, v in wk.head(3).items())
        out.append(f"  best this week : {best}")
        out.append(f"  worst this week: {worst}")
    missing = sorted(set(df.index[df["since"].isna()]))
    if missing:
        out.append(f"  no close yet for: {', '.join(missing)}")
    return out


# ── 4. paper account vs target ──────────────────────────────────────────────────

def _latest_recon() -> tuple[Path, dict] | None:
    """The newest look at the paper account: an execution's recon_*.json or any broker
    connection's positions_*.json (--read-only included). Both carry timestamp, account
    and positions_after. Reading executions only kept a 2026-10-01 view for 7 weeks."""
    if not RECON_DIR.exists():
        return None
    files = list(RECON_DIR.glob("recon_*.json")) + list(RECON_DIR.glob("positions_*.json"))
    if not files:
        return None
    newest = max(files, key=lambda p: p.stem.split("_", 1)[1])     # YYYYmmdd_HHMMSS
    rec = json.loads(newest.read_text(encoding="utf-8"))
    # in today's shares: a view older than a recorded split holds pre-split quantities
    from forward_test.baselines import positions_in_current_shares
    rec["positions_after"] = positions_in_current_shares(
        rec.get("positions_after") or [], ck.SNAP.parent, rec.get("timestamp"))
    return newest, rec


def _age_days(rec: dict, today: date) -> int | None:
    try:
        return (today - datetime.fromisoformat(str(rec.get("timestamp"))).date()).days
    except (TypeError, ValueError):
        return None


def _buy_and_hold_targets(book: pd.DataFrame, closes: pd.Series) -> tuple[pd.Series, str]:
    """Where each name's weight SHOULD be today: the forward test is buy-and-hold, so
    its weights move with prices (broker/targets.py, the same rule the broker sizes
    top-ups with). Comparing the account with the flat entry weight instead would call
    ordinary price moves "drift" and invite a rebalance away from the tracked test.
    """
    from broker.targets import baselines_for, buy_and_hold_weights
    w, note = buy_and_hold_weights(book, baselines_for(BOOK), closes)
    return w / w.sum(), note


def _account(today: date) -> list[str]:
    got = _latest_recon()
    if not got:
        return ["  no reconciliation on file yet (saved on every broker connection, --read-only included)"]
    path, rec = got
    pos = pd.DataFrame(rec.get("positions_after", []))
    book = pd.read_csv(BOOK)
    when = rec.get("timestamp", path.stem)
    age = _age_days(rec, today)
    stale = age is None or age > POSITIONS_STALE_DAYS
    seen = "an execution" if path.name.startswith("recon_") else "a broker connection"
    out = [f"  account {rec.get('account', '?')}, positions as of {when} "
           f"({'age unknown' if age is None else f'{age}d ago'}, from {seen})"]
    if stale:
        out.append(f"  ! older than {POSITIONS_STALE_DAYS} days: this may not be what the "
                   f"account holds now")
    refresh = ("  -> refresh the view: python -m broker.ibkr_paper --read-only "
               "(IB Gateway logged in)")
    if pos.empty:
        return out + ["  no positions held"] + ([refresh] if stale else [])

    snap = pd.read_csv(ck.SNAP)
    closes = (snap.sort_values("date").groupby("ticker")["close"].last()
              if not snap.empty else pd.Series(dtype=float))
    target, basis = _buy_and_hold_targets(book, closes)
    pos["price"] = pos["ticker"].map(closes).fillna(pos.get("avg_cost"))
    pos["value"] = pos["position"] * pos["price"]
    total = pos["value"].sum()
    held = set(pos["ticker"])
    in_book = sorted(held & set(target.index))

    missing = sorted(set(target.index) - held)
    extra = sorted(held - set(target.index))
    # Compare sizing among the names actually held, both sides renormalised over them,
    # so a missing name (listed separately) does not show up as every other name
    # being overweight.
    drift = []
    v = pos.set_index("ticker")["value"].reindex(in_book)
    if in_book and v.sum():
        acct_w = v / v.sum()
        tgt_w = target.reindex(in_book) / target.reindex(in_book).sum()
        for t in in_book:
            if abs(acct_w[t] - tgt_w[t]) > DRIFT_FLAG:
                drift.append(f"{t} {acct_w[t]:.1%} vs {tgt_w[t]:.1%}")
    out.append(f"  {len(in_book)}/{len(target)} target names held; "
               f"invested value {total:,.0f} (at latest snapshot closes)")
    out.append(f"  missing from book : {', '.join(missing) if missing else 'none'}")
    out.append(f"  not in the book   : {', '.join(extra) if extra else 'none'}")
    out.append(f"  off target > {DRIFT_FLAG:.0%}  : {'; '.join(drift) if drift else 'none'}")
    out.append(f"  (target = {basis}; price moves alone are not drift)")
    if stale:
        out.append(refresh)                   # act on a fresh view, not on this one
    elif missing:
        out.append("  -> finish the book: dry run, then --paper-execute during US hours")
    return out


# ── 5. executions ───────────────────────────────────────────────────────────────

def _executions(today: date) -> list[str]:
    from broker import execution_store as xs
    runs = xs.run_summary()
    if runs is None or runs.empty:
        return ["  no execution runs on record"]
    since = pd.Timestamp(today - timedelta(days=7))
    wk = runs[(runs["timestamp"] >= since) & (~runs["dry_run"].astype(bool))]
    if wk.empty:
        last = runs.iloc[0]
        return [f"  none this week (last run {pd.Timestamp(last['timestamp']):%Y-%m-%d}, "
                f"{'dry run' if last['dry_run'] else 'executed'})"]
    out = []
    for r in wk.itertuples():
        out.append(f"  {pd.Timestamp(r.timestamp):%Y-%m-%d %H:%M}  {r.account}  "
                   f"{int(r.orders)} orders, {r.share_fill_rate:.0%} of shares filled, "
                   f"{r.filled_notional:,.0f} notional, {int(r.unfilled)} unfilled")
    return out


# ── 6. execution cost vs reference ──────────────────────────────────────────────

def _with_ref(trades: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """Fill in ref_price for runs recorded before it was stored.

    The engine sets limit = ref * (1 + sign * buffer_bps / 1e4) and logs both in the
    PLANNED event, so ref is recoverable exactly (to the limit's cent rounding).
    """
    t = trades.copy()
    if "ref_price" not in t:
        t["ref_price"] = float("nan")
    if events is not None and not events.empty and "event" in events:
        pl = events[events["event"] == "PLANNED"].dropna(subset=["limit", "buffer_bps"])
        if not pl.empty:
            side = t.drop_duplicates(["run_id", "ticker"]).set_index(
                ["run_id", "ticker"])["side"]
            pl = pl.join(side, on=["run_id", "ticker"])
            sign = pl["side"].map({"BUY": 1, "SELL": -1})
            pl = pl.assign(ref=pl["limit"] / (1 + sign * pl["buffer_bps"] / 1e4))
            rec = pl.groupby(["run_id", "ticker"])["ref"].mean()
            t = t.join(rec, on=["run_id", "ticker"])
            t["ref_price"] = t["ref_price"].fillna(t["ref"])
            t = t.drop(columns="ref")
    return t


def _cost(today: date) -> list[str]:
    from broker import execution_store as xs
    tr = xs.load_trades()
    if tr is None or tr.empty:
        return ["  no execution runs on record"]
    tr = tr[~tr["dry_run"].astype(bool)]
    if tr.empty:
        return ["  no executed (non-dry) runs yet"]
    tr = _with_ref(tr, xs.load_events())
    filled = tr[(tr["filled"] > 0) & tr["avg_fill"].notna() & tr["ref_price"].notna()].copy()
    sign = filled["side"].map({"BUY": 1, "SELL": -1})
    filled["bps"] = sign * (filled["avg_fill"] - filled["ref_price"]) / filled["ref_price"] * 1e4
    filled["notional"] = filled["filled"] * filled["avg_fill"]
    # Older run records stored a slice's working qty as "ordered", so a fill can read
    # above 100%. Clip per trade; the count of unfilled names is unaffected.
    tr = tr.assign(fill=(tr["filled"] / tr["ordered"].where(tr["ordered"] > 0)).clip(upper=1))

    out = ["  positive bps = paid above (bought) / sold below the price the order was "
           "sized off",
           f"  {'run':<18}{'orders':>7}{'filled':>8}{'unfilled':>9}{'cost bps':>10}"
           f"{'notional':>13}"]
    for (rid, ts), g in tr.groupby(["run_id", "timestamp"], sort=True):
        f = filled[filled["run_id"] == rid]
        bps = ((f["bps"] * f["notional"]).sum() / f["notional"].sum()
               if f["notional"].sum() else None)
        out.append(f"  {pd.Timestamp(ts):%Y-%m-%d %H:%M}  {len(g):>7}"
                   f"{g['fill'].mean():>8.0%}{int((g['filled'] == 0).sum()):>9}"
                   f"{('n/a' if bps is None else f'{bps:+.1f}'):>10}"
                   f"{f['notional'].sum():>13,.0f}")
    total = filled["notional"].sum()
    if not total:
        return out + ["  no fills with a known reference price yet"]
    cum = (filled["bps"] * filled["notional"]).sum() / total
    unf = int((tr["filled"] == 0).sum())
    out.append(f"  all runs: {cum:+.1f} bps notional-weighted over {total:,.0f} filled, "
               f"{len(filled)} fills, {unf} order(s) never filled")
    worst = filled.sort_values("bps").iloc[::-1].head(3)
    out.append("  costliest fills: " + ", ".join(
        f"{r.ticker} {r.side} {r.bps:+.0f}" for r in worst.itertuples()))
    if cum > COST_FLAG_BPS or unf:
        out.append(f"  -> cost above {COST_FLAG_BPS:.0f} bps or unfilled orders: weigh "
                   f"real-time market data (QUARTER_END_PLAN §6)")
    else:
        out.append(f"  -> within {COST_FLAG_BPS:.0f} bps and everything filled: delayed "
                   f"data is not costing much so far")
    return out


# ── 7. hedged paper test ────────────────────────────────────────────────────────

def _hedged_update():
    """Recalculate the hedged paper test (forward_test/hedged_paper.py): downloads IWM
    closes it does not have yet and rewrites its CSV, which the dashboard shows."""
    from forward_test import hedged_paper as hp
    return hp.update(ck.PERIOD)


def _hedged(today: date) -> list[str]:
    from forward_test import hedged_paper as hp
    _h, r = _hedged_update()
    if r["days"] < 1:
        return [f"  nothing to compare yet: only the start day ({r['last']})"]
    return [f"  {r['days']} trading days to {r['last']}: hedged {_pct(r['hedged']['total'])}"
            f" vs S&P 500 {_pct(r['spy']['total'])}",
            f"  max drawdown {_pct(r['hedged']['max_dd'])} vs {_pct(r['spy']['max_dd'])}",
            f"  {r['verdict']}; evaluation on/after {hp.EVAL_DATE} "
            "(docs/research/hedged_paper_PREREG.md)"]


def _upcoming(today: date) -> list[str]:
    from monitoring import upcoming
    return upcoming.lines(today)


# ── assemble ────────────────────────────────────────────────────────────────────

def build_report(results=None, today: date | None = None) -> tuple[str, str]:
    """Return (subject, plain-text body)."""
    today = today or date.today()
    failed = [r for r in (results or []) if r.failed]
    subject = (f"[quant-system weekly] {today:%Y-%m-%d} -- "
               + (f"{len(failed)} check(s) FAILING" if failed else "all checks clean"))
    lines = [f"Quant system -- weekly summary, {today:%A %Y-%m-%d}",
             f"generated {datetime.now():%Y-%m-%d %H:%M} (Europe/Helsinki local)",
             "=" * 66, ""]
    lines += _section("Coming up", lambda: _upcoming(today))
    lines += _section("1. Pipeline health", lambda: _health(results))
    lines += _section("2. Forward test", lambda: _forward_test(today))
    lines += _section("3. Return attribution", lambda: _attribution(today))
    lines += _section("4. Paper account vs target", lambda: _account(today))
    lines += _section("5. Executions in the last 7 days", lambda: _executions(today))
    lines += _section("6. Execution cost vs reference price", lambda: _cost(today))
    lines += _section("7. Hedged paper test (S&P 500 + picks - IWM)", lambda: _hedged(today))
    lines += ["-" * 66,
              "PAPER ONLY -- this system is not authorised for real money.",
              "Full detail: python -m monitoring.health_check -v"]
    return subject, "\n".join(lines)


def write_report(subject: str, body: str, today: date | None = None) -> Path:
    today = today or date.today()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    p = REPORT_DIR / f"weekly_report_{today:%Y-%m-%d}.txt"
    p.write_text(f"{subject}\n\n{body}\n", encoding="utf-8")
    return p
