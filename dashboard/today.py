"""
dashboard/today.py — the dashboard's home page: everything that matters, at a glance.

Answers four questions in plain words, from local files only (works with IB Gateway
closed and no internet): is the system running, how is the portfolio doing against
the market, how is each holding doing, and what is coming up.

The data functions are pure (paths in, values out) so they are tested without
Streamlit; render() draws them.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "monitoring" / "state"


# ── data ────────────────────────────────────────────────────────────────────────

def health(state: Path = STATE, now: datetime | None = None) -> dict:
    """Last health-check run: checks passing, failures, and how long ago."""
    p = state / "health_history.json"
    try:
        hist = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    except ValueError:
        hist = []
    if not hist:
        return {"ran": False}
    last = hist[-1]
    statuses = last.get("statuses", {}) or {}
    failed = last.get("failed", []) or []
    try:
        age_h = ((now or datetime.now()) - datetime.fromisoformat(last["ts"])).total_seconds() / 3600
    except (KeyError, ValueError):
        age_h = None
    return {"ran": True, "n": len(statuses), "failed": failed, "age_h": age_h,
            "ts": last.get("ts", "")}


def alerts(state: Path = STATE) -> list[dict]:
    p = state / "alert_state.json"
    try:
        st = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        st = {}
    return [{"key": k, **v} for k, v in st.items()]


def nav(state: Path = STATE) -> pd.DataFrame:
    """Paper-account NAV history (base currency) for the most recent account."""
    p = state / "nav_history.jsonl"
    rows = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"])
    acct = df.sort_values("ts")["account"].iloc[-1]
    return df[df["account"] == acct].sort_values("ts").reset_index(drop=True)


def curve(ft_dir: Path) -> pd.DataFrame:
    p = ft_dir / "equity_curves.csv"
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p, parse_dates=["date"]).sort_values("date")
    return df


def holding_returns(ft_dir: Path) -> pd.Series:
    """Each held name's return since its baseline (first close), latest close."""
    from forward_test.baselines import current_baselines
    try:
        base = current_baselines(ft_dir)["incumbent"]      # split-restated
        snap = pd.read_csv(ft_dir / "price_snapshots.csv")
    except (OSError, ValueError):
        return pd.Series(dtype=float)
    if not base or snap.empty:
        return pd.Series(dtype=float)
    last = snap.sort_values("date").groupby("ticker")["close"].last()
    return (last.reindex(list(base)) / pd.Series(base) - 1).dropna().sort_values()


def freshness(ft_dir: Path, today: date | None = None) -> dict:
    """Latest recorded session and how many trading sessions behind it is."""
    from forward_test.check_snapshot_fresh import is_trading_day, most_recent_trading_day
    c = curve(ft_dir)
    if c.empty:
        return {"latest": None, "behind": None}
    latest = c["date"].max().date()
    d, behind = most_recent_trading_day(today or date.today()), 0
    while d > latest:
        if is_trading_day(d):
            behind += 1
        d -= timedelta(days=1)
    return {"latest": latest, "behind": behind}


def next_monday(today: date) -> date:
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


def summary(h: dict, fresh: dict, al: list[dict], dd: float | None) -> tuple[str, list[str]]:
    """(state, plain-language issues). state is good / warning / critical."""
    issues, crit = [], False
    if not h.get("ran"):
        issues.append("The health check has not run yet")
    elif h["failed"]:
        issues.append(f"{len(h['failed'])} health check(s) failing")
    if h.get("age_h") is not None and h["age_h"] > 8 * 24:
        issues.append(f"The weekly health check last ran {h['age_h'] / 24:.0f} days ago")
    if fresh.get("behind") is not None and fresh["behind"] >= 2:
        issues.append(f"Price data is {fresh['behind']} trading days behind")
        crit = True
    n_crit = sum(a.get("severity") == "CRITICAL" for a in al)
    if n_crit:
        issues.append(f"{n_crit} critical alert(s) open")
        crit = True
    if dd is not None and dd <= -0.08:
        issues.append(f"The paper account is {dd:.1%} below its peak")
        crit = crit or dd <= -0.12
    return ("good" if not issues else "critical" if crit else "warning"), issues


