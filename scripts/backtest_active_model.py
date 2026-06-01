import argparse
import json
import os
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import db
from train_model_v2 import flatten_features, json_safe

load_dotenv(PROJECT_ROOT / ".env")

BUCKETS = [895, 840, 720, 600, 480, 360, 240, 180, 120, 90, 60, 30, 15, 5]
STRATEGY_SET_VERSION = "dashboard-strategies-v1"

STRATEGIES = {
    "conservative": {
        "name": "Directional Conservative",
        "allowed_buckets": [480, 360, 240],
        "min_prob": 0.55,
        "min_edge": 0.05,
        "allow_contrarian": False,
        "max_entry": 0.95,
        "exclude_below": 120,
    },
    "value": {
        "name": "Value Bet / Longshot",
        "allowed_buckets": BUCKETS,
        "min_prob": 0.01,
        "min_edge": 0.10,
        "allow_contrarian": True,
        "max_entry": 0.25,
        "exclude_below": 0,
        "min_ask_size": 50,
    },
    # --- High-edge variants: require larger edge to filter noise ---
    "value_high_edge": {
        "name": "Value Bet / High Edge",
        "allowed_buckets": BUCKETS,
        "min_prob": 0.01,
        "min_edge": 0.15,
        "allow_contrarian": True,
        "max_entry": 0.20,
        "exclude_below": 0,
        "min_ask_size": 50,
    },
    # --- Late precision: only bet in last 2 minutes where odds are most informed ---
    "late_value": {
        "name": "Late Value (T-120 to T-15)",
        "allowed_buckets": [120, 90, 60, 30, 15],
        "min_prob": 0.01,
        "min_edge": 0.10,
        "allow_contrarian": True,
        "max_entry": 0.25,
        "exclude_below": 0,
        "min_ask_size": 50,
    },
    # --- Aligned value: longshot but only when model agrees (no contrarian) ---
    "value_aligned": {
        "name": "Value Bet / Aligned Only",
        "allowed_buckets": BUCKETS,
        "min_prob": 0.40,
        "min_edge": 0.10,
        "allow_contrarian": False,
        "max_entry": 0.40,
        "exclude_below": 0,
        "min_ask_size": 50,
    },
    "custom": {
        "name": "Custom Strategy",
        "allowed_buckets": [480, 360, 240],
        "min_prob": 0.55,
        "min_edge": 0.05,
        "allow_contrarian": False,
        "max_entry": 0.95,
        "exclude_below": 120,
    },
}


def finite_float(value):
    if value is None or value == "":
        return None
    try:
        numeric = float(value)
    except Exception:
        return None
    if not np.isfinite(numeric):
        return None
    return numeric


def simulated_pnl(stake, entry_price, won):
    if not won:
        return -float(stake)
    if not entry_price:
        return 0.0
    # 2% Polymarket fee on gross profit
    return float(stake) * ((1.0 / float(entry_price)) - 1.0) * 0.98


def classify_alignment(prediction, action):
    if action == "BUY_UP" and prediction == "UP":
        return "ALIGNED"
    if action == "BUY_DOWN" and prediction == "DOWN":
        return "ALIGNED"
    if action == "BUY_UP" and prediction == "DOWN":
        return "CONTRARIAN"
    if action == "BUY_DOWN" and prediction == "UP":
        return "CONTRARIAN"
    return "NO_SIGNAL"


def candidate_sides(row, strategy):
    prob_up = finite_float(row["bt_prob_up"])
    prob_down = finite_float(row["bt_prob_down"])
    up_ask = finite_float(row.get("up_best_ask"))
    down_ask = finite_float(row.get("down_best_ask"))
    up_ask_size = finite_float(row.get("up_ask_size"))
    down_ask_size = finite_float(row.get("down_ask_size"))
    prediction = row["bt_prediction"]
    min_ask_size = strategy.get("min_ask_size", 0)
    candidates = []

    def ask_size_ok(ask_size):
        return min_ask_size <= 0 or (ask_size is not None and ask_size >= min_ask_size)

    if strategy["allow_contrarian"]:
        if prob_up is not None and up_ask is not None and ask_size_ok(up_ask_size):
            candidates.append(("UP", prob_up, up_ask, prob_up - up_ask))
        if prob_down is not None and down_ask is not None and ask_size_ok(down_ask_size):
            candidates.append(("DOWN", prob_down, down_ask, prob_down - down_ask))
    elif prediction == "UP" and prob_up is not None and up_ask is not None and ask_size_ok(up_ask_size):
        candidates.append(("UP", prob_up, up_ask, prob_up - up_ask))
    elif prediction == "DOWN" and prob_down is not None and down_ask is not None and ask_size_ok(down_ask_size):
        candidates.append(("DOWN", prob_down, down_ask, prob_down - down_ask))

    candidates.sort(key=lambda item: item[3], reverse=True)
    return candidates


