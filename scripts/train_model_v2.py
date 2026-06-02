import argparse
import hashlib
import json
import os
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sklearn.calibration import CalibratedClassifierCV
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import ExtraTreesClassifier, HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.impute import SimpleImputer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import db
from features import FEATURE_COLUMNS

load_dotenv()

# Only these technical features are valid model inputs. Anything else found in the
# stored feature JSON (model outputs like prob_up/edge/confidence, poly_* quotes,
# or feat__-duplicated keys from old collector versions) is leakage and is dropped.
ALLOWED_TECH_FEATURES = set(FEATURE_COLUMNS)


def _strip_feat_prefix(key):
    while key.startswith("feat__"):
        key = key[len("feat__"):]
    return key


QUOTE_COLUMNS = [
    "up_best_bid",
    "up_best_ask",
    "up_midpoint",
    "up_spread",
    "up_bid_size",
    "up_ask_size",
    "up_last_trade_price",
    "down_best_bid",
    "down_best_ask",
    "down_midpoint",
    "down_spread",
    "down_bid_size",
    "down_ask_size",
    "down_last_trade_price",
]

CONTEXT_COLUMNS = [
    "seconds_to_cutoff",
    "seconds_bucket",
    "btc_price",
    "baseline",
    "dist_to_baseline",
    "dist_to_baseline_pct",
]


def fetch_rows(limit):
    return db.fetch_training_decision_snapshots(limit)


def flatten_features(frame):
    feature_rows = []
    for item in frame.get("features", []):
        if isinstance(item, dict):
            raw = item
        elif isinstance(item, str):
            try:
                raw = json.loads(item)
            except json.JSONDecodeError:
                raw = {}
            if not isinstance(raw, dict):
                raw = {}
        else:
            raw = {}
        # Sanitize: collapse repeated feat__ prefixes and keep ONLY whitelisted
        # technical features. Drops leaked model outputs (prob_up/edge/confidence),
        # poly_* quote dupes, and context dupes that must not be model inputs.
        clean = {}
        for key, value in raw.items():
            base_name = _strip_feat_prefix(key)
            if base_name in ALLOWED_TECH_FEATURES:
                clean[f"feat__{base_name}"] = value
        feature_rows.append(clean)
    features = pd.DataFrame(feature_rows)
    # Guarantee a stable, complete technical column set (missing -> NaN -> imputed).
    for col in (f"feat__{name}" for name in FEATURE_COLUMNS):
        if col not in features.columns:
            features[col] = np.nan
    features = features[[f"feat__{name}" for name in FEATURE_COLUMNS]]
    base = frame[[col for col in CONTEXT_COLUMNS + QUOTE_COLUMNS if col in frame]].copy()
    matrix = pd.concat([base.reset_index(drop=True), features.reset_index(drop=True)], axis=1)
    matrix = matrix.apply(pd.to_numeric, errors="coerce")
    matrix = matrix.replace([np.inf, -np.inf], np.nan)
    return matrix


def temporal_split(frame, test_size):
    # Split by unique rounds to prevent same-round leakage across train/test
    unique_rounds = sorted(frame["round_cutoff"].unique())
    n_test = max(1, int(len(unique_rounds) * test_size))
    cutoff_round = unique_rounds[-n_test]
    train = frame[frame["round_cutoff"] < cutoff_round].copy()
    test = frame[frame["round_cutoff"] >= cutoff_round].copy()
    return train.reset_index(drop=True), test.reset_index(drop=True)


