"""
Starter training script for the next model generation.

This script expects migrations/002_modeling_views.sql to be executed in Supabase.
It intentionally refuses to train when the dataset is tiny; otherwise the model
will look great in-sample and fail live.
"""

import json
import os
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss, roc_auc_score

sys.path.append(str(Path(__file__).resolve().parents[1]))

import db

MIN_ROWS = int(os.getenv("MIN_MODELING_ROWS", "500"))
MODEL_DIR = "model_artifacts"


def load_modeling_rows(limit=10000):
    try:
        rows = db._get(
            "modeling_snapshots",
            {
                "select": "*",
                "target_up": "not.is.null",
                "order": "observed_at.asc",
                "limit": limit,
            },
        )
    except Exception as exc:
        print(json.dumps({
            "status": "missing_modeling_view",
            "detail": str(exc),
            "action": "Run migrations/002_modeling_views.sql in Supabase SQL Editor.",
        }, indent=2))
        return pd.DataFrame()
    return pd.DataFrame(rows)


def quote_features_from_raw(raw):
    quotes = (raw or {}).get("quotes") or []
    quote_by_outcome = {quote.get("outcome"): quote for quote in quotes if quote.get("outcome")}

    features = {}
    for side in ("UP", "DOWN"):
        quote = quote_by_outcome.get(side) or {}
        prefix = side.lower()
        for field in (
            "best_bid",
            "best_ask",
            "midpoint",
            "spread",
            "last_trade_price",
            "bid_size",
            "ask_size",
        ):
            features[f"poly_{prefix}_{field}"] = quote.get(field)

    up_ask = features.get("poly_up_best_ask")
    down_ask = features.get("poly_down_best_ask")
    up_bid = features.get("poly_up_best_bid")
    down_bid = features.get("poly_down_best_bid")

    if up_ask is not None and down_ask is not None:
        features["poly_ask_sum"] = up_ask + down_ask
        features["poly_ask_imbalance"] = up_ask - down_ask
        features["poly_implied_up_from_asks"] = 1.0 - up_ask
    if up_bid is not None and down_bid is not None:
        features["poly_bid_sum"] = up_bid + down_bid
        features["poly_bid_imbalance"] = up_bid - down_bid
    return features


def flatten_feature_values(df):
    feature_rows = []
    for _, row in df.iterrows():
        values = row.get("feature_values") or {}
        raw = row.get("prediction_raw") or {}
        feature_rows.append({**values, **quote_features_from_raw(raw)})
    features = pd.DataFrame(feature_rows)

    base_cols = [
        "seconds_to_cutoff",
        "btc_price",
        "baseline",
        "dist_to_baseline",
        "dist_to_baseline_pct",
        "prob_up",
        "prob_down",
        "confidence",
        "edge_up",
        "edge_down",
    ]
    base = df[base_cols].apply(pd.to_numeric, errors="coerce")
    return pd.concat([base, features], axis=1).replace([np.inf, -np.inf], np.nan).fillna(0)


def metric_block(y_true, proba):
    pred = (proba >= 0.5).astype(int)
    metrics = {
        "accuracy": round(float(accuracy_score(y_true, pred)), 4),
        "brier_score": round(float(brier_score_loss(y_true, proba)), 4),
        "log_loss": round(float(log_loss(y_true, proba)), 4),
    }
    if len(set(y_true)) > 1:
        metrics["roc_auc"] = round(float(roc_auc_score(y_true, proba)), 4)
    else:
        metrics["roc_auc"] = None
    return metrics


def main():
    df = load_modeling_rows()
    if len(df) < MIN_ROWS:
        print(json.dumps({
            "status": "not_enough_data",
            "rows": int(len(df)),
            "min_rows": MIN_ROWS,
            "note": "Keep the collector running before training a Supabase-based model.",
        }, indent=2))
        return

    X = flatten_feature_values(df)
    y = pd.to_numeric(df["target_up"], errors="coerce").astype(int)

    split_idx = int(len(X) * 0.8)
    X_train, X_val = X.iloc[:split_idx], X.iloc[split_idx:]
    y_train, y_val = y.iloc[:split_idx], y.iloc[split_idx:]

    model = HistGradientBoostingClassifier(
        max_iter=500,
        max_depth=4,
        learning_rate=0.03,
        min_samples_leaf=20,
        l2_regularization=0.5,
        early_stopping=True,
        random_state=42,
    )
    model.fit(X_train, y_train)
    proba = model.predict_proba(X_val)[:, 1]

    metrics = {
        "rows": len(df),
        "unique_rounds": int(df["round_cutoff"].nunique()),
        "train_size": len(X_train),
        "val_size": len(X_val),
        **metric_block(y_val, proba),
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "source": "supabase:modeling_snapshots",
        "feature_count": len(X.columns),
        "uses_polymarket_quote_features": any(col.startswith("poly_") for col in X.columns),
    }

    os.makedirs(MODEL_DIR, exist_ok=True)
    with open(os.path.join(MODEL_DIR, "model_supabase.pkl"), "wb") as f:
        pickle.dump({"model": model, "columns": list(X.columns), "metrics": metrics}, f)
    with open(os.path.join(MODEL_DIR, "metrics_supabase.json"), "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)

    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