# ── page ────────────────────────────────────────────────────────────────────────

def coming_up(today: date, items=None) -> list[tuple[str, str | None, str | None]]:
    """(text, state, word) notes: every DUE action, then the next open one
    (monitoring/upcoming.py reads the dates from the system's own files)."""
    from monitoring import upcoming
    items = upcoming.actions() if items is None else items
    open_ = [a for a in items if not a.done()]
    out = [(f"**{a.what}**, due since {a.when:%d %b %Y}. Run `{a.command}`, then {a.then}.",
            "warning", "Due") for a in open_ if a.due(today)]
    out += [(f"Next: **{a.what}** on or after {a.when:%d %b %Y} "
             f"(in {(a.when - today).days} days).", None, None)
            for a in [a for a in open_ if not a.due(today)][:1]]
    return out


def render(st, go, ft_dir: Path, period: str, state: Path = STATE) -> None:
    from dashboard import ui
    today = date.today()
    st.markdown(ui.css(), unsafe_allow_html=True)
    st.markdown(ui.header_html(
        "Today", f"{today:%A %d %B %Y} · paper trading only · forward test {period}"),
        unsafe_allow_html=True)

    h, al, nv, fresh = health(state), alerts(state), nav(state), freshness(ft_dir, today)
    dd = None
    if len(nv):
        peak = nv["nav"].cummax().iloc[-1]
        dd = nv["nav"].iloc[-1] / peak - 1 if peak else None
    state_, issues = summary(h, fresh, al, dd)
    headline = ("Everything is running normally." if state_ == "good" else
                "Needs a look: " + "; ".join(issues) + ".")
    word = {"good": "All good", "warning": "Check", "critical": "Attention"}[state_]
    st.markdown(ui.headline_html(state_, word, headline), unsafe_allow_html=True)
    try:
        notes = coming_up(today)
    except Exception as e:                                          # noqa: BLE001
        notes = [(f"Coming up: unavailable ({type(e).__name__}: {e})", "warning", "Check")]
    for text, st_, w in notes:
        st.markdown(ui.note_html(text, st_, w), unsafe_allow_html=True)

    # status cards
    cards = []
    if h.get("ran"):
        ok = len(h["failed"]) == 0
        cards.append(ui.status_card_html(
            "System health", "good" if ok else "critical",
            f"{h['n'] - len(h['failed'])} of {h['n']} checks pass",
            (f"Last run {h['age_h'] / 24:.0f} days ago" if h["age_h"] is not None else "")
            + ("" if ok else " · failing: " + ", ".join(h["failed"][:3]))))
    else:
        cards.append(ui.status_card_html("System health", "warning", "Not run yet",
                                         "Runs every Monday 15:30"))
    if fresh["latest"]:
        b = fresh["behind"]
        cards.append(ui.status_card_html(
            "Price data", "good" if b <= 1 else "critical",
            f"{fresh['latest']:%d %b %Y}",
            "Up to date" if b == 0 else f"{b} trading day(s) behind"
            + (" (normal before tonight's update)" if b == 1 else "")))
    if len(nv):
        last = nv.iloc[-1]
        cards.append(ui.status_card_html(
            "Paper account", "good" if (dd or 0) > -0.08 else "warning",
            f"{last['nav']:,.0f} {last.get('currency', '')}",
            f"{dd:+.1%} from its peak · {last['ts']:%d %b}" if dd is not None else ""))
    n_al = len(al)
    cards.append(ui.status_card_html(
        "Open alerts", "good" if n_al == 0 else
        ("critical" if any(a.get("severity") == "CRITICAL" for a in al) else "warning"),
        "None" if n_al == 0 else f"{n_al} open",
        "" if n_al == 0 else ", ".join(a["key"] for a in al[:3])))
    st.markdown(ui.cards_html(cards), unsafe_allow_html=True)

    # portfolio vs the market
    c = curve(ft_dir)
    st.markdown(ui.section_html("Portfolio vs the market"), unsafe_allow_html=True)
    if c.empty:
        st.markdown(ui.note_html("No forward-test history recorded yet."),
                    unsafe_allow_html=True)
    else:
        last = c.iloc[-1]
        inc, spy = last["incumbent_ret"], last["spy_ret"]
        st.markdown(ui.kv_html([
            ("Your portfolio", f"{inc:+.2%}"),
            ("S&P 500 (SPY)", f"{spy:+.2%}"),
            ("Difference", f"{inc - spy:+.2%}"),
            ("Deepest dip", f"{c['incumbent_drawdown'].min():.2%}"),
            ("Trading days", f"{len(c)}")]), unsafe_allow_html=True)
        fig = ui.line_fig(go, c["date"], [
            ("Your portfolio", list((c["incumbent_index"] - 1) * 100)),
            ("S&P 500", list((c["spy_index"] - 1) * 100))], height=300,
            colors=[ui.PORTFOLIO, ui.SP500])
        st.plotly_chart(fig, width="stretch", config=ui.CHART_CONFIG)
        with st.expander("Show as a table"):
            st.dataframe(pd.DataFrame({
                "date": c["date"].dt.date,
                "your portfolio": ((c["incumbent_index"] - 1) * 100).round(2).astype(str) + "%",
                "S&P 500": ((c["spy_index"] - 1) * 100).round(2).astype(str) + "%"}),
                width="stretch", hide_index=True)
        st.caption(f"Cumulative return since {c['date'].iloc[0]:%d %b %Y}, both started "
                   f"at 0%. The portfolio is equal-weighted at the start and then left "
                   f"alone, exactly as the forward test records it.")

    # holdings
    hr = holding_returns(ft_dir)
    if len(hr):
        st.markdown(ui.section_html(f"Your {len(hr)} holdings since the start"),
                    unsafe_allow_html=True)
        hf = ui.hbar_fig(go, list(hr.index), list(hr.values), pct=True,
                         colors=[ui.GAIN if v >= 0 else ui.LOSS for v in hr.values],
                         text=[f"{v:+.1%}" for v in hr.values],
                         hover="%{y}: %{x:+.1%}<extra></extra>")
        st.plotly_chart(hf, width="stretch", config=ui.CHART_CONFIG)
        st.caption("Blue = up, red = down, each labelled with its return (+ or −), so the "
                   "colour is never the only cue. Price change only (no dividends).")

    # paper account value
    if len(nv) >= 2:
        st.markdown(ui.section_html("Paper account value"), unsafe_allow_html=True)
        nf = ui.line_fig(go, nv["ts"], [("Account value", list(nv["nav"]))], height=240,
                         pct=False, fmt="{:,.0f}", markers=True, colors=[ui.PORTFOLIO])
        st.plotly_chart(nf, width="stretch", config=ui.CHART_CONFIG)
        st.caption(f"Recorded each time the broker connects, in "
                   f"{nv['currency'].iloc[-1]}. Simulated money.")

    # coming up
    st.markdown(ui.section_html("Coming up"), unsafe_allow_html=True)
    try:
        from forward_test.evaluate import evaluation_date
        ev = evaluation_date(ft_dir)
    except Exception:                                                  # noqa: BLE001
        ev = None
    nm = next_monday(today)
    items = [("Next weekly email", f"{nm:%a %d %b}, 15:30")]
    if ev:
        items.append(("Quarter verdict", f"{ev:%d %b %Y} ({(ev - today).days} days)"))
    items.append(("Price update", "Tonight 23:30" if today.weekday() < 5 else "Monday 23:30"))
    st.markdown(ui.kv_html(items), unsafe_allow_html=True)
    if issues:
        st.markdown(ui.note_html("Details: System Health page for checks and alerts; "
                                 "Execution page for trades."), unsafe_allow_html=True)
