"""
tests/test_sentiment.py

Unit tests for the Phase 4 NLP sentiment pipeline.

Tests:
  - build_sentiment_score: percentile ranking of raw scores
  - calculate_composite_scores: sentiment integration, incl. the DB auto-fetch
  - agents.sentiment_agent (Claude on transcripts): cache round-trip, every
    failure path returning None, response parsing, the hallucination guard
  - extract_scores
  - news_fetcher: NewsItem / TickerNews helpers

All tests use synthetic data. No API call is made (the Anthropic client is
faked) and the only database is a temporary SQLite file.

Rewritten 2026-10-04: 12 of the previous tests imported SentimentResult and
analyze_ticker_sentiment from the old FinBERT/news agent, which no longer
exist, and 4 composite tests used fixtures that the f_score>=5 / roic gates
excluded. All 16 sat in monitoring/known_test_failures.txt, so NOTHING was
testing the sentiment layer that carries 11% of the Q4b composite.
"""

import sys
import unittest
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

# Ensure project root is on sys.path
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_factor_df(tickers: list[str], seed: int = 42) -> pd.DataFrame:
    """Create a minimal factor DataFrame for composite tests."""
    rng = np.random.default_rng(seed)
    n = len(tickers)
    return pd.DataFrame(
        {
            "ev_ebitda":      rng.uniform(5, 50, n),
            "pb_ratio":       rng.uniform(0.5, 5, n),
            "fcf_yield":      rng.uniform(0.01, 0.15, n),
            "earnings_yield": rng.uniform(0.01, 0.12, n),
            "roic":           rng.uniform(-0.05, 0.30, n),
            "gross_margin":   rng.uniform(0.10, 0.60, n),
            "f_score":        rng.integers(1, 9, n).astype(float),
            "accruals_ratio": rng.uniform(-0.10, 0.10, n),
            "momentum_12_1":  rng.uniform(-0.30, 0.80, n),
        },
        index=pd.Index(tickers, name="ticker"),
    )


# ---------------------------------------------------------------------------
# SentimentResult tests
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# build_sentiment_score tests
# ---------------------------------------------------------------------------

class TestBuildSentimentScore(unittest.TestCase):

    def setUp(self):
        from factors.composite import build_sentiment_score
        self.build = build_sentiment_score

    def test_basic_ranking(self):
        scores = {"A": 0.8, "B": 0.0, "C": -0.5}
        result = self.build(scores, ["A", "B", "C"])
        # A should rank highest, C lowest
        self.assertGreater(result["A"], result["B"])
        self.assertGreater(result["B"], result["C"])

    def test_missing_ticker_is_nan(self):
        scores = {"A": 0.5}
        result = self.build(scores, ["A", "B"])
        self.assertTrue(np.isnan(result["B"]))

    def test_empty_scores_all_nan(self):
        result = self.build({}, ["A", "B", "C"])
        self.assertTrue(result.isna().all())

    def test_scores_in_0_100_range(self):
        scores = {"A": 0.9, "B": 0.3, "C": -0.2, "D": -0.8}
        result = self.build(scores, ["A", "B", "C", "D"])
        valid = result.dropna()
        self.assertTrue((valid >= 0).all())
        self.assertTrue((valid <= 100).all())

    def test_single_ticker_gets_50(self):
        scores = {"X": 0.7}
        result = self.build(scores, ["X"])
        self.assertAlmostEqual(result["X"], 50.0)

    def test_returns_series_named_sentiment_score(self):
        result = self.build({"A": 0.5}, ["A"])
        self.assertEqual(result.name, "sentiment_score")


# ---------------------------------------------------------------------------
# calculate_composite_scores with sentiment tests
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Sentiment agent fallback tests (no real API call)
# ---------------------------------------------------------------------------

def _make_mock_pipeline(pos=0.7, neg=0.1, neu=0.2):
    """Return a callable that mimics a FinBERT pipeline response."""
    def mock_pipe(_text, **_kwargs):
        return [[
            {"label": "positive", "score": pos},
            {"label": "negative", "score": neg},
            {"label": "neutral",  "score": neu},
        ]]
    return mock_pipe


# ---------------------------------------------------------------------------
# NewsItem / TickerNews helpers tests
# ---------------------------------------------------------------------------

