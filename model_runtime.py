import os
import pickle

import numpy as np
import pandas as pd

from features import FEATURE_COLUMNS

MODEL_DIR = "model_artifacts"
BASE_MODEL_PATH = os.path.join(MODEL_DIR, "model.pkl")
MARKET_MODEL_PATH = os.path.join(MODEL_DIR, "model_supabase.pkl")


def _as_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def load_base_model():
    with open(BASE_MODEL_PATH, "rb") as f:
        return pickle.load(f)


def load_market_model():
    if not os.path.exists(MARKET_MODEL_PATH):
        return None
    with open(MARKET_MODEL_PATH, "rb") as f:
        return pickle.load(f)


def base_prediction(base_model, df_feat):
    latest = df_feat[FEATURE_COLUMNS].iloc[-1:].fillna(0)
    probs = base_model.predict_proba(latest)[0]
    prob_up = float(probs[1])
    return {
        "model_version": "binance-hgb-v1",
        "prob_up": prob_up,
        "prob_down": 1.0 - prob_up,
        "prediction": "UP" if prob_up >= 0.5 else "DOWN",
        "confidence": abs(prob_up - 0.5) * 2,
        "features": latest.iloc[0].to_dict(),
    }


def quote_by_outcome(quotes):
    return {q.get("outcome"): q for q in quotes or [] if q.get("outcome")}


def edge_from_probabilities(prob_up, quotes, threshold):
    by_outcome = quote_by_outcome(quotes)
    up_ask = _as_float((by_outcome.get("UP") or {}).get("best_ask"))
    down_ask = _as_float((by_outcome.get("DOWN") or {}).get("best_ask"))
    prob_down = 1.0 - float(prob_up)
    edge_up = float(prob_up) - up_ask if up_ask is not None else None
    edge_down = prob_down - down_ask if down_ask is not None else None

    action = "WAIT"
    if edge_up is not None and edge_up >= threshold and (edge_down is None or edge_up >= edge_down):
        action = "BUY_UP"
    elif edge_down is not None and edge_down >= threshold:
        action = "BUY_DOWN"

    return {
        "edge_up": edge_up,
        "edge_down": edge_down,
        "recommended_action": action,
        "up_ask": up_ask,
        "down_ask": down_ask,
    }


def quote_features(quotes):
    by_outcome = quote_by_outcome(quotes)
    features = {}
    for side in ("UP", "DOWN"):
        quote = by_outcome.get(side) or {}
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
            features[f"{prefix}_{field}"] = quote.get(field)
    return features


def market_feature_frame(market_model, context, technical_features, quotes):
    columns = market_model["columns"]
    # Technical features are stored in DB with feat__ prefix; add it here for inference
    feat_prefixed = {f"feat__{k}": v for k, v in (technical_features or {}).items()}
    row = {
        "seconds_to_cutoff": context.get("seconds_to_cutoff"),
        "seconds_bucket": context.get("seconds_bucket"),
        "btc_price": context.get("btc_price"),
        "baseline": context.get("baseline"),
        "dist_to_baseline": context.get("dist_to_baseline"),
        "dist_to_baseline_pct": context.get("dist_to_baseline_pct"),
        **feat_prefixed,
        **quote_features(quotes),
    }
    frame = pd.DataFrame([{col: row.get(col, 0) for col in columns}])
    return frame.apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(0)


def market_prediction(market_model, context, technical_features, quotes):
    X = market_feature_frame(market_model, context, technical_features, quotes)
    probs = market_model["model"].predict_proba(X)[0]
    prob_up = float(probs[1])
    metrics = market_model.get("metrics", {})
    return {
        "model_version": market_model.get("model_version", "market-aware-v1"),
        "prob_up": prob_up,
        "prob_down": 1.0 - prob_up,
        "prediction": "UP" if prob_up >= 0.5 else "DOWN",
        "confidence": abs(prob_up - 0.5) * 2,
        "feature_values": X.iloc[0].to_dict(),
        "metrics": metrics,
    }


def select_prediction(active_model, base_pred, market_model, context, quotes):
    if active_model == "market-aware-v1" and market_model is not None and quotes:
        try:
            return market_prediction(market_model, context, base_pred["features"], quotes)
        except Exception as exc:
            fallback = dict(base_pred)
            fallback["model_version"] = "binance-hgb-v1"
            fallback["fallback_reason"] = str(exc)
            return fallback
    return dict(base_pred)
