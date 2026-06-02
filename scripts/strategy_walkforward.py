"""
Walk-forward backtest of EVERY strategy (the user's intended ones + current dashboard
ones), out-of-sample. Each fold: train model on past rounds, predict test rounds, apply
each strategy's exact decision rule, accumulate PnL. Gives the honest per-strategy number
(not the lucky single test split) and tells whether ANY rule/combination is +EV.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import db
from train_model_v2 import flatten_features

ALL_BUCKETS = {895, 840, 720, 600, 480, 360, 240, 180, 120, 90, 60, 30, 15, 5}

STRATS = {
    "dir_conservative":  dict(buckets={480, 360, 240}, minProb=0.55, minEdge=0.05, maxEntry=0.95, contrarian=False, excludeBelow=120, minSize=0),
    "dir_aggressive":    dict(buckets={600, 480, 360, 240, 180}, minProb=0.50, minEdge=0.03, maxEntry=0.98, contrarian=False, excludeBelow=120, minSize=0),
    "no_last_minute":    dict(buckets={895, 840, 720, 600, 480, 360, 240, 180}, minProb=0.50, minEdge=0.03, maxEntry=0.95, contrarian=False, excludeBelow=120, minSize=0),
    "value_longshot":    dict(buckets=ALL_BUCKETS, minProb=0.01, minEdge=0.10, maxEntry=0.25, contrarian=True, excludeBelow=0, minSize=50),
    "value_high_edge":   dict(buckets=ALL_BUCKETS, minProb=0.01, minEdge=0.15, maxEntry=0.20, contrarian=True, excludeBelow=0, minSize=50),
    "value_aligned":     dict(buckets=ALL_BUCKETS, minProb=0.40, minEdge=0.10, maxEntry=0.40, contrarian=False, excludeBelow=0, minSize=50),
    "edge_base_collector": dict(buckets=ALL_BUCKETS, minProb=0.0, minEdge=0.05, maxEntry=1.0, contrarian=True, excludeBelow=0, minSize=0),
    "favorite_88_96":    dict(mode="favorite", buckets=ALL_BUCKETS, minEntry=0.88, maxEntry=0.96, excludeBelow=0, minSize=0),
}


def decide(s, prob_up, up_ask, up_sz, dn_ask, dn_sz, bucket):
    if bucket not in s["buckets"] or bucket <= s.get("excludeBelow", 0):
        return None
    if s.get("mode") == "favorite":
        cands = []
        if not np.isnan(up_ask):
            cands.append(("UP", up_ask, up_sz))
        if not np.isnan(dn_ask):
            cands.append(("DOWN", dn_ask, dn_sz))
        cands.sort(key=lambda c: -c[1])
        if not cands:
            return None
        side, entry, sz = cands[0]
        if entry < s["minEntry"] or entry > s["maxEntry"]:
            return None
        return side, entry
    pred = "UP" if prob_up >= 0.5 else "DOWN"
    cands = []
    if s["contrarian"] or pred == "UP":
        if not np.isnan(up_ask):
            cands.append(("UP", prob_up, up_ask, up_sz, prob_up - up_ask))
    if s["contrarian"] or pred == "DOWN":
        if not np.isnan(dn_ask):
            cands.append(("DOWN", 1 - prob_up, dn_ask, dn_sz, (1 - prob_up) - dn_ask))
    cands.sort(key=lambda c: -c[4])
    for side, mp, entry, sz, edge in cands:
        if mp < s["minProb"] or edge < s["minEdge"] or entry > s["maxEntry"]:
            continue
        if not s["contrarian"] and side != pred:
            continue
        if s["minSize"] and (np.isnan(sz) or sz < s["minSize"]):
            continue
        return side, entry
    return None


def main():
    frame = pd.DataFrame(db.fetch_training_decision_snapshots(50000))
    frame = frame[frame["target_up"].notna()].copy()
    rounds = sorted(frame["round_cutoff"].unique())
    start = max(200, int(len(rounds) * 0.45))
    step = 40
    agg = {k: [] for k in STRATS}  # list of (entry, won)
    fold_net = {k: [] for k in STRATS}  # per-fold net pnl
    i = start
    while i < len(rounds):
        fold_legs = {k: [] for k in STRATS}
        tr = frame[frame["round_cutoff"].isin(set(rounds[:i]))]
        te = frame[frame["round_cutoff"].isin(set(rounds[i:i + step]))]
        if te.empty:
            break
        Xtr = flatten_features(tr).apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan)
        ytr = pd.to_numeric(tr["target_up"], errors="coerce").astype(int)
        Xte = flatten_features(te).reindex(columns=Xtr.columns).apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan)
        model = Pipeline([("i", SimpleImputer(strategy="median")),
                          ("m", HistGradientBoostingClassifier(learning_rate=0.05, max_iter=150, max_leaf_nodes=15, random_state=42))])
        model.fit(Xtr, ytr)
        p = model.predict_proba(Xte)[:, 1]
        te = te.reset_index(drop=True)
        ua = pd.to_numeric(te.get("up_best_ask"), errors="coerce").to_numpy()
        da = pd.to_numeric(te.get("down_best_ask"), errors="coerce").to_numpy()
        us = pd.to_numeric(te.get("up_ask_size"), errors="coerce").to_numpy()
        ds = pd.to_numeric(te.get("down_ask_size"), errors="coerce").to_numpy()
        bk = pd.to_numeric(te["seconds_bucket"], errors="coerce").to_numpy()
        oc = te["outcome"].to_numpy()
        for j in range(len(te)):
            if oc[j] not in ("UP", "DOWN"):
                continue
            for name, s in STRATS.items():
                d = decide(s, p[j], ua[j], us[j], da[j], ds[j], int(bk[j]) if not np.isnan(bk[j]) else -1)
                if d:
                    side, entry = d
                    leg = (entry, side == oc[j])
                    agg[name].append(leg)
                    fold_legs[name].append(leg)
        for name in STRATS:
            fl = fold_legs[name]
            fold_net[name].append(sum((1 / e - 1) if w else -1 for e, w in fl) if fl else 0.0)
        i += step
    print(f"{'strategy':>20} {'signals':>8} {'win%':>6} {'avgEnt':>7} {'net':>9} {'roi':>8} {'folds+':>8}")
    for name, legs in agg.items():
        if not legs:
            print(f"{name:>20} {0:>8}")
            continue
        net = sum((1 / e - 1) if w else -1 for e, w in legs)
        wr = np.mean([1 if w else 0 for _, w in legs])
        ae = np.mean([e for e, _ in legs])
        roi = net / len(legs)
        fnets = fold_net[name]
        pos = sum(1 for x in fnets if x > 0)
        flag = "  <== +EV" if net > 0 else ""
        print(f"{name:>20} {len(legs):>8} {wr*100:>5.1f} {ae:>7.3f} {net:>+9.2f} {roi*100:>+7.1f}% {pos:>3}/{len(fnets):<3}{flag}")


if __name__ == "__main__":
    main()