def evaluate_strategy(row, strategy):
    bucket = int(row.get("seconds_bucket") or 0)
    failed = []
    passed = []

    if bucket in strategy["allowed_buckets"]:
        passed.append(f"bucket T-{bucket} allowed")
    else:
        failed.append(f"bucket T-{bucket} outside selected strategy")

    if bucket > strategy["exclude_below"]:
        passed.append(f"not inside last {strategy['exclude_below']}s")
    else:
        failed.append(f"bucket T-{bucket} excluded by last-minute filter")

    for side, model_prob, entry_price, edge in candidate_sides(row, strategy):
        action = "BUY_UP" if side == "UP" else "BUY_DOWN"
        alignment = classify_alignment(row["bt_prediction"], action)
        local_failed = list(failed)
        local_passed = list(passed)

        if model_prob >= strategy["min_prob"]:
            local_passed.append("model probability passed")
        else:
            local_failed.append("model probability below threshold")

        if edge >= strategy["min_edge"]:
            local_passed.append("edge passed")
        else:
            local_failed.append("edge below threshold")

        if entry_price <= strategy["max_entry"]:
            local_passed.append("entry price passed")
        else:
            local_failed.append("entry price above threshold")

        if strategy["allow_contrarian"] or alignment != "CONTRARIAN":
            local_passed.append("alignment allowed")
        else:
            local_failed.append("contrarian signal not allowed")

        if not local_failed:
            return {
                "action": action,
                "side": side,
                "entry_price": entry_price,
                "model_prob": model_prob,
                "edge": edge,
                "alignment": alignment,
                "reason": ", ".join(local_passed),
            }

    return None


def load_artifact(path):
    with open(path, "rb") as handle:
        return pickle.load(handle)