class TestNewsHelpers(unittest.TestCase):

    def _make_item(self, days_old=5, title="Test headline"):
        from agents.news_fetcher import NewsItem
        return NewsItem(
            ticker="TST",
            title=title,
            summary="Test summary " * 10,
            source="Reuters",
            published_at=datetime.utcnow() - timedelta(days=days_old),
        )

    def test_age_days_recent(self):
        item = self._make_item(days_old=3)
        # Allow up to 1 day of delta — utcnow() vs now() timezone drift
        self.assertAlmostEqual(item.age_days(), 3.0, delta=1.0)

    def test_age_days_no_date(self):
        from agents.news_fetcher import NewsItem
        item = NewsItem(ticker="X", title="t", summary="", source="S", published_at=None)
        self.assertEqual(item.age_days(), 999.0)

    def test_recent_items_filters_by_age(self):
        from agents.news_fetcher import TickerNews
        items = [
            self._make_item(days_old=5, title="New"),
            self._make_item(days_old=60, title="Old"),
        ]
        news = TickerNews(ticker="TST", items=items)
        recent = news.recent_items(max_age_days=30)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0].title, "New")

    def test_as_text_block_no_news(self):
        from agents.news_fetcher import TickerNews
        news = TickerNews(ticker="TST", items=[])
        text = news.as_text_block()
        self.assertIn("No recent news", text)

    def test_as_text_block_truncates_summary(self):
        from agents.news_fetcher import TickerNews
        item = self._make_item(days_old=1, title="Breaking news")
        item.summary = "A" * 500
        news = TickerNews(ticker="TST", items=[item])
        text = news.as_text_block(max_items=1)
        # Summary should be truncated to 200 chars + "..."
        self.assertIn("...", text)
        self.assertIn("Breaking news", text)

    def test_has_news_property(self):
        from agents.news_fetcher import TickerNews
        empty = TickerNews(ticker="X", items=[])
        self.assertFalse(empty.has_news)
        nonempty = TickerNews(ticker="X", items=[self._make_item()])
        self.assertTrue(nonempty.has_news)


# ---------------------------------------------------------------------------
# extract_scores helper tests
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# calculate_composite_scores with sentiment
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

TICKERS = ["A", "B", "C", "D", "E"]


@pytest.fixture
def factors():
    """Five names that all PASS the quality gates (f_score>=5, roic>=0.02,
    momentum present). The old fixture drew f_score from 1-8, so the gate
    excluded most names and the ranking tests compared one survivor to itself."""
    rng = np.random.default_rng(10)
    n = len(TICKERS)
    df = pd.DataFrame({
        "ev_ebitda": rng.uniform(5, 50, n), "pb_ratio": rng.uniform(0.5, 5, n),
        "fcf_yield": rng.uniform(0.01, 0.15, n),
        "earnings_yield": rng.uniform(0.01, 0.12, n),
        "roic": rng.uniform(0.05, 0.30, n), "gross_margin": rng.uniform(0.1, 0.6, n),
        "f_score": rng.integers(5, 10, n).astype(float),
        "accruals_ratio": rng.uniform(-0.1, 0.1, n),
        "momentum_12_1": rng.uniform(-0.3, 0.8, n),
    }, index=pd.Index(TICKERS, name="ticker"))
    return (df[["ev_ebitda", "pb_ratio", "fcf_yield", "earnings_yield"]],
            df[["roic", "gross_margin", "f_score", "accruals_ratio"]],
            df[["momentum_12_1"]])


@pytest.fixture
def no_autofetch(monkeypatch):
    import factors.composite as fc
    monkeypatch.setattr(fc, "SENTIMENT_AVAILABLE", False)
    return fc


def _ranks(df):
    return df["universe_rank"]


def test_all_gate_passing_names_are_ranked(factors, no_autofetch):
    out = no_autofetch.calculate_composite_scores(*factors)
    assert out["universe_rank"].notna().sum() == len(TICKERS)


def test_explicit_empty_sentiment_equals_none(factors, no_autofetch):
    fc = no_autofetch
    pd.testing.assert_series_equal(
        fc.calculate_composite_scores(*factors)["composite_score"],
        fc.calculate_composite_scores(*factors, sentiment_scores={})["composite_score"],
        check_names=False)


def test_sentiment_changes_rankings(factors, no_autofetch):
    fc = no_autofetch
    base = fc.calculate_composite_scores(*factors)
    worst = base["universe_rank"].idxmax()
    sent = {t: (0.95 if t == worst else -0.8) for t in TICKERS}
    out = fc.calculate_composite_scores(*factors, sentiment_scores=sent)
    assert "sentiment_score" in out.columns
    assert out.loc[worst, "universe_rank"] < base.loc[worst, "universe_rank"]


