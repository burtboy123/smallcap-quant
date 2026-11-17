"""
Quant System — Management Dashboard
Run with: streamlit run dashboard/app.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
# Make project root importable
sys.path.append(str(Path(__file__).parent.parent))

from config.settings import ACTIVE_FT_PERIOD, DATABASE_URL  # noqa: E402
from dashboard import ui  # noqa: E402

# ── Helpers ────────────────────────────────────────────────────────────────────

def _db_path(url: str) -> str:
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///"):]
    raise ValueError(f"Not a SQLite URL: {url}")


@st.cache_resource
def get_connection():
    path = _db_path(DATABASE_URL)
    return sqlite3.connect(path, check_same_thread=False)


def query(sql: str, params=()) -> pd.DataFrame:
    try:
        return pd.read_sql(sql, get_connection(), params=params)
    except Exception as e:
        st.error(f"DB error: {e}")
        return pd.DataFrame()


# One look on every page (dashboard/ui.py): a title with a plain-language purpose,
# calm notes, and status that is always an icon + a word, never colour alone.
def _head(title: str, purpose: str) -> None:
    st.markdown(ui.header_html(title, purpose), unsafe_allow_html=True)


def _note(text: str, state: str | None = None, word: str | None = None) -> None:
    st.markdown(ui.note_html(text, state, word), unsafe_allow_html=True)


def _section(title: str) -> None:
    st.markdown(ui.section_html(title), unsafe_allow_html=True)


def _chart(fig, table: pd.DataFrame | None = None) -> None:
    """Every chart: the shared config, plus its numbers as a table (a chart is never
    the only way to read a value)."""
    st.plotly_chart(fig, width="stretch", config=ui.CHART_CONFIG)
    if table is not None:
        with st.expander("Show as a table"):
            st.dataframe(table, width="stretch", hide_index=True)


# ── Page config ────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Quant System",
    page_icon="S",
    layout="wide",
    initial_sidebar_state="auto",          # open on desktop, tucked away on a phone
)

# Forward-test data dir (dashboard READS these CSVs, never writes them)
# 2026Q3_parallel CONCLUDED 2026-09-14 (verdict KEEP INCUMBENT, commit b69c444).
# 2026Q4 RETIRED 2026-09-29 -- it tracked an UN-INVESTABLE book; preserved as
# 2026Q4_retired. The live readout is 2026Q4b.
FT_DIR = Path(__file__).parent.parent / "forward_test" / ACTIVE_FT_PERIOD
RETIRED_DIR = Path(__file__).parent.parent / "forward_test" / "2026Q4_retired"

# ══════════════════════════════════════════════════════════════════
# PAGE 0 — FORWARD TEST (LIVE)  — the actual current performance readout
# ══════════════════════════════════════════════════════════════════
def _page_forward_test():
    _head(f"Forward Test · {ACTIVE_FT_PERIOD}",
          "How the portfolio you hold is doing against the S&P 500, and against a "
          "comparison portfolio kept on paper only. Both are calculated from closing "
          "prices, not from broker fills.")
    _note("**Your portfolio** is the one held in the paper account. The **comparison "
          "portfolio** is a value-heavy alternative that is never traded: it is there to "
          "answer one question when the quarter ends, *would it have done better?*")
    _bg = st.expander("Background: how this test is built, and the retired 2026Q4 test")
    _bg.markdown(
        "**2026Q4 was RETIRED on 2026-09-29 after 5 sessions — its results are "
        "HISTORICAL and must not be cited as evidence about the strategy.** It tracked "
        "a book that could never have been held: Energy **46.69%** against a 30% cap, "
        "and WEYS/HBB/ATNI below the $5m/day ADV floor. Removing those leaves 8 names, "
        "under the 10-name minimum — **no compliant subset existed**, and the broker's "
        "kill-switch blocked **all 15 of 15** orders. Cause: `build_q4_books.py` ran "
        "only the *selection* path and skipped the risk stage. Preserved with its data "
        "in `forward_test/2026Q4_retired/`.")
    _bg.markdown(
        f"**Now tracking {ACTIVE_FT_PERIOD} — Incumbent-minus-ML, risk-compliant.** Built by the "
        "FULL production path (selection → limits applied *during* selection → "
        "`run_risk_check` + `enforce_sector_limits` → a hard guard that **raises**). "
        "Energy 26.7% (at cap), min ADV $5.3m/day, 15 names, 6.67% each. Started "
        "2026-09-30, ~90 days, first-close baselines, `EVAL_CRITERIA.md` carried "
        "forward byte-identical. It tracks **intended weights**, so it measures "
        "SELECTION cleanly; the paper account is a separate EXECUTION record and may "
        "differ on unfilled names. **Challenger is a PAPER REFERENCE ONLY — no "
        f"capital.** See `forward_test/README_{ACTIVE_FT_PERIOD}.md`.")

    curve_p = FT_DIR / "equity_curves.csv"
    inc_p   = FT_DIR / "incumbent_book.csv"
    ch_p    = FT_DIR / "value_tilt_book.csv"

    if not curve_p.exists():
        _note(f"No history recorded yet at `{curve_p}`. Run: `python forward_test/snapshot.py`",
              "warning", "Not started")
    else:
        curve = pd.read_csv(curve_p)
        curve["date"] = pd.to_datetime(curve["date"])
        start = curve["date"].min().date()
        latest = curve["date"].max().date()
        weeks_in = max((latest - start).days / 7.0, 0)

        from forward_test.evaluate import evaluation_date
        _eval_d = evaluation_date(FT_DIR)
        st.caption(
            f"Your portfolio: Incumbent-minus-ML, the deployed construction (health "
            f"score at least 5 of 9). Comparison: value-tilt (0.70 value / 0.30 quality, "
            f"health at least 3), paper only. Both equal-weighted at the {start} close, "
            f"then held. Verdict on **{_eval_d or 'see QUARTER_END_PLAN.md'}**. "
            f"**~{weeks_in:.0f} weeks in: too early to judge. Differences this early "
            f"are mostly noise (directional only, not proof of an edge).**"
        )

        # ── snapshot freshness / staleness indicator ─────────────────────────
        # Same NYSE calendar as the health check (incl. Thanksgiving/Christmas), so
        # this panel and the monitor cannot disagree about a holiday.
        from forward_test.check_snapshot_fresh import is_trading_day as _is_trading
        from forward_test.check_snapshot_fresh import most_recent_trading_day
        _d = most_recent_trading_day(date.today())
        missing = 0; _c = _d
        while _c > latest:
            if _is_trading(_c): missing += 1
            _c -= timedelta(days=1)
        if missing >= 2:
            _note(f"Prices are {missing} trading days behind (latest close recorded "
                  f"{latest}). Check the QuantForwardTestSnapshot scheduled task and "
                  f"`forward_test/snapshot_log.txt`.", "critical", "Stale")
        else:
            _note(f"Prices up to date: latest close recorded {latest}"
                  + (" (the next one is added tonight at 23:30)." if missing == 1 else "."),
                  "good", "Fresh")

        last = curve.iloc[-1]
        inc_r, ch_r, spy_r = last["incumbent_ret"], last["challenger_ret"], last["spy_ret"]
        inc_dd = curve["incumbent_drawdown"].min()
        ch_dd  = curve["challenger_drawdown"].min()
        spy_dd = curve["spy_drawdown"].min()
        spread = ch_r - inc_r

        c1, c2, c3, c4 = st.columns(4)
        _dip = "Largest fall from a previous high since the start."
        c1.metric("Your portfolio", f"{inc_r:+.2%}", f"{inc_r - spy_r:+.2%} vs S&P 500")
        c2.metric("Comparison (not traded)", f"{ch_r:+.2%}", f"{ch_r - spy_r:+.2%} vs S&P 500",
                  help="A paper-only alternative. No money is invested in it.")
        c3.metric("S&P 500 (SPY)", f"{spy_r:+.2%}")
        c4.metric("Comparison minus yours", f"{spread:+.2%}",
                  help="Positive: the comparison portfolio is ahead of yours so far.")

        d1, d2, d3 = st.columns(3)
        d1.metric("Your deepest dip", f"{inc_dd:.2%}", help=_dip)
        d2.metric("Comparison deepest dip", f"{ch_dd:.2%}", help=_dip)
        d3.metric("S&P 500 deepest dip", f"{spy_dd:.2%}", help=_dip)

        # ── equity curves ────────────────────────────────────────────────────
        _section("Return since the start")
        _lines = [("Your portfolio", "incumbent_index"),
                  ("Comparison (not traded)", "challenger_index"),
                  ("S&P 500", "spy_index")]
        _chart(ui.line_fig(go, curve["date"], [(n, list((curve[c] - 1) * 100))
                                               for n, c in _lines], height=360,
                           colors=[ui.PORTFOLIO, ui.COMPARISON, ui.SP500]),
               pd.DataFrame({"date": curve["date"].dt.date,
                             **{n: ((curve[c] - 1) * 100).round(2).astype(str) + "%"
                                for n, c in _lines}}))

        # ── interim evaluation, computed exactly as December's will be ─────────
        _section(f"Where the quarter's verdict stands today ({ACTIVE_FT_PERIOD} rules, fixed in advance)")
        st.caption(
            "The same code that gives the final verdict on or after "
            f"{_eval_d} (`forward_test/evaluate.py`), run on today's data. A reading this "
            "early is NOT a verdict: very few independent data points so far. In the "
            "reading, **Incumbent** = your portfolio and **Challenger** = the comparison "
            "portfolio. Criteria: "
            f"`forward_test/{ACTIVE_FT_PERIOD}/EVAL_CRITERIA.md`; window and dates: "
            f"`forward_test/{ACTIVE_FT_PERIOD}/QUARTER_END_PLAN.md`."
        )
        try:
            from forward_test.evaluate import evaluate as _evaluate
            with st.container(border=True):
                st.markdown(_evaluate(FT_DIR))
        except Exception as _e:                                          # noqa: BLE001
            _note(f"The interim reading could not be computed ({type(_e).__name__}: {_e}). "
                  f"Run `python forward_test/evaluate.py` for the reason.",
                  "warning", "Unavailable")

        # ── constituents ─────────────────────────────────────────────────────
        _hedged_section()

        _section("The stocks in each portfolio (fixed for the whole quarter)")
        cc1, cc2 = st.columns(2)
        if inc_p.exists():
            inc_b = pd.read_csv(inc_p)
            cc1.markdown(f"**Your portfolio — {len(inc_b)} stocks** (held in the paper account)")
            cc1.dataframe(inc_b[["ticker", "composite_score"]].round(1),
                          width="stretch", hide_index=True, height=340)
        if ch_p.exists():
            ch_b = pd.read_csv(ch_p)
            cc2.markdown(f"**Comparison portfolio — {len(ch_b)} stocks** (paper only, not traded)")
            cc2.dataframe(ch_b[["ticker", "composite_score", "f_score", "sector"]].round(1),
                          width="stretch", hide_index=True, height=340)

def _hedged_section():
    """The paper forward test of S&P 500 + (your picks - small-cap index). Rules:
    docs/research/hedged_paper_PREREG.md. Reads forward_test/hedged_paper/<period>.csv;
    the button recalculates it (downloads IWM closes)."""
    from forward_test import hedged_paper as hp
    _section("Trying to beat the S&P 500: the hedged paper test")
    _note("Hold the S&P 500, add your small-cap picks, and short the small-cap index "
          "(IWM): the small-cap drag cancels and only your stock picking is left. "
          "**Paper only.** It needs borrowing (200% long, 100% short); a 2%/yr cost is "
          "deducted. It passes only if, from 30 Sep 2027, it has beaten the S&P 500 "
          "**and** is at least as good per unit of risk "
          "(`docs/research/hedged_paper_PREREG.md`). Expect little: the stock picking "
          "it relies on was not confirmed in the years before 2019 "
          "(`docs/research/oos_picking_RESULT.md`).")
    path = hp.OUT_DIR / f"{ACTIVE_FT_PERIOD}.csv"
    if st.button("Update the hedged test (downloads small-cap index prices)"):
        try:
            hp.update(ACTIVE_FT_PERIOD)
        except Exception as e:                                         # noqa: BLE001
            _note(f"Could not update: {e}", "critical", "Failed")
    if not path.exists():
        _note("Not calculated yet. Press the button above, or run "
              "`python -m forward_test.hedged_paper`.")
        return
    h = pd.read_csv(path, parse_dates=["date"])
    if len(h) < 2:
        _note("Only the start day so far.")
        return
    r = hp.reading(h)
    hs, ss = r["hedged"], r["spy"]
    st.markdown(ui.kv_html([
        ("Hedged", f"{hs['total']:+.2%}"), ("S&P 500", f"{ss['total']:+.2%}"),
        ("Difference", f"{hs['total'] - ss['total']:+.2%}"),
        ("Deepest dip", f"{hs['max_dd']:.2%} vs {ss['max_dd']:.2%}"),
        ("Status", "too early to judge" if r["verdict"].startswith("interim")
         else r["verdict"])]), unsafe_allow_html=True)
    _chart(ui.line_fig(go, h["date"], [("Hedged", list((h["hedged_index"] - 1) * 100)),
                                       ("S&P 500", list((h["spy_index"] - 1) * 100))],
                       height=300, colors=[ui.HEDGED, ui.SP500]),
           pd.DataFrame({"date": h["date"].dt.date,
                         "hedged": (h["hedged_ret"] * 100).round(2).astype(str) + "%",
                         "S&P 500": (h["spy_ret"] * 100).round(2).astype(str) + "%",
                         "your portfolio": ((h["portfolio_index"] - 1) * 100).round(2)
                         .astype(str) + "%",
                         "small-cap index": ((h["iwm_index"] - 1) * 100).round(2)
                         .astype(str) + "%"}))
    st.caption(f"{r['days']} trading days to {r['last']:%d %b %Y}. Price change only "
               "(no dividends), like the rest of the forward test. Recalculated when you "
               "press the button or run the command.")


# ══════════════════════════════════════════════════════════════════
# PAGE 1 — STOCK PICKS (plain-language cards: dashboard/picks_view.py)
# ══════════════════════════════════════════════════════════════════
def _page_stock_picks():
    from config.settings import QUALITY_GATES as _QG, UNIVERSE as _UNI
    from dashboard import picks_view as pv

    _head("Stock Picks",
          "The companies the system currently likes, explained in plain words. "
          "**Your portfolio** shows the stocks held in the paper account; **Top screen "
          "picks** shows the highest-ranked companies in the latest screen, including "
          "some that are not held.")
    st.markdown(pv.CSS, unsafe_allow_html=True)
    with st.container(border=True):
        st.markdown("**How a stock makes it into the portfolio**")
        st.markdown(pv.steps_html(), unsafe_allow_html=True)

    raw = query("""
        SELECT ticker, composite_score, value_score, quality_score,
               momentum_score, f_score, roic, momentum_12_1
        FROM factor_scores
        WHERE calculation_date = (SELECT MAX(calculation_date) FROM factor_scores)
    """)

    if raw.empty:
        _note("No scores in the database yet. Run `python -m scripts.calculate_factors` first.",
              "warning", "No data")
    else:
        from factors.composite import ensemble_rank

        _calc = query("SELECT MAX(calculation_date) AS d FROM factor_scores")
        _calc_d = _calc["d"].iloc[0] if not _calc.empty else "?"
        _stocks = query("SELECT ticker, company_name, sector, industry FROM stocks")
        _bookp = FT_DIR / "incumbent_book.csv"
        _book = pd.read_csv(_bookp) if _bookp.exists() else pd.DataFrame(columns=["ticker"])
        _held = set(_book["ticker"])
        profiles = pv.load_profiles()

        raw["passed_quality_gate"] = raw["composite_score"].notna()
        ranked = ensemble_rank(raw)
        detail = raw.set_index("ticker")
        cols = ["value_score", "quality_score", "momentum_score", "f_score", "roic",
                "momentum_12_1"]

        def _rows(tickers_frame: pd.DataFrame) -> list[dict]:
            df = tickers_frame.copy()
            for c in cols:
                df[c] = df["ticker"].map(detail[c]) if c in detail else None
            if not _stocks.empty:
                meta = _stocks.drop_duplicates("ticker").set_index("ticker")
                for c in ("company_name", "sector", "industry"):
                    df[c] = df["ticker"].map(meta[c])
            if "sector" in tickers_frame and "sector" in df:
                df["sector"] = df["sector"].fillna(tickers_frame["sector"])
            return df.to_dict("records")

        top = ranked.head(25)
        tier_of = ranked.set_index("ticker")["conviction_tier"]
        book_rows = _book[["ticker"] + (["sector"] if "sector" in _book else [])].copy()
        book_rows["conviction_tier"] = book_rows["ticker"].map(tier_of)
        picks, mine = _rows(top), _rows(book_rows)

        st.caption(
            f"Scores calculated **{_calc_d}** · health check: at least "
            f"{_QG['f_score_min']} of 9 (Piotroski F-score), return on capital > "
            f"{_QG['min_roic']:.0%}, debt < {_QG['debt_to_ebitda_max']:.0f}× yearly "
            f"operating earnings (EBITDA) · {', '.join(_UNI['exclude_sectors'])} excluded · "
            f"descriptions: {len(profiles)} saved")

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Stocks in your portfolio", len(_held))
        m2.metric("Of the top 25 picks, held", f"{sum(t in _held for t in top['ticker'])}")
        _f = pd.to_numeric(pd.Series([r.get("f_score") for r in mine]), errors="coerce")
        m3.metric("Portfolio avg. health", f"{_f.mean():.1f} / 9" if _f.notna().any() else "n/a")
        _sec = pd.Series([r.get("sector") for r in mine]).dropna()
        m4.metric("Sectors held", _sec.nunique())

        if not profiles:
            _note("Company descriptions are not downloaded yet. Run once: "
                  "`venv\\Scripts\\python.exe scripts\\fetch_company_profiles.py` "
                  "(about a minute), then refresh this page.")

        tab_mine, tab_top, tab_sect = st.tabs(
            [f"Your portfolio ({len(mine)})", f"Top screen picks ({len(picks)})",
             "By sector"])
        with tab_mine:
            try:
                _meta = json.loads((FT_DIR / "meta.json").read_text(encoding="utf-8"))
                _start = _meta.get("baseline_date", "the start of the quarter")
            except (OSError, ValueError):
                _start = "the start of the quarter"
            st.markdown(
                f"Bought at equal weights at the {_start} close for the forward test and "
                "held unchanged until the quarter ends. Scores below are from the latest "
                "screen, so they can drift from when the stock was picked.")
            st.markdown(pv.cards_html(mine, profiles, _held), unsafe_allow_html=True)
        with tab_top:
            st.markdown(
                "Ranked best first. A highly ranked name can be missing from the portfolio "
                "because adding it would break a risk limit (a sector already at 30%, or "
                "shares that trade too thinly), or because it ranked high only after the "
                "portfolio was built.")
            st.markdown(pv.cards_html(picks, profiles, _held), unsafe_allow_html=True)
        with tab_sect:
            _ps = pd.DataFrame(mine)
            if "sector" in _ps and _ps["sector"].notna().any():
                cnt = (_ps["sector"].fillna("Unknown").value_counts()
                       .sort_values(ascending=True))
                _section("Your portfolio by sector (number of stocks · share)")
                fig = ui.hbar_fig(go, list(cnt.index), list(cnt.values),
                                  text=[f"{v} · {v / cnt.sum():.0%}" for v in cnt.values],
                                  hover="%{y}: %{x} stocks<extra></extra>")
                fig.update_xaxes(visible=False)
                _chart(fig, pd.DataFrame({"sector": cnt.index[::-1],
                                          "stocks": cnt.values[::-1]}))
                st.caption("No sector may exceed 30% of the portfolio. Equal weights mean "
                           "4 stocks ≈ 27%.")
            else:
                _note("Sector data not available.")

        with st.expander("What the words and numbers mean"):
            st.markdown(
                "- **Cheapness (0–100):** how cheap the stock is next to the ~1,900 "
                "companies screened, using operating earnings (EV/EBITDA), book value and "
                "free cash flow. 85 means "
                "cheaper than 85% of them.\n"
                "- **Quality (0–100):** profitability, financial health and steady "
                "margins.\n"
                "- **Price trend (0–100):** the price change over the past 12 months, "
                "leaving out the most recent month (very recent moves often reverse).\n"
                "- **Health (x/9):** the Piotroski F-score, nine yes/no checks on "
                "profits, cash flow, debt and efficiency. The system requires at least 5.\n"
                "- **Return on capital:** profit earned per dollar invested in the "
                "business, per year.\n"
                "- **Top conviction / Value pick / Momentum watch:** how strongly the two "
                "ranking methods agree. Top conviction = both rank it near the top.\n"
                "- **Worth knowing:** caveats, not reasons to sell. The system has no "
                "stop-losses; positions are reviewed at the quarterly rebuild.")

        with st.expander("All numbers (table)"):
            show = pd.DataFrame(picks)[["ticker", "company_name", "sector",
                                        "conviction_tier", "value_score",
                                        "quality_score", "momentum_score", "f_score",
                                        "roic", "momentum_12_1"]]
            st.dataframe(show.round(2), width="stretch", hide_index=True)

# ══════════════════════════════════════════════════════════════════
# PAGE 2 — RUN SCREEN
# ══════════════════════════════════════════════════════════════════
def _page_run_screen():
    _head("Run Screen",
          "Rank every company in the universe right now with the factor engine. A quick "
          "check, not the official ranking: the monthly run produces that.")
    from config.settings import FACTOR_WEIGHTS_BASE as _FWB
    _det = st.expander("Details: how this differs from the official ranking")
    _det.markdown(
        "Runs the factor engine (`factors/calculator.py`) across the universe with the "
        "deployed quality gate. **It is a quick screen, not the production ranking:** "
        "it does not fetch the auxiliary signals (sentiment, short interest, insider, "
        "earnings surprise, 52-week proximity, revenue acceleration), so the engine "
        "uses its 4-factor fallback weights below instead of the 9-factor "
        "`FACTOR_WEIGHTS`. The monthly run (`python -m scripts.monthly_run`) is the "
        "production ranking.")
    _det.caption("Fallback weights (FACTOR_WEIGHTS_BASE): "
               + " · ".join(f"{k} {v:.0%}" for k, v in _FWB.items())
               + " (no sentiment here, so the rest are re-weighted proportionally)")

    portfolio_size = st.slider("Rows to show", 10, 50, 25)
    store_scores = st.checkbox(
        "Save the scores to the database (the Stock Picks page will then show them)",
        value=False)

    if st.button("Run Screen Now", type="primary"):
        with st.spinner("Running factor screen across the universe..."):
            try:
                from data.universe_builder import get_universe
                from factors.calculator import calculate_factors_for_date

                tickers = get_universe()
                scores = calculate_factors_for_date(
                    tickers,
                    rebalance_date=date.today(),
                    store=store_scores,
                )

                if scores.empty:
                    _note("The screen returned no results. Check that the database has "
                          "fundamentals and prices.", "critical", "No results")
                else:
                    top = scores.sort_values("composite_score", ascending=False).head(portfolio_size)
                    _note(f"Screen complete: {len(scores)} stocks ranked, top "
                          f"{portfolio_size} shown.", "good", "Done")

                    show_cols = [
                        c for c in [
                            "ticker", "composite_score", "value_score",
                            "quality_score", "momentum_score", "f_score",
                            "ev_ebitda", "roic", "momentum_12_1",
                        ] if c in top.columns
                    ]

                    fmt = {
                        "composite_score": "{:.1f}",
                        "value_score": "{:.1f}",
                        "quality_score": "{:.1f}",
                        "momentum_score": "{:.1f}",
                        "f_score": "{:.0f}",
                        "ev_ebitda": "{:.1f}x",
                        "roic": "{:.1%}",
                        "momentum_12_1": "{:.1%}",
                    }
                    fmt = {k: v for k, v in fmt.items() if k in top.columns}

                    st.dataframe(
                        top[show_cols].style.background_gradient(
                            subset=["composite_score"], cmap="Greens"
                        ).format(fmt),
                        width="stretch",
                        hide_index=True,
                    )

            except Exception as e:
                _note(f"The screen failed: {e}", "critical", "Failed")
                import traceback
                st.code(traceback.format_exc())

# ══════════════════════════════════════════════════════════════════
# PAGE 3 — BACKTEST RESULTS
# ══════════════════════════════════════════════════════════════════
def _page_backtest():
    _head("Backtest Results",
          "Archive: the old 2020–2024 backtest, kept for reference. It is not the "
          "current system.")
    _note("**Retired results. Don't read them as current performance.** The live "
          "readout is **Forward Test** (Portfolio section).", "info", "Archive")
    st.expander("Details: why these results are retired").markdown(
        "**HISTORICAL / SUPERSEDED — for reference only.** This is the pre-rebuild "
        "2020–2024 backtest (current constituents only, so delisted/bankrupt names are "
        "absent) and it pre-dates the value-tilt rebuild. That survivorship gap has since "
        "been measured: net bias ~±1%/yr with an uncertain sign — **not** a large "
        "inflation (see docs/survivorship_findings.md). These numbers are superseded for "
        "other reasons. "
        "Its factors, gate (old f>=5 on the truncated f-score), and ML overlay are all "
        "superseded. Do NOT read these as current performance — the live readout is "
        "**Forward Test** (Portfolio section).")

    results = query("SELECT * FROM backtest_results ORDER BY run_date DESC LIMIT 20")

    if results.empty:
        _note("No backtest results in the database.")
        st.markdown("""
