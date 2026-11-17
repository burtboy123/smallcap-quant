"""
agents/sentiment_agent.py

Earnings transcript sentiment analysis using the Claude API (claude-haiku).

Scores management tone, guidance direction, and red flags.
Results cached in the sentiment_scores table for 90 days to minimise API cost.
Approximate cost: $0.01-0.03 per ticker per analysis.

Score ranges:
  overall_sentiment -1.0 to +1.0   (negative to positive)
  management_confidence 0.0 to 1.0 (uncertain to highly confident)
  guidance_direction: raised | maintained | lowered | mixed | none
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

from loguru import logger

from config.settings import ANTHROPIC_API_KEY, SENTIMENT, DATABASE_URL
from agents.transcript_fetcher import fetch_transcript_text


# ---------------------------------------------------------------------------
# Claude prompt
# ---------------------------------------------------------------------------

_PROMPT = """Analyse this earnings call transcript or SEC filing and return ONLY a JSON object.

JSON format (no other text, no markdown):
{{
  "overall_sentiment": <float -1.0 to 1.0>,
  "guidance_direction": "<raised|maintained|lowered|mixed|none>",
  "management_confidence": <float 0.0 to 1.0>,
  "red_flags": ["<concerning phrase, max 5, empty list if none>"],
  "key_positives": ["<positive signal, max 5, empty list if none>"],
  "summary": "<2 sentence summary of the quarter and outlook>"
}}

Scoring guide:
- overall_sentiment: 1.0=very positive, 0=neutral, -1.0=very negative
- management_confidence: 1.0=highly confident, 0=very uncertain/hedging
- red_flags: revenue decline, margin pressure, guidance cut, executive departure,
  debt concerns, customer loss, "challenging environment"
- key_positives: beat expectations, raised guidance, new contracts,
  market share gains, margin expansion, strong demand