def test_partial_sentiment_coverage_still_ranks_everyone(factors, no_autofetch):
    out = no_autofetch.calculate_composite_scores(
        *factors, sentiment_scores={"A": 0.8, "B": 0.3, "C": -0.4})
    assert out["universe_rank"].notna().sum() == len(TICKERS)


def test_autofetch_with_no_real_data_leaves_rankings_unchanged(factors, monkeypatch):
    """No sentiment data (or an unreadable DB) fills EVERY name with neutral 50.0 and
    the auto-fetch then switches the sentiment layer ON. That is only acceptable if
    an all-constant layer cannot reorder anything -- pinned here."""
    import factors.composite as fc
    monkeypatch.setattr(fc, "SENTIMENT_AVAILABLE", False)
    base = fc.calculate_composite_scores(*factors)
    monkeypatch.setattr(fc, "SENTIMENT_AVAILABLE", True)
    monkeypatch.setattr(fc, "add_sentiment_scores",
                        lambda df: df.assign(sentiment_score=50.0))
    out = fc.calculate_composite_scores(*factors)
    pd.testing.assert_series_equal(_ranks(base), _ranks(out), check_names=False)


def test_autofetch_uses_database_scores_when_present(factors, monkeypatch):
    import factors.composite as fc
    monkeypatch.setattr(fc, "SENTIMENT_AVAILABLE", True)
    db = dict(zip(TICKERS, [90.0, 10.0, 50.0, 70.0, 30.0]))
    monkeypatch.setattr(fc, "add_sentiment_scores",
                        lambda df: df.assign(sentiment_score=df.index.map(db)))
    out = fc.calculate_composite_scores(*factors)
    assert out["sentiment_score"].notna().all()


# ---------------------------------------------------------------------------
# agents.sentiment_agent — Claude on earnings transcripts
# ---------------------------------------------------------------------------

_GOOD = {"overall_sentiment": 0.4, "guidance_direction": "raised",
         "management_confidence": 0.8, "red_flags": [],
         "key_positives": ["beat expectations"], "summary": "Solid quarter."}


@pytest.fixture
def agent(tmp_path, monkeypatch):
    """The agent pointed at a temporary DB whose table is built FROM schema.sql,
    the way database/init_db.py builds it, so the tests and the schema cannot drift."""
    import sqlite3
    import agents.sentiment_agent as sa
    db = tmp_path / "s.db"
    with sqlite3.connect(db) as c:
        c.execute(_schema_sql_create("sentiment_scores"))
    monkeypatch.setattr(sa, "DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setattr(sa, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(sa, "fetch_transcript_text", lambda t: "Transcript text.")
    return sa


def _schema_sql_create(table: str) -> str:
    """The CREATE TABLE statement for `table` from database/schema.sql, adapted to
    SQLite exactly as database/init_db.py does it."""
    import re
    from database.init_db import adapt_schema
    from pathlib import Path
    sql = (Path(__file__).resolve().parent.parent / "database" / "schema.sql").read_text(
        encoding="utf-8")
    sql = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
    sql = re.sub(r"--[^\n]*", "", sql)
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?\);", sql, re.S)
    assert m, f"{table} is not defined in database/schema.sql"
    return adapt_schema(m.group(0))


def test_schema_defines_every_column_the_agent_reads_and_writes(tmp_path):
    import sqlite3
    with sqlite3.connect(tmp_path / "x.db") as c:
        c.execute(_schema_sql_create("sentiment_scores"))
        cols = {r[1] for r in c.execute("PRAGMA table_info(sentiment_scores)")}
    assert {"ticker", "transcript_date", "available_date", "overall_sentiment",
            "guidance_direction", "management_confidence", "red_flags",
            "key_positives", "summary", "model_used"} <= cols


def _fake_claude(monkeypatch, text=None, raises=None):
    """Install a fake `anthropic` module; returns the list of calls made."""
    import types
    calls = []

    class _Msgs:
        def create(self, **kw):
            calls.append(kw)
            if raises:
                raise raises
            return types.SimpleNamespace(content=[types.SimpleNamespace(text=text)])

    class _Client:
        def __init__(self, api_key):
            self.messages = _Msgs()

    monkeypatch.setitem(sys.modules, "anthropic",
                        types.SimpleNamespace(Anthropic=_Client))
    return calls


def test_cache_round_trip(agent):
    agent.save_sentiment("AMN", "2026-09-30", dict(_GOOD))
    got = agent.get_cached_sentiment("AMN")
    assert got["from_cache"] and got["overall_sentiment"] == 0.4
    assert got["key_positives"] == ["beat expectations"]


