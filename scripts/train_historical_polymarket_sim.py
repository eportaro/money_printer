import argparse
import json
import math
import os
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sklearn.ensemble import ExtraTreesClassifier, HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from features import FEATURE_COLUMNS, compute_all_features
from market_config import WINDOW_MS, WINDOW_SECONDS
from model import fetch_historical_data
from model_runtime import load_base_model

load_dotenv()

MODEL_DIR = PROJECT_ROOT / "model_artifacts"
DEFAULT_BUCKETS = "895,840,720,600,480,360,240,180,120,60"


def normal_cdf(values):
    erf = np.vectorize(math.erf)
    return 0.5 * (1.0 + erf(values / np.sqrt(2.0)))


def load_candles(days, refresh=False):
    MODEL_DIR.mkdir(exist_ok=True)
    cache_path = MODEL_DIR / "training_data.csv"
    if cache_path.exists() and not refresh:
        frame = pd.read_csv(cache_path)
        if "timestamp" in frame and len(frame):
            return frame.sort_values("timestamp").reset_index(drop=True)

    frame = fetch_historical_data(days=days)
    frame.to_csv(cache_path, index=False)
    return frame.sort_values("timestamp").reset_index(drop=True)


def parse_buckets(raw):
    buckets = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        value = int(item)
        if 0 < value < WINDOW_SECONDS:
            buckets.append(value)
    return sorted(set(buckets), reverse=True)