def main():
    parser = argparse.ArgumentParser(description="Backtest the active market-aware model by strategy.")
    parser.add_argument("--artifact", default=str(PROJECT_ROOT / "model_artifacts" / "model_supabase.pkl"))
    parser.add_argument("--limit", type=int, default=int(os.getenv("TRAINING_ROW_LIMIT", "50000")))
    parser.add_argument("--stake", type=float, default=float(os.getenv("STAKE_SIZE", "1")))
    parser.add_argument("--strategy-set-version", default=STRATEGY_SET_VERSION)
    parser.add_argument("--model-version", default="active")
    args = parser.parse_args()

    if not db.db_enabled():
        raise SystemExit("No database backend is configured.")

    artifact = load_artifact(args.artifact)
    artifact_model_version = artifact.get("model_version", "unknown-model")
    model_version = artifact_model_version if args.model_version == "active" else args.model_version
    dataset_version = artifact.get("dataset_version")
    columns = artifact["columns"]
    model = artifact["model"]

    rows = db.fetch_training_decision_snapshots(args.limit)
    frame = pd.DataFrame(rows)
    if frame.empty:
        raise SystemExit("No resolved snapshots available for backtest.")
    frame = frame[frame["outcome"].isin(["UP", "DOWN"])].copy()
    if frame.empty:
        raise SystemExit("No UP/DOWN resolved snapshots available for backtest.")

    # Only evaluate on the test-set rounds (last 25% by time) so metrics are out-of-sample
    test_size = artifact.get("metrics", {}).get("dataset", {}).get("test_size", 0.25)
    unique_rounds = sorted(frame["round_cutoff"].unique())
    n_test = max(1, int(len(unique_rounds) * test_size))
    test_cutoff_round = unique_rounds[-n_test]
    frame = frame[frame["round_cutoff"] >= test_cutoff_round].copy()
    if frame.empty:
        raise SystemExit("No test-set rounds available.")
    print(f"[backtest] evaluating {frame['round_cutoff'].nunique()} out-of-sample rounds "
          f"({len(frame)} rows, cutoff >= round {test_cutoff_round})", flush=True)

    x_all = flatten_features(frame).reindex(columns=columns)
    probs_up = model.predict_proba(x_all)[:, 1]
    frame["bt_prob_up"] = probs_up
    frame["bt_prob_down"] = 1.0 - probs_up
    frame["bt_prediction"] = np.where(probs_up >= 0.5, "UP", "DOWN")
    frame["bt_confidence"] = np.abs(probs_up - 0.5) * 2

    now = datetime.now(timezone.utc)
    run_id = f"bt_{now.strftime('%Y%m%d_%H%M%S')}_{model_version}"
    run_pk = db.insert_strategy_backtest_run(
        {
            "run_id": run_id,
            "model_version": model_version,
            "dataset_version": dataset_version,
            "strategy_set_version": args.strategy_set_version,
            "source_row_count": int(len(frame)),
            "resolved_round_count": int(frame["round_cutoff"].nunique()),
            "signal_count": 0,
            "started_at": now.isoformat(),
            "metrics": {},
            "raw": {"artifact": args.artifact, "strategies": STRATEGIES},
        }
    )

    signal_count = 0
    pnl_by_strategy = {key: 0.0 for key in STRATEGIES}
    signals_by_strategy = {key: 0 for key in STRATEGIES}
    wins_by_strategy = {key: 0 for key in STRATEGIES}
    losses_by_strategy = {key: 0 for key in STRATEGIES}

    for _, row in frame.iterrows():
        for key, strategy in STRATEGIES.items():
            decision = evaluate_strategy(row, strategy)
            if not decision:
                continue

            outcome = row.get("outcome")
            won = decision["side"] == outcome
            result = "WIN" if won else "LOSS"
            pnl = simulated_pnl(args.stake, decision["entry_price"], won)
            roi = pnl / args.stake if args.stake else None

            db.insert_strategy_backtest_signal(
                {
                    "backtest_run_id": run_pk,
                    "snapshot_id": row["snapshot_id"],
                    "round_id": row["round_pk"],
                    "observed_at": row["observed_at"],
                    "round_cutoff": row["round_cutoff"],
                    "seconds_to_cutoff": row.get("seconds_to_cutoff"),
                    "seconds_bucket": row.get("seconds_bucket"),
                    "model_version": model_version,
                    "model_stage": "backtest",
                    "strategy_version": key,
                    "action": decision["action"],
                    "side": decision["side"],
                    "prediction": row["bt_prediction"],
                    "prob_up": round(float(row["bt_prob_up"]), 8),
                    "prob_down": round(float(row["bt_prob_down"]), 8),
                    "confidence": round(float(row["bt_confidence"]), 8),
                    "entry_price": round(float(decision["entry_price"]), 6),
                    "model_prob": round(float(decision["model_prob"]), 8),
                    "edge": round(float(decision["edge"]), 8),
                    "stake": args.stake,
                    "alignment": decision["alignment"],
                    "result": result,
                    "pnl": round(float(pnl), 6),
                    "roi": None if roi is None else round(float(roi), 8),
                    "actual_close": row.get("actual_close"),
                    "outcome": outcome,
                    "reason": decision["reason"],
                    "raw": {
                        "strategy": strategy,
                        "source_prediction_model_version": row.get("model_version"),
                        "source_prediction_id": row.get("prediction_id"),
                    },
                }
            )
            signal_count += 1
            signals_by_strategy[key] += 1
            pnl_by_strategy[key] += float(pnl)
            if result == "WIN":
                wins_by_strategy[key] += 1
            else:
                losses_by_strategy[key] += 1

    metrics = {
        "model_version": model_version,
        "dataset_version": dataset_version,
        "source_rows": int(len(frame)),
        "resolved_rounds": int(frame["round_cutoff"].nunique()),
        "signal_count": signal_count,
        "strategy": {
            key: {
                "signals": signals_by_strategy[key],
                "wins": wins_by_strategy[key],
                "losses": losses_by_strategy[key],
                "win_rate": wins_by_strategy[key] / signals_by_strategy[key] if signals_by_strategy[key] else None,
                "pnl": pnl_by_strategy[key],
            }
            for key in STRATEGIES
        },
    }
    db.update_strategy_backtest_run(
        run_id,
        {
            "signal_count": signal_count,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "metrics": json_safe(metrics),
        },
    )

    print(json.dumps(json_safe({"run_id": run_id, **metrics}), indent=2))


if __name__ == "__main__":
    main()
