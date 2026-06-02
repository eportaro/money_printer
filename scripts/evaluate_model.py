"""
Honest evaluation harness for a v2 model.

Separates real anticipatory signal from artifacts:
- accuracy/AUC by seconds_bucket (late buckets are near-decided -> trivially high)
- INITIAL-15m accuracy (earliest bucket only = true 15-min-ahead anticipation)
- model vs MARKET: does the model beat the Polymarket midpoint, and when they
  disagree, who is right more often (the only path to a real edge)

Usage:
    python scripts/evaluate_model.py --model model_artifacts/<version>.pkl
"""
import argparse
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import db
from train_model_v2 import flatten_features, temporal_split


def _auc(y, p):
    y = np.asarray(y)
    if len(np.unique(y)) < 2:
        return None
    return round(float(roc_auc_score(y, p)), 4)


def _acc(y, pred):
    y = np.asarray(y)
    if len(y) == 0:
        return None
    return round(float(np.mean(np.asarray(pred) == y)), 4)


def market_up_prob(frame):
    up = pd.to_numeric(frame.get("up_midpoint"), errors="coerce")
    down = pd.to_numeric(frame.get("down_midpoint"), errors="coerce")
    # midpoints sometimes only present on one side; reconcile to an UP probability
    implied = up.copy()
    implied = implied.where(implied.notna(), 1.0 - down)
    return implied


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="model_artifacts/model_supabase.pkl")
    parser.add_argument("--limit", type=int, default=50000)
    parser.add_argument("--test-size", type=float, default=0.25)
    args = parser.parse_args()

    with open(args.model, "rb") as f:
        art = pickle.load(f)
    model = art["model"]
    columns = art["columns"]

    frame = pd.DataFrame(db.fetch_training_decision_snapshots(args.limit))
    if frame.empty:
        raise SystemExit("no data")
    _, test = temporal_split(frame, args.test_size)

    X = flatten_features(test).reindex(columns=columns, fill_value=0.0)
    X = X.apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(0.0)
    y = pd.to_numeric(test["target_up"], errors="coerce").astype(int).to_numpy()
    p = model.predict_proba(X)[:, 1]
    pred = (p >= 0.5).astype(int)

    mkt = market_up_prob(test).to_numpy()
    mkt_pred = np.where(np.isnan(mkt), -1, (mkt >= 0.5).astype(int))
    bucket = pd.to_numeric(test["seconds_bucket"], errors="coerce").to_numpy()

    print(f"model: {args.model}")
    print(f"test rows: {len(y)}  unique rounds: {test['round_cutoff'].nunique()}  up_rate: {y.mean():.3f}")
    print(f"OVERALL  model_acc={_acc(y, pred)}  model_auc={_auc(y, p)}")
    mkt_mask = mkt_pred >= 0
    print(f"OVERALL  market_acc={_acc(y[mkt_mask], mkt_pred[mkt_mask])}  market_auc={_auc(y[mkt_mask], mkt[mkt_mask])}")

    print("\nBY BUCKET (seconds_to_cutoff):")
    print(f"{'bucket':>8} {'n':>5} {'model_acc':>10} {'model_auc':>10} {'market_acc':>11}")
    for b in sorted(set(int(x) for x in bucket if not np.isnan(x)), reverse=True):
        m = bucket == b
        mm = m & (mkt_pred >= 0)
        print(f"{b:>8} {int(m.sum()):>5} {str(_acc(y[m], pred[m])):>10} {str(_auc(y[m], p[m])):>10} {str(_acc(y[mm], mkt_pred[mm])):>11}")

    # INITIAL-15m: earliest bucket per round = true 15-min-ahead call
    initb = int(np.nanmax(bucket))
    im = bucket == initb
    print(f"\nINITIAL bucket T-{initb} (true 15-min anticipation): n={int(im.sum())}")
    print(f"  model_acc={_acc(y[im], pred[im])}  model_auc={_auc(y[im], p[im])}  market_acc={_acc(y[im & (mkt_pred>=0)], mkt_pred[im & (mkt_pred>=0)])}")

    # BEAT THE MARKET: where model disagrees with the market, who is right?
    dis = (mkt_pred >= 0) & (pred != mkt_pred)
    if dis.sum() > 0:
        model_right = float(np.mean(pred[dis] == y[dis]))
        market_right = float(np.mean(mkt_pred[dis] == y[dis]))
        print(f"\nDISAGREEMENTS with market: n={int(dis.sum())} ({100*dis.sum()/mkt_mask.sum():.1f}% of rows)")
        print(f"  when they disagree -> model right {100*model_right:.1f}%  vs  market right {100*market_right:.1f}%")
        print("  (model needs to be clearly >50% here to have any real edge)")
    else:
        print("\nNo disagreements with market (model just follows the quotes).")


if __name__ == "__main__":
    main()