def add_window_targets(features):
    frame = features.copy()
    frame["window_id"] = frame["timestamp"] // WINDOW_MS
    stats = frame.groupby("window_id").agg(
        baseline=("open", "first"),
        actual_close=("close", "last"),
        window_start=("timestamp", "min"),
    )
    stats["target_up"] = (stats["actual_close"] > stats["baseline"]).astype(int)
    frame = frame.merge(
        stats[["baseline", "actual_close", "target_up", "window_start"]],
        left_on="window_id",
        right_index=True,
        suffixes=("", "_round"),
    )
    frame["cutoff_ts"] = (frame["window_start"] // 1000) + WINDOW_SECONDS
    frame["seconds_to_cutoff_raw"] = frame["cutoff_ts"] - (frame["timestamp"] // 1000)
    return frame


def sample_decision_rows(frame, buckets):
    rows = []
    for _, group in frame.groupby("window_id", sort=True):
        if len(group) < max(2, WINDOW_SECONDS // 60 - 1):
            continue
        for bucket in buckets:
            idx = (group["seconds_to_cutoff_raw"] - bucket).abs().idxmin()
            row = group.loc[idx].copy()
            row["seconds_bucket"] = bucket
            row["seconds_to_cutoff"] = bucket
            rows.append(row)
    if not rows:
        return pd.DataFrame()
    sampled = pd.DataFrame(rows)
    sampled = sampled.drop_duplicates(["window_id", "seconds_bucket"]).sort_values(["timestamp", "seconds_bucket"])
    return sampled.reset_index(drop=True)


def simulate_polymarket_quotes(frame, spread_floor=0.02):
    out = frame.copy()
    log_ret = np.log(out["close"]).diff()
    # Use historical 1m realized volatility available at the decision time.
    vol = log_ret.rolling(90, min_periods=30).std().reindex(out.index)
    vol = vol.fillna(log_ret.rolling(30, min_periods=10).std()).fillna(log_ret.std())
    vol = vol.clip(lower=0.00005)

    minutes_left = np.maximum(out["seconds_to_cutoff"].astype(float) / 60.0, 1.0)
    distance = np.log(out["close"].astype(float) / out["baseline"].astype(float))
    z = distance / (vol * np.sqrt(minutes_left))
    market_prob_up = pd.Series(normal_cdf(z.to_numpy()), index=out.index).clip(0.02, 0.98)

    uncertainty = 1.0 - (market_prob_up - 0.5).abs() * 2.0
    time_pressure = 1.0 / np.sqrt(minutes_left)
    spread = (spread_floor + 0.015 * uncertainty + 0.005 * time_pressure).clip(0.015, 0.08)
    half_spread = spread / 2.0

    out["poly_up_midpoint"] = market_prob_up
    out["poly_down_midpoint"] = 1.0 - market_prob_up
    out["poly_up_best_ask"] = (out["poly_up_midpoint"] + half_spread).clip(0.01, 0.99)
    out["poly_up_best_bid"] = (out["poly_up_midpoint"] - half_spread).clip(0.01, 0.99)
    out["poly_down_best_ask"] = (out["poly_down_midpoint"] + half_spread).clip(0.01, 0.99)
    out["poly_down_best_bid"] = (out["poly_down_midpoint"] - half_spread).clip(0.01, 0.99)
    out["poly_up_spread"] = out["poly_up_best_ask"] - out["poly_up_best_bid"]
    out["poly_down_spread"] = out["poly_down_best_ask"] - out["poly_down_best_bid"]
    out["poly_up_last_trade_price"] = out["poly_up_midpoint"]
    out["poly_down_last_trade_price"] = out["poly_down_midpoint"]
    out["poly_up_bid_size"] = 100 + 900 * uncertainty
    out["poly_up_ask_size"] = 100 + 900 * uncertainty
    out["poly_down_bid_size"] = 100 + 900 * uncertainty
    out["poly_down_ask_size"] = 100 + 900 * uncertainty
    return out


def add_base_model_context(frame):
    base_model = load_base_model()
    X_base = frame[FEATURE_COLUMNS].apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(0)
    prob_up = base_model.predict_proba(X_base)[:, 1]
    out = frame.copy()
    out["prob_up"] = prob_up
    out["prob_down"] = 1.0 - prob_up
    out["confidence"] = np.abs(prob_up - 0.5) * 2.0
    out["edge_up"] = out["prob_up"] - out["poly_up_best_ask"]
    out["edge_down"] = out["prob_down"] - out["poly_down_best_ask"]
    out["btc_price"] = out["close"]
    out["dist_to_baseline"] = out["btc_price"] - out["baseline"]
    out["dist_to_baseline_pct"] = out["dist_to_baseline"] / out["baseline"] * 100.0
    return out


def feature_matrix(frame):
    context_cols = [
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
    quote_cols = [
        "poly_up_best_bid",
        "poly_up_best_ask",
        "poly_up_midpoint",
        "poly_up_spread",
        "poly_up_bid_size",
        "poly_up_ask_size",
        "poly_up_last_trade_price",
        "poly_down_best_bid",
        "poly_down_best_ask",
        "poly_down_midpoint",
        "poly_down_spread",
        "poly_down_bid_size",
        "poly_down_ask_size",
        "poly_down_last_trade_price",
    ]
    columns = context_cols + FEATURE_COLUMNS + quote_cols
    X = frame[columns].apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan)
    return X, columns


def make_models():
    return {
        "logistic": Pipeline([
            ("impute", SimpleImputer(strategy="median")),
            ("scale", StandardScaler()),
            ("model", LogisticRegression(max_iter=1000, class_weight="balanced")),
        ]),
        "random_forest": Pipeline([
            ("impute", SimpleImputer(strategy="median")),
            ("model", RandomForestClassifier(
                n_estimators=300,
                min_samples_leaf=12,
                class_weight="balanced_subsample",
                random_state=42,
                n_jobs=-1,
            )),
        ]),
        "extra_trees": Pipeline([
            ("impute", SimpleImputer(strategy="median")),
            ("model", ExtraTreesClassifier(
                n_estimators=350,
                min_samples_leaf=10,
                class_weight="balanced",
                random_state=42,
                n_jobs=-1,
            )),
        ]),
        "hist_gradient_boosting": Pipeline([
            ("impute", SimpleImputer(strategy="median")),
            ("model", HistGradientBoostingClassifier(
                learning_rate=0.035,
                max_iter=350,
                max_leaf_nodes=21,
                l2_regularization=0.08,
                random_state=42,
            )),
        ]),
    }


def score(model, X, y):
    prob = model.predict_proba(X)[:, 1]
    pred = (prob >= 0.5).astype(int)
    metrics = {
        "accuracy": round(float(accuracy_score(y, pred)), 4),
        "brier_score": round(float(brier_score_loss(y, prob)), 4),
        "log_loss": round(float(log_loss(y, prob, labels=[0, 1])), 4),
    }
    metrics["roc_auc"] = round(float(roc_auc_score(y, prob)), 4) if len(np.unique(y)) == 2 else None
    return metrics


def json_safe(value):
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    return value


def main():
    parser = argparse.ArgumentParser(description="Train a market-aware model from historical 15m Polymarket-like simulations.")
    parser.add_argument("--days", type=int, default=int(os.getenv("HISTORICAL_SIM_DAYS", "30")))
    parser.add_argument("--refresh", action="store_true", help="Ignore cached model_artifacts/training_data.csv and download again.")
    parser.add_argument("--buckets", default=os.getenv("HISTORICAL_SIM_BUCKETS", DEFAULT_BUCKETS))
    parser.add_argument("--test-size", type=float, default=0.25)
    args = parser.parse_args()

    candles = load_candles(args.days, refresh=args.refresh)
    features = compute_all_features(candles)
    labeled = add_window_targets(features)
    sampled = sample_decision_rows(labeled, parse_buckets(args.buckets))
    if sampled.empty:
        raise SystemExit("No historical decision rows were produced.")

    sampled = sampled.dropna(subset=FEATURE_COLUMNS + ["target_up", "baseline", "close"])
    sampled = simulate_polymarket_quotes(sampled)
    sampled = add_base_model_context(sampled)
    X, columns = feature_matrix(sampled)
    y = sampled["target_up"].astype(int)

    split_at = max(1, int(len(sampled) * (1 - args.test_size)))
    split_at = min(split_at, len(sampled) - 1)
    X_train, X_val = X.iloc[:split_at], X.iloc[split_at:]
    y_train, y_val = y.iloc[:split_at], y.iloc[split_at:]

    results = {}
    fitted = {}
    for name, model in make_models().items():
        model.fit(X_train, y_train)
        results[name] = score(model, X_val, y_val)
        fitted[name] = model

    best_name, best_metrics = sorted(results.items(), key=lambda item: (item[1]["brier_score"], -item[1]["accuracy"]))[0]
    best_model = fitted[best_name]
    model_version = f"historical-sim-{best_name}-v1"

    metrics = {
        **best_metrics,
        "all_model_metrics": results,
        "algorithm": best_name,
        "model_version": model_version,
        "source": "historical_15m_polymarket_sim",
        "rows": int(len(sampled)),
        "unique_rounds": int(sampled["window_id"].nunique()),
        "train_rows": int(len(X_train)),
        "val_rows": int(len(X_val)),
        "window_seconds": WINDOW_SECONDS,
        "buckets": parse_buckets(args.buckets),
        "feature_count": len(columns),
        "uses_simulated_polymarket_quotes": True,
        "trained_at": datetime.now(timezone.utc).isoformat(),
    }

    artifact = {
        "model": best_model,
        "columns": columns,
        "feature_columns": columns,
        "metrics": metrics,
        "model_version": model_version,
        "feature_set_version": "historical-polymarket-sim-v1",
    }

    MODEL_DIR.mkdir(exist_ok=True)
    with open(MODEL_DIR / "model_supabase.pkl", "wb") as f:
        pickle.dump(artifact, f)
    with open(MODEL_DIR / "metrics_supabase.json", "w", encoding="utf-8") as f:
        json.dump(json_safe(metrics), f, indent=2)
    with open(MODEL_DIR / f"{model_version}.pkl", "wb") as f:
        pickle.dump(artifact, f)
    with open(MODEL_DIR / f"{model_version}_metrics.json", "w", encoding="utf-8") as f:
        json.dump(json_safe(metrics), f, indent=2)

    print(json.dumps(json_safe(metrics), indent=2))


if __name__ == "__main__":
    main()
