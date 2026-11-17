"""
models/factor_model.py

Gradient boosting model that learns optimal factor weights
from historical factor scores + realized 90-day forward returns.

Falls back to weighted average when:
  - Model not yet trained (FileNotFoundError)
  - Training data < MIN_SAMPLES threshold (model not saved)
  - Any prediction error
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger

MODEL_PATH  = Path("models/factor_model.pkl")
SCALER_PATH = Path("models/feature_scaler.pkl")

MIN_SAMPLES = 100
MIN_SAMPLES_FOR_REGIME_MODEL = 100

REGIME_MODEL_PATHS: dict[str, Path] = {
    "RISK_ON_GROWTH":     Path("models/model_risk_on.pkl"),
    "RISK_OFF_DEFENSIVE": Path("models/model_risk_off.pkl"),
    "INFLATION_SHOCK":    Path("models/model_inflation.pkl"),
    "RECOVERY":           Path("models/model_recovery.pkl"),
    "NEUTRAL":            Path("models/model_neutral.pkl"),
}

REGIME_SCALER_PATHS: dict[str, Path] = {
    regime: Path(f"models/scaler_{regime.lower()}.pkl")
    for regime in REGIME_MODEL_PATHS
}

FEATURES = [
    "value_score",
    "quality_score",
    "momentum_score",
    "sentiment_score",
    "short_interest_score",
    "insider_score",
    "earnings_surprise_score",
    "proximity_52w",
    "revenue_accel_score",
]


def train_model(training_data_path: str = "data/training_data.csv") -> dict:
    """
    Train GradientBoostingRegressor on historical factor scores.
    Uses TimeSeriesSplit to avoid lookahead bias in cross-validation.

    Returns results dict. Model is only persisted when samples >= MIN_SAMPLES.
    """
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.preprocessing import RobustScaler
    from sklearn.metrics import mean_squared_error
    import joblib

    Path("models").mkdir(exist_ok=True)

    df = pd.read_csv(training_data_path)

    # Fill missing feature columns with neutral 50
    for feat in FEATURES:
        if feat not in df.columns:
            df[feat] = 50.0

    df = df.dropna(subset=FEATURES + ["forward_return_90d"])
    n  = len(df)

    logger.info(f"Training on {n} samples ({df['calculation_date'].nunique()} dates)")

    if n < 2:
        logger.error("Fewer than 2 training samples — cannot train model.")
        return {"samples": n, "cv_scores": [], "avg_ic": 0.0,
                "feature_importance": {}, "model_path": None,
                "model_saved": False, "note": "Insufficient data"}

    X = df[FEATURES].values
    y = df["forward_return_90d"].values

    scaler   = RobustScaler()
    X_scaled = scaler.fit_transform(X)

    model = GradientBoostingRegressor(
        n_estimators=200,
        learning_rate=0.05,
        max_depth=4,
        min_samples_leaf=max(1, min(20, n // 10)),  # scale to sample size
        subsample=0.8,
        random_state=42,
    )

    # TimeSeriesSplit — honour temporal order to avoid lookahead
    # TimeSeriesSplit needs n > n_splits; bail out of CV when too few samples
    cv_scores: list[dict] = []
    run_cv = n >= 4   # need at least 4 samples for meaningful 2-fold CV
    splits = TimeSeriesSplit(n_splits=min(5, n // 2)).split(X_scaled) if run_cv else []
    for fold, (train_idx, val_idx) in enumerate(splits):
        if len(val_idx) < 2:
            continue
        X_tr, X_val = X_scaled[train_idx], X_scaled[val_idx]
        y_tr, y_val = y[train_idx], y[val_idx]

        model.fit(X_tr, y_tr)
        y_pred = model.predict(X_val)
        rmse   = float(np.sqrt(mean_squared_error(y_val, y_pred)))
        corr   = np.corrcoef(y_val, y_pred)
        ic     = float(corr[0, 1]) if corr.shape == (2, 2) else 0.0
        cv_scores.append({"fold": fold, "rmse": rmse, "ic": ic})
        logger.info(f"Fold {fold}: RMSE={rmse:.4f}  IC={ic:.4f}")

    # Final model on all data
    model.fit(X_scaled, y)

    importance = {
        feat: float(imp)
        for feat, imp in zip(FEATURES, model.feature_importances_)
    }
    importance_sorted = dict(sorted(importance.items(), key=lambda x: x[1], reverse=True))

    avg_ic = float(np.mean([s["ic"] for s in cv_scores])) if cv_scores else 0.0

    model_saved = False
    note        = ""
    if n >= MIN_SAMPLES:
        joblib.dump(model, MODEL_PATH)
        joblib.dump(scaler, SCALER_PATH)
        logger.info(f"Model saved to {MODEL_PATH}")
        model_saved = True
    else:
        note = (
            f"Model NOT saved — only {n} training samples "
            f"(need {MIN_SAMPLES}+). Weighted average fallback will be used. "
            f"Re-run the backtest with store_factor_scores=True to generate historical data."
        )
        logger.warning(note)

    return {
        "samples":            n,
        "cv_scores":          cv_scores,
        "avg_ic":             avg_ic,
        "feature_importance": importance_sorted,
        "model_path":         str(MODEL_PATH) if model_saved else None,
        "model_saved":        model_saved,
        "note":               note,
    }


def load_model():
    """Load trained model and scaler from disk."""
    if not MODEL_PATH.exists():
        raise FileNotFoundError(
            f"No trained model at {MODEL_PATH}. The ML overlay is disabled "
            "(ML_OVERLAY_ENABLED=False) and its training scripts were removed on "
            "2026-10-12; re-enabling it needs its own pre-registered evaluation."
        )
    import joblib
    model  = joblib.load(MODEL_PATH)
    scaler = joblib.load(SCALER_PATH)
    return model, scaler


def predict_scores(scores_df: pd.DataFrame) -> pd.Series | None:
    """
    Generate ML-based composite scores (0-100 percentile) for a factor scores DataFrame.

    Returns None when model is unavailable — caller should use weighted average fallback.
    """
    try:
        model, scaler = load_model()
    except FileNotFoundError:
        logger.debug("ML model not found — using weighted average fallback")
        return None
    except Exception as e:
        logger.error(f"Model load failed: {e} — using fallback")
        return None

    try:
        df = scores_df.copy()
        for feat in FEATURES:
            if feat not in df.columns:
                df[feat] = 50.0

        X        = df[FEATURES].fillna(50.0).values
        X_scaled = scaler.transform(X)

        raw_preds = model.predict(X_scaled)

        # Convert predicted forward returns to 0-100 percentile scores
        from scipy.stats import rankdata
        ml_scores = rankdata(raw_preds) / len(raw_preds) * 100

        return pd.Series(ml_scores, index=scores_df.index, name="ml_composite_score")

    except Exception as e:
        logger.error(f"ML prediction failed: {e} — using fallback")
        return None


def train_regime_models(
    training_data_path: str = "data/training_data.csv",
) -> dict[str, dict]:
    """
    Train a separate GBM for each macro regime.
    Falls back to universal model for regimes with < MIN_SAMPLES_FOR_REGIME_MODEL rows.
    """
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.preprocessing import RobustScaler
    import joblib

    Path("models").mkdir(exist_ok=True)

    df = pd.read_csv(training_data_path)
    for feat in FEATURES:
        if feat not in df.columns:
            df[feat] = 50.0

    if "regime" not in df.columns:
        logger.warning("No 'regime' column in training data — re-run with include_regime_labels=True")
        return {}

    df = df.dropna(subset=FEATURES + ["forward_return_90d", "regime"])
    results: dict[str, dict] = {}

    for regime, model_path in REGIME_MODEL_PATHS.items():
        scaler_path = REGIME_SCALER_PATHS[regime]
        regime_df = df[df["regime"] == regime].copy()
        n = len(regime_df)

        logger.info(f"{regime}: {n} training samples")

        if n < MIN_SAMPLES_FOR_REGIME_MODEL:
            logger.warning(
                f"{regime}: {n} samples < {MIN_SAMPLES_FOR_REGIME_MODEL} — using universal fallback"
            )
            results[regime] = {
                "samples": n,
                "status": "insufficient_data",
                "model": "universal_fallback",
                "avg_ic": None,
                "feature_importance": {},
                "top_factor": None,
            }
            continue

        X = regime_df[FEATURES].values
        y = regime_df["forward_return_90d"].values

        scaler = RobustScaler()
        X_scaled = scaler.fit_transform(X)

        n_estimators = min(200, max(50, n // 10))
        model = GradientBoostingRegressor(
            n_estimators=n_estimators,
            learning_rate=0.05,
            max_depth=3,
            min_samples_leaf=max(10, n // 50),
            subsample=0.8,
            random_state=42,
        )

        avg_ic: float | None = None
        if n >= 200:
            tscv = TimeSeriesSplit(n_splits=3)
            cv_ics = []
            for train_idx, val_idx in tscv.split(X_scaled):
                if len(val_idx) < 2:
                    continue
                model.fit(X_scaled[train_idx], y[train_idx])
                pred = model.predict(X_scaled[val_idx])
                ic = float(np.corrcoef(y[val_idx], pred)[0, 1])
                if not np.isnan(ic):
                    cv_ics.append(ic)
            avg_ic = float(np.mean(cv_ics)) if cv_ics else None

        model.fit(X_scaled, y)

        importance = dict(zip(FEATURES, model.feature_importances_))
        importance_sorted = dict(sorted(importance.items(), key=lambda x: x[1], reverse=True))
        top_factor = max(importance, key=importance.get)

        joblib.dump(model, model_path)
        joblib.dump(scaler, scaler_path)
        logger.info(f"{regime}: saved — IC={avg_ic}  top={top_factor}")

        results[regime] = {
            "samples":            n,
            "status":             "trained",
            "model":              str(model_path),
            "avg_ic":             avg_ic,
            "feature_importance": importance_sorted,
            "top_factor":         top_factor,
        }

    return results


def predict_scores_for_regime(
    scores_df: pd.DataFrame,
    current_regime: str,
) -> pd.Series | None:
    """
    Score stocks using the regime-specific model.
    Falls back to universal predict_scores() when the regime model is unavailable.
    """
    import joblib

    model_path  = REGIME_MODEL_PATHS.get(current_regime)
    scaler_path = REGIME_SCALER_PATHS.get(current_regime)

    if model_path and model_path.exists() and scaler_path and scaler_path.exists():
        try:
            model  = joblib.load(model_path)
            scaler = joblib.load(scaler_path)

            df = scores_df.copy()
            for feat in FEATURES:
                if feat not in df.columns:
                    df[feat] = 50.0

            X        = df[FEATURES].fillna(50.0).values
            X_scaled = scaler.transform(X)
            raw_preds = model.predict(X_scaled)

            from scipy.stats import rankdata
            ml_scores = rankdata(raw_preds) / len(raw_preds) * 100

            logger.info(f"Using {current_regime} regime model for scoring")
            return pd.Series(ml_scores, index=scores_df.index, name="ml_composite_score")

        except Exception as e:
            logger.warning(f"Regime model {current_regime} failed — {e} — using universal")

    logger.info(f"No {current_regime} model — using universal fallback")
    return predict_scores(scores_df)


def retrain_with_outcomes(
    outcomes_table: str = "portfolio_outcomes",
) -> dict:
    """
    Retrain models merging backtest history with real live-portfolio outcomes.

    Real outcomes are weighted 3x vs backtest rows because they include
    all 9 factor signals with actual live data — not backtest-era fills.
    Enhanced model saved alongside the standard model path so it is picked
    up automatically by predict_scores() on the next run.

    [PRINCIPLED — the one rule that generalized in walk-forward validation
    (sandbox/validate_3x_weight.py). Up-weighting the most-recent training year
    improved out-of-sample IC in BOTH folds (+0.022 test-2023, +0.019 test-2024,
    mean +0.020). It is currently IMMATERIAL: 45 live outcomes = 1.0% of training
    at 1x, 2.9% at 3x, so it barely moves the model today — but the effect is
    favorable and will grow as live outcomes accumulate. See
    docs/regime_rules_validation.md.]
    """
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.preprocessing import RobustScaler
    import joblib
    import sqlite3
    import pandas as pd_local

    Path("models").mkdir(exist_ok=True)

    # ── Load existing backtest training data ──────────────────────────────────
    try:
        existing = pd_local.read_csv("data/training_data.csv")
        existing["data_source"]   = "backtest"
        existing["sample_weight"] = 1.0
        logger.info(f"Loaded {len(existing)} backtest training rows")
    except FileNotFoundError:
        existing = pd_local.DataFrame()
        logger.warning("No existing training_data.csv — starting from live outcomes only")

    # ── Load real portfolio outcomes ──────────────────────────────────────────
    try:
        from config.settings import DATABASE_URL
        db_path = DATABASE_URL.replace("sqlite:///", "")
        conn = sqlite3.connect(db_path)
        real_outcomes = pd_local.read_sql(f"""
            SELECT
                entry_value_score        AS value_score,
                entry_quality_score      AS quality_score,
                entry_momentum_score     AS momentum_score,
                COALESCE(entry_sentiment_score, 50.0) AS sentiment_score,
                COALESCE(entry_short_interest_score, 50.0) AS short_interest_score,
                COALESCE(entry_insider_score, 50.0)        AS insider_score,
                COALESCE(entry_earnings_surprise_score, 50.0) AS earnings_surprise_score,
                50.0 AS proximity_52w,
                50.0 AS revenue_accel_score,
                realized_return AS forward_return_90d,
                entry_regime    AS regime,
                entry_date      AS calculation_date,
                ticker
            FROM {outcomes_table}
            WHERE realized_return IS NOT NULL
        """, conn)
        conn.close()
    except Exception as e:
        logger.error(f"Could not load live outcomes: {e} — falling back to standard retrain")
        return train_model()

    if real_outcomes.empty:
        logger.warning("No real outcomes yet — retraining on backtest data only")
        return train_model()

    real_outcomes["data_source"]   = "live_portfolio"
    real_outcomes["sample_weight"] = 3.0
    logger.info(f"Loaded {len(real_outcomes)} live outcome rows (weighted 3x)")

    # ── Merge datasets ────────────────────────────────────────────────────────
    merge_cols = FEATURES + ["forward_return_90d", "sample_weight"]
    if not existing.empty:
        for feat in FEATURES:
            if feat not in existing.columns:
                existing[feat] = 50.0
        combined = pd_local.concat(
            [existing[[c for c in merge_cols if c in existing.columns]],
             real_outcomes[[c for c in merge_cols if c in real_outcomes.columns]]],
            ignore_index=True,
        )
    else:
        combined = real_outcomes[[c for c in merge_cols if c in real_outcomes.columns]]

    combined = combined.dropna(subset=FEATURES + ["forward_return_90d"])
    for feat in FEATURES:
        if feat not in combined.columns:
            combined[feat] = 50.0

    n_live = len(real_outcomes)
    n_bt   = len(existing) if not existing.empty else 0
    logger.info(f"Combined training set: {len(combined)} rows ({n_live} live, {n_bt} backtest)")

    combined.to_csv("data/training_data_enhanced.csv", index=False)

    # ── Train with sample weights ─────────────────────────────────────────────
    X       = combined[FEATURES].fillna(50.0).values
    y       = combined["forward_return_90d"].values
    weights = combined["sample_weight"].fillna(1.0).values

    scaler   = RobustScaler()
    X_scaled = scaler.fit_transform(X)

    model = GradientBoostingRegressor(
        n_estimators=200,
        learning_rate=0.05,
        max_depth=4,
        min_samples_leaf=max(1, min(20, len(combined) // 10)),
        subsample=0.8,
        random_state=42,
    )
    model.fit(X_scaled, y, sample_weight=weights)

    joblib.dump(model,  Path("models/factor_model_enhanced.pkl"))
    joblib.dump(scaler, Path("models/feature_scaler_enhanced.pkl"))
    # Overwrite active model so predict_scores() uses enhanced version
    joblib.dump(model,  MODEL_PATH)
    joblib.dump(scaler, SCALER_PATH)

    importance = dict(sorted(
        zip(FEATURES, model.feature_importances_),
        key=lambda x: x[1], reverse=True,
    ))
    logger.info(f"Enhanced model trained and saved — top factors: {list(importance.keys())[:3]}")

    return {
        "total_samples":     len(combined),
        "live_samples":      n_live,
        "backtest_samples":  n_bt,
        "feature_importance": importance,
        "model_path":        str(MODEL_PATH),
    }


def print_model_report(results: dict) -> None:
    print("\nML MODEL TRAINING REPORT")
    print("=" * 55)
    print(f"Training samples: {results['samples']}")
    print(f"Model saved:      {results['model_saved']}")
    if results.get("note"):
        print(f"\nNOTE: {results['note']}")
    if results["cv_scores"]:
        avg_ic = results["avg_ic"]
        print(f"\nAverage IC: {avg_ic:.4f}")
        print(f"  (IC > 0.05 = useful signal, > 0.10 = strong signal)")
        print(f"\nCross-validation results:")
        for s in results["cv_scores"]:
            print(f"  Fold {s['fold']}: RMSE={s['rmse']:.4f}  IC={s['ic']:.4f}")
    print(f"\nFeature importance (learned from data):")
    for feat, imp in results["feature_importance"].items():
        bar = "#" * int(imp * 50)
        print(f"  {feat:<28} {imp:.3f}  {bar}")