**To run the backtest:**
```python
from backtesting.engine import run_backtest
from data.universe_builder import get_universe
from config.settings import BACKTEST
from datetime import date

tickers = get_universe()
result = run_backtest(
    tickers=tickers,
    start_date=BACKTEST["out_of_sample_start"],
    end_date=BACKTEST["out_of_sample_end"],
    initial_capital=10_000_000,
)
```
        """)
    else:
        latest = results.iloc[0]

        # NOTE: `x or 0` does NOT catch NaN (NaN is truthy in Python), so a NaN
        # benchmark_return/alpha used to render as "nan%". Use pd.notna() guards
        # and show "N/A" instead of nan% / a misleading green pill.
        col1, col2, col3, col4 = st.columns(4)
        ann_ret = latest.get("annualized_return")
        bench   = latest.get("benchmark_return")
        alpha   = latest.get("alpha")

        if pd.notna(ann_ret):
            # only render the "vs benchmark" delta pill when a benchmark exists
            if pd.notna(bench):
                col1.metric("Annual Return", f"{ann_ret:.1%}", f"{ann_ret - bench:.1%} vs benchmark")
            else:
                col1.metric("Annual Return", f"{ann_ret:.1%}")
        else:
            col1.metric("Annual Return", "N/A")
        col2.metric("Sharpe Ratio",  f"{latest.get('sharpe_ratio', 0) or 0:.2f}")
        col3.metric("Max Drawdown",  f"{latest.get('max_drawdown', 0) or 0:.1%}")
        col4.metric("Alpha", f"{alpha:.1%}" if pd.notna(alpha) else "N/A (no benchmark recorded)")
        if pd.isna(bench):
            st.caption("No benchmark return was recorded for this run — alpha and "
                       "'vs benchmark' are shown as N/A rather than a spurious nan%.")

        st.markdown("---")
        _section("All Runs")
        st.dataframe(results, width="stretch", hide_index=True)

# ══════════════════════════════════════════════════════════════════
# PAGE 4 — UNIVERSE EXPLORER
# ══════════════════════════════════════════════════════════════════
def _page_universe():
    _head("Universe Explorer",
          "Every company the system screens, with its latest scores. Filter by health "
          "score (F-score, 0–9), quality and sector.")

    all_scores = query("""
        SELECT fs.ticker, fs.value_score, fs.quality_score, fs.momentum_score,
               fs.composite_score, fs.f_score, fs.ev_ebitda, fs.roic,
               fs.momentum_12_1, fs.universe_rank,
               s.company_name, s.sector, s.industry
        FROM factor_scores fs
        LEFT JOIN stocks s ON fs.ticker = s.ticker
        WHERE fs.calculation_date = (SELECT MAX(calculation_date) FROM factor_scores)
        ORDER BY fs.composite_score DESC
    """)

    if all_scores.empty:
        _note("No scores in the database yet. Run the screen first.", "warning", "No data")
    else:
        col1, col2, col3 = st.columns(3)
        min_fscore  = col1.slider("Min F-Score",     0, 9, 0)
        min_quality = col2.slider("Min Quality Score", 0, 100, 0)
        sectors = sorted(all_scores["sector"].dropna().unique().tolist())
        selected_sectors = col3.multiselect("Sectors", sectors, default=sectors[:5] if sectors else [])

        mask = (all_scores["f_score"].fillna(0) >= min_fscore) & \
               (all_scores["quality_score"].fillna(0) >= min_quality)
        if selected_sectors:
            mask &= all_scores["sector"].isin(selected_sectors)

        filtered = all_scores[mask]
        st.markdown(f"**{len(filtered)} stocks match filters** (showing first 200)")

        show = [c for c in [
            "universe_rank", "ticker", "company_name", "sector",
            "composite_score", "value_score", "quality_score", "momentum_score",
            "f_score", "ev_ebitda", "roic", "momentum_12_1",
        ] if c in filtered.columns]

        st.dataframe(filtered[show].head(200), width="stretch", hide_index=True)

# ══════════════════════════════════════════════════════════════════
# PAGE 5 — MACRO & REGIME
# ══════════════════════════════════════════════════════════════════
def _page_macro():
    _head("Macro & Regime",
          "The market backdrop (volatility, trend, oil) and how the system tilts its "
          "factor weights in response. Press the button to fetch fresh data.")

    if st.button("Refresh Regime Detection", type="primary"):
        with st.spinner("Fetching macro data (yfinance)..."):
            try:
                from agents.macro_agent import get_current_regime
                st.session_state["regime"] = get_current_regime()
            except Exception as e:
                _note(f"Could not fetch the market data: {e}", "critical", "Failed")

    regime = st.session_state.get("regime")

    if regime:
        s = regime["signals"]
        w = regime["weights"]

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Regime",      regime["regime"].replace("_", " "))
        col2.metric("VIX",         f"{s.get('vix', 0):.1f}",
                    delta="FEAR" if s.get("fear") else "calm",
                    delta_color="inverse" if s.get("fear") else "off")
        col3.metric("Above 200MA", "YES" if s.get("above_200ma") else "NO")
        col4.metric("Oil Shock",   "YES" if s.get("oil_shock") else "NO")

        st.markdown(f"**{regime['description']}**  \nAs of {regime['as_of']}")
        st.markdown("---")

        # Factor weights bar chart
        weight_keys = [k for k in w if k != "description"]
        _section("Factor weights for this regime")
        _chart(ui.hbar_fig(go, weight_keys, [w[k] for k in weight_keys], pct=True),
               pd.DataFrame({"factor": weight_keys,
                             "weight": [f"{w[k]:.0%}" for k in weight_keys]}))

        # Signals table
        signals_df = pd.DataFrame([
            {"Signal": k, "Value": str(v)}
            for k, v in s.items()
        ])
        _section("Raw Signals")
        st.dataframe(signals_df, width="stretch", hide_index=True)
    else:
        _note("Press **Refresh Regime Detection** to fetch the latest market data.")


# ══════════════════════════════════════════════════════════════════
# PAGE 6 — ECONOMIC INDICATORS
# ══════════════════════════════════════════════════════════════════
def _page_economic():
    _head("Economic Indicators",
          "Interest rates, inflation, the yield curve, credit stress and which small-cap "
          "sectors are leading. Press the button to fetch fresh data (FRED and yfinance).")

    if st.button("Refresh Economic Data", type="primary"):
        with st.spinner("Fetching FRED + sector ETF data..."):
            try:
                from agents.economic_agent import get_full_economic_snapshot
                st.session_state["econ"] = get_full_economic_snapshot()
            except Exception as e:
                _note(f"Could not fetch the economic data: {e}", "critical", "Failed")

    econ = st.session_state.get("econ")

    if econ:
        # ── Row 1: key rate metrics ──────────────────────────────
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Fed Funds Rate",
                  f"{econ.get('fed_funds_rate', 'N/A')}%")
        c2.metric("CPI YoY",
                  f"{econ.get('cpi_yoy', 'N/A')}%")

        real = econ.get("real_rate")
        c3.metric("Real Rate",
                  f"{real}%" if real is not None else "N/A",
                  delta="restrictive" if econ.get("real_rate_restrictive") else "accommodative",
                  delta_color="inverse" if econ.get("real_rate_restrictive") else "normal")

        spread = econ.get("yield_curve_spread")
        c4.metric("10Y-2Y Spread",
                  f"{spread}%" if spread is not None else "N/A",
                  delta="INVERTED" if econ.get("yield_curve_inverted") else "normal",
                  delta_color="inverse" if econ.get("yield_curve_inverted") else "off")

        hy = econ.get("fred_hy_spread")
        c5.metric("HY Spread",
                  f"{hy}%" if hy is not None else "N/A",
                  delta="STRESS" if econ.get("fred_credit_stress") else "normal",
                  delta_color="inverse" if econ.get("fred_credit_stress") else "off")

        st.markdown(f"*As of {econ.get('as_of')}* &nbsp;|&nbsp; "
                    f"Recession warning: **{'YES' if econ.get('recession_warning') else 'NO'}**")
        st.markdown("---")

        # ── Row 2: PMI gauge + consumer sentiment ────────────────
        col_left, col_right = st.columns(2)

        with col_left:
            pmi = econ.get("ism_pmi")
            _section("ISM Manufacturing PMI")
            if pmi is not None:
                _word = "expanding" if pmi > 50 else "shrinking" if pmi < 50 else "flat"
                st.metric("Factory activity (50 = no change)", f"{pmi:.1f}",
                          f"{pmi - 50:+.1f} vs 50: {_word}",
                          help="Above 50: manufacturing is growing; below 50: shrinking.")
                st.caption(f"Trend: {econ.get('pmi_trend', 'n/a')} · 1-month change: "
                           f"{econ.get('pmi_1m_change', 'N/A')} pts")
            else:
                _note("Factory activity (ISM PMI) not available: needs a FRED key.")

        with col_right:
            _section("Consumer Sentiment (U-Mich)")
            sent = econ.get("consumer_sentiment")
            if sent is not None:
                st.metric("Current", f"{sent}",
                          delta=econ.get("sentiment_trend", ""),
                          delta_color="normal" if econ.get("sentiment_trend") == "improving" else "inverse")
                st.metric("Inflation Expectations",
                          f"{econ.get('inflation_expectations', 'N/A')}%",
                          delta="HIGH" if econ.get("high_inflation_expectations") else "normal",
                          delta_color="inverse" if econ.get("high_inflation_expectations") else "off")
                st.metric("HY Spread trend", econ.get("fred_spread_trend", "N/A"))
                if econ.get("fred_hy_spread_1m_change") is not None:
                    st.metric("HY Spread 1M chg", f"{econ.get('fred_hy_spread_1m_change'):+.2f}%")
            else:
                _note("Consumer sentiment not available: needs a FRED key.")

        st.markdown("---")

        # ── Sector rotation table ────────────────────────────────
        _section(f"Sector Rotation vs IWM (3M, IWM={econ.get('iwm_return_3m', 0):.1%})")
        sector_data = econ.get("sector_returns", {})
        if sector_data:
            sector_rows = [
                {
                    "Sector":         s,
                    "3M Return":      f"{d['return_3m']:.1%}",
                    "vs IWM":         f"{d['vs_iwm']:+.1%}",
                    "Outperforming":  "YES" if d["outperforming"] else "no",
                }
                for s, d in sorted(sector_data.items(),
                                   key=lambda x: x[1]["vs_iwm"], reverse=True)
            ]
            sector_df = pd.DataFrame(sector_rows)
            st.dataframe(sector_df, width="stretch", hide_index=True)
        else:
            _note("Sector rotation data not available.")

    else:
        _note("Press **Refresh Economic Data** to fetch the indicators. Most need a free "
              "FRED key (fred.stlouisfed.org); sector rotation works without one.")


# ══════════════════════════════════════════════════════════════════
# PAGE 7 — RISK MONITOR
# ══════════════════════════════════════════════════════════════════
def _page_risk():
    _head("Risk Monitor",
          "Checks the portfolio you hold for concentration and risk: sector weights "
          "against the 30% cap, stocks that move together, and big falls since the "
          "start. Press the button to fetch live prices.")
    st.caption(
        f"Checks the **{ACTIVE_FT_PERIOD} Incumbent book** (the one held in the paper "
        "account). The nightly early-warning alerts (`monitoring/risk_watch.py`) cover "
        "the same ground automatically.")

    if st.button("Run Risk Check", type="primary"):
        with st.spinner("Fetching prices and running risk analysis..."):
            try:
                from agents.macro_agent import get_current_regime
                from agents.risk_agent import run_risk_check

                from broker.targets import baselines_for
                _bp = FT_DIR / "incumbent_book.csv"
                holdings = pd.DataFrame()
                if _bp.exists():
                    holdings = pd.read_csv(_bp)[["ticker", "composite_score",
                                                 "weight_equal"]].rename(
                        columns={"weight_equal": "weight"})
                    holdings["entry_price"] = holdings["ticker"].map(baselines_for(_bp))
                if holdings.empty:
                    _note(f"No portfolio file found at `{_bp}`.", "warning", "Missing")
                else:
                    regime = get_current_regime()
                    st.session_state["risk_report"] = run_risk_check(holdings, regime)
            except Exception as e:
                _note(f"The risk check failed: {e}", "critical", "Failed")
                import traceback
                st.code(traceback.format_exc())

    report = st.session_state.get("risk_report")

    if report:
        _alert = report["status"] == "ALERT"
        st.markdown(ui.headline_html(
            "critical" if _alert else "good", "Alert" if _alert else "OK",
            "Risk flags found, listed below." if _alert else "No risk flags."),
            unsafe_allow_html=True)
        st.caption(f"Regime: {report['regime']}  |  As of: {report['as_of']}  |  "
                   f"Portfolio 1M: {report['portfolio_return_1m']:.1%}")

        for flag in report.get("flags", []):
            _note(str(flag), "warning", "Flag")

        if report["stop_losses"]:
            _note("**Down 20% or more since the start.** Information only: the "
                       "system has NO stop-losses (a value book sells cheap names at "
                       "the bottom; see docs/post_september_research.md). The 5.2 "
                  "kill-switch blocks additions to a name down 50% from entry.", "info", "Info")
            st.dataframe(pd.DataFrame(report["stop_losses"]), width="stretch", hide_index=True)

        col1, col2 = st.columns(2)

        with col1:
            _section("Sector Exposure")
            if report["sector_exposure"]:
                sector_df = (
                    pd.DataFrame([
                        {"Sector": k, "Weight": v}
                        for k, v in report["sector_exposure"].items()
                    ])
                    .sort_values("Weight", ascending=False)
                )
                _over = sector_df["Weight"] > 0.30
                _chart(ui.hbar_fig(
                    go, list(sector_df["Sector"]), list(sector_df["Weight"]), pct=True,
                    colors=[ui.LOSS if o else ui.SERIES[0] for o in _over],
                    text=[f"{v:.0%}" + (" · over the cap" if o else "")
                          for v, o in zip(sector_df["Weight"], _over)],
                    ref=0.30, ref_label="30% cap"),
                    sector_df.assign(Weight=sector_df["Weight"].map("{:.1%}".format)))
            else:
                _note("No sector data available.")

        with col2:
            _section("Risk-Adjusted Position Sizes")
            sizes_df = pd.DataFrame(report["position_sizes"])
            if not sizes_df.empty:
                sizes_df["weight_pct"] = (sizes_df["weight"] * 100).round(1)
                _chart(ui.hbar_fig(go, list(sizes_df["ticker"]), list(sizes_df["weight"]),
                                   pct=True, text=[f"{v:.1%}" for v in sizes_df["weight"]]),
                       sizes_df[["ticker", "weight_pct"]].rename(
                           columns={"weight_pct": "weight %"}))

        if report["high_correlation"]:
            _section("High Correlation Flags")
            _note("These stocks tend to move together with a higher-ranked holding: "
                  f"{', '.join(report['high_correlation'])}", "warning", "Overlap")
    else:
        _note("Press **Run Risk Check** to analyse the portfolio.")


# ══════════════════════════════════════════════════════════════════
# PAGE 8 — LEARNING & PERFORMANCE
# ══════════════════════════════════════════════════════════════════
def _page_learning():
    import json as _json

    _head("Learning & Performance",
          "Archive: how the picks of the retired March–June 2026 paper trade turned "
          "out. Kept for reference; it is not the current system.")
    _note("**Retired results. Don't read them as current performance.** The live "
          "readout is **Forward Test** (Portfolio section).", "info", "Archive")
    st.expander("Details: why this cohort is retired").markdown(
        "Self-improvement engine: past pick evaluation, factor accuracy, ML validity.\n\n"
        "**RETIRED COHORT — not the current system.** The −9.75% avg-alpha / 45-pick "
        "record below is the **Mar–Jun 2026 paper trade** that failed due to a diagnosed "
        "**INFLATION_SHOCK regime misfire** (oil-shock override that has since been fixed). "
        "It is NOT the current construction (Incumbent-minus-ML: 9-factor weights, "
        "f-score >= 5, regime sector bias on, ML disabled). The ML overlay it relied "
        "on is now **disabled and frozen**. "
        "For current performance see **Forward Test** (Portfolio section).")

    outcomes = query("SELECT * FROM portfolio_outcomes ORDER BY entry_date DESC")

    if outcomes.empty:
        _note("No evaluated outcomes yet. They appear once picks are 60+ days old. "
              "Run `python scripts/evaluate_performance.py` to check for any ready "
              "evaluations.")
    else:
        # ── Summary metrics ────────────────────────────────────────────────────
        n        = len(outcomes)
        win_rate = float(outcomes["beat_spy"].mean())
        avg_alpha = float(outcomes["alpha"].mean())
        avg_ret   = float(outcomes["realized_return"].mean())

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Picks Evaluated", n)
        m2.metric("Win Rate vs SPY", f"{win_rate:.1%}")
        m3.metric("Avg Alpha",  f"{avg_alpha:+.2%}")
        m4.metric("Avg Return", f"{avg_ret:+.2%}")

        st.divider()

        col_left, col_right = st.columns(2)

        # ── Win rate over time ─────────────────────────────────────────────────
        with col_left:
            _section("Share of picks that beat the S&P 500, by start date")
            cohort = (
                outcomes.groupby("entry_date")
                .agg(win_rate=("beat_spy", "mean"), n=("ticker", "count"))
                .reset_index()
            )
            if not cohort.empty:
                _chart(ui.vbar_fig(go, cohort["entry_date"].astype(str), cohort["win_rate"],
                                   pct=True, ref=0.5, ref_label="50% = a coin flip",
                                   height=280),
                       cohort.assign(win_rate=cohort["win_rate"].map("{:.0%}".format))
                       .rename(columns={"entry_date": "start date", "win_rate": "beat SPY",
                                        "n": "picks"}))

        # ── Alpha distribution ─────────────────────────────────────────────────
        with col_right:
            _section("How far each pick beat or lagged the S&P 500")
            _chart(ui.hist_fig(go, outcomes["alpha"].dropna(), ref=0.0,
                               ref_label="0 = same as SPY", height=280))

        st.divider()

        # ── Factor accuracy ────────────────────────────────────────────────────
        _section("Factor Accuracy — What Actually Predicted Returns")

        all_correct, all_failed = [], []
        for _, row in outcomes.iterrows():
            all_correct.extend(_json.loads(row.get("which_factors_predicted_correctly") or "[]"))
            all_failed.extend(_json.loads(row.get("which_factors_failed") or "[]"))

        from collections import Counter as _Counter
        cc = _Counter(all_correct)
        fc = _Counter(all_failed)
        all_factors = set(list(cc.keys()) + list(fc.keys()))

        if all_factors:
            fa_rows = []
            for f in all_factors:
                c = cc.get(f, 0)
                fail = fc.get(f, 0)
                total = c + fail
                fa_rows.append({
                    "Factor": f,
                    "Accuracy": round(c / total, 3) if total else None,
                    "Correct": c,
                    "Failed": fail,
                    "Total": total,
                })
            fa_df = pd.DataFrame(fa_rows).sort_values("Accuracy", ascending=False)
            fa_df["Accuracy %"] = fa_df["Accuracy"].apply(
                lambda x: f"{x:.1%}" if x is not None else "N/A"
            )
            st.dataframe(fa_df[["Factor", "Accuracy %", "Correct", "Failed", "Total"]],
                         width="stretch", hide_index=True)
        else:
            _note("Factor attribution appears once more outcomes are evaluated.")

        st.divider()
        col3, col4 = st.columns(2)

        # ── ML score vs alpha scatter ──────────────────────────────────────────
        with col3:
            _section("ML score at entry vs result")
            if "entry_ml_score" in outcomes.columns:
                _known = outcomes[outcomes["beat_spy"].notna()]   # no result: not "beat"
                _won = _known["beat_spy"].astype(bool)
                _groups = [("Beat SPY", _known[_won], ui.GAIN),
                           ("Lagged SPY", _known[~_won], ui.LOSS)]
                fig_sc = ui.scatter_fig(
                    go, [(n, d["entry_ml_score"], d["alpha"], c) for n, d, c in _groups],
                    hover_text=[d["ticker"] + " " + d["entry_date"].astype(str)
                                for _, d, _ in _groups])
                ui.ref_line(fig_sc, 0.0, "0 = same as SPY", axis="y")
                _chart(fig_sc)
                ml_corr = outcomes["entry_ml_score"].corr(outcomes["alpha"])
                st.caption(f"Correlation between ML score and result: {ml_corr:+.3f} "
                           "(0 = the score told nothing).")

        # ── Best / worst picks ─────────────────────────────────────────────────
        with col4:
            _section("Best & Worst Picks")
            top5    = outcomes.nlargest(5, "alpha")[
                ["ticker", "entry_date", "realized_return", "alpha", "entry_ml_score"]
            ].copy()
            bottom5 = outcomes.nsmallest(5, "alpha")[
                ["ticker", "entry_date", "realized_return", "alpha", "entry_ml_score"]
            ].copy()
            for df_bw in [top5, bottom5]:
                df_bw["realized_return"] = df_bw["realized_return"].map("{:+.1%}".format)
                df_bw["alpha"]           = df_bw["alpha"].map("{:+.1%}".format)
            st.markdown("**Top 5 by Alpha**")
            st.dataframe(top5,    width="stretch", hide_index=True)
            st.markdown("**Bottom 5 by Alpha**")
            st.dataframe(bottom5, width="stretch", hide_index=True)

        # ── Training data growth ───────────────────────────────────────────────
        st.divider()
        _section("Training Data Growth")
        try:
            import os
            bt_path = "data/training_data.csv"
            en_path = "data/training_data_enhanced.csv"
            growth_rows = []
            if os.path.exists(bt_path):
                n_bt = len(pd.read_csv(bt_path))
                growth_rows.append({"Source": "Backtest", "Rows": n_bt})
            growth_rows.append({"Source": "Live Outcomes", "Rows": n})
            if os.path.exists(en_path):
                n_en = len(pd.read_csv(en_path))
                growth_rows.append({"Source": "Enhanced (Combined)", "Rows": n_en})
            g_df = pd.DataFrame(growth_rows)
            _chart(ui.hbar_fig(go, list(g_df["Source"]), list(g_df["Rows"]), sort=False), g_df)
        except Exception:
            _note("Training data summary unavailable.")


# ══════════════════════════════════════════════════════════════════
# PAGE 9 — SYSTEM HEALTH
# ══════════════════════════════════════════════════════════════════
def _page_execution():
    _head("Execution",
          "What the paper broker account actually bought and sold, order by order. "
          "Simulated money; this page only reads the records and cannot trade.")
    _note("This is the broker's record. The **Forward Test** page measures stock "
          "selection from closing prices instead, so the two can differ.")
    st.expander("Details: what this page reads").markdown(
        "**PAPER ACCOUNT — SIMULATED MONEY. Read-only page.** This shows the "
        "**execution record** of the IBKR paper account (`DU1234567`, port 4002; runs "
        "before 2026-09-30 afternoon are from the retired `DUR222564`). It "
        "is **not** the Q4b forward test: Q4b tracks **intended weights** to measure "
        "*selection* cleanly, while this page shows what actually *filled*. The two "
        "legitimately differ. This page cannot place, modify or cancel any order — it "
        "only reads `broker/execution_log/`, so it works with Gateway closed. No real "
        "money is involved and real money is not authorized.")

    import broker.execution_store as _xs  # noqa: E402
    from dashboard import execution_view as _ev_view
    from monitoring import weekly_report as _wr

    _got = _wr._latest_recon()
    _hold = _ev_view.holdings_line(_got[1]) if _got else None
    if _hold:
        st.markdown(ui.headline_html("good", "Holding", _hold), unsafe_allow_html=True)

    _trades = _xs.load_trades()
    _runs = _xs.run_summary()

    if _trades.empty:
        st.markdown(ui.note_html("No execution records yet in `broker/execution_log/`.",
                                 "info", "Empty"), unsafe_allow_html=True)
        st.markdown(
            "Run a paper cycle:\n"
            f"```\npython -m broker.ibkr_paper --book forward_test/{ACTIVE_FT_PERIOD}/"
            "incumbent_book.csv --execution-engine --paper-execute\n```\n"
            "or import pre-store history with `python -m broker.execution_store`."
        )
    else:
        # ── run picker ──────────────────────────────────────────────────────
        # Opens on the newest run that sent orders: a dry run shows "0 filled" by
        # design, which read as if nothing had ever been bought.
        _labels = {
            r.run_id: _ev_view.run_label(r.timestamp, r.account, r.engine, r.dry_run,
                                         r.share_fill_rate)
            for r in _runs.itertuples()
        }
        _sel = st.selectbox("Execution run", list(_labels), format_func=lambda k: _labels[k],
                            index=_ev_view.default_run(_runs),
                            help="Opens on the newest run that actually sent orders.")
        _t = _trades[_trades.run_id == _sel].copy()
        _r = _runs[_runs.run_id == _sel].iloc[0]

        if _r.dry_run:
            _note("This was a **rehearsal** (dry run): the orders were worked out and "
                  "printed but never sent to the broker, so nothing could fill. Pick a "
                  "run marked *% filled* to see real trades.", "info", "Rehearsal")

        # ── headline metrics ────────────────────────────────────────────────
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Orders", f"{int(_r.orders)}",
                  f"{int(_r.fully_filled)} filled / {int(_r.partial)} partial / "
                  f"{int(_r.unfilled)} unfilled")
        c2.metric("Shares filled", f"{_r.share_fill_rate*100:.1f}%",
                  f"{_r.shares_filled:,.0f} of {_r.shares_ordered:,.0f}")
        _nav = _r.nav_usd if pd.notna(_r.nav_usd) and _r.nav_usd else None
        c3.metric("Book deployed",
                  f"{_r.filled_notional/_nav*100:.2f}%" if _nav else "n/a",
                  f"${_r.filled_notional:,.0f}" + (f" of ${_nav:,.0f}" if _nav else ""))
        c4.metric("Mean slippage",
                  f"{_r.mean_slippage_bps:+.1f} bps" if pd.notna(_r.mean_slippage_bps) else "n/a",
                  "negative = better than limit", delta_color="off")
        c5.metric("Reprices / children", f"{int(_r.reprices)} / {int(_r.children)}",
                  _r.engine, delta_color="off")

        # ── every order ─────────────────────────────────────────────────────
        _section("Every buy and sell in this run" + (" (planned, not sent)" if _r.dry_run else ""))
        from monitoring.weekly_report import _with_ref
        _t = _with_ref(_t, _xs.load_events())
        _sign = _t.side.map({"BUY": 1, "SELL": -1})
        _t["vs ref (bps)"] = _sign * (_t.avg_fill - _t.ref_price) / _t.ref_price * 1e4
        _t["fill %"] = (_t.fill_rate.clip(upper=1) * 100).round(0)
        _show = _t[["timestamp", "ticker", "side", "ordered", "filled", "remaining",
                    "fill %", "ref_price", "limit", "avg_fill", "vs ref (bps)",
                    "slippage_bps", "reprices", "children", "status"]].rename(columns={
            "timestamp": "time", "ordered": "shares ord", "filled": "shares fill",
            "ref_price": "ref price", "avg_fill": "fill price",
            "slippage_bps": "vs limit (bps)"})

        _show.insert(1, "result", _show.apply(
            lambda row: "– not sent" if _r.dry_run          # nothing sent: nothing "unfilled"
            else "✕ unfilled" if row["shares fill"] == 0
            else "! partial" if row["remaining"] > 0 else "✓ filled", axis=1))
        st.dataframe(_show.style
                     .format({"limit": "{:.2f}", "fill price": "{:.2f}",
                              "ref price": "{:.2f}", "vs ref (bps)": "{:+.1f}",
                              "vs limit (bps)": "{:+.1f}", "fill %": "{:.0f}%"},
                             na_rep="—"),
                     width="stretch", hide_index=True)
        st.caption("**result**: ✓ filled · ! partial · ✕ unfilled · **vs ref** = cost against the "
                   "price the order was sized off (positive = paid more / sold for "
                   "less); this is the number for the real-time-data decision · vs "
                   "limit is flattered by the limit buffer. Fills come from execution "
                   "records, never from a live order's working quantity.")

        # ── problem orders with reasons ─────────────────────────────────────
        _bad = _t[(_t.filled == 0) | (_t.remaining > 0)]
        if not _bad.empty and not _r.dry_run:
            _section("Unfilled and partial orders, with reasons")
            _diag_p = Path(__file__).parent.parent / "broker" / "reconciliation" / "underfill_diagnosis.csv"
            # broker/diagnose_underfill.py measured the 2026-09-29 run only. Attaching
            # it to other runs by ticker printed 09-29's causes on unrelated orders.
            _diag = (pd.read_csv(_diag_p)
                     if _diag_p.exists() and str(_r.timestamp)[:10] == "2026-09-29"
                     else pd.DataFrame())
            for r in _bad.sort_values("fill_rate").itertuples():
                _why = []
                if r.filled == 0:
                    _why.append("**zero fill**")
                elif r.remaining > 0:
                    _why.append(f"**partial** — {r.remaining:,.0f} of "
                                f"{r.ordered:,.0f} shares left")
                if len(_diag) and r.ticker in set(_diag.ticker):
                    d = _diag[_diag.ticker == r.ticker].iloc[0]
                    cause = str(d.get("cause", ""))
                    if cause.startswith("A_"):
                        _why.append(f"limit {r.limit:.2f} never reached — true low was "
                                    f"{d['true_low']:.2f} (delayed quote was "
                                    f"{d['stale_bps']:+.0f} bps stale)")
                    elif cause.startswith("B_"):
                        _why.append(f"limit WAS reached (true low {d['true_low']:.2f}) — "
                                    f"size/time constrained, not price")
                if str(r.status).find("DRIFT_CAP") >= 0:
                    _why.append("hit the cumulative reprice **drift cap** — deliberately "
                                "not chased further")
                if str(r.status).find("GAVE_UP") >= 0:
                    _why.append("**reprice attempts exhausted**")
                st.markdown(f"- **{r.ticker}** ({r.side}): " + "; ".join(_why))

        # ── paper positions vs the forward-test book ────────────────────────
        _section(f"What the account holds vs the {ACTIVE_FT_PERIOD} portfolio")
        st.caption(
            "Positions from the last reconciliation the broker wrote (after the latest "
            "executed run), valued at the latest snapshot closes. **Target = buy-and-"
            "hold weight**: the book's entry weight moved by price since the baseline, "
            "which is what the forward test tracks, so price moves alone are not drift. "
            "For live positions: `python -m broker.ibkr_paper --read-only`.")
        _bookp = FT_DIR / "incumbent_book.csv"
        if not _bookp.exists():
            _note(f"Portfolio file not found: `{_bookp}`", "warning", "Missing")
        elif not _got:
            _note("No account view on file yet (saved on every broker connection, --read-only included).")
        else:
            _rp, _rec = _got
            _pos = pd.DataFrame(_rec.get("positions_after", []))
            _bk = pd.read_csv(_bookp)
            _snap = pd.read_csv(FT_DIR / "price_snapshots.csv")
            _cl = _snap.sort_values("date").groupby("ticker")["close"].last()
            _tw, _basis = _wr._buy_and_hold_targets(_bk, _cl)
            _cmp = _bk[["ticker", "sector"]].merge(
                _pos[["ticker", "position", "avg_cost"]] if len(_pos) else
                pd.DataFrame(columns=["ticker", "position", "avg_cost"]),
                on="ticker", how="outer")
            _cmp["shares"] = _cmp.position.fillna(0.0)
            _cmp["price"] = _cmp.ticker.map(_cl).fillna(_cmp.avg_cost)
            _cmp["value"] = _cmp.shares * _cmp.price.fillna(0.0)
            _tot = _cmp.value.sum()
            _cmp["actual_w"] = _cmp.value / _tot if _tot else 0.0
            _cmp["target_w"] = _cmp.ticker.map(_tw).fillna(0.0)
            _cmp["diff_pp"] = (_cmp.actual_w - _cmp.target_w) * 100
            _cmp["state"] = _cmp.apply(
                lambda r: "NOT HELD (target)" if r.shares == 0 and r.target_w > 0
                else ("NOT IN BOOK (held)" if r.target_w == 0 and r.shares > 0
                      else "matched"), axis=1)
            _cmp["check"] = _cmp.apply(
                lambda r: "✕ not held" if r.state == "NOT HELD (target)"
                else "! not in portfolio" if r.state == "NOT IN BOOK (held)"
                else "! off target" if abs(r.diff_pp) > 2.0 else "✓ ok", axis=1)

            st.caption(f"Reconciliation `{_rp.name}` ({_rec.get('timestamp', '?')}); "
                       f"target basis: {_basis}.")
            st.dataframe(
                _cmp[["ticker", "check", "sector", "shares", "price", "value",
                      "actual_w", "target_w", "diff_pp", "state"]]
                .sort_values("diff_pp")
                .style
                .format({"shares": "{:,.0f}", "price": "{:.2f}",
                         "value": "${:,.0f}", "actual_w": "{:.2%}",
                         "target_w": "{:.2%}", "diff_pp": "{:+.2f}pp"}, na_rep="—"),
                width="stretch", hide_index=True)
            st.caption("**check**: ✕ not held = in the portfolio but not in the account · "
                       "! not in portfolio = held but not in the portfolio (needs exiting) · "
                       "! off target = more than 2 percentage points from its target. "
                       "Weights are shares of the invested value.")
            k1, k2 = st.columns(2)
            k1.metric("Invested value", f"${_tot:,.0f}", "at latest snapshot closes",
                      delta_color="off")
            k2.metric("Names matched",
                      f"{int((_cmp.state == 'matched').sum())}/{len(_cmp)}",
                      f"{int((_cmp.state != 'matched').sum())} differ", delta_color="off")

        # ── run comparison ──────────────────────────────────────────────────
        if len(_runs) > 1:
            _section("All runs: is execution getting better?")
            _rc = _runs[["timestamp", "engine", "dry_run", "orders", "fully_filled",
                         "partial", "unfilled", "share_fill_rate",
                         "mean_slippage_bps", "reprices", "children"]].copy()
            _rc["share_fill_rate"] = (_rc.share_fill_rate * 100).round(1)
            st.dataframe(_rc.rename(columns={"share_fill_rate": "shares filled %",
                                             "mean_slippage_bps": "slip (bps)"}),
                         width="stretch", hide_index=True)

        # ── reprice / slice event log ────────────────────────────────────────
        _ev = _xs.load_events()
        if not _ev.empty and _sel in set(_ev.run_id):
            with st.expander("Execution event log (plan / submit / reprice / drift-cap)"):
                st.dataframe(_ev[_ev.run_id == _sel], width="stretch",
                             hide_index=True)

    st.divider()
    st.caption("Source: `broker/execution_log/` via `broker/execution_store.py`. "
               "This page is READ-ONLY — it never connects to IB and never places, "
               "modifies or cancels an order.")

def _page_health():
    _head("System Health",
          "Is the automatic pipeline alive? The last weekly health check, the alerts "
          "that are open and what each one means. This page reads saved results only; "
          "it runs nothing.")

    # ══════════════════════════════════════════════════════════════════════
    # PIPELINE HEALTH — "is the pipeline actually alive?" at a glance.
    #
    # Reads what monitoring/health_check.py last WROTE (monitoring/state/), so this
    # panel works with Gateway closed and reflects the same verdicts the scheduled
    # task and the broker's pre-rebalance gate act on. It never runs the checks
    # itself: a dashboard that re-runs them would show a different answer than the
    # one that actually gated trading, and running pytest on page load is no way to
    # build a dashboard.
    #
    # This is also where the one gap the checks cannot close is visible. Nothing
    # self-hosted can report "the health check never ran at all" from inside a run
    # that is happening, so the AGE of the last run is shown prominently here.
    # ══════════════════════════════════════════════════════════════════════
    _section("Pipeline health")

    _MON = Path(__file__).parent.parent / "monitoring" / "state"

    def _load_json(p: Path, default):
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default
        except Exception:
            return default

    _hist = _load_json(_MON / "health_history.json", [])
    _astate = _load_json(_MON / "alert_state.json", {})

    if not _hist:
        _note("**The health check has never recorded a run.** Nothing is watching the "
              "pipeline yet. Run `python -m monitoring.health_check -v`, then register "
              "the scheduled tasks with "
              "`powershell -ExecutionPolicy Bypass -File monitoring\\register_tasks.ps1 -Elevate`. "
              "See `monitoring/README.md`.", "warning", "Not running")
    else:
        _last = _hist[-1]
        try:
            _ts = datetime.fromisoformat(_last["ts"])
            _age_h = (datetime.now() - _ts).total_seconds() / 3600.0
        except Exception:
            _ts, _age_h = None, None

        _statuses = _last.get("statuses", {}) or {}
        _failed = _last.get("failed", []) or []
        _n_fail = len(_failed)
        _n_pass = sum(1 for v in _statuses.values() if v == "PASS")

        # A health run that is itself stale is the quiet failure. Say so loudly.
        # The task is weekly (Mondays 15:30), so stale = more than a week plus a day.
        _stale_run = _age_h is not None and _age_h > 8 * 24

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Overall", "FAIL" if _n_fail else "OK",
                  delta=f"{_n_fail} failing" if _n_fail else "all clear",
                  delta_color="inverse" if _n_fail else "normal")
        c2.metric("Checks passing", f"{_n_pass}/{len(_statuses)}")
        c3.metric("Last health run",
                  f"{_age_h:.0f}h ago" if _age_h is not None else "unknown",
                  delta="STALE — is the task running?" if _stale_run else "recent",
                  delta_color="inverse" if _stale_run else "normal")
        c4.metric("Active alerts", len(_astate))

        if _stale_run:
            _note(f"**The health check itself has not run for {_age_h / 24:.0f} days.** "
                  "Nothing can detect this from the inside, which is why it is shown "
                  "here. Check the scheduled task: "
                  "`powershell -Command \"Get-ScheduledTaskInfo -TaskName QuantHealthCheck\"`",
                  "critical", "Not running")

        st.caption(
            f"period **{_last.get('period', '?')}** · recorded "
            f"{_ts:%Y-%m-%d %H:%M} local · source `monitoring/state/health_history.json` "
            f"· read-only, this page runs no checks"
            if _ts else "recorded time unknown"
        )

        # ── per-check table ────────────────────────────────────────────────
        _metrics = _last.get("metrics", {}) or {}
        _rows = []
        for _name, _status in _statuses.items():
            _m = _metrics.get(_name, {}) or {}
            _bits = [f"{k}={v}" for k, v in _m.items()
                     if k in ("rows", "sessions", "tickers", "latest_recorded",
                              "sessions_behind", "carried", "latest", "state",
                              "last_result", "age_hours", "failed", "passed")]
            _rows.append({"check": _name, "status": ui.status_cell(_status),
                          "detail": ", ".join(_bits)[:110]})
        _df = pd.DataFrame(_rows)
        st.dataframe(_df, width="stretch", hide_index=True)

        # ── active alerts, with what each one blocks and how to fix it ──────
        if _astate:
            _section("Open alerts (one row per ongoing problem)")
            _arows = []
            for _k, _v in _astate.items():
                _since = _v.get("since", "")
                try:
                    _days = (datetime.now() - datetime.fromisoformat(_since)).days
                    _since_txt = f"{_since[:16]}  ({_days}d)"
                except Exception:
                    _since_txt = _since
                _arows.append({"alert": _k, "severity": _v.get("severity", ""),
                               "since": _since_txt,
                               "occurrences": _v.get("occurrences", ""),
                               "what": str(_v.get("summary", ""))[:90]})
            _adf = pd.DataFrame(_arows).sort_values(
                "severity", key=lambda s: s.map({"CRITICAL": 0, "WARNING": 1,
                                                 "INFO": 2}).fillna(3))
            st.dataframe(_adf, width="stretch", hide_index=True)
            if any(k.startswith(("killswitch:", "rebalance:")) for k in _astate):
                _note("Kill-switch and blocked-rebalance alerts close by themselves the next "
                      "time the broker passes its safety gate without a problem. A dry run "
                      "is enough (it sends nothing; needs IB Gateway running): "
                      f"`python -m broker.ibkr_paper --book forward_test/{ACTIVE_FT_PERIOD}/incumbent_book.csv`. "
                      "If an alert comes back, it is real: see `broker/RUNBOOK.md` section 7.")
        else:
            _note("No open alerts.", "good", "Clear")

        # ── recent alert history ───────────────────────────────────────────
        _log = _MON / "alert_log.jsonl"
        if _log.exists():
            with st.expander("Alert log (newest first)"):
                _entries = []
                for _line in _log.read_text(encoding="utf-8").splitlines():
                    _line = _line.strip()
                    if not _line:
                        continue
                    try:
                        _entries.append(json.loads(_line))
                    except Exception:
                        continue
                if _entries:
                    st.dataframe(
                        pd.DataFrame([
                            {"ts": e.get("ts", "")[:19],
                             "severity": e.get("severity", ""),
                             "resolved": e.get("resolved", False),
                             "key": e.get("key", ""),
                             "summary": str(e.get("summary", ""))[:80],
                             "run": str(e.get("runbook", ""))[:60]}
                            for e in reversed(_entries)][:100]),
                        width="stretch", hide_index=True)
                else:
                    st.caption("alert log is empty")

        # ── trend: is the number of failures growing? ──────────────────────
        if len(_hist) > 1:
            with st.expander("Health history"):
                _tr = pd.DataFrame([
                    {"ts": h.get("ts", "")[:19],
                     "failing": len(h.get("failed", []) or []),
                     "checks": len(h.get("statuses", {}) or {}),
                     "failed_checks": ", ".join(h.get("failed", []) or []) or "-"}
                    for h in _hist[-60:]])
                st.dataframe(_tr.iloc[::-1], width="stretch",
                             hide_index=True)

    with st.expander("How to run the health check / configure alerts"):
        st.markdown(
            "```\n"
            "python -m monitoring.health_check -v        # full run, alerts on failure\n"
            "python -m monitoring.health_check --fast    # skip the pytest run\n"
            "python -m monitoring.break_test             # prove the monitor works\n"
            "```\n"
            "Exit code 1 means at least one check FAILED. The broker path runs the "
            "same checks before sizing a book and refuses on any CRITICAL failure.\n\n"
            "Email alerts are **off** until `ALERT_EMAIL_ENABLED=true` and the SMTP "
            "settings are in the gitignored `.env` (see `.env.example`). Alerts are "
            "written to `monitoring/state/alert_log.jsonl` either way, so monitoring "
            "is never silently disabled by a missing setting. Full detail in "
            "`monitoring/README.md`."
        )

    st.divider()
    _section("Can the code load? (import checks)")
    st.caption(
        "Smoke tests that the pipeline's entry points import and the DB tables are "
        "populated. These answer *can the code load*, not *is the data fresh* — "
        "freshness is the Pipeline health panel above."
    )

    def check(label: str, fn):
        try:
            ok, msg = True, str(fn())
        except Exception as e:
            ok, msg = False, str(e)
        st.markdown(ui.check_row_html(ok, label, msg), unsafe_allow_html=True)

    def db_prices():
        row = pd.read_sql("SELECT COUNT(*) as n, MAX(date) as latest FROM prices",
                          get_connection()).iloc[0]
        return f"{int(row['n']):,} rows | latest: {row['latest']}"

    def db_fundamentals():
        row = pd.read_sql("SELECT COUNT(*) as n, MAX(available_date) as latest FROM fundamentals",
                          get_connection()).iloc[0]
        return f"{int(row['n']):,} rows | latest available_date: {row['latest']}"

    def db_factor_scores():
        row = pd.read_sql("SELECT COUNT(*) as n, MAX(calculation_date) as latest FROM factor_scores",
                          get_connection()).iloc[0]
        n = int(row["n"])
        if n == 0:
            return "0 rows — run the screen to populate"
        return f"{n:,} rows | latest: {row['latest']}"

    def universe_check():
        from data.universe_builder import get_universe
        return f"{len(get_universe())} tickers from IWM CSV"

    def fscore_check():
        from factors.quality import calculate_piotroski_fscore  # noqa: F401
        return "calculate_piotroski_fscore available"

    def sharpe_check():
        from backtesting.metrics import calculate_sharpe  # noqa: F401
        return "calculate_sharpe available"

    def value_check():
        from factors.value import calculate_value_factors  # noqa: F401
        return "calculate_value_factors available"

    def momentum_check():
        from factors.momentum import calculate_momentum_factors  # noqa: F401
        return "calculate_momentum_factors available"

    def engine_check():
        from backtesting.engine import run_backtest  # noqa: F401
        return "run_backtest available"

    check("Database — prices",        db_prices)
    check("Database — fundamentals",  db_fundamentals)
    check("Database — factor scores", db_factor_scores)
    check("Universe",                 universe_check)
    check("factors.quality — calculate_piotroski_fscore", fscore_check)
    check("backtesting.metrics — calculate_sharpe",       sharpe_check)
    check("factors.value — calculate_value_factors",      value_check)
    check("factors.momentum — calculate_momentum_factors", momentum_check)
    check("backtesting.engine — run_backtest",            engine_check)


# ══════════════════════════════════════════════════════════════════
# NAVIGATION — grouped sections; Today is the home page
# ══════════════════════════════════════════════════════════════════
def _page_today():
    from dashboard import today as _today
    _today.render(st, go, FT_DIR, ACTIVE_FT_PERIOD)


_SECTIONS = {
    "Overview": [("Today", _page_today, ":material/today:", "today")],
    "Portfolio": [("Stock Picks", _page_stock_picks, ":material/format_list_bulleted:", "picks"),
                  ("Forward Test", _page_forward_test, ":material/show_chart:", "forward-test"),
                  ("Execution", _page_execution, ":material/receipt_long:", "execution"),
                  ("Risk Monitor", _page_risk, ":material/shield:", "risk")],
    "Research": [("Run Screen", _page_run_screen, ":material/tune:", "screen"),
                 ("Universe Explorer", _page_universe, ":material/travel_explore:", "universe"),
                 ("Macro & Regime", _page_macro, ":material/public:", "macro"),
                 ("Economic Indicators", _page_economic, ":material/insights:", "economy")],
    "System": [("System Health", _page_health, ":material/monitor_heart:", "health")],
    "Archive": [("Backtest Results", _page_backtest, ":material/history:", "backtest"),
                ("Learning & Performance", _page_learning, ":material/inventory_2:", "learning")],
}


def _sidebar_footer():
    with st.sidebar.expander("About this system", expanded=False):
        st.markdown(
            "**Incumbent-minus-ML**, the deployed construction: 9-factor weights "
            "(value, quality, momentum and six smaller signals; sentiment and revenue "
            "acceleration are currently the same for every stock, so 84% of the weight "
            "ranks), health gate F-score ≥ 5, "
            "regime sector tilt, ML overlay disabled. Weights 0.22 value / 0.27 quality / "
            "0.16 momentum / ...; ranking ensemble_rank TIER1/TIER2. Restored 2026-09-21 "
            "(`42a6daf`) per the 2026Q3 verdict (`b69c444`).  \n"
            f"Forward test **{ACTIVE_FT_PERIOD}** · paper account only · real money is "
            "not authorised.  \n"
            "Archive pages show retired results, kept for reference.")


st.markdown(ui.css(), unsafe_allow_html=True)

if hasattr(st, "navigation"):
    _navigation = st.navigation({
        sec: [st.Page(fn, title=t, icon=icon, url_path=slug, default=(t == "Today"))
              for t, fn, icon, slug in pages]
        for sec, pages in _SECTIONS.items()})
    _sidebar_footer()
    _navigation.run()
else:                                     # older Streamlit: the same pages, one list
    _flat = [(t, fn) for pages in _SECTIONS.values() for t, fn, _i, _s in pages]
    _choice = st.sidebar.radio("Navigate", [t for t, _ in _flat])
    _sidebar_footer()
    dict(_flat)[_choice]()