def make_models(random_state):
    return {
        "dummy": DummyClassifier(strategy="prior"),
        "logistic": Pipeline([
            ("impute", SimpleImputer(strategy="median")),
            ("scale", StandardScaler()),
            ("model", LogisticRegression(max_iter=1000, class_weight="balanced")),
        ]),
        "random_forest": CalibratedClassifierCV(
            Pipeline([
                ("impute", SimpleImputer(strategy="median")),
                ("model", RandomForestClassifier(
                    n_estimators=250,
                    min_samples_leaf=8,
                    class_weight="balanced_subsample",
                    random_state=random_state,
                    n_jobs=int(os.getenv("TRAINING_N_JOBS", "-1")),
                )),
            ]),
            cv=3, method="isotonic",
        ),
        "extra_trees": CalibratedClassifierCV(
            Pipeline([
                ("impute", SimpleImputer(strategy="median")),
                ("model", ExtraTreesClassifier(
                    n_estimators=300,
                    min_samples_leaf=8,
                    class_weight="balanced",
                    random_state=random_state,
                    n_jobs=int(os.getenv("TRAINING_N_JOBS", "-1")),
                )),
            ]),
            cv=3, method="isotonic",
        ),
        "hist_gradient_boosting": Pipeline([
            ("impute", SimpleImputer(strategy="median")),
            ("model", HistGradientBoostingClassifier(
                learning_rate=0.04,
                max_iter=200,
                max_leaf_nodes=15,
                l2_regularization=0.05,
                random_state=random_state,
            )),
        ]),
    }


def score_model(model, x_test, y_test):
    probs = model.predict_proba(x_test)[:, 1]
    preds = (probs >= 0.5).astype(int)
    metrics = {
        "accuracy": float(np.mean(preds == y_test)),
        "brier_score": float(brier_score_loss(y_test, probs)),
        "log_loss": float(log_loss(y_test, probs, labels=[0, 1])),
    }
    if len(np.unique(y_test)) == 2:
        metrics["roc_auc"] = float(roc_auc_score(y_test, probs))
    else:
        metrics["roc_auc"] = None
    return metrics


