"""
ML Training Pipeline — Polymarket BTC configurable-window Predictor
=====================================================
Uses HistGradientBoostingClassifier (sklearn) for Python 3.14 compatibility.
Fetches 30 days of data, trains, calibrates, and saves the model.
"""

import os
import json
import pickle
import time
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.calibration import CalibratedClassifierCV
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import (
    accuracy_score, precision_score, recall_score, f1_score,
    roc_auc_score, brier_score_loss, log_loss, confusion_matrix
)
from features import compute_all_features, prepare_dataset, FEATURE_COLUMNS
from market_config import WINDOW_SECONDS

# ─── Config ───
SYMBOL = "BTCUSDT"
INTERVAL = "1m"
DAYS = 30
LIMIT = 1000
BASE_URL = "https://api.binance.com/api/v3/klines"
MODEL_DIR = "model_artifacts"


def fetch_historical_data(days=DAYS):
    """Fetch N days of 1-minute OHLCV data from Binance."""
    ms_per_min = 60_000
    total_minutes = days * 24 * 60
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = now_ms - (total_minutes * ms_per_min)
    total_requests = (total_minutes // LIMIT) + 1

    print(f"Fetching {days} days of {SYMBOL} 1m data ({total_minutes:,} candles)...")
    print(f"  Estimated requests: {total_requests}\n")

    all_candles = []
    current_start = start_ms
    req_num = 0

    while current_start < now_ms:
        req_num += 1
        if req_num % 10 == 1:
            print(f"  Request {req_num}/{total_requests}...", flush=True)

        try:
            params = {
                "symbol": SYMBOL, "interval": INTERVAL,
                "startTime": current_start, "endTime": now_ms, "limit": LIMIT,
            }
            resp = requests.get(BASE_URL, params=params, timeout=30)
            resp.raise_for_status()
            raw = resp.json()
        except Exception as e:
            print(f"  Error: {e}, retrying in 5s...")
            time.sleep(5)
            continue

        if not raw:
            break

        for c in raw:
            all_candles.append({
                "timestamp": c[0],
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume_btc": float(c[5]),
                "volume_usdt": float(c[7]),
                "num_trades": int(c[8]),
                "taker_buy_base": float(c[9]),
                "taker_buy_quote": float(c[10]),
            })

        current_start = raw[-1][0] + ms_per_min
        time.sleep(0.25)

    print(f"\n  Total candles fetched: {len(all_candles):,}")
    return pd.DataFrame(all_candles)


def compute_feature_importance(model, feature_names, X_val=None, y_val=None):
    """Extract feature importance using permutation importance."""
    from sklearn.inspection import permutation_importance
    if X_val is not None and y_val is not None:
        result = permutation_importance(model, X_val, y_val, n_repeats=10,
                                         random_state=42, n_jobs=-1, scoring='roc_auc')
        imp = dict(zip(feature_names, result.importances_mean.tolist()))
    else:
        imp = {c: 1.0/len(feature_names) for c in feature_names}
    return dict(sorted(imp.items(), key=lambda x: x[1], reverse=True))


def train_model():
    """Full training pipeline."""
    os.makedirs(MODEL_DIR, exist_ok=True)

    # 1. Fetch data (with cache)
    print("=" * 60)
    print("  STEP 1: Loading historical data")
    print("=" * 60)
    cache_file = os.path.join(MODEL_DIR, "training_data.csv")
    cache_fresh = False
    if os.path.exists(cache_file):
        age_hours = (time.time() - os.path.getmtime(cache_file)) / 3600
        if age_hours < 1:
            print(f"  Using cached data ({age_hours:.1f}h old)")
            df = pd.read_csv(cache_file)
            cache_fresh = True
    if not cache_fresh:
        df = fetch_historical_data(DAYS)
        df.to_csv(cache_file, index=False)
        print(f"  Data cached to {cache_file}")

    # 2. Prepare features & target
    print("\n" + "=" * 60)
    print("  STEP 2: Computing features & preparing dataset")
    print("=" * 60)
    X, y, meta = prepare_dataset(df)
    print(f"  Dataset shape: {X.shape}")
    print(f"  Target distribution: UP={y.sum()} ({y.mean()*100:.1f}%) | DOWN={len(y)-y.sum()} ({(1-y.mean())*100:.1f}%)")

    # 3. Time-series split (80/20)
    split_idx = int(len(X) * 0.8)
    X_train, X_val = X.iloc[:split_idx], X.iloc[split_idx:]
    y_train, y_val = y.iloc[:split_idx], y.iloc[split_idx:]
    print(f"  Train: {len(X_train)} | Validation: {len(X_val)}")

    # 4. Train model
    print("\n" + "=" * 60)
    print("  STEP 3: Training HistGradientBoosting model")
    print("=" * 60)

    base_model = HistGradientBoostingClassifier(
        max_iter=1000,
        max_depth=5,
        learning_rate=0.03,
        min_samples_leaf=15,
        max_leaf_nodes=31,
        l2_regularization=0.5,
        early_stopping=True,
        n_iter_no_change=50,
        validation_fraction=0.15,
        random_state=42,
        verbose=0,
    )
    base_model.fit(X_train, y_train)
    print(f"  Iterations used: {base_model.n_iter_}")

    # 5. Calibrate probabilities
    print("\n  Calibrating probabilities...")
    cal_model = CalibratedClassifierCV(base_model, cv=3, method='isotonic')
    cal_model.fit(X_train, y_train)

    # 6. Evaluate
    print("\n" + "=" * 60)
    print("  STEP 4: Evaluation on validation set")
    print("=" * 60)

    y_pred = cal_model.predict(X_val)
    y_proba = cal_model.predict_proba(X_val)[:, 1]

    metrics = {
        "accuracy": round(accuracy_score(y_val, y_pred), 4),
        "precision": round(precision_score(y_val, y_pred, zero_division=0), 4),
        "recall": round(recall_score(y_val, y_pred, zero_division=0), 4),
        "f1": round(f1_score(y_val, y_pred, zero_division=0), 4),
        "roc_auc": round(roc_auc_score(y_val, y_proba), 4),
        "brier_score": round(brier_score_loss(y_val, y_proba), 4),
        "log_loss": round(log_loss(y_val, y_proba), 4),
        "train_size": len(X_train),
        "val_size": len(X_val),
        "up_ratio": round(y.mean(), 4),
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "days_of_data": DAYS,
        "window_seconds": WINDOW_SECONDS,
        "window_minutes": WINDOW_SECONDS // 60,
    }

    cm = confusion_matrix(y_val, y_pred)
    metrics["confusion_matrix"] = cm.tolist()

    print(f"  Accuracy:    {metrics['accuracy']:.4f}")
    print(f"  Precision:   {metrics['precision']:.4f}")
    print(f"  Recall:      {metrics['recall']:.4f}")
    print(f"  F1:          {metrics['f1']:.4f}")
    print(f"  ROC-AUC:     {metrics['roc_auc']:.4f}")
    print(f"  Brier Score: {metrics['brier_score']:.4f}")
    print(f"  Log Loss:    {metrics['log_loss']:.4f}")
    print(f"  Confusion Matrix:\n  {cm}")

    # 7. Feature importance
    feat_imp = compute_feature_importance(cal_model, FEATURE_COLUMNS, X_val, y_val)
    top10 = list(feat_imp.items())[:10]
    print("\n  Top 10 Features:")
    for name, imp in top10:
        bar = "#" * int(imp * 200)
        print(f"    {name:25s} {imp:.4f} {bar}")

    # 8. Save everything
    print("\n" + "=" * 60)
    print("  STEP 5: Saving model artifacts")
    print("=" * 60)

    with open(os.path.join(MODEL_DIR, "model.pkl"), "wb") as f:
        pickle.dump(cal_model, f)
    with open(os.path.join(MODEL_DIR, "base_model.pkl"), "wb") as f:
        pickle.dump(base_model, f)
    with open(os.path.join(MODEL_DIR, "metrics.json"), "w") as f:
        json.dump(metrics, f, indent=2)
    with open(os.path.join(MODEL_DIR, "feature_importance.json"), "w") as f:
        json.dump(feat_imp, f, indent=2)
    with open(os.path.join(MODEL_DIR, "feature_columns.json"), "w") as f:
        json.dump(FEATURE_COLUMNS, f, indent=2)

    print(f"  Saved to {MODEL_DIR}/")
    print(f"    - model.pkl (calibrated)")
    print(f"    - base_model.pkl")
    print(f"    - metrics.json")
    print(f"    - feature_importance.json")
    print(f"    - feature_columns.json")
    print("\n  DONE. Model ready for serving.")

    return cal_model, metrics, feat_imp


if __name__ == "__main__":
    train_model()
