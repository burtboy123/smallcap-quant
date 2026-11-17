"""
agents/news_fetcher.py

Fetch recent news headlines and summaries for a list of tickers.

Data sources (in priority order):
1. yfinance news (free, no API key required) — primary source
2. Fallback: empty list (agent handles missing data gracefully)

Returns structured NewsItem objects ready for sentiment analysis.

Rate-limit discipline: 0.3s delay between tickers to avoid throttling.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from loguru import logger


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class NewsItem:
    ticker: str
    title: str
    summary: str
    source: str
    published_at: Optional[datetime]
    url: str = ""

    def age_days(self, reference_date: Optional[date] = None) -> float:
        """Days between published_at and reference_date (or today)."""
        if self.published_at is None:
            return 999.0
        ref = datetime.combine(reference_date or date.today(), datetime.min.time())
        return (ref - self.published_at).total_seconds() / 86400.0


@dataclass
class TickerNews:
    ticker: str
    items: list[NewsItem] = field(default_factory=list)
    fetch_timestamp: datetime = field(default_factory=datetime.utcnow)
    error: Optional[str] = None

    @property
    def has_news(self) -> bool:
        return len(self.items) > 0

    def recent_items(self, max_age_days: int = 30) -> list[NewsItem]:
        return [n for n in self.items if n.age_days() <= max_age_days]

    def as_text_block(self, max_items: int = 5, max_age_days: int = 30) -> str:
        """Format news as a text block for the LLM prompt."""
        recent = self.recent_items(max_age_days)[:max_items]
        if not recent:
            return f"No recent news available for {self.ticker}."
        lines = [f"Recent news for {self.ticker}:"]
        for i, item in enumerate(recent, 1):
            age = item.age_days()
            age_str = f"{int(age)}d ago" if age < 999 else "date unknown"
            lines.append(f"  {i}. [{age_str}] {item.title}")
            if item.summary and item.summary != item.title:
                # Truncate summary to 200 chars
                summary = item.summary[:200].rstrip()
                if len(item.summary) > 200:
                    summary += "..."
                lines.append(f"     {summary}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# yfinance news fetcher
# ---------------------------------------------------------------------------

def fetch_news_yfinance(ticker: str, max_items: int = 10) -> TickerNews:
    """
    Fetch recent news for a single ticker via yfinance.

    Args:
        ticker:    Stock ticker symbol.
        max_items: Maximum number of news items to return.

    Returns:
        TickerNews object.
    """
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        raw_news = t.news or []
    except Exception as e:
        logger.debug(f"[{ticker}] yfinance news fetch failed: {e}")
        return TickerNews(ticker=ticker, error=str(e))

    items: list[NewsItem] = []
    for article in raw_news[:max_items]:
        try:
            # yfinance 1.x news structure
            content = article.get("content", {})

            # Title
            title = (
                content.get("title")
                or article.get("title")
                or ""
            )

            # Summary / description
            summary = (
                content.get("summary")
                or content.get("description")
                or article.get("summary")
                or ""
            )

            # Source
            provider = content.get("provider", {})
            source = (
                provider.get("displayName")
                or provider.get("name")
                or article.get("publisher")
                or "Unknown"
            )

            # Published timestamp
            pub_ts = (
                content.get("pubDate")
                or article.get("providerPublishTime")
                or article.get("published")
            )
            if isinstance(pub_ts, (int, float)):
                published_at = datetime.utcfromtimestamp(pub_ts)
            elif isinstance(pub_ts, str):
                try:
                    published_at = datetime.fromisoformat(pub_ts.replace("Z", "+00:00"))
                    published_at = published_at.replace(tzinfo=None)
                except ValueError:
                    published_at = None
            else:
                published_at = None

            # URL
            url = (
                content.get("canonicalUrl", {}).get("url", "")
                or content.get("clickThroughUrl", {}).get("url", "")
                or article.get("link", "")
            )

            if not title:
                continue

            items.append(NewsItem(
                ticker=ticker,
                title=title,
                summary=summary,
                source=source,
                published_at=published_at,
                url=url,
            ))
        except Exception as e:
            logger.debug(f"[{ticker}] Error parsing news item: {e}")
            continue

    logger.debug(f"[{ticker}] Fetched {len(items)} news items via yfinance")
    return TickerNews(ticker=ticker, items=items)


# ---------------------------------------------------------------------------
# Batch fetcher
# ---------------------------------------------------------------------------

def fetch_news_batch(
    tickers: list[str],
    max_items_per_ticker: int = 8,
    rate_limit_delay: float = 0.3,
) -> dict[str, TickerNews]:
    """
    Fetch news for a list of tickers with rate limiting.

    Args:
        tickers:              List of ticker symbols.
        max_items_per_ticker: Max articles per ticker.
        rate_limit_delay:     Seconds to wait between tickers.

    Returns:
        {ticker: TickerNews} dict.
    """
    results: dict[str, TickerNews] = {}

    logger.info(f"Fetching news for {len(tickers)} tickers...")

    for i, ticker in enumerate(tickers):
        results[ticker] = fetch_news_yfinance(ticker, max_items=max_items_per_ticker)

        if (i + 1) % 10 == 0:
            logger.info(f"  News fetch progress: {i+1}/{len(tickers)}")

        if i < len(tickers) - 1:
            time.sleep(rate_limit_delay)

    n_with_news = sum(1 for v in results.values() if v.has_news)
    logger.info(f"News fetch complete: {n_with_news}/{len(tickers)} tickers have news")

    return results