def dataset_version(frame, filters):
    digest = hashlib.sha256(json.dumps(filters, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return f"ds_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{digest}"


def json_safe(value):
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [json_safe(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def main():
    parser = argparse.ArgumentParser(description="Train and track a v2 model from training_decision_snapshots.")
    parser.add_argument("--limit", type=int, default=int(os.getenv("TRAINING_ROW_LIMIT", "50000")))
    parser.add_argument("--test-size", type=float, default=0.25)
    parser.add_argument("--min-rounds", type=int, default=int(os.getenv("MIN_UNIQUE_ROUNDS", "500")))
    parser.add_argument("--force", action="store_true", help="Train even if the resolved-round count is below --min-rounds.")
    parser.add_argument("--activate", action="store_true", help="Promote the trained artifact to model_supabase.pkl for live use.")
    parser.add_argument("--model-version", default=None)
    parser.add_argument("--feature-set-version", default=os.getenv("FEATURE_SET_VERSION", "market-features-v1"))
    parser.add_argument("--notes", default="")
    args = parser.parse_args()

    if not db.db_enabled():
        raise SystemExit("No database backend is configured. Set DB_BACKEND=sqlserver or configure Supabase.")

    rows = fetch_rows(args.limit)
    frame = pd.DataFrame(rows)
    if frame.empty:
        raise SystemExit("training_decision_snapshots returned no resolved rows.")
    unique_rounds = int(frame["round_cutoff"].nunique())
    if unique_rounds < args.min_rounds and not args.force:
        raise SystemExit(
            f"not_enough_resolved_rounds: got {unique_rounds}, need {args.min_rounds}. "
            "Keep collector.py running or pass --force for a throwaway experiment."
        )
    if frame["target_up"].nunique() < 2:
        raise SystemExit("not_enough_classes: resolved rows only contain one outcome direction so far.")

    filters = {"view": "training_decision_snapshots", "limit": args.limit, "target_up": "not null"}
    ds_version = dataset_version(frame, filters)
    db.insert_dataset_version(
        {
            "dataset_version": ds_version,
            "source_query_hash": ds_version.rsplit("_", 1)[-1],
            "row_count": int(len(frame)),
            "round_count": unique_rounds,
            "start_time": frame["observed_at"].min(),
            "end_time": frame["observed_at"].max(),
            "filters": filters,
            "parquet_path": None,
            "raw": {"script": "scripts/train_model_v2.py"},
        }
    )

    train_df, test_df = temporal_split(frame, args.test_size)
    x_train = flatten_features(train_df)
    x_test = flatten_features(test_df).reindex(columns=x_train.columns)
    y_train = pd.to_numeric(train_df["target_up"], errors="coerce").astype(int)
    y_test = pd.to_numeric(test_df["target_up"], errors="coerce").astype(int)
    dataset_stats = {
        "rows": int(len(frame)),
        "unique_rounds": unique_rounds,
        "train_rows": int(len(train_df)),
        "test_rows": int(len(test_df)),
        "start_time": str(frame["observed_at"].min()),
        "end_time": str(frame["observed_at"].max()),
        "target_up_rate": float(pd.to_numeric(frame["target_up"], errors="coerce").mean()),
        "feature_count": int(len(x_train.columns)),
        "test_size": float(args.test_size),
    }

    results = {}
    fitted = {}
    for name, estimator in make_models(random_state=42).items():
        estimator.fit(x_train, y_train)
        results[name] = score_model(estimator, x_test, y_test)
        fitted[name] = estimator

    # Rank by AUC (higher = better discriminative power); fall back to brier if all null
    auc_ranked = sorted(
        [(n, m) for n, m in results.items() if m.get("roc_auc") is not None],
        key=lambda item: item[1]["roc_auc"],
        reverse=True,
    )
    ranked = auc_ranked if auc_ranked else sorted(
        results.items(), key=lambda item: item[1]["brier_score"]
    )
    best_name, best_metrics = ranked[0]
    best_model = fitted[best_name]
    model_version = args.model_version or f"{best_name}-v2-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"

    importance = {}
    try:
        perm = permutation_importance(best_model, x_test, y_test, n_repeats=5, random_state=42, n_jobs=int(os.getenv("TRAINING_N_JOBS", "-1")))
        importance = dict(
            sorted(
                zip(x_train.columns, perm.importances_mean),
                key=lambda item: abs(item[1]),
                reverse=True,
            )[:40]
        )
    except Exception as exc:
        importance = {"error": str(exc)}

    artifact = {
        "model": best_model,
        "columns": list(x_train.columns),
        "feature_columns": list(x_train.columns),
        "model_version": model_version,
        "dataset_version": ds_version,
        "feature_set_version": args.feature_set_version,
        "metrics": {**best_metrics, "dataset": dataset_stats},
        "all_model_metrics": results,
    }

    os.makedirs("model_artifacts", exist_ok=True)
    artifact_path = os.path.join("model_artifacts", f"{model_version}.pkl")
    metrics_path = os.path.join("model_artifacts", f"{model_version}_metrics.json")
    metrics_payload = {
        **best_metrics,
        "dataset": dataset_stats,
        "all_model_metrics": results,
        "feature_importance": importance,
        "activated": bool(args.activate),
    }
    with open(artifact_path, "wb") as f:
        pickle.dump(artifact, f)
    with open(metrics_path, "w", encoding="utf-8") as f:
        json.dump(json_safe(metrics_payload), f, indent=2)

    if args.activate:
        with open(os.path.join("model_artifacts", "model_supabase.pkl"), "wb") as f:
            pickle.dump(artifact, f)
        with open(os.path.join("model_artifacts", "metrics_supabase.json"), "w", encoding="utf-8") as f:
            json.dump(json_safe(metrics_payload), f, indent=2)

    run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{best_name}"
    db.insert_model_run(
        {
            "run_id": run_id,
            "model_version": model_version,
            "dataset_version": ds_version,
            "feature_set_version": args.feature_set_version,
            "algorithm": best_name,
            "hyperparameters": json_safe(best_model.get_params() if hasattr(best_model, "get_params") else {}),
            "metrics": json_safe(metrics_payload),
            "artifact_path": artifact_path,
            "model_stage": "production" if args.activate else "candidate",
            "notes": args.notes,
        }
    )
    if args.activate and hasattr(db, "promote_model_version"):
        db.promote_model_version(model_version)

    print(json.dumps({
        "run_id": run_id,
        "model_version": model_version,
        "dataset_version": ds_version,
        "algorithm": best_name,
        "metrics": best_metrics,
        "dataset": dataset_stats,
        "artifact_path": artifact_path,
        "activated": bool(args.activate),
    }, indent=2))


if __name__ == "__main__":
    main()
