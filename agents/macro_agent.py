"""
agents/macro_agent.py

Macro Agent — detects current market regime and adjusts
factor weights accordingly. Free data only (yfinance).
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import yfinance as yf
from loguru import logger

from agents.economic_agent import get_full_economic_snapshot


# ---------------------------------------------------------------------------
# Regime definitions and their optimal factor weights
# ---------------------------------------------------------------------------

REGIME_WEIGHTS: dict[str, dict] = {
    # Aux signals (short_interest, insider, earnings_surprise) have fixed
    # small weights across all regimes; the core 4 factors are scaled.
    "RISK_ON_GROWTH": {
        "value": 0.17, "quality": 0.25, "momentum": 0.30, "sentiment": 0.13,
        "short_interest": 0.08, "insider": 0.04, "earnings_surprise": 0.03,
        "description": "Bull market — momentum leads",
    },
    "RISK_OFF_DEFENSIVE": {
        "value": 0.34, "quality": 0.38, "momentum": 0.04, "sentiment": 0.09,
        "short_interest": 0.08, "insider": 0.04, "earnings_surprise": 0.03,
        "description": "Fear/crisis — quality and value lead",
    },
    "INFLATION_SHOCK": {
        "value": 0.38, "quality": 0.30, "momentum": 0.09, "sentiment": 0.08,
        "short_interest": 0.08, "insider": 0.04, "earnings_surprise": 0.03,
        "description": "High inflation — deep value leads",
    },
    "RECOVERY": {
        "value": 0.25, "quality": 0.25, "momentum": 0.21, "sentiment": 0.13,
        "short_interest": 0.08, "insider": 0.04, "earnings_surprise": 0.04,
        "description": "Post-recession recovery — balanced",
    },
    "NEUTRAL": {
        "value": 0.25, "quality": 0.30, "momentum": 0.18, "sentiment": 0.12,
        "short_interest": 0.08, "insider": 0.04, "earnings_surprise": 0.03,
        "description": "Normal conditions — base weights",
    },
}

_MACRO_TICKERS = {
    "SPY": "SPY",     # S&P 500
    "IWM": "IWM",     # Russell 2000
    "TLT": "TLT",     # 20yr Treasury
    "HYG": "HYG",     # High Yield Credit
    "GLD": "GLD",     # Gold
    "USO": "USO",     # Oil
    "VIX": "^VIX",    # Fear gauge
    "DXY": "DX-Y.NYB",  # Dollar index
}


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------

def fetch_macro_data() -> dict[str, pd.Series]:
    """
    Fetch macro indicators from yfinance. No API key required.
    Returns {key: Close price Series}.
    """
    logger.info("Fetching macro indicators...")
    end   = date.today()
    start = end - timedelta(days=400)  # Extra buffer so 200-day MA is available

    indicators: dict[str, pd.Series] = {}
    for key, ticker in _MACRO_TICKERS.items():
        try:
            df = yf.Ticker(ticker).history(
                start=start.isoformat(),
                end=end.isoformat(),
            )
            if not df.empty:
                indicators[key] = df["Close"]
        except Exception as e:
            logger.warning(f"{key} ({ticker}): fetch failed — {e}")

    return indicators


# ---------------------------------------------------------------------------
# Sector confirmation helpers
# ---------------------------------------------------------------------------

def get_energy_sector_outperforming() -> bool:
    """
    Check if energy stocks are actually outperforming the broader market.
    Oil shock means nothing if energy stocks aren't benefiting.

    [NEUTRAL — UNVALIDATED HARMLESS GUARD] Walk-forward validation (2005-2026,
    sandbox/validate_regime_rules.py): only 27 oil-shock months in 21 years and
    just n=4 in the decisive "XLE lags -> suppress INFLATION_SHOCK" branch — too
    few to validate. The premise (XLE leadership predicts durable energy/value
    outperformance) is NOT supported: energy mean-reverts after oil shocks
    (forward XLE-SPY negative whether XLE leads or lags). Faint directional support
    only — XLE-lagging shocks fare worse forward (-6.3% vs -1.2%). Keep as a sanity
    guard; do not credit as a validated improvement. See docs/regime_rules_validation.md.
    """
    try:
        end   = date.today()
        start = end - timedelta(days=63)

        xle = yf.Ticker("XLE").history(start=start.isoformat(), end=end.isoformat())
        spy = yf.Ticker("SPY").history(start=start.isoformat(), end=end.isoformat())

        if xle.empty or spy.empty:
            return False

        xle_return = float(xle["Close"].iloc[-1] / xle["Close"].iloc[0] - 1)
        spy_return = float(spy["Close"].iloc[-1] / spy["Close"].iloc[0] - 1)
        return xle_return > spy_return

    except Exception as e:
        logger.warning(f"Energy sector check failed — {e}")
        return False


def get_tech_sector_leading() -> bool:
    """
    Check if technology is leading the market by 10%+ on a 3-month basis.
    Tech leadership overrides oil shock — market is in growth mode, not inflation mode.

    [UNVALIDATED — CURVE-FIT GUARD] Walk-forward validation (2005-2026,
    sandbox/validate_regime_rules.py) found:
      - The broad trigger `xlk_3m > spy_3m*1.1` fires ~57% of months and shows NO
        forward discrimination: growth-minus-value over the next quarter is the same
        whether it fires (+0.94%) or not (+0.80%).
      - The specific oil-shock OVERRIDE this feeds (see classify_enhanced_regime)
        fired exactly ONCE in 21 years (2025-2026), i.e. it is fit to its origin
        window (n=1) and cannot be validated.
    Do NOT treat this as a general regime signal. Kept only as a low-frequency
    guard against the Mar-Jun 2026 INFLATION_SHOCK misfire. See
    docs/regime_rules_validation.md.
    """
    try:
        end   = date.today()
        start = end - timedelta(days=63)

        xlk = yf.Ticker("XLK").history(start=start.isoformat(), end=end.isoformat())
        spy = yf.Ticker("SPY").history(start=start.isoformat(), end=end.isoformat())

        if xlk.empty or spy.empty:
            return False

        xlk_return = float(xlk["Close"].iloc[-1] / xlk["Close"].iloc[0] - 1)
        spy_return = float(spy["Close"].iloc[-1] / spy["Close"].iloc[0] - 1)
        return xlk_return > spy_return * 1.1  # Tech outperforming by 10%+

    except Exception as e:
        logger.warning(f"Tech sector check failed — {e}")
        return False


# ---------------------------------------------------------------------------
# Supplementary: Finnhub news sentiment
# ---------------------------------------------------------------------------

def get_market_news_sentiment() -> dict:
    """
    Supplementary market sentiment signal from Finnhub general news.
    Supporting signal only — never overrides price-based regime signals alone.
    """
    try:
        import finnhub
        from config.settings import FINNHUB_API_KEY
        if not FINNHUB_API_KEY:
            return {"news_sentiment": 0.0, "news_signal": "neutral", "articles_analysed": 0}

        client = finnhub.Client(api_key=FINNHUB_API_KEY)
        news   = client.general_news("general", min_id=0)

        if not news:
            return {"news_sentiment": 0.0, "news_signal": "neutral", "articles_analysed": 0}

        sentiments = [
            n["sentiment"] for n in news[:20]
            if n.get("sentiment") is not None
        ]

        if not sentiments:
            return {"news_sentiment": 0.0, "news_signal": "neutral", "articles_analysed": 0}

        avg    = sum(sentiments) / len(sentiments)
        signal = "positive" if avg > 0.2 else "negative" if avg < -0.2 else "neutral"

        return {
            "news_sentiment":    round(avg, 3),
            "news_signal":       signal,
            "articles_analysed": len(sentiments),
        }

    except Exception as e:
        logger.debug(f"Finnhub news sentiment failed — {e}")
        return {"news_sentiment": 0.0, "news_signal": "neutral", "articles_analysed": 0}


# ---------------------------------------------------------------------------
# Regime detection
# ---------------------------------------------------------------------------

def detect_regime(indicators: dict[str, pd.Series]) -> dict:
    """
    Classify current macro regime from indicator signals.

    Priority order:
      1. Extreme fear / credit stress → RISK_OFF_DEFENSIVE
      2. Oil shock → INFLATION_SHOCK
      3. Bull + small-cap leadership + calm → RISK_ON_GROWTH
      4. Below 200MA but recovering → RECOVERY
      5. Otherwise → NEUTRAL
    """
    signals: dict = {}

    # VIX — fear gauge
    if "VIX" in indicators and len(indicators["VIX"]) > 0:
        vix = float(indicators["VIX"].iloc[-1])
    else:
        vix = 20.0
    signals["vix"]          = vix
    signals["fear"]         = vix > 25
    signals["extreme_fear"] = vix > 35

    # SPY — bull/bear trend via 200-day MA
    if "SPY" in indicators and len(indicators["SPY"]) > 200:
        spy = indicators["SPY"]
        ma200   = float(spy.rolling(200).mean().iloc[-1])
        current = float(spy.iloc[-1])
        signals["above_200ma"]      = current > ma200
        signals["spy_return_1m"]    = float(spy.iloc[-1] / spy.iloc[-21] - 1) if len(spy) > 21 else 0.0
        signals["spy_return_3m"]    = float(spy.iloc[-1] / spy.iloc[-63] - 1) if len(spy) > 63 else 0.0
    else:
        signals["above_200ma"]   = True
        signals["spy_return_1m"] = 0.0
        signals["spy_return_3m"] = 0.0

    # HYG — credit stress (down 3%+ in a month)
    if "HYG" in indicators and len(indicators["HYG"]) > 21:
        hyg = indicators["HYG"]
        signals["credit_stress"] = float(hyg.iloc[-1] / hyg.iloc[-21] - 1) < -0.03
    else:
        signals["credit_stress"] = False

    # USO — oil shock (up 20%+ in 3 months)
    if "USO" in indicators and len(indicators["USO"]) > 63:
        uso = indicators["USO"]
        oil_3m = float(uso.iloc[-1] / uso.iloc[-63] - 1)
        signals["oil_shock"]      = oil_3m > 0.20
        signals["oil_return_3m"]  = oil_3m
    else:
        signals["oil_shock"]     = False
        signals["oil_return_3m"] = 0.0

    # Finnhub news sentiment — supporting signal only
    news = get_market_news_sentiment()
    signals["news_sentiment"]    = news["news_sentiment"]
    signals["news_signal"]       = news["news_signal"]
    signals["articles_analysed"] = news["articles_analysed"]

    # IWM vs SPY — small-cap leading
    if "IWM" in indicators and "SPY" in indicators:
        iwm, spy_ = indicators["IWM"], indicators["SPY"]
        if len(iwm) > 63 and len(spy_) > 63:
            iwm_ret = float(iwm.iloc[-1] / iwm.iloc[-63] - 1)
            spy_ret = float(spy_.iloc[-1] / spy_.iloc[-63] - 1)
            signals["small_cap_leading"] = iwm_ret > spy_ret
        else:
            signals["small_cap_leading"] = False
    else:
        signals["small_cap_leading"] = False

    # Classify — news_signal strengthens toward RISK_OFF only when fear is already present
    news_negative = signals.get("news_signal") == "negative"

    if signals["extreme_fear"] or signals["credit_stress"]:
        regime = "RISK_OFF_DEFENSIVE"
    elif signals["oil_shock"]:
        regime = "INFLATION_SHOCK"
    elif (signals["above_200ma"]
          and signals["small_cap_leading"]
          and not signals["fear"]):
        regime = "RISK_ON_GROWTH"
    elif (not signals["above_200ma"]
          and signals.get("spy_return_3m", 0) > 0.05):
        regime = "RECOVERY"
    elif signals["fear"] and news_negative:
        # Supporting signal confirms fear — strengthen to defensive
        regime = "RISK_OFF_DEFENSIVE"
    else:
        regime = "NEUTRAL"

    return {
        "regime":      regime,
        "signals":     signals,
        "weights":     REGIME_WEIGHTS[regime],
        "description": REGIME_WEIGHTS[regime]["description"],
        "as_of":       str(date.today()),
    }


# ---------------------------------------------------------------------------
# Enhanced regime classification (market + economic signals)
# ---------------------------------------------------------------------------

def classify_enhanced_regime(signals: dict) -> str:
    """
    Combine market signals (VIX, 200MA, HYG, oil) with economic signals
    (yield curve, real rates, PMI, consumer) for more robust regime detection.

    Sector confirmation rules:
    - INFLATION_SHOCK requires oil_shock AND energy sector actually outperforming.
      Oil price and energy stocks have decoupled in 2026 — oil alone is insufficient.
    - Tech leadership (XLK outperforming SPY by 10%+) + above 200MA = RISK_ON_GROWTH,
      regardless of oil shock. Market is voting growth, not inflation.
    """
    # Recession warning — highest priority defensive signal
    if (signals.get("recession_warning")
            and signals.get("yield_curve_inverted")
            and (signals.get("credit_stress") or signals.get("fred_credit_stress"))):
        return "RISK_OFF_DEFENSIVE"

    # Extreme fear or credit stress → defensive
    if signals.get("extreme_fear") or signals.get("credit_stress") or signals.get("fred_credit_stress"):
        return "RISK_OFF_DEFENSIVE"

    # Tech leadership overrides oil shock — if market is pricing growth, honour it.
    # [UNVALIDATED / CURVE-FIT — see get_tech_sector_leading() and
    # docs/regime_rules_validation.md] This override fired exactly once in 21 years
    # of walk-forward testing (2025-2026). It is a single-event guard against the
    # Mar-Jun 2026 INFLATION_SHOCK misfire, NOT validated general logic. Downstream
    # code must not weight this as a confirmed edge.
    if signals.get("tech_sector_leading") and signals.get("above_200ma"):
        return "RISK_ON_GROWTH"

    # INFLATION_SHOCK — requires BOTH oil shock AND energy sector actually outperforming.
    # If oil is up but energy stocks lag while tech leads, the market is ignoring
    # the inflation signal — fall through rather than misfiring.
    # [XLE-confirmation is NEUTRAL/UNVALIDATED — n=4 in the decisive branch; see
    # get_energy_sector_outperforming() and docs/regime_rules_validation.md.]
    if signals.get("oil_shock"):
        if signals.get("energy_sector_outperforming"):
            return "INFLATION_SHOCK"
        else:
            logger.info(
                "Oil shock detected but energy sector not outperforming — "
                "market ignoring oil signal, falling through to other checks"
            )
        # Also fire INFLATION_SHOCK when macro data confirms: high expectations + restrictive rates
        if signals.get("high_inflation_expectations") and signals.get("real_rate_restrictive"):
            return "INFLATION_SHOCK"

    # Inflation macro confirmation without oil shock
    if (signals.get("high_inflation_expectations") and signals.get("real_rate_restrictive")):
        return "INFLATION_SHOCK"

    # Recovery — below 200MA but PMI improving and yield curve steepening
    if (not signals.get("above_200ma")
            and signals.get("pmi_trend") == "improving"
            and signals.get("yield_curve_trend") == "steepening"):
        return "RECOVERY"

    # Risk-on — trend intact, PMI expanding, no fear, no rate drag
    if (signals.get("above_200ma")
            and not signals.get("fear")
            and not signals.get("real_rate_restrictive")):
        return "RISK_ON_GROWTH"

    # Fear confirmed by news → defensive
    if signals.get("fear") and signals.get("news_signal") == "negative":
        return "RISK_OFF_DEFENSIVE"

    return "NEUTRAL"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def get_current_regime() -> dict:
    """Fetch macro + economic data and return current regime + adjusted weights."""
    # Market signals (yfinance — fast, no key required)
    indicators     = fetch_macro_data()
    market_result  = detect_regime(indicators)

    # Economic signals (FRED — requires API key; gracefully returns defaults when missing)
    economic = get_full_economic_snapshot()

    # Merge all signals
    all_signals = {**market_result["signals"], **economic}

    # Add sector confirmation signals (fetched once here, read by classifier)
    all_signals["energy_sector_outperforming"] = get_energy_sector_outperforming()
    all_signals["tech_sector_leading"]         = get_tech_sector_leading()

    # Enhanced classification overrides the market-only one
    regime = classify_enhanced_regime(all_signals)

    result = {
        "regime":      regime,
        "signals":     all_signals,
        "weights":     REGIME_WEIGHTS[regime],
        "description": REGIME_WEIGHTS[regime]["description"],
        "as_of":       str(date.today()),
    }

    logger.info(f"Regime: {regime} — {result['description']}")
    logger.info(
        f"VIX: {all_signals.get('vix', 0):.1f}  "
        f"Above200MA: {all_signals.get('above_200ma')}  "
        f"OilShock: {all_signals.get('oil_shock')}  "
        f"YieldCurve: {all_signals.get('yield_curve_spread')}  "
        f"RealRate: {all_signals.get('real_rate')}  "
        f"PMI: {all_signals.get('ism_pmi')}"
    )
    logger.info(f"Weights: {result['weights']}")
    return result


# ---------------------------------------------------------------------------
# Human-readable report
# ---------------------------------------------------------------------------

def print_regime_report(regime: dict) -> None:
    s = regime["signals"]
    w = regime["weights"]
    print("\nMACRO REGIME REPORT")
    print("=" * 55)
    print(f"Regime:      {regime['regime']}")
    print(f"Description: {regime['description']}")
    print(f"As of:       {regime['as_of']}")
    print(f"\nMARKET SIGNALS")
    print(f"  VIX:               {s.get('vix', 0):.1f}")
    print(f"  Above 200MA:       {s.get('above_200ma')}")
    print(f"  Fear mode:         {s.get('fear')}")
    print(f"  Credit stress:     {s.get('credit_stress')}")
    print(f"  Oil shock:         {s.get('oil_shock')}")
    print(f"  Oil 3M return:     {s.get('oil_return_3m', 0):.1%}")
    print(f"  Energy outperform: {s.get('energy_sector_outperforming')}")
    print(f"  Tech leading:      {s.get('tech_sector_leading')}")
    print(f"  Small cap leading: {s.get('small_cap_leading')}")
    print(f"  SPY 1M return:     {s.get('spy_return_1m', 0):.1%}")
    print(f"  SPY 3M return:     {s.get('spy_return_3m', 0):.1%}")
    print(f"\nECONOMIC SIGNALS")
    print(f"  Yield curve:       {s.get('yield_curve_spread', 'N/A')} ({'INVERTED' if s.get('yield_curve_inverted') else 'normal'})")
    print(f"  Recession warning: {s.get('recession_warning')}")
    print(f"  Fed Funds:         {s.get('fed_funds_rate', 'N/A')}%")
    print(f"  Real rate:         {s.get('real_rate', 'N/A')}% ({'restrictive' if s.get('real_rate_restrictive') else 'accommodative'})")
    print(f"  CPI YoY:           {s.get('cpi_yoy', 'N/A')}%")
    print(f"  ISM PMI:           {s.get('ism_pmi', 'N/A')} ({s.get('pmi_trend', 'N/A')})")
    print(f"  Consumer sent.:    {s.get('consumer_sentiment', 'N/A')} ({s.get('sentiment_trend', 'N/A')})")
    print(f"  Infl. expectations:{s.get('inflation_expectations', 'N/A')}%")
    print(f"  HY spread (FRED):  {s.get('fred_hy_spread', 'N/A')}% ({s.get('fred_spread_trend', 'N/A')})")
    print(f"\nAdjusted Factor Weights:")
    print(f"  Value:            {w['value']:.0%}")
    print(f"  Quality:          {w['quality']:.0%}")
    print(f"  Momentum:         {w['momentum']:.0%}")
    print(f"  Sentiment:        {w['sentiment']:.0%}")
    print(f"  Short interest:   {w.get('short_interest', 0):.0%}")
    print(f"  Insider:          {w.get('insider', 0):.0%}")
    print(f"  Earnings surp.:   {w.get('earnings_surprise', 0):.0%}")


if __name__ == "__main__":
    print_regime_report(get_current_regime())
