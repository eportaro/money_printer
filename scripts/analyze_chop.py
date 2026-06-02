"""
Last untested idea: predict round 'choppiness' from EARLY data and trade the straddle
ONLY on rounds predicted to oscillate. Targets volatility (predictable, clusters), not
direction. Walk-forward so it's honest. No look-ahead: features come from the early
buckets (T-895..T-480); the straddle only trades the later buckets (T-360..T-30).

If selecting rounds with the chop model lifts the straddle from negative to clearly
positive (gross), there's something. Otherwise even volatility selection can't beat it.
"""
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import db_sqlserver as dbs

EARLY = {895, 840, 720, 600, 480}
TRADE = {360, 240, 180, 120, 90, 60, 30}
T = 0.20  # straddle limit price per leg


def fetch():
    return dbs._fetch_all(
        """
        select r.round_cutoff as cutoff, rr.outcome as outcome, ds.seconds_bucket as bucket,
               ds.btc_price as price, coalesce(ds.baseline, r.baseline) as baseline,
               max(case when mq.outcome = N'UP' then mq.best_ask end) as up_ask,
               max(case when mq.outcome = N'DOWN' then mq.best_ask end) as down_ask
        from dbo.decision_snapshots ds
        join dbo.rounds r on r.id = ds.round_id
        join dbo.round_results rr on rr.round_cutoff = r.round_cutoff and rr.outcome in (N'UP', N'DOWN')
        left join dbo.market_quotes mq on mq.snapshot_id = ds.id
        group by r.round_cutoff, rr.outcome, ds.seconds_bucket, ds.btc_price, coalesce(ds.baseline, r.baseline)
        """
    )


def straddle_pnl(trade_rows, outcome, t=T):
    up_fill = any(r["up_ask"] is not None and float(r["up_ask"]) <= t for r in trade_rows)
    dn_fill = any(r["down_ask"] is not None and float(r["down_ask"]) <= t for r in trade_rows)
    cost = t * (int(up_fill) + int(dn_fill))
    payout = (1.0 if up_fill and outcome == "UP" else 0.0) + (1.0 if dn_fill and outcome == "DOWN" else 0.0)
    return payout - cost


def build():
    by_round = defaultdict(lambda: {"outcome": None, "rows": []})
    for r in fetch():
        rec = by_round[int(r["cutoff"])]
        rec["outcome"] = r["outcome"]
        rec["rows"].append(r)
    samples = []
    for cutoff in sorted(by_round):
        rec = by_round[cutoff]
        rows = rec["rows"]
        bl = next((float(x["baseline"]) for x in rows if x["baseline"] is not None), None)
        early = sorted([x for x in rows if int(x["bucket"]) in EARLY], key=lambda x: -int(x["bucket"]))
        trade = [x for x in rows if int(x["bucket"]) in TRADE]
        if bl is None or len(early) < 3 or not trade:
            continue
        dists = [(float(x["price"]) - bl) / bl * 100.0 for x in early if x["price"] is not None]
        if len(dists) < 3:
            continue
        crossings = sum(1 for i in range(1, len(dists)) if dists[i] * dists[i - 1] < 0)
        feat = {
            "vol_early": float(np.std(dists)),
            "range_early": float(max(dists) - min(dists)),
            "abs_dist_last": abs(dists[-1]),
            "abs_dist_min": min(abs(d) for d in dists),
            "crossings": crossings,
            "hour": (cutoff // 3600) % 24,
        }
        samples.append({"cutoff": cutoff, "feat": feat, "pnl": straddle_pnl(trade, rec["outcome"])})
    # vol clustering: previous round's straddle pnl as a feature
    for i, s in enumerate(samples):
        s["feat"]["prev_pnl"] = samples[i - 1]["pnl"] if i > 0 else 0.0
    return samples


def main():
    samples = build()
    feat_cols = ["vol_early", "range_early", "abs_dist_last", "abs_dist_min", "crossings", "hour", "prev_pnl"]
    X = pd.DataFrame([s["feat"] for s in samples])[feat_cols].to_numpy()
    pnl = np.array([s["pnl"] for s in samples])
    y = (pnl > 0).astype(int)
    n = len(samples)
    print(f"rounds usable: {n}  | straddle T={T}")
    print(f"BASELINE (trade EVERY round): mean pnl/round = {pnl.mean():+.4f}  total = {pnl.sum():+.2f}")
    print(f"ORACLE  (perfect selection)  : mean pnl/round = {pnl[pnl>0].mean():+.4f}  on {int((pnl>0).sum())} rounds ({100*(pnl>0).mean():.0f}%)\n")

    start = max(150, int(n * 0.45))
    step = 40
    sel_pnls, sel_count, fold = [], 0, 0
    print(f"{'fold':>4} {'trainN':>7} {'picked':>7} {'sel_mean':>9} {'all_mean':>9}")
    i = start
    while i < n:
        fold += 1
        Xtr, ytr = X[:i], y[:i]
        Xte = X[i:i + step]
        seg_pnl = pnl[i:i + step]
        if len(np.unique(ytr)) < 2:
            i += step
            continue
        model = Pipeline([("imp", SimpleImputer(strategy="median")),
                          ("m", HistGradientBoostingClassifier(learning_rate=0.05, max_iter=150, max_leaf_nodes=15, random_state=42))])
        model.fit(Xtr, ytr)
        pick = model.predict(Xte).astype(bool)
        picked_pnl = seg_pnl[pick]
        sel_pnls.extend(picked_pnl.tolist())
        sel_count += int(pick.sum())
        sm = picked_pnl.mean() if pick.sum() else float("nan")
        print(f"{fold:>4} {i:>7} {int(pick.sum()):>7} {sm:>+9.4f} {seg_pnl.mean():>+9.4f}")
        i += step
    sel = np.array(sel_pnls)
    print(f"\nSELECTED straddle (chop model, walk-forward): picked {sel_count} rounds")
    if len(sel):
        print(f"  mean pnl/round = {sel.mean():+.4f}   total = {sel.sum():+.2f}   %picked-positive = {100*(sel>0).mean():.0f}%")
    print("  Real edge => selected mean clearly > 0 (and > baseline). Then check fees on 2 legs.")


if __name__ == "__main__":
    main()
