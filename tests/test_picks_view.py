"""
tests/test_picks_view.py — plain-language stock cards (dashboard/picks_view.py).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dashboard import picks_view as pv  # noqa: E402

ROW = {"ticker": "SM", "company_name": "SM Energy", "sector": "Energy",
       "industry": "Oil & Gas E&P", "conviction_tier": "TIER1_STRONG_BUY",
       "value_score": 88.0, "quality_score": 70.0, "momentum_score": 60.0,
       "f_score": 8.0, "roic": 0.386, "momentum_12_1": 0.43}


def test_short_description_is_one_clean_sentence():
    s = ("SM Energy Company, together with its subsidiaries, engages in the acquisition "
         "and production of oil and gas in Texas. The company was founded in 1908.")
    assert pv.short_description(s) == ("SM Energy Company engages in the acquisition and "
                                       "production of oil and gas in Texas.")
    long = "Acme Inc. " + "makes very many things " * 30 + "for people. More."
    out = pv.short_description(long, max_chars=80)
    assert len(out) <= 81 and out.endswith("…")
    assert pv.short_description(None) is None


def test_reasons_use_everyday_language_and_thresholds():
    why, watch = pv.reasons(ROW)
    assert "Very cheap: priced lower than 88% of similar companies" in why
    assert "Very strong finances: passes 8 of 9 health checks" in why
    assert any(x.startswith("Highly profitable: earns 39%") for x in why)
    assert "Rising price: up 43% over the past year" in why and watch == []


def test_watch_outs_for_run_ups_losses_and_missing_data():
    why, watch = pv.reasons({**ROW, "momentum_12_1": 1.34, "roic": None})
    assert any("fall back sharply" in w for w in watch)
    assert "Profitability figure not available" in watch
    _, watch = pv.reasons({**ROW, "momentum_12_1": -0.2, "roic": 0.02})
    assert "Price has fallen -20% over the past year" in watch
    assert any(w.startswith("Thin profits") for w in watch)


def test_card_marks_held_names_and_escapes_text():
    html = pv.card_html({**ROW, "company_name": "<script>x</script>"},
                        {"summary": "Does <b>things</b>. More."}, held=True)
    assert "In your portfolio" in html and "Top conviction" in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<b>things</b>" not in html
    assert 'href="https://finance.yahoo.com/quote/SM"' in html


def test_card_falls_back_to_industry_without_a_profile():
    html = pv.card_html(ROW, None, held=False)
    assert "Oil &amp; Gas E&amp;P company." in html and "In your portfolio" not in html


def test_score_bars_are_clamped_and_handle_missing():
    html = pv.card_html({**ROW, "value_score": 140, "momentum_score": None}, None, False)
    assert "width:100%" in html and ">n/a<" in html


def test_load_profiles_tolerates_a_missing_or_broken_file(tmp_path):
    assert pv.load_profiles(tmp_path / "none.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert pv.load_profiles(bad) == {}