def test_cache_older_than_max_age_is_ignored(agent):
    import sqlite3
    with sqlite3.connect(agent._db_path()) as c:
        c.execute("INSERT INTO sentiment_scores (ticker, transcript_date, "
                  "available_date, overall_sentiment, guidance_direction, "
                  "management_confidence, red_flags, key_positives, summary, model_used) "
                  "VALUES ('AMN','2026-01-01','2026-01-01',0.4,'raised',0.8,'[]','[]',"
                  "'x','m')")
    assert agent.get_cached_sentiment("AMN", max_age_days=90) is None


def test_cache_hit_makes_no_transcript_fetch_or_api_call(agent, monkeypatch):
    agent.save_sentiment("AMN", "2026-09-30", dict(_GOOD))
    monkeypatch.setattr(agent, "fetch_transcript_text",
                        lambda t: pytest.fail("fetched despite cache"))
    calls = _fake_claude(monkeypatch, text=json_dumps(_GOOD))
    assert agent.analyse_sentiment("AMN")["from_cache"]
    assert calls == []


def test_no_transcript_returns_none_without_calling_claude(agent, monkeypatch):
    monkeypatch.setattr(agent, "fetch_transcript_text", lambda t: None)
    calls = _fake_claude(monkeypatch, text=json_dumps(_GOOD))
    assert agent.analyse_sentiment("AMN") is None and calls == []


def test_missing_api_key_returns_none_without_calling_claude(agent, monkeypatch):
    monkeypatch.setattr(agent, "ANTHROPIC_API_KEY", "")
    calls = _fake_claude(monkeypatch, text=json_dumps(_GOOD))
    assert agent.analyse_sentiment("AMN") is None and calls == []


def test_fenced_json_is_parsed_saved_and_truncated_to_the_limit(agent, monkeypatch):
    monkeypatch.setattr(agent, "fetch_transcript_text", lambda t: "\u2588" * 100_000)
    calls = _fake_claude(monkeypatch, text="```json\n" + json_dumps(_GOOD) + "\n```")
    r = agent.analyse_sentiment("AMN")
    assert r["overall_sentiment"] == 0.4 and r["guidance_direction"] == "raised"
    assert agent.get_cached_sentiment("AMN") is not None            # persisted
    sent = calls[0]["messages"][0]["content"]
    assert sent.count("\u2588") == agent.SENTIMENT["max_transcript_chars"]
    assert calls[0]["model"] == agent.SENTIMENT["model"]


@pytest.mark.parametrize("text", ["not json at all", '{"overall_sentiment": 0.4}'])
def test_bad_or_incomplete_response_returns_none_and_saves_nothing(agent, monkeypatch,
                                                                   text):
    _fake_claude(monkeypatch, text=text)
    assert agent.analyse_sentiment("AMN") is None
    assert agent.get_cached_sentiment("AMN") is None


def test_api_error_returns_none(agent, monkeypatch):
    _fake_claude(monkeypatch, raises=RuntimeError("overloaded"))
    assert agent.analyse_sentiment("AMN") is None


def _yf_info(monkeypatch, info):
    import yfinance as yf
    monkeypatch.setattr(yf, "Ticker", lambda t: type("T", (), {"info": info})())


def test_hallucinated_bankruptcy_on_an_active_company_is_neutralised(agent, monkeypatch):
    _yf_info(monkeypatch, {"marketCap": 2_000_000_000, "quoteType": "EQUITY"})
    r = agent._validate_hallucinations("AMN", {
        **_GOOD, "overall_sentiment": -0.7,
        "red_flags": ["bankruptcy risk", "margin pressure"]})
    assert r["red_flags"] == ["margin pressure"]
    assert r["overall_sentiment"] == -0.1 and r["hallucination_corrected"]


def test_distress_flags_kept_for_a_tiny_or_non_equity_listing(agent, monkeypatch):
    _yf_info(monkeypatch, {"marketCap": 10_000_000, "quoteType": "EQUITY"})
    r = agent._validate_hallucinations("XYZ", {
        **_GOOD, "overall_sentiment": -0.7, "red_flags": ["going concern doubt"]})
    assert r["red_flags"] == ["going concern doubt"] and r["overall_sentiment"] == -0.7


def test_extract_scores_skips_missing_values():
    from agents.sentiment_agent import extract_scores
    assert extract_scores({"A": {"overall_sentiment": 0.3},
                           "B": {"overall_sentiment": None}, "C": {}}) == {"A": 0.3}


def json_dumps(obj):
    import json
    return json.dumps(obj)
