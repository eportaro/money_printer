import json
import os
import time
from datetime import datetime, timezone

import pandas as pd
import requests
from dotenv import load_dotenv

import db
from features import compute_all_features
from model_runtime import (
    base_prediction,
    edge_from_probabilities,
    load_base_model,
    load_market_model,
    select_prediction,
)
from polymarket import PolymarketError, discover_btc_5m_market, fetch_market_quotes

load_dotenv()

BINANCE_URL = "https://api.binance.com/api/v3/klines"
SYMBOL = os.getenv("BINANCE_SYMBOL", "BTCUSDT")
ACTIVE_MODEL = os.getenv("ACTIVE_MODEL", "market-aware-v1")


def get_next_cutoff():
    now = int(time.time())
    return ((now // 300) + 1) * 300


def expected_event_slug(next_cutoff=None):
    next_cutoff = next_cutoff or get_next_cutoff()
    return f"btc-updown-5m-{next_cutoff - 300}"


def fetch_recent_candles(n=200):
    params = {"symbol": SYMBOL, "interval": "1m", "limit": n}
    resp = requests.get(BINANCE_URL, params=params, timeout=15)
    resp.raise_for_status()
    candles = []
    for c in resp.json():
        candles.append(
            {
                "timestamp": c[0],
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume_btc": float(c[5]),
                "volume_usdt": float(c[7]),
                "num_trades": int(c[8]),
                "taker_buy_base": float(c[9]),
                "taker_buy_quote": float(c[10]),
            }
        )
    return pd.DataFrame(candles)


def load_model():
    return {
        "base": load_base_model(),
        "market": load_market_model(),
    }


def infer_binance_baseline(df, next_cutoff):
    window_start = next_cutoff - 300
    prev_candle_ts = (window_start - 60) * 1000
    prev_candle = df[df["timestamp"] == prev_candle_ts]
    if not prev_candle.empty:
        return float(prev_candle.iloc[0]["close"])

    window_candles = df[df["timestamp"] >= window_start * 1000]
    if not window_candles.empty:
        return float(window_candles.iloc[0]["open"])
    return float(df.iloc[-1]["close"])


def compute_model_prediction(model, df):
    df_feat = compute_all_features(df)
    return base_prediction(model["base"], df_feat)


def edge_from_quotes(prediction, quotes):
    threshold = float(os.getenv("EDGE_THRESHOLD", "0.03"))
    edge = edge_from_probabilities(prediction["prob_up"], quotes, threshold)
    return edge["edge_up"], edge["edge_down"], edge["recommended_action"]


def quote_by_side(quotes, side):
    return next((q for q in quotes if q["outcome"] == side), None)


def simulated_pnl(stake, entry_price, won):
    if not won:
        return -float(stake)
    if not entry_price:
        return 0.0
    return float(stake) * ((1.0 / float(entry_price)) - 1.0)


def resolve_open_simulated_bets():
    if not db.db_enabled():
        return {"checked": 0, "resolved": 0}

    now = int(time.time())
    open_bets = db.fetch_open_simulated_bets()
    resolved = 0
    if not open_bets:
        return {"checked": 0, "resolved": 0}

    df = fetch_recent_candles(20)
    for bet in open_bets:
        cutoff = int(bet["round_cutoff"])
        if now <= cutoff + 15:
            continue

        matching = df[df["timestamp"] >= cutoff * 1000]
        if matching.empty:
            continue

        actual_close = float(matching.iloc[0]["open"])
        raw = bet.get("raw") or {}
        baseline = raw.get("baseline")
        if baseline is None:
            baseline = raw.get("snapshot", {}).get("baseline")
        if baseline is None:
            continue

        baseline = float(baseline)
        outcome = "UP" if actual_close > baseline else "DOWN" if actual_close < baseline else "TIE"
        won = bet["side"] == outcome
        pnl = simulated_pnl(bet.get("stake", 10), bet.get("entry_price"), won)
        db.update_simulated_bet(
            bet["id"],
            {
                "result": "WIN" if won else "LOSS",
                "pnl": round(pnl, 4),
                "raw": {
                    **raw,
                    "resolved": {
                        "actual_close": actual_close,
                        "baseline": baseline,
                        "outcome": outcome,
                        "resolved_at": datetime.now(timezone.utc).isoformat(),
                    },
                },
            },
        )
        db.upsert_round_result(
            {
                "round_cutoff": cutoff,
                "baseline": baseline,
                "actual_close": actual_close,
                "outcome": outcome,
                "raw": {"source": "collector", "bet_id": bet["id"]},
            }
        )
        resolved += 1

    return {"checked": len(open_bets), "resolved": resolved}


def resolve_recent_round_results():
    if not db.db_enabled():
        return {"checked": 0, "resolved": 0}

    now = int(time.time())
    snapshots = db.fetch_recent_snapshots(1000)
    if not snapshots:
        return {"checked": 0, "resolved": 0}

    existing = {int(r["round_cutoff"]) for r in db.fetch_recent_round_results(500)}
    by_cutoff = {}
    for snapshot in snapshots:
        cutoff = int(snapshot["round_cutoff"])
        if cutoff in existing or now <= cutoff + 15:
            continue
        by_cutoff.setdefault(cutoff, []).append(snapshot)

    if not by_cutoff:
        return {"checked": 0, "resolved": 0}

    df = fetch_recent_candles(500)
    resolved = 0
    for cutoff, rows in by_cutoff.items():
        rows.sort(key=lambda row: row["observed_at"])
        baseline = next((row.get("baseline") for row in rows if row.get("baseline") is not None), None)
        if baseline is None:
            continue

        matching = df[df["timestamp"] >= cutoff * 1000]
        if matching.empty:
            continue

        baseline = float(baseline)
        actual_close = float(matching.iloc[0]["open"])
        outcome = "UP" if actual_close > baseline else "DOWN" if actual_close < baseline else "TIE"
        db.upsert_round_result(
            {
                "round_cutoff": cutoff,
                "baseline": baseline,
                "actual_close": actual_close,
                "outcome": outcome,
                "raw": {
                    "source": "collector_snapshots",
                    "snapshots_seen": len(rows),
                    "first_snapshot_id": rows[0].get("id"),
                    "last_snapshot_id": rows[-1].get("id"),
                },
            }
        )
        resolved += 1

    return {"checked": len(by_cutoff), "resolved": resolved}


def collect_once(model=None, market=None):
    model = model or load_model()
    df = fetch_recent_candles(200)
    current_price = float(df.iloc[-1]["close"])
    next_cutoff = get_next_cutoff()
    window_start = next_cutoff - 300
    seconds_to_cutoff = next_cutoff - int(time.time())

    if market is None or market.get("event_slug") != expected_event_slug(next_cutoff):
        market = discover_btc_5m_market()
    db.upsert_market(market)

    baseline_source = "polymarket_gamma" if market.get("baseline") else "binance_prev_close"
    baseline = market.get("baseline") or infer_binance_baseline(df, next_cutoff)
    dist = current_price - baseline if baseline else None
    dist_pct = (dist / baseline * 100) if baseline else None

    snapshot = {
        "observed_at": datetime.now(timezone.utc),
        "round_cutoff": next_cutoff,
        "window_start": window_start,
        "seconds_to_cutoff": seconds_to_cutoff,
        "symbol": SYMBOL,
        "btc_price": current_price,
        "baseline": baseline,
        "dist_to_baseline": dist,
        "dist_to_baseline_pct": dist_pct,
        "source": "binance",
        "market_condition_id": market.get("condition_id"),
        "raw": {"market": market, "baseline_source": baseline_source},
    }
    snapshot_id = db.insert_round_snapshot(snapshot)

    quotes = []
    try:
        quotes = fetch_market_quotes(market, round_cutoff=next_cutoff)
        for quote in quotes:
            db.insert_quote(quote)
    except Exception as exc:
        print(f"Polymarket quote capture failed: {exc}")

    base_pred = compute_model_prediction(model, df)
    base_edge_up, base_edge_down, _base_action = edge_from_quotes(base_pred, quotes)
    context = {
        "seconds_to_cutoff": seconds_to_cutoff,
        "btc_price": current_price,
        "baseline": baseline,
        "dist_to_baseline": dist,
        "dist_to_baseline_pct": dist_pct,
        "base_prob_up": base_pred["prob_up"],
        "base_prob_down": base_pred["prob_down"],
        "base_confidence": base_pred["confidence"],
        "base_edge_up": base_edge_up,
        "base_edge_down": base_edge_down,
    }
    pred = select_prediction(ACTIVE_MODEL, base_pred, model.get("market"), context, quotes)
    edge_up, edge_down, action = edge_from_quotes(pred, quotes)
    prediction_id = db.insert_prediction(
        {
            "observed_at": datetime.now(timezone.utc),
            "round_cutoff": next_cutoff,
            "model_version": pred.get("model_version", ACTIVE_MODEL),
            "prediction": pred["prediction"],
            "prob_up": pred["prob_up"],
            "prob_down": pred["prob_down"],
            "confidence": pred["confidence"],
            "edge_up": edge_up,
            "edge_down": edge_down,
            "recommended_action": action,
            "feature_values": pred.get("feature_values") or pred.get("features", {}),
            "source_snapshot_id": snapshot_id,
            "raw": {
                "quotes": quotes,
                "base_prediction": base_pred,
                "active_model": ACTIVE_MODEL,
                "baseline_source": baseline_source,
            },
        }
    )

    if action in {"BUY_UP", "BUY_DOWN"}:
        side = "UP" if action == "BUY_UP" else "DOWN"
        quote = quote_by_side(quotes, side)
        if quote and quote.get("best_ask") is not None:
            db.insert_simulated_bet(
                {
                    "observed_at": datetime.now(timezone.utc),
                    "round_cutoff": next_cutoff,
                    "side": side,
                    "entry_price": quote["best_ask"],
                    "stake": float(os.getenv("COLLECTOR_STAKE_SIZE", "10")),
                    "model_prob": pred["prob_up"] if side == "UP" else pred["prob_down"],
                    "edge": edge_up if side == "UP" else edge_down,
                    "result": "OPEN",
                    "model_prediction_id": prediction_id,
                    "raw": {
                        "quote": quote,
                        "prediction": pred,
                        "base_prediction": base_pred,
                        "baseline": baseline,
                        "btc_price": current_price,
                        "seconds_to_cutoff": seconds_to_cutoff,
                        "window_start": window_start,
                        "market": {
                            "question": market.get("question"),
                            "event_slug": market.get("event_slug"),
                            "condition_id": market.get("condition_id"),
                            "resolution_source": market.get("resolution_source"),
                        },
                        "baseline_source": baseline_source,
                    },
                }
            )

    return {
        "round_cutoff": next_cutoff,
        "btc_price": current_price,
        "baseline": baseline,
        "prediction": pred["prediction"],
        "prob_up": round(pred["prob_up"], 4),
        "edge_up": None if edge_up is None else round(edge_up, 4),
        "edge_down": None if edge_down is None else round(edge_down, 4),
        "action": action,
        "snapshot_id": snapshot_id,
        "prediction_id": prediction_id,
    }


def run_forever():
    if not db.db_enabled():
        print("WARNING: SUPABASE_DB_URL/DATABASE_URL is not set. Collector will run without persistence.")
    model = load_model()
    market = None
    interval = int(os.getenv("COLLECTOR_INTERVAL_SECONDS", "5"))
    while True:
        try:
            round_resolution = resolve_recent_round_results()
            if round_resolution["resolved"]:
                print(f"Resolved round results: {round_resolution}")
            resolved = resolve_open_simulated_bets()
            if resolved["resolved"]:
                print(f"Resolved simulated bets: {resolved}")
            expected_slug = expected_event_slug()
            if market is None or market.get("event_slug") != expected_slug:
                market = discover_btc_5m_market()
                print(f"Tracking Polymarket market: {market.get('question')} ({market.get('slug')})")
            result = collect_once(model=model, market=market)
            print(json.dumps(result, default=str))
        except PolymarketError as exc:
            print(f"Polymarket discovery failed: {exc}")
            market = None
        except Exception as exc:
            print(f"Collector error: {exc}")
        time.sleep(interval)


if __name__ == "__main__":
    run_forever()
