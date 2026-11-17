"""
agents/transcript_fetcher.py

Fetches earnings-related text from SEC EDGAR (free, no API key required)
and falls back to yfinance news when EDGAR returns nothing usable.

Returns cleaned text suitable for Claude sentiment analysis.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup
from loguru import logger


EDGAR_SEARCH = "https://efts.sec.gov/LATEST/search-index"
HEADERS = {"User-Agent": "quant_system research@quant.com"}


def fetch_latest_transcript(ticker: str, lookback_days: int = 120) -> dict | None:
    """
    Fetch earnings press release text from SEC EDGAR 8-K filings.

    Algorithm:
      1. Search EDGAR for recent 8-K filings for the ticker.
      2. For each hit: get company CIK from _source.ciks[0], fetch the full
         submission text file to list all documents, find the EX-99 press
         release exhibit (contains the actual earnings data), fetch and
         return its cleaned text.
      3. Fall back to the main 8-K document if no EX-99 found but it has
         sufficient content.

    Returns dict with keys: text, filing_date, source — or None.
    """
    end_date   = date.today()
    start_date = end_date - timedelta(days=lookback_days)

    try:
        search_url = (
            f"https://efts.sec.gov/LATEST/search-index?"
            f"q=%22{ticker}%22"
            f"&forms=8-K"
            f"&dateRange=custom"
            f"&startdt={start_date.isoformat()}"
            f"&enddt={end_date.isoformat()}"
        )
        resp = requests.get(search_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        hits = resp.json().get("hits", {}).get("hits", [])

        if not hits:
            return None

        for hit in hits[:3]:
            try:
                source    = hit.get("_source", {})
                file_date = source.get("file_date", "")
                raw_id    = hit.get("_id", "")
                if not raw_id or ":" not in raw_id:
                    continue

                # _id format: "ACCESSION-NO:primary-filename.htm"
                accession = raw_id.split(":")[0]           # e.g. "0000832988-25-000228"
                adsh_clean = accession.replace("-", "")    # "000083298825000228"

                # Company CIK is in _source.ciks[0], NOT the accession prefix
                ciks = source.get("ciks", [])
                if not ciks:
                    continue
                cik = int(ciks[0])

                # Fetch the full submission text to enumerate all documents
                txt_url = (
                    f"https://www.sec.gov/Archives/edgar/data/"
                    f"{cik}/{adsh_clean}/{accession}.txt"
                )
                txt_resp = requests.get(txt_url, headers=HEADERS, timeout=15)
                if txt_resp.status_code != 200:
                    continue

                # Extract all FILENAME entries
                all_files = re.findall(r"<FILENAME>(.*)", txt_resp.text)
                htm_files = [f for f in all_files if f.endswith(".htm")]

                # Priority 1: EX-99 press release (standard naming)
                # Priority 2: Earnings/press release with non-standard naming
                # Priority 3: Any .htm that isn't XBRL viewer or main 8-K cover
                _XBRL_NOISE = {"xsd", "lab", "pre", "cal", "def", "R1", "report"}
                _RELEASE_PATTERNS = [
                    r"ex.?99",
                    r"earnings.?release",
                    r"press.?release",
                    r"earningsrelease",
                    r"pressrelease",
                    r"results",
                ]
                exhibit_file = None
                for pattern in _RELEASE_PATTERNS:
                    exhibit_file = next(
                        (f for f in htm_files if re.search(pattern, f, re.IGNORECASE)),
                        None,
                    )
                    if exhibit_file:
                        break

                # Last resort: first .htm that isn't XBRL noise or the cover page 8-K
                # (identified as the file whose name matches the accession number pattern)
                if not exhibit_file:
                    cover_name = raw_id.split(":")[1]  # primary 8-K document
                    exhibit_file = next(
                        (f for f in htm_files
                         if f != cover_name
                         and not any(x in f for x in _XBRL_NOISE)),
                        None,
                    )

                # Skip if no exhibit found — don't return the tiny cover page
                if not exhibit_file:
                    logger.debug(f"{ticker}: No exhibit in filing {accession} — trying next")
                    continue

                doc_url = (
                    f"https://www.sec.gov/Archives/edgar/data/"
                    f"{cik}/{adsh_clean}/{exhibit_file}"
                )
                doc_resp = requests.get(doc_url, headers=HEADERS, timeout=15)
                if doc_resp.status_code != 200:
                    continue

                soup = BeautifulSoup(doc_resp.text, "html.parser")
                for tag in soup(["script", "style", "meta", "link"]):
                    tag.decompose()
                text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()

                if len(text) < 500:
                    continue

                logger.info(
                    f"{ticker}: Fetched {len(text):,} chars from EDGAR "
                    f"({exhibit_file})"
                )
                return {
                    "text":        text,
                    "filing_date": file_date,
                    "source":      "SEC_EDGAR_8K_EXHIBIT",
                }

            except Exception as e:
                logger.debug(f"{ticker}: Filing attempt failed — {e}")
                continue

        return None

    except Exception as e:
        logger.warning(f"{ticker}: EDGAR fetch failed — {e}")
        return None


def fetch_transcript_text(ticker: str) -> str | None:
    """
    Main entry point. Returns cleaned text for Claude to analyse, or None.

    Tries:
    1. SEC EDGAR 8-K filing (free)
    2. yfinance recent news headlines + summaries (fallback)
    """
    result = fetch_latest_transcript(ticker)
    if result and result.get("text"):
        text = re.sub(r"\s+", " ", result["text"]).strip()
        return text[:15_000]

    # Fallback: build rich context from yfinance financial data
    try:
        import yfinance as yf
        tk = yf.Ticker(ticker)
        parts: list[str] = []

        # Business description
        info = tk.info or {}
        biz = info.get("longBusinessSummary", "")
        if biz:
            parts.append(f"Company: {info.get('longName', ticker)}\n{biz}")

        # Recent earnings history
        try:
            ed = tk.get_earnings_dates(limit=8)
            if ed is not None and not ed.empty:
                ed_text = ["Recent earnings history (EPS estimate vs actual):"]
                for dt, row in ed.iterrows():
                    est = row.get("EPS Estimate")
                    act = row.get("Reported EPS")
                    surp = row.get("Surprise(%)")
                    if act is not None:
                        ed_text.append(
                            f"  {str(dt)[:10]}: est={est:.2f}  actual={act:.2f}  "
                            f"surprise={surp:.1f}%" if all(
                                v is not None for v in [est, act, surp]
                            ) else f"  {str(dt)[:10]}: actual={act}"
                        )
                parts.append("\n".join(ed_text))
        except Exception:
            pass

        # Quarterly revenue and income trend
        try:
            qf = tk.quarterly_financials
            if qf is not None and not qf.empty:
                rows = []
                for metric in ["Total Revenue", "Gross Profit", "Net Income"]:
                    if metric in qf.index:
                        vals = qf.loc[metric].dropna().head(4)
                        vals_str = "  ".join(
                            f"{str(c)[:10]}: ${v/1e6:.1f}M" for c, v in vals.items()
                        )
                        rows.append(f"  {metric}: {vals_str}")
                if rows:
                    parts.append("Quarterly financials:\n" + "\n".join(rows))
        except Exception:
            pass

        # Analyst estimates
        try:
            analysis = tk.get_analyst_price_targets()
            if analysis is not None:
                parts.append(f"Analyst targets: {analysis}")
        except Exception:
            pass

        if parts:
            combined = "\n\n".join(parts)
            logger.debug(
                f"{ticker}: Using yfinance financial data fallback ({len(combined):,} chars)"
            )
            return combined[:15_000]

    except Exception:
        pass

    return None
