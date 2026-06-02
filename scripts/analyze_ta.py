"""
Explicit classic technical-analysis rules (NO ML) vs 15-min outcome.

For each well-known TA signal we take its directional call and measure how often it's
right, restricted to EARLY buckets (T-480 and earlier = real anticipation, before the
move is decided). If any rule beats ~50% consistently, TA has a directional edge.
The indicators are the ones already computed per snapshot (feature_snapshots).
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import db
from train_model_v2 import flatten_features


def acc(pred, y):
    m = ~np.isnan(pred)
    if m.sum() == 0:
        return None, 0
    return float(np.mean(pred[m] == y[m])), int(m.sum())


def main():
    frame = pd.DataFrame(db.fetch_training_decision_snapshots(50000))
    frame = frame[frame["target_up"].notna()].copy()
    X = flatten_features(frame)
    y = pd.to_numeric(frame["target_up"], errors="coerce").astype(int).to_numpy()
    bucket = pd.to_numeric(frame["seconds_bucket"], errors="coerce").to_numpy()
    early = bucket >= 480  # anticipatory window

    def col(name):
        return pd.to_numeric(X.get(f"feat__{name}"), errors="coerce").to_numpy()

    rsi = col("rsi_14"); macd = col("macd_hist"); sma20 = col("price_vs_sma20")
    bbp = col("bb_pct_b"); stoch = col("stoch_k"); willr = col("willr_14")
    ema_x = col("ema_cross_5_20"); roc = col("roc_10"); ret5 = col("returns_5")

    def nan(): return np.full(len(y), np.nan)

    rules = {}
    # mean-reversion (fade extremes)
    p = nan(); p[rsi < 35] = 1; p[rsi > 65] = 0; rules["RSI mean-rev (<35 UP / >65 DOWN)"] = p
    p = nan(); p[bbp < 0.1] = 1; p[bbp > 0.9] = 0; rules["Bollinger %B mean-rev"] = p
    p = nan(); p[stoch < 20] = 1; p[stoch > 80] = 0; rules["Stochastic mean-rev"] = p
    p = nan(); p[willr < -80] = 1; p[willr > -20] = 0; rules["Williams %R mean-rev"] = p
    # momentum / trend (follow)
    p = nan(); p[rsi > 55] = 1; p[rsi < 45] = 0; rules["RSI trend (>55 UP / <45 DOWN)"] = p
    p = nan(); p[macd > 0] = 1; p[macd < 0] = 0; rules["MACD hist sign (momentum)"] = p
    p = nan(); p[sma20 > 0] = 1; p[sma20 < 0] = 0; rules["Price vs SMA20 (trend)"] = p
    p = nan(); p[ema_x > 0] = 1; p[ema_x < 0] = 0; rules["EMA 5/20 cross"] = p
    p = nan(); p[roc > 0] = 1; p[roc < 0] = 0; rules["ROC(10) sign (momentum)"] = p
    p = nan(); p[ret5 > 0] = 1; p[ret5 < 0] = 0; rules["Return(5) sign (momentum)"] = p

    ua = pd.to_numeric(frame.get("up_best_ask"), errors="coerce").to_numpy()
    da = pd.to_numeric(frame.get("down_best_ask"), errors="coerce").to_numpy()

    def trade_pnls(pred):
        # bet predicted side at its ask, EARLY only; return pnls in time order
        out = []
        for i in range(len(pred)):
            if np.isnan(pred[i]) or not early[i]:
                continue
            ask = ua[i] if pred[i] == 1 else da[i]
            if np.isnan(ask) or ask <= 0:
                continue
            won = pred[i] == y[i]
            out.append((1.0 / ask - 1.0) if won else -1.0)
        return out

    print(f"rows total={len(y)}  early (T>=480)={int(early.sum())}  base UP rate={y.mean():.3f}\n")
    print(f"{'TA rule':>34} {'acc(early)':>10} {'bets':>6} {'net':>9} {'roi':>8} {'win+':>7}")
    for name, pred in rules.items():
        pe = pred.copy(); pe[~early] = np.nan
        a_e, n_e = acc(pe, y)
        pn = trade_pnls(pred)
        net = sum(pn); nb = len(pn); roi = net / nb if nb else 0.0
        # 6 equal time windows -> how many are net-positive (consistency)
        W = 6
        chunks = np.array_split(np.array(pn), W) if nb >= W else [np.array(pn)]
        winpos = sum(1 for c in chunks if c.sum() > 0)
        se = f"{a_e:.3f}" if a_e is not None else "--"
        flag = "  <== +EV" if net > 0 else ""
        print(f"{name:>34} {se:>10} {nb:>6} {net:>+9.2f} {roi*100:>+7.1f}% {winpos:>2}/{len(chunks):<2}{flag}")
    print("\nacc>0.50 NO basta: si compras al ask del mercado (que ya refleja el movimiento),")
    print("el ROI es lo que importa. +EV solo si net>0 tras pagar el precio.")


if __name__ == "__main__":
    main()