Transcript:
{transcript}"""


def _db_path() -> str:
    return DATABASE_URL.replace("sqlite:///", "")


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def get_cached_sentiment(ticker: str, max_age_days: int = 90) -> dict | None:
    """Return most recent cached sentiment if within max_age_days, else None."""
    cutoff = date.today() - timedelta(days=max_age_days)
    conn = sqlite3.connect(_db_path())
    try:
        cursor = conn.execute(
            """
            SELECT overall_sentiment, guidance_direction,
                   management_confidence, red_flags,
                   key_positives, summary, transcript_date
            FROM sentiment_scores
            WHERE ticker = ?
              AND available_date >= ?
            ORDER BY transcript_date DESC
            LIMIT 1
            """,
            (ticker, str(cutoff)),
        )
        row = cursor.fetchone()
        if row:
            return {
                "overall_sentiment":    row[0],
                "guidance_direction":   row[1],
                "management_confidence": row[2],
                "red_flags":     json.loads(row[3]) if row[3] else [],
                "key_positives": json.loads(row[4]) if row[4] else [],
                "summary":       row[5],
                "transcript_date": row[6],
                "from_cache":    True,
            }
    finally:
        conn.close()
    return None


def save_sentiment(ticker: str, transcript_date: str, result: dict) -> None:
    """Persist a sentiment result to the database."""
    conn = sqlite3.connect(_db_path())
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO sentiment_scores
                (ticker, transcript_date, available_date, overall_sentiment,
                 guidance_direction, management_confidence,
                 red_flags, key_positives, summary, model_used)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ticker,
                transcript_date,
                str(date.today()),
                result.get("overall_sentiment", 0.0),
                result.get("guidance_direction", "none"),
                result.get("management_confidence", 0.5),
                json.dumps(result.get("red_flags", [])),
                json.dumps(result.get("key_positives", [])),
                result.get("summary", ""),
                SENTIMENT["model"],
            ),
        )
        conn.commit()
        score = result.get("overall_sentiment", 0.0)
        logger.info(f"{ticker}: Sentiment saved — {score:+.2f}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Hallucination guard
# ---------------------------------------------------------------------------

_HALLUCINATION_TRIGGERS = [
    "delist", "bankrupt", "chapter 11", "chapter 7",
    "suspended", "fraud", "sec investigation", "going concern",
    "liquidat",
]


def _validate_hallucinations(ticker: str, result: dict) -> dict:
    """
    Cross-check critical factual claims against yfinance before saving.
    If Claude flags bankruptcy/delisting for an actively-trading company,
    neutralise the hallucinated red flags and moderate the sentiment score.
    """
    red_flags = result.get("red_flags", [])
    summary   = result.get("summary", "").lower()

    triggered = [
        t for t in _HALLUCINATION_TRIGGERS
        if t in summary or any(t in f.lower() for f in red_flags)
    ]
    if not triggered:
        return result

    try:
        import yfinance as yf
        info       = yf.Ticker(ticker).info
        market_cap = info.get("marketCap", 0)
        quote_type = info.get("quoteType", "")

        if market_cap > 50_000_000 and quote_type == "EQUITY":
            logger.warning(
                f"{ticker}: Sentiment flagged {triggered} but yfinance shows "
                f"active ${market_cap/1e9:.1f}B company — neutralising hallucinated flags."
            )
            result["red_flags"] = [
                f for f in red_flags
                if not any(t in f.lower() for t in _HALLUCINATION_TRIGGERS)
            ]
            if result.get("overall_sentiment", 0) < -0.3:
                result["overall_sentiment"] = -0.1
            result["hallucination_corrected"] = True

    except Exception as e:
        logger.warning(f"{ticker}: Hallucination check could not verify — {e}")

    return result


# ---------------------------------------------------------------------------
# Single-ticker analysis
# ---------------------------------------------------------------------------

def analyse_sentiment(ticker: str) -> dict | None:
    """
    Return sentiment dict for a ticker.
    Uses cache if available (within cache_days). Calls Claude API if not.
    Returns None if transcript unavailable or API call fails.
    """
    # 1. Check cache
    cached = get_cached_sentiment(ticker, SENTIMENT["cache_days"])
    if cached:
        logger.debug(f"{ticker}: Using cached sentiment (date={cached['transcript_date']})")
        return cached

    # 2. Fetch transcript text
    transcript = fetch_transcript_text(ticker)
    if not transcript:
        logger.warning(f"{ticker}: No transcript available")
        return None

    # 3. Require API key
    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not set in .env — cannot call Claude API")
        return None

    # 4. Call Claude
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        message = client.messages.create(
            model=SENTIMENT["model"],
            max_tokens=1000,
            messages=[{
                "role": "user",
                "content": _PROMPT.format(
                    transcript=transcript[: SENTIMENT["max_transcript_chars"]]
                ),
            }],
        )

        raw = message.content[0].text.strip()

        # Strip markdown code fence if present
        if "```" in raw:
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        result = json.loads(raw)

        required = [
            "overall_sentiment", "guidance_direction",
            "management_confidence", "red_flags",
            "key_positives", "summary",
        ]
        missing = [f for f in required if f not in result]
        if missing:
            raise ValueError(f"Claude response missing fields: {missing}")

        result = _validate_hallucinations(ticker, result)
        save_sentiment(ticker, str(date.today()), result)
        return result

    except json.JSONDecodeError as e:
        logger.error(f"{ticker}: Claude returned invalid JSON — {e}")
        return None
    except Exception as e:
        logger.error(f"{ticker}: Sentiment analysis failed — {e}")
        return None


# ---------------------------------------------------------------------------
# Batch analysis
# ---------------------------------------------------------------------------

def analyse_portfolio_sentiment(tickers: list[str]) -> dict[str, dict]:
    """
    Analyse sentiment for a list of tickers.
    Returns {ticker: result} for tickers where analysis succeeded.
    """
    results: dict[str, dict] = {}
    for i, ticker in enumerate(tickers):
        logger.info(f"Sentiment {i+1}/{len(tickers)}: {ticker}")
        result = analyse_sentiment(ticker)
        if result:
            results[ticker] = result
    logger.info(f"Sentiment complete: {len(results)}/{len(tickers)} tickers scored")
    return results


# ---------------------------------------------------------------------------
# Score extraction (used by composite layer)
# ---------------------------------------------------------------------------

def extract_scores(results: dict[str, dict]) -> dict[str, float]:
    """
    Extract overall_sentiment float from a batch results dict.
    Returns {ticker: overall_sentiment} for tickers with data.
    """
    return {
        ticker: r["overall_sentiment"]
        for ticker, r in results.items()
        if r.get("overall_sentiment") is not None
    }
