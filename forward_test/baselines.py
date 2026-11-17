"""
forward_test/baselines.py -- a forward test's baselines in TODAY's share units.

meta.json locks each name's first close (first_close mode). yfinance back-adjusts its
whole history when a stock splits, and the broker reports post-split prices, so after a
split every price this system sees is in new shares while the locked baseline is still
in old ones. On 2026-11-17 SENEA split 5-for-1: the snapshot rewrote 34 days of SENEA
closes to ~1/5, the locked baseline stayed 178.00, and the book read -12.9% instead of
-7.5%, the risk watch emailed "SENEA -80%", and a top-up would have sized SENEA at a
fifth of its target. The health check stayed green throughout.

The fix keeps meta.json untouched (the locked record of what was bought) and records
each split in an append-only corporate_actions.json beside it:

    {"splits": [{"ticker": "SENEA", "date": "2026-11-17", "ratio": 5.0,
                 "source": "yfinance Stock Splits", "recorded": "2026-11-17T18:02:11"}]}

ratio = new shares per old share (5.0 for 5-for-1, 0.1 for a 1-for-10 reverse split).
Only forward_test/snapshot.py writes it, and only for a split yfinance itself reports
that explains the mismatch between the locked baseline and the price series to within
TOLERANCE. Every reader of baselines -- the equity curve, the evaluation, the risk
watch, the weekly report, the dashboard and the broker's buy-and-hold targets -- goes
through current_baselines(), so they all see the same numbers.

A period with no corporate_actions.json (including every evaluated period) gets its
meta.json baselines back unchanged.
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

CA_NAME = "corporate_actions.json"
TOLERANCE = 0.01          # a locked baseline and the series may differ by 1% at most

# meta.json key per group, and per book file (broker/targets.py reads by book file).
GROUP_KEYS = {"incumbent": "incumbent_baselines", "challenger": "challenger_baselines"}
BOOK_GROUPS = {"incumbent_book.csv": "incumbent", "value_tilt_book.csv": "challenger"}


def read_splits(period_dir: Path) -> list[dict]:
    p = Path(period_dir) / CA_NAME
    if not p.exists():
        return []
    return list(json.loads(p.read_text(encoding="utf-8")).get("splits", []))


def split_factor(splits: list[dict], ticker: str) -> float:
    """Shares now per share held at the baseline (1.0 when the name never split)."""
    return math.prod(float(s["ratio"]) for s in splits if s["ticker"] == ticker)


def adjust(baselines: dict, splits: list[dict]) -> dict:
    """Locked baselines restated in today's share units. Empty or zero entries dropped."""
    return {t: float(b) / split_factor(splits, t) for t, b in baselines.items() if b}


def current_baselines(period_dir: Path) -> dict:
    """{"incumbent": {...}, "challenger": {...}, "spy": float|None, "baseline_date": str|None,
    "locked": bool} from meta.json, restated for every recorded split. All empty when the
    period has no meta.json or has not locked its baselines yet."""
    period_dir = Path(period_dir)
    mp = period_dir / "meta.json"
    meta = json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else {}
    splits = read_splits(period_dir)
    out = {g: adjust(meta.get(k) or {}, splits) for g, k in GROUP_KEYS.items()}
    spy = meta.get("spy_baseline")
    out["spy"] = float(spy) / split_factor(splits, "SPY") if spy else None
    out["baseline_date"] = meta.get("baseline_date")
    out["locked"] = bool(meta.get("baselines_locked"))
    return out


def for_book(book_path: str | Path) -> dict:
    """Current-unit baselines for one book file, or {} if it has none."""
    p = Path(book_path)
    group = BOOK_GROUPS.get(p.name)
    if not group:
        return {}
    return current_baselines(p.parent)[group]


def mismatches(locked: dict, day1: dict, splits: list[dict]) -> dict:
    """{ticker: observed / expected} for every name whose series close on the baseline
    date disagrees with its (split-restated) locked baseline by more than TOLERANCE.
    Names without a day-1 close are skipped: nothing to compare."""
    expected = adjust(locked, splits)
    out = {}
    for t, e in expected.items():
        obs = day1.get(t)
        if obs is None or not math.isfinite(float(obs)) or not e:
            continue
        r = float(obs) / e
        if abs(r - 1.0) > TOLERANCE:
            out[t] = r
    return out


def explain(ratio: float, reported: list[tuple[str, float]]) -> bool:
    """True when the reported splits (date, ratio) account for an observed/expected
    ratio: a 5-for-1 split leaves the back-adjusted series at 1/5 of the old baseline."""
    if not reported:
        return False
    return abs(ratio * math.prod(r for _, r in reported) - 1.0) <= TOLERANCE


def positions_in_current_shares(positions: list[dict], period_dir: Path,
                                as_of: str | None) -> list[dict]:
    """Restate a positions snapshot taken at `as_of` (ISO timestamp) into today's shares:
    quantity x ratio and average cost / ratio for every split dated after it. A
    2026-10-01 view held pre-split SENEA shares; valued at post-split prices it showed a
    fifth of the position and made every other name look overweight."""
    splits = read_splits(period_dir)
    day = str(as_of or "")[:10]
    out = []
    for p in positions:
        q = dict(p)
        f = math.prod(float(s["ratio"]) for s in splits
                      if s["ticker"] == q.get("ticker") and s["date"] > day)
        if f != 1.0:
            q["position"] = float(q.get("position") or 0) * f
            if q.get("avg_cost") is not None:
                q["avg_cost"] = float(q["avg_cost"]) / f
        out.append(q)
    return out


def append_splits(period_dir: Path, records: list[dict]) -> None:
    """Add split records (append-only; an already recorded ticker+date is never rewritten)."""
    p = Path(period_dir) / CA_NAME
    have = read_splits(period_dir)
    seen = {(s["ticker"], s["date"]) for s in have}
    now = datetime.now().isoformat(timespec="seconds")
    new = [dict(r, recorded=r.get("recorded", now)) for r in records
           if (r["ticker"], r["date"]) not in seen]
    if not new:
        return
    p.write_text(json.dumps({"splits": have + new}, indent=2) + "\n", encoding="utf-8")
