"""
Walk-forward validation of the Edge Base strategy.

Expanding window: train on all rounds before fold, test on the next `step` rounds the
model never saw, roll forward. For each fold we retrain and run the Edge Base rule
(buy the side with edge = model_prob - ask >= threshold; stop at T-30). If the strategy
is genuinely +EV it should be positive across MOST folds. If it swings around 0, the
single test-split result was regime/overfit.

Uses HistGradientBoosting (fast, reasonably calibrated) on the SAME clean features.
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import Pipeline

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import db
from train_model_v2 import flatten_features
from strategy_walkforward import FEE_RATE, leg_pnl


def build_model():
    return Pipeline([
        ("impute", SimpleImputer(strategy="median")),
        ("m", HistGradientBoostingClassifier(learning_rate=0.05, max_iter=200,
                                             max_leaf_nodes=15, l2_regularization=0.1,
                                             random_state=42)),
    ])


def edge_legs(te, probs, threshold=0.03):
    up = pd.to_numeric(te.get("up_best_ask"), errors="coerce").to_numpy()
    dn = pd.to_numeric(te.get("down_best_ask"), errors="coerce").to_numpy()
    bk = pd.to_numeric(te.get("seconds_bucket"), errors="coerce").to_numpy()
    oc = te["outcome"].to_numpy()
    legs = []
    for i in range(len(te)):
        if not np.isnan(bk[i]) and bk[i] <= 15:
            continue
        pu = probs[i]
        eu = pu - up[i] if not np.isnan(up[i]) else -9
        ed = (1 - pu) - dn[i] if not np.isnan(dn[i]) else -9
        side = entry = None
        edge = threshold
        if eu >= edge:
            side, entry, edge = "UP", up[i], eu
        if ed >= edge:
            side, entry = "DOWN", dn[i]
        if side is None or oc[i] not in ("UP", "DOWN"):
            continue
        legs.append((float(entry), side == oc[i]))
    return legs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fee-rate", type=float, default=FEE_RATE,
                        help="crypto_fees_v2 taker rate (0 = ignore fees, old behavior)")
    args = parser.parse_args()
    fee_rate = args.fee_rate
    frame = pd.DataFrame(db.fetch_training_decision_snapshots(50000))
    frame = frame[frame["target_up"].notna()].copy()
    rounds = sorted(frame["round_cutoff"].unique())
    start = max(250, int(len(rounds) * 0.45))
    step = 30
    print(f"total rounds={len(rounds)}  initial train={start}  step={step} rounds/fold  fee_rate={fee_rate}\n")
    print(f"{'fold':>4} {'trainR':>7} {'testR':>6} {'sigs':>5} {'win%':>6} {'avgEnt':>7} {'net':>9} {'roi':>7} {'initAUC':>8}")
    agg = []
    fold = 0
    i = start
    while i < len(rounds):
        fold += 1
        tr = frame[frame["round_cutoff"].isin(set(rounds[:i]))]
        te = frame[frame["round_cutoff"].isin(set(rounds[i:i + step]))]
        if te.empty:
            break
        Xtr = flatten_features(tr).apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan)
        ytr = pd.to_numeric(tr["target_up"], errors="coerce").astype(int)
        Xte = flatten_features(te).reindex(columns=Xtr.columns).apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan)
        model = build_model()
        model.fit(Xtr, ytr)
        p = model.predict_proba(Xte)[:, 1]
        legs = edge_legs(te, p)
        if legs:
            net = sum(leg_pnl(e, w, fee_rate) for e, w in legs)
            wr = np.mean([1.0 if w else 0.0 for _, w in legs])
            ae = np.mean([e for e, _ in legs])
            roi = net / len(legs)
        else:
            net = roi = 0.0
            wr = ae = float("nan")
        bk = pd.to_numeric(te["seconds_bucket"], errors="coerce").to_numpy()
        yte = pd.to_numeric(te["target_up"], errors="coerce").astype(int).to_numpy()
        im = bk == np.nanmax(bk)
        try:
            iauc = roc_auc_score(yte[im], p[im]) if len(np.unique(yte[im])) > 1 else float("nan")
        except Exception:
            iauc = float("nan")
        agg.append((len(legs), net))
        print(f"{fold:>4} {i:>7} {len(set(rounds[i:i+step])):>6} {len(legs):>5} {wr*100:>5.1f} {ae:>7.3f} {net:>9.2f} {roi*100:>6.1f}% {iauc:>8.3f}")
        i += step
    tot_s = sum(a[0] for a in agg)
    tot_n = sum(a[1] for a in agg)
    pos = sum(1 for a in agg if a[1] > 0)
    print(f"\nWALK-FORWARD TOTAL: signals={tot_s} net={tot_n:.2f} roi={100*tot_n/tot_s if tot_s else 0:.1f}%")
    print(f"folds net-positive: {pos}/{len(agg)}  (real edge => most folds positive AND total roi > 0 after costs)")


if __name__ == "__main__":
    main()
