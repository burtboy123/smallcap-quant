"""
agents/earnings_surprise.py

Earnings Surprise History Signal.
Companies that beat estimates consistently keep beating them.
3 of last 4 quarters beating = strong positive signal.

Primary source:   yfinance (free)
Supplementary:    Finnhub (free tier, FINNHUB_API_KEY required)
When both available, the two scores are averaged.
"""

from __future__ import annotations

import pandas as pd
import yfinance as yf
from loguru import logger


# ---------------------------------------------------------------------------
# yfinance source
# ---------------------------------------------------------------------------

def get_earnings_surprise_yfinance(ticker: str) -> dict | None:
    """
    Fetch earnings surprise history via yfinance for the last 4 quarters.
    Returns None on failure so callers can fall back to Finnhub.
    """
    try:
        earnings = yf.Ticker(ticker).earnings_history

        if earnings is None or earnings.empty:
            return None

        recent = earnings.tail(4).copy()
        if recent.empty:
            return None

        if "epsActual" not in recent.columns or "epsEstimate" not in recent.columns:
            return None

        recent = recent.dropna(subset=["epsActual", "epsEstimate"])
        recent = recent[recent["epsEstimate"] != 0]
        if recent.empty:
            return None

        recent["surprise_pct"] = (
            (recent["epsActual"] - recent["epsEstimate"])
            / recent["epsEstimate"].abs()
        )

        beats       = int((recent["surprise_pct"] > 0).sum())
        avg_surp    = float(recent["surprise_pct"].mean())
        last_surp   = float(recent["surprise_pct"].iloc[-1]) if len(recent) > 0 else 0.0

        consecutive = 0
        for surp in reversed(recent["surprise_pct"].tolist()):
            if surp > 0:
                consecutive += 1
            else:
                break

        return {
            "ticker":            ticker,
            "beats_last_4q":     beats,
            "avg_surprise_pct":  round(avg_surp, 4),
            "consecutive_beats": consecutive,
            "last_surprise_pct": round(last_surp, 4),
            "source":            "yfinance",
        }

    except Exception as e:
        logger.debug(f"{ticker}: yfinance earnings surprise failed — {e}")
        return None


# ---------------------------------------------------------------------------
# Finnhub source
# ---------------------------------------------------------------------------

def get_earnings_surprise_finnhub(ticker: str) -> dict | None:
    """
    Supplementary earnings surprise data from Finnhub.
    Returns None when API key is missing or call fails.
    """
    try:
        import finnhub
        from config.settings import FINNHUB_API_KEY
        if not FINNHUB_API_KEY:
            return None

        client    = finnhub.Client(api_key=FINNHUB_API_KEY)
        surprises = client.company_earnings(ticker, limit=4)

        if not surprises:
            return None

        beats = sum(
            1 for s in surprises
            if s.get("actual") is not None
            and s.get("estimate") is not None
            and s["actual"] > s["estimate"]
        )

        surprise_pcts = []
        for s in surprises:
            est = s.get("estimate")
            act = s.get("actual")
            if est is not None and act is not None and est != 0:
                surprise_pcts.append((act - est) / abs(est))

        avg_surp  = sum(surprise_pcts) / len(surprise_pcts) if surprise_pcts else 0.0
        last_surp = surprise_pcts[-1] if surprise_pcts else 0.0

        consecutive = 0
        for s in surprises:
            act = s.get("actual")
            est = s.get("estimate")
            if act is not None and est is not None and act > est:
                consecutive += 1
            else:
                break

        return {
            "ticker":            ticker,
            "beats_last_4q":     beats,
            "avg_surprise_pct":  round(avg_surp, 4),
            "consecutive_beats": consecutive,
            "last_surprise_pct": round(last_surp, 4),
            "source":            "finnhub",
        }

    except Exception as e:
        logger.debug(f"{ticker}: Finnhub earnings failed — {e}")
        return None


# ---------------------------------------------------------------------------
# Merged entry point
# ---------------------------------------------------------------------------

def get_earnings_surprise_history(ticker: str) -> dict:
    """
    Fetch from both yfinance and Finnhub, averaging scores when both succeed.
    Falls back to whichever single source worked, or returns zero defaults.
    """
    yf_result = get_earnings_surprise_yfinance(ticker)
    fh_result = get_earnings_surprise_finnhub(ticker)

    if yf_result and fh_result:
        return {
            "ticker":            ticker,
            "beats_last_4q":     round((yf_result["beats_last_4q"] + fh_result["beats_last_4q"]) / 2),
            "avg_surprise_pct":  round((yf_result["avg_surprise_pct"] + fh_result["avg_surprise_pct"]) / 2, 4),
            "consecutive_beats": max(yf_result["consecutive_beats"], fh_result["consecutive_beats"]),
            "last_surprise_pct": round((yf_result["last_surprise_pct"] + fh_result["last_surprise_pct"]) / 2, 4),
            "source":            "yfinance+finnhub",
        }

    return yf_result or fh_result or {
        "ticker":            ticker,
        "beats_last_4q":     0,
        "avg_surprise_pct":  0.0,
        "consecutive_beats": 0,
        "last_surprise_pct": 0.0,
        "source":            "none",
    }


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def calculate_earnings_surprise_score(data: dict) -> float:
    """
    Convert earnings surprise history to 0-100 score.

    4/4 beats + large positive surprises = 90+
    0/4 beats = 20
    """
    beats       = data.get("beats_last_4q",     0)
    avg_surp    = data.get("avg_surprise_pct",  0.0)
    consecutive = data.get("consecutive_beats", 0)

    base = {4: 85.0, 3: 70.0, 2: 50.0, 1: 35.0, 0: 20.0}.get(beats, 50.0)

    if avg_surp > 0.10:    base = min(100.0, base + 10.0)
    elif avg_surp > 0.05:  base = min(100.0, base + 5.0)
    elif avg_surp < -0.05: base = max(0.0,   base - 10.0)

    if consecutive >= 3:   base = min(100.0, base + 8.0)
    elif consecutive >= 2: base = min(100.0, base + 4.0)

    return round(base, 2)


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------

def get_earnings_surprise_batch(tickers: list[str]) -> pd.DataFrame:
    """Fetch earnings surprise history for a list of tickers."""
    results = []
    for i, ticker in enumerate(tickers):
        if i % 50 == 0 and i > 0:
            logger.info(f"Earnings surprise: {i}/{len(tickers)}")
        data = get_earnings_surprise_history(ticker)
        data["earnings_surprise_score"] = calculate_earnings_surprise_score(data)
        results.append(data)
    return pd.DataFrame(results)
