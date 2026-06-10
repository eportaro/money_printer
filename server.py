"""
Flask Backend — Polymarket BTC Predictor
=========================================
Serves the dashboard and provides live prediction API endpoints.
"""

import os
import json
import pickle
import time
import threading
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from dotenv import load_dotenv
from flask import Flask, jsonify, send_from_directory, request
from features import compute_all_features, FEATURE_COLUMNS
from model_runtime import (
    base_prediction,
    edge_from_probabilities,
    load_base_model,
    load_market_model,
    select_prediction,
)
import db
from price_feed import active_symbol, fetch_recent_candles, oracle_price_at, source_label

try:
    from polymarket import discover_btc_market, fetch_market_quotes
except Exception:
    discover_btc_market = None
    fetch_market_quotes = None
from market_config import MARKET_INTERVAL, WINDOW_MS, WINDOW_SECONDS, next_cutoff as configured_next_cutoff, round_start, slug_candidates

load_dotenv()

# ─── Config ───
MODEL_DIR = "model_artifacts"
SYMBOL = active_symbol()
PRICE_SOURCE_LABEL = source_label()
PREDICTIONS_FILE = "predictions_db.json"
MAX_HISTORY = 200
EDGE_THRESHOLD = float(os.getenv("EDGE_THRESHOLD", "0.03"))
ACTIVE_MODEL = os.getenv("ACTIVE_MODEL", "market-aware-v1")
MODEL_STAGE = os.getenv("MODEL_STAGE", "production")
PERFORMANCE_CACHE_SECONDS = int(os.getenv("PERFORMANCE_CACHE_SECONDS", "60"))
POLYMARKET_MARKET_REFRESH_SECONDS = int(os.getenv("POLYMARKET_MARKET_REFRESH_SECONDS", "30"))
BASELINE_MAX_FEED_DELTA_ABS = float(os.getenv("BASELINE_MAX_FEED_DELTA_ABS", "75"))
BASELINE_MAX_FEED_DELTA_PCT = float(os.getenv("BASELINE_MAX_FEED_DELTA_PCT", "0.12"))
REQUIRE_EXACT_BASELINE_FOR_ACTION = os.getenv("REQUIRE_EXACT_BASELINE_FOR_ACTION", "true").lower() == "true"
ALLOW_PRICE_FEED_BASELINE_FOR_ACTION = os.getenv("ALLOW_PRICE_FEED_BASELINE_FOR_ACTION", "false").lower() == "true"
PROXY_BASELINE_MIN_ELAPSED_SECONDS = int(os.getenv("PROXY_BASELINE_MIN_ELAPSED_SECONDS", "60"))
EXACT_BASELINE_SOURCES = {"polymarket_gamma_event_metadata", "manual_sync"}
ORACLE_BASELINE_SOURCE = "pyth_btc_usd_window_open"
ALLOW_ORACLE_BASELINE_FOR_ACTION = os.getenv("ALLOW_ORACLE_BASELINE_FOR_ACTION", "true").lower() == "true"

app = Flask(__name__, static_folder="dashboard", static_url_path="")
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0


@app.after_request
def add_no_cache_headers(response):
    if request.path == "/" or request.path.startswith("/api/") or request.path.endswith((".js", ".css", ".html")):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# ─── Load Model & State ───
model = None
market_model = None
metrics = {}
feat_importance = {}
active_poly_market = None
active_poly_market_fetched_at = 0
performance_cache = {"ts": 0, "payload": None}

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


def active_model_version():
    if ACTIVE_MODEL == "market-aware-v1" and market_model is not None:
        return market_model.get("model_version", ACTIVE_MODEL)
    return ACTIVE_MODEL


def load_model():
    global model, market_model, metrics, feat_importance
    try:
        model = load_base_model()
        market_model = load_market_model()
        with open(os.path.join(MODEL_DIR, "metrics.json"), "r") as f:
            metrics = json.load(f)
        with open(os.path.join(MODEL_DIR, "feature_importance.json"), "r") as f:
            feat_importance = json.load(f)
        print("Model loaded successfully.")
    except FileNotFoundError:
        print("WARNING: No trained model found. Run model.py first.")


def get_active_poly_market(window_start=None):
    global active_poly_market, active_poly_market_fetched_at
    current_start = int(window_start) if window_start is not None else round_start()
    expected_slugs = set(slug_candidates(current_start))
    refresh_due = (time.time() - active_poly_market_fetched_at) >= POLYMARKET_MARKET_REFRESH_SECONDS
    if active_poly_market is not None and active_poly_market.get("event_slug") not in expected_slugs:
        active_poly_market = None
    if active_poly_market is not None and active_poly_market.get("baseline") is not None and not refresh_due:
        return active_poly_market
    if discover_btc_market is None:
        return None
    try:
        active_poly_market = discover_btc_market(window_start=current_start)
        active_poly_market_fetched_at = time.time()
        print(f"Polymarket market detected: {active_poly_market.get('question')}")
    except Exception as e:
        print(f"Polymarket market discovery failed: {e}")
        active_poly_market = None
        active_poly_market_fetched_at = 0
    return active_poly_market


def baseline_is_exact(source):
    return source in EXACT_BASELINE_SOURCES


def baseline_allows_action(source):
    if baseline_is_exact(source):
        return True
    if ALLOW_ORACLE_BASELINE_FOR_ACTION and source == ORACLE_BASELINE_SOURCE:
        return True
    return ALLOW_PRICE_FEED_BASELINE_FOR_ACTION and source == f"{PRICE_SOURCE_LABEL}_prev_close"


def _directional_action(row):
    action = row.get("recommended_action")
    if action in {"BUY_UP", "BUY_DOWN"}:
        return action
    return None


def _forecast_blocker_reason(row):
    action = row.get("recommended_action") or "WAIT"
    if action in {"BUY_UP", "BUY_DOWN"}:
        return "buy_signal"
    if not row.get("baseline_exact") and not row.get("baseline_action_allowed"):
        return "baseline_proxy"
    if row.get("up_best_ask") is None and row.get("down_best_ask") is None:
        return "missing_quotes"
    edge_up = row.get("edge_up")
    edge_down = row.get("edge_down")
    viable_edges = [edge for edge in (edge_up, edge_down) if edge is not None]
    if not viable_edges or max(viable_edges) < EDGE_THRESHOLD:
        return "edge_below_threshold"
    return "strategy_or_execution_filter"


def _summarize_live_blockers(forecasts, live_trades):
    resolved = [row for row in forecasts if row.get("outcome") in {"UP", "DOWN"}]
    action_counts = {}
    blocker_counts = {}
    exact_count = 0
    for row in forecasts:
        action = row.get("recommended_action") or "WAIT"
        action_counts[action] = action_counts.get(action, 0) + 1
        if row.get("baseline_exact"):
            exact_count += 1
        reason = _forecast_blocker_reason(row)
        blocker_counts[reason] = blocker_counts.get(reason, 0) + 1

    resolved_trades = [row for row in live_trades if row.get("result") in {"WIN", "LOSS"}]
    top_reason = None
    if blocker_counts:
        top_reason = sorted(blocker_counts.items(), key=lambda item: item[1], reverse=True)[0][0]

    return {
        "forecasts": len(forecasts),
        "resolved_forecasts": len(resolved),
        "live_trades": len(live_trades),
        "closed_live_trades": len(resolved_trades),
        "baseline_exact": exact_count,
        "baseline_proxy": len(forecasts) - exact_count,
        "baseline_exact_rate": (exact_count / len(forecasts)) if forecasts else None,
        "actions": action_counts,
        "blockers": blocker_counts,
        "top_blocker": top_reason,
    }


def _enrich_forecasts(predictions, active_version):
    rows = []
    for row in predictions:
        if row.get("model_version") != active_version:
            continue
        enriched = dict(row)
        raw = row.get("prediction_raw") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {}
        raw_baseline_source = (raw or {}).get("baseline_source")
        snapshot_baseline_source = row.get("baseline_source")
        enriched["decision_baseline_source"] = snapshot_baseline_source or raw_baseline_source
        enriched["baseline_exact"] = baseline_is_exact(enriched["decision_baseline_source"])
        enriched["baseline_action_allowed"] = baseline_allows_action(enriched["decision_baseline_source"])
        enriched["directional_action"] = _directional_action(row)
        enriched["blocker_reason"] = _forecast_blocker_reason(enriched)
        rows.append(enriched)
    return rows


def _flatten_training_features(frame):
    # Must match scripts/train_model_v2.flatten_features exactly: keep ONLY whitelisted
    # technical features (collapsing repeated feat__ prefixes), dropping leaked model
    # outputs / poly_* dupes. Otherwise the test-split eval feeds the model different
    # features than it was trained on and the metrics are bogus.
    allowed = set(FEATURE_COLUMNS)
    feature_rows = []
    for item in frame.get("features", []):
        if isinstance(item, dict):
            raw = item
        elif isinstance(item, str):
            try:
                raw = json.loads(item)
            except Exception:
                raw = {}
            if not isinstance(raw, dict):
                raw = {}
        else:
            raw = {}
        clean = {}
        for key, value in raw.items():
            base_name = key
            while base_name.startswith("feat__"):
                base_name = base_name[len("feat__"):]
            if base_name in allowed:
                clean[f"feat__{base_name}"] = value
        feature_rows.append(clean)
    features = pd.DataFrame(feature_rows)
    for col in (f"feat__{name}" for name in FEATURE_COLUMNS):
        if col not in features.columns:
            features[col] = np.nan
    features = features[[f"feat__{name}" for name in FEATURE_COLUMNS]]
    base = frame[[col for col in CONTEXT_COLUMNS + QUOTE_COLUMNS if col in frame]].copy()
    matrix = pd.concat([base.reset_index(drop=True), features.reset_index(drop=True)], axis=1)
    matrix = matrix.apply(pd.to_numeric, errors="coerce")
    return matrix.replace([np.inf, -np.inf], np.nan)


def _json_value(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def _model_test_predictions(limit=50000):
    if not market_model or not db.db_enabled():
        return []
    rows = db.fetch_training_decision_snapshots(limit)
    if not rows:
        return []
    frame = pd.DataFrame(rows)
    if frame.empty or "target_up" not in frame:
        return []
    frame = frame[frame["target_up"].notna()].copy()
    if frame.empty:
        return []
    # Round-based split — consistent with how train_model_v2.py splits
    unique_rounds = sorted(frame["round_cutoff"].unique())
    n_test = max(1, int(len(unique_rounds) * 0.25))
    cutoff_round = unique_rounds[-n_test]
    test_frame = frame[frame["round_cutoff"] >= cutoff_round].copy().reset_index(drop=True)
    columns = market_model.get("columns") or market_model.get("feature_columns") or []
    if not columns:
        return []
    x_test = _flatten_training_features(test_frame).reindex(columns=columns)
    x_test = x_test.apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(0)
    probs_up = market_model["model"].predict_proba(x_test)[:, 1]
    output = []
    for (_, row), prob_up in zip(test_frame.iterrows(), probs_up):
        prob_up = float(prob_up)
        outcome = row.get("outcome")
        output.append({
            "snapshot_id": _json_value(row.get("snapshot_id")),
            "round_id": _json_value(row.get("round_id")),
            "observed_at": _json_value(row.get("observed_at")),
            "round_cutoff": _json_value(row.get("round_cutoff")),
            "seconds_to_cutoff": _json_value(row.get("seconds_to_cutoff")),
            "seconds_bucket": _json_value(row.get("seconds_bucket")),
            "btc_price": _json_value(row.get("btc_price")),
            "baseline": _json_value(row.get("baseline")),
            "baseline_source": _json_value(row.get("baseline_source")),
            "up_best_bid": _json_value(row.get("up_best_bid")),
            "up_best_ask": _json_value(row.get("up_best_ask")),
            "up_ask_size": _json_value(row.get("up_ask_size")),
            "down_best_bid": _json_value(row.get("down_best_bid")),
            "down_best_ask": _json_value(row.get("down_best_ask")),
            "down_ask_size": _json_value(row.get("down_ask_size")),
            "prediction": "UP" if prob_up >= 0.5 else "DOWN",
            "prob_up": prob_up,
            "prob_down": 1.0 - prob_up,
            "confidence": abs(prob_up - 0.5) * 2,
            "actual_close": _json_value(row.get("actual_close")),
            "outcome": _json_value(outcome),
            "target_up": 1 if outcome == "UP" else 0 if outcome == "DOWN" else None,
            "model_version": market_model.get("model_version", active_model_version()),
            "model_stage": "test_split",
        })
    return sorted(output, key=lambda item: str(item.get("observed_at") or ""), reverse=True)


# ─── Round-Based Prediction History ───
prediction_rounds = {} # Key: cutoff timestamp string
manual_baselines = {} # Key: cutoff timestamp string

def load_predictions():
    global prediction_rounds, manual_baselines
    if os.path.exists(PREDICTIONS_FILE):
        try:
            with open(PREDICTIONS_FILE, 'r') as f:
                data = json.load(f)
                prediction_rounds = {str(p["next_cutoff"]): p for p in data}
                # Also load manual baselines if stored (optional)
        except:
            prediction_rounds = {}
    else:
        prediction_rounds = {}

def save_predictions():
    with open(PREDICTIONS_FILE, 'w') as f:
        # Save as sorted list
        sorted_rounds = sorted(prediction_rounds.values(), key=lambda x: x["next_cutoff"])
        json.dump(sorted_rounds[-MAX_HISTORY:], f)

def validate_old_predictions():
    """Background task to check outcomes of past rounds."""
    while True:
        try:
            now = int(time.time())
            changed = False
            for cutoff_str, p in list(prediction_rounds.items()):
                cutoff = int(cutoff_str)
                # If round has passed (15s buffer)
                if p.get("outcome") is None and now > cutoff + 15:
                    df = fetch_recent_candles(max(200, WINDOW_SECONDS // 60 + 10))
                    matching = df[df['timestamp'] >= cutoff * 1000]
                    if not matching.empty:
                        # Use the open price of the cutoff candle as the final close price
                        close_price = float(matching.iloc[0]['open'])
                        p["actual_close"] = close_price
                        
                        # Use the actual baseline (manual or calculated)
                        baseline = p.get("manual_baseline") or p["window_open"]
                        is_up = close_price > baseline
                        p["actual_outcome"] = "UP" if is_up else "DOWN" if close_price < baseline else "TIE"
                        evaluated_prediction = p.get("initial_prediction") or p.get("prediction")

                        if (evaluated_prediction == "UP" and is_up) or (evaluated_prediction == "DOWN" and not is_up):
                            p["outcome"] = "WIN"
                        else:
                            p["outcome"] = "LOSS"
                        changed = True
            
            if changed:
                save_predictions()
                
        except Exception as e:
            print(f"Error in validator: {e}")
        time.sleep(30)

load_predictions()


def get_next_cutoff():
    """Get the next Polymarket cutoff timestamp."""
    return configured_next_cutoff()


def get_current_window_start():
    """Get the start of the current Polymarket window."""
    return round_start()


# ─── Analyst Engine ───

def generate_analysis(indicators, prob_up, prob_down, confidence, current_price, window_open):
    """
    Generate a complete human-readable analysis in Spanish.
    Acts as a professional crypto analyst interpreting all metrics.
    """
    analysis = {"signals": [], "conclusion": "", "risk_level": "", "details": {}}
    bullish_count = 0
    bearish_count = 0
    neutral_count = 0

    # ── RSI Analysis ──
    rsi = indicators.get('rsi_14')
    if rsi is not None:
        if rsi < 30:
            analysis["details"]["rsi"] = {
                "name": "RSI (14)",
                "value": round(rsi, 1),
                "signal": "SOBREVENTA",
                "emoji": "🟢",
                "interpretation": f"RSI en {rsi:.1f} — zona de sobreventa extrema. "
                    "El activo está 'barato' estadísticamente. Históricamente esto precede rebotes alcistas. "
                    "Señal de posible reversión al alza."
            }
            bullish_count += 2
        elif rsi < 40:
            analysis["details"]["rsi"] = {
                "name": "RSI (14)", "value": round(rsi, 1), "signal": "DÉBIL", "emoji": "🟡",
                "interpretation": f"RSI en {rsi:.1f} — momentum débil pero no extremo. "
                    "Los vendedores tienen control moderado. Zona de transición."
            }
            bullish_count += 1
        elif rsi > 70:
            analysis["details"]["rsi"] = {
                "name": "RSI (14)", "value": round(rsi, 1), "signal": "SOBRECOMPRA", "emoji": "🔴",
                "interpretation": f"RSI en {rsi:.1f} — zona de sobrecompra. "
                    "El activo está 'caro' estadísticamente. Alto riesgo de corrección bajista. "
                    "Los compradores pueden estar agotándose."
            }
            bearish_count += 2
        elif rsi > 60:
            analysis["details"]["rsi"] = {
                "name": "RSI (14)", "value": round(rsi, 1), "signal": "FUERTE", "emoji": "🟢",
                "interpretation": f"RSI en {rsi:.1f} — momentum alcista saludable. "
                    "Los compradores tienen el control sin estar en zona de peligro."
            }
            bullish_count += 1
        else:
            analysis["details"]["rsi"] = {
                "name": "RSI (14)", "value": round(rsi, 1), "signal": "NEUTRAL", "emoji": "⚪",
                "interpretation": f"RSI en {rsi:.1f} — zona neutral. "
                    "No hay presión clara de compradores ni vendedores. El mercado está indeciso."
            }
            neutral_count += 1

    # ── MACD Analysis ──
    macd_h = indicators.get('macd_hist')
    if macd_h is not None:
        if macd_h > 0:
            analysis["details"]["macd"] = {
                "name": "MACD Histograma", "value": round(macd_h, 4), "signal": "ALCISTA", "emoji": "🟢",
                "interpretation": f"MACD histograma positivo ({macd_h:.4f}). "
                    "La media rápida está por encima de la lenta → el momentum de corto plazo es alcista. "
                    "Mientras más positivo, más fuerte la tendencia."
            }
            bullish_count += 1
        else:
            analysis["details"]["macd"] = {
                "name": "MACD Histograma", "value": round(macd_h, 4), "signal": "BAJISTA", "emoji": "🔴",
                "interpretation": f"MACD histograma negativo ({macd_h:.4f}). "
                    "La media rápida está por debajo de la lenta → el momentum es bajista. "
                    "Esto indica presión vendedora en el corto plazo."
            }
            bearish_count += 1

    # ── Stochastic Analysis ──
    stoch_k = indicators.get('stoch_k')
    stoch_d = indicators.get('stoch_d')
    if stoch_k is not None and stoch_d is not None:
        if stoch_k < 20:
            sig, emoji = "SOBREVENTA", "🟢"
            interp = f"Estocástico %K en {stoch_k:.1f} — sobreventa. El precio está cerca del mínimo reciente. Alta probabilidad de rebote."
            bullish_count += 1
        elif stoch_k > 80:
            sig, emoji = "SOBRECOMPRA", "🔴"
            interp = f"Estocástico %K en {stoch_k:.1f} — sobrecompra. El precio está cerca del máximo reciente. Riesgo de retroceso."
            bearish_count += 1
        elif stoch_k > stoch_d:
            sig, emoji = "CRUCE ALCISTA", "🟢"
            interp = f"%K ({stoch_k:.1f}) cruzó por encima de %D ({stoch_d:.1f}) — señal de compra del estocástico."
            bullish_count += 1
        else:
            sig, emoji = "CRUCE BAJISTA", "🔴"
            interp = f"%K ({stoch_k:.1f}) está debajo de %D ({stoch_d:.1f}) — señal de venta del estocástico."
            bearish_count += 1
        analysis["details"]["stochastic"] = {
            "name": "Estocástico", "value": round(stoch_k, 1), "signal": sig, "emoji": emoji,
            "interpretation": interp
        }

    # ── MFI Analysis ──
    mfi = indicators.get('mfi_14')
    if mfi is not None:
        if mfi < 20:
            sig, emoji = "SOBREVENTA", "🟢"
            interp = f"MFI en {mfi:.1f} — flujo de dinero en zona de sobreventa. Poco dinero entrando = posible piso."
            bullish_count += 1
        elif mfi > 80:
            sig, emoji = "SOBRECOMPRA", "🔴"
            interp = f"MFI en {mfi:.1f} — exceso de flujo de dinero entrando. Puede indicar techo temporal."
            bearish_count += 1
        else:
            sig, emoji = "NEUTRAL", "⚪"
            interp = f"MFI en {mfi:.1f} — flujo de dinero normal. Sin señales extremas de presión."
            neutral_count += 1
        analysis["details"]["mfi"] = {
            "name": "Money Flow (MFI)", "value": round(mfi, 1), "signal": sig, "emoji": emoji,
            "interpretation": interp
        }

    # ── Bollinger Bands ──
    bb_pct = indicators.get('bb_pct_b')
    bb_squeeze = indicators.get('bb_squeeze')
    if bb_pct is not None:
        if bb_pct < 0.2:
            sig, emoji = "BANDA BAJA", "🟢"
            interp = f"Precio en el {bb_pct*100:.0f}% inferior de Bollinger. Cerca de la banda baja → zona de posible rebote (mean reversion)."
            bullish_count += 1
        elif bb_pct > 0.8:
            sig, emoji = "BANDA ALTA", "🔴"
            interp = f"Precio en el {bb_pct*100:.0f}% superior de Bollinger. Cerca de la banda alta → riesgo de retroceso hacia la media."
            bearish_count += 1
        else:
            sig, emoji = "MEDIO", "⚪"
            interp = f"Precio en el {bb_pct*100:.0f}% de Bollinger — zona media, sin presión extrema."
            neutral_count += 1
        if bb_squeeze and bb_squeeze > 0:
            interp += " ⚡ SQUEEZE detectado: las bandas están comprimiéndose, lo que suele preceder un movimiento explosivo."
        analysis["details"]["bollinger"] = {
            "name": "Bollinger %B", "value": round(bb_pct, 3), "signal": sig, "emoji": emoji,
            "interpretation": interp
        }

    # ── Volume Analysis ──
    vol_ratio = indicators.get('volume_ratio_20')
    taker = indicators.get('taker_buy_ratio')
    if vol_ratio is not None and taker is not None:
        vol_desc = "normal"
        if vol_ratio > 2.0:
            vol_desc = "MUY ALTO (spike)"
        elif vol_ratio > 1.3:
            vol_desc = "alto"
        elif vol_ratio < 0.5:
            vol_desc = "muy bajo"
        elif vol_ratio < 0.8:
            vol_desc = "bajo"

        buy_pct = taker * 100
        if taker > 0.55:
            sig, emoji = "PRESIÓN COMPRADORA", "🟢"
            interp = f"Volumen {vol_desc} ({vol_ratio:.1f}x promedio). {buy_pct:.0f}% del volumen son compras agresivas (taker buy). Los compradores dominan."
            bullish_count += 1
        elif taker < 0.45:
            sig, emoji = "PRESIÓN VENDEDORA", "🔴"
            interp = f"Volumen {vol_desc} ({vol_ratio:.1f}x promedio). Solo {buy_pct:.0f}% son compras — los vendedores dominan el mercado."
            bearish_count += 1
        else:
            sig, emoji = "EQUILIBRADO", "⚪"
            interp = f"Volumen {vol_desc} ({vol_ratio:.1f}x promedio). Presión de compra/venta equilibrada ({buy_pct:.0f}% compras)."
            neutral_count += 1
        analysis["details"]["volume"] = {
            "name": "Volumen & Order Flow", "value": f"{vol_ratio:.1f}x", "signal": sig, "emoji": emoji,
            "interpretation": interp
        }

    # ── VWAP Analysis ──
    vwap_dist = indicators.get('price_vs_vwap')
    if vwap_dist is not None:
        if vwap_dist > 0.1:
            sig, emoji = "SOBRE VWAP", "🟢"
            interp = f"Precio {vwap_dist:.3f}% por encima del VWAP. Compradores tienen control. Los institucionales ven esto como tendencia alcista."
            bullish_count += 1
        elif vwap_dist < -0.1:
            sig, emoji = "BAJO VWAP", "🔴"
            interp = f"Precio {abs(vwap_dist):.3f}% por debajo del VWAP. Vendedores dominan. Operar por debajo del VWAP es señal bajista institucional."
            bearish_count += 1
        else:
            sig, emoji = "EN VWAP", "⚪"
            interp = f"Precio prácticamente en el VWAP ({vwap_dist:.3f}%). Zona de equilibrio — el mercado está indeciso."
            neutral_count += 1
        analysis["details"]["vwap"] = {
            "name": "VWAP", "value": f"{vwap_dist:.3f}%", "signal": sig, "emoji": emoji,
            "interpretation": interp
        }

    # ── Volatility ──
    rolling_vol = indicators.get('rolling_vol_5')
    if rolling_vol is not None:
        if rolling_vol > 0.05:
            vol_level = "ALTA"
            interp = f"Volatilidad {MARKET_INTERVAL}: {rolling_vol:.4f}%. Mercado muy movido; predicciones menos confiables."
        elif rolling_vol > 0.02:
            vol_level = "MODERADA"
            interp = f"Volatilidad {MARKET_INTERVAL}: {rolling_vol:.4f}%. Movimiento normal del mercado. Buen entorno para predicciones."
        else:
            vol_level = "BAJA"
            interp = f"Volatilidad {MARKET_INTERVAL}: {rolling_vol:.4f}%. Mercado tranquilo; las predicciones son más confiables pero las oportunidades menores."
        analysis["details"]["volatility"] = {
            "name": "Volatilidad", "value": f"{rolling_vol:.4f}%", "signal": vol_level, "emoji": "📊",
            "interpretation": interp
        }

    # ── Price Action ──
    consec = indicators.get('consecutive_direction')
    if consec is not None:
        direction = "alcista" if consec > 0 else "bajista"
        bars = abs(int(consec))
        if bars >= 5:
            interp = f"{bars} velas consecutivas {direction}. Racha extendida → posible agotamiento y reversión inminente."
        elif bars >= 3:
            interp = f"{bars} velas consecutivas {direction}. Tendencia de corto plazo establecida."
        else:
            interp = f"Sin racha clara. Movimiento mixto en las últimas velas."
        analysis["details"]["price_action"] = {
            "name": "Price Action", "value": f"{bars} {direction}", "signal": direction.upper(), "emoji": "📈" if consec > 0 else "📉",
            "interpretation": interp
        }

    # ── Model Probability Interpretation ──
    if prob_up > 0.65:
        prob_interp = f"El modelo asigna {prob_up*100:.1f}% de probabilidad alcista — señal fuerte de subida."
    elif prob_up > 0.55:
        prob_interp = f"El modelo asigna {prob_up*100:.1f}% de probabilidad alcista — ligera inclinación al alza."
    elif prob_up > 0.45:
        prob_interp = f"Modelo indeciso ({prob_up*100:.1f}% UP). La señal es débil — zona de incertidumbre."
    elif prob_up > 0.35:
        prob_interp = f"El modelo asigna {prob_down*100:.1f}% de probabilidad bajista — ligera inclinación a la baja."
    else:
        prob_interp = f"El modelo asigna {prob_down*100:.1f}% de probabilidad bajista — señal fuerte de caída."

    analysis["details"]["model"] = {
        "name": "Probabilidad ML", "value": f"{prob_up*100:.1f}% UP", "signal": "UP" if prob_up >= 0.5 else "DOWN",
        "emoji": "🤖",
        "interpretation": prob_interp
    }

    # ── Window Status ──
    price_vs_open = ((current_price - window_open) / window_open) * 100 if window_open else 0
    if abs(price_vs_open) < 0.005:
        win_interp = f"Precio prácticamente igual al de apertura de la ventana (${window_open:,.2f}). La apuesta es un coin-flip en este momento."
    elif price_vs_open > 0:
        win_interp = f"Precio actual ${current_price:,.2f} está {price_vs_open:.4f}% ARRIBA del precio de apertura ${window_open:,.2f}. Si cierra ahora → UP gana."
    else:
        win_interp = f"Precio actual ${current_price:,.2f} está {abs(price_vs_open):.4f}% ABAJO del precio de apertura ${window_open:,.2f}. Si cierra ahora → DOWN gana."
    analysis["details"]["window"] = {
        "name": "Estado de la Ventana", "value": f"{price_vs_open:+.4f}%",
        "signal": "UP" if price_vs_open > 0 else "DOWN" if price_vs_open < 0 else "EMPATE",
        "emoji": "⏱️", "interpretation": win_interp
    }

    # ── FINAL CONCLUSION ──
    total = bullish_count + bearish_count + neutral_count
    if total == 0:
        total = 1

    conclusion_parts = []
    if bullish_count > bearish_count + 2:
        verdict = "ALCISTA"
        risk = "MEDIO"
        conclusion_parts.append(f"📊 VEREDICTO: ALCISTA — {bullish_count} de {total} señales son alcistas.")
        conclusion_parts.append(f"Los indicadores técnicos favorecen una subida en los próximos {WINDOW_SECONDS // 60} minutos.")
        if confidence > 0.3:
            conclusion_parts.append("El modelo ML también respalda esta dirección con confianza moderada-alta.")
        else:
            conclusion_parts.append("Sin embargo, la confianza del modelo es baja — proceder con precaución.")
    elif bearish_count > bullish_count + 2:
        verdict = "BAJISTA"
        risk = "MEDIO"
        conclusion_parts.append(f"📊 VEREDICTO: BAJISTA — {bearish_count} de {total} señales son bajistas.")
        conclusion_parts.append(f"Los indicadores técnicos apuntan a una caída en los próximos {WINDOW_SECONDS // 60} minutos.")
        if confidence > 0.3:
            conclusion_parts.append("El modelo ML confirma la presión vendedora.")
        else:
            conclusion_parts.append("La confianza del modelo es baja — la señal no es contundente.")
    elif bullish_count > bearish_count:
        verdict = "LIGERAMENTE ALCISTA"
        risk = "ALTO"
        conclusion_parts.append(f"📊 VEREDICTO: LIGERAMENTE ALCISTA — {bullish_count} alcistas vs {bearish_count} bajistas.")
        conclusion_parts.append("La ventaja es marginal. Solo operar si Polymarket ofrece odds favorables (edge > 10%).")
    elif bearish_count > bullish_count:
        verdict = "LIGERAMENTE BAJISTA"
        risk = "ALTO"
        conclusion_parts.append(f"📊 VEREDICTO: LIGERAMENTE BAJISTA — {bearish_count} bajistas vs {bullish_count} alcistas.")
        conclusion_parts.append("Señal débil. No recomendado apostar a menos que el edge sea significativo.")
    else:
        verdict = "INDECISO"
        risk = "MUY ALTO"
        conclusion_parts.append(f"📊 VEREDICTO: MERCADO INDECISO — señales mixtas ({bullish_count} alcistas, {bearish_count} bajistas, {neutral_count} neutrales).")
        conclusion_parts.append("⚠️ RECOMENDACIÓN: NO APOSTAR. Cuando el mercado está indeciso, la mejor jugada es esperar.")

    # Risk assessment
    if rolling_vol and rolling_vol > 0.05:
        risk = "MUY ALTO"
        conclusion_parts.append("🚨 ALERTA: Volatilidad extrema. Los movimientos son impredecibles.")
    
    conclusion_parts.append(f"Nivel de riesgo: {risk}")

    analysis["conclusion"] = "\n".join(conclusion_parts)
    analysis["verdict"] = verdict
    analysis["risk_level"] = risk
    analysis["signal_summary"] = {
        "bullish": bullish_count,
        "bearish": bearish_count,
        "neutral": neutral_count,
        "total": total,
    }

    return analysis


# ─── Routes ───

@app.route("/")
def index():
    return send_from_directory("dashboard", "index.html")


@app.route("/api/predict")
def predict():
    """Live prediction for the current/next Polymarket round."""
    if model is None:
        return jsonify({"error": "Model not loaded. Train first."}), 503

    try:
        df = fetch_recent_candles(200)
        current_price = float(df.iloc[-1]['close'])
        next_cutoff = get_next_cutoff()
        cutoff_key = str(next_cutoff)

        # Window open (Baseline)
        # Polymarket "Precio a superar" is often the CLOSE of the last minute
        window_start = next_cutoff - WINDOW_SECONDS
        window_start_ms = window_start * 1000
        market = get_active_poly_market(window_start)
        
        # We look for the candle ending at exactly window_start
        # Which is the one with timestamp (window_start - 60)
        prev_candle_ts = (window_start - 60) * 1000
        prev_candle = df[df['timestamp'] == prev_candle_ts]
        
        if not prev_candle.empty:
            window_open = float(prev_candle.iloc[0]['close'])
            baseline_source = f"{PRICE_SOURCE_LABEL}_prev_close"
        else:
            # Fallback to the open of the first candle of the window
            window_candles = df[df['timestamp'] >= window_start_ms]
            window_open = float(window_candles.iloc[0]['open']) if len(window_candles) > 0 else current_price
            baseline_source = f"{PRICE_SOURCE_LABEL}_window_open"

        if market and market.get("baseline"):
            market_baseline = float(market["baseline"])
            market_source = market.get("baseline_source") or "polymarket_gamma_event_metadata"
            delta = abs(market_baseline - window_open)
            delta_pct = (delta / window_open * 100) if window_open else 0
            if delta <= BASELINE_MAX_FEED_DELTA_ABS and delta_pct <= BASELINE_MAX_FEED_DELTA_PCT:
                window_open = market_baseline
                baseline_source = market_source
            else:
                print(
                    f"Rejected Polymarket baseline {market_baseline:.2f} from {market_source}; "
                    f"feed baseline is {window_open:.2f} (delta {delta:.2f}, {delta_pct:.3f}%)."
                )
        
        # If we didn't get the exact Polymarket baseline (usual case during the live
        # round), use the Pyth oracle at the window open. It's the source Polymarket
        # settles on, so it matches the real priceToBeat within a few dollars vs ~$25.
        if baseline_source.startswith(PRICE_SOURCE_LABEL) and os.getenv("USE_ORACLE_BASELINE", "true").lower() == "true":
            oracle_baseline = oracle_price_at(window_start)
            if oracle_baseline is not None:
                window_open = oracle_baseline
                baseline_source = ORACLE_BASELINE_SOURCE

        # Check for manual override
        if cutoff_key in manual_baselines:
            window_open = manual_baselines[cutoff_key]
            baseline_source = "manual_sync"
        
        # Features & base prediction
        df_feat = compute_all_features(df)
        
        # Keep the model's distance feature aligned with the same baseline shown on the dashboard.
        df_feat.loc[df_feat.index[-1], 'dist_to_window_open'] = (current_price - window_open) / (window_open + 1e-10) * 100
        
        base_pred = base_prediction(model, df_feat)

        quotes = []
        if market and fetch_market_quotes is not None:
            try:
                quotes = fetch_market_quotes(market, round_cutoff=next_cutoff)
            except Exception as exc:
                print(f"Polymarket quote fetch failed: {exc}")

        dist = current_price - window_open
        dist_pct = (dist / window_open * 100) if window_open else None
        seconds_to_cutoff = max(0, next_cutoff - int(time.time()))
        display_buckets = [5, 15, 30, 60, 90, 120, 180, 240, 360, 480, 600, 720, 840, 895]
        seconds_bucket = next((b for b in display_buckets if seconds_to_cutoff <= b), 895)
        base_edge = edge_from_probabilities(base_pred["prob_up"], quotes, EDGE_THRESHOLD)
        context = {
            "seconds_to_cutoff": seconds_to_cutoff,
            "btc_price": current_price,
            "baseline": window_open,
            "dist_to_baseline": dist,
            "dist_to_baseline_pct": dist_pct,
            "base_prob_up": base_pred["prob_up"],
            "base_prob_down": base_pred["prob_down"],
            "base_confidence": base_pred["confidence"],
            "base_edge_up": base_edge["edge_up"],
            "base_edge_down": base_edge["edge_down"],
        }
        final_pred = select_prediction(ACTIVE_MODEL, base_pred, market_model, context, quotes)
        edge = edge_from_probabilities(final_pred["prob_up"], quotes, EDGE_THRESHOLD)
        baseline_exact = baseline_is_exact(baseline_source)
        baseline_action_allowed = baseline_allows_action(baseline_source)
        raw_recommended_action = edge["recommended_action"]
        recommended_action = raw_recommended_action
        if REQUIRE_EXACT_BASELINE_FOR_ACTION and not baseline_action_allowed:
            recommended_action = "WAIT"
        elif not baseline_exact and seconds_to_cutoff > WINDOW_SECONDS - PROXY_BASELINE_MIN_ELAPSED_SECONDS:
            recommended_action = "WAIT"
        quotes_by_side = {q.get("outcome"): q for q in quotes}
        up_quote = quotes_by_side.get("UP") or {}
        down_quote = quotes_by_side.get("DOWN") or {}
        quote_summary = {
            "up_bid": up_quote.get("best_bid"),
            "up_ask": up_quote.get("best_ask"),
            "up_midpoint": up_quote.get("midpoint"),
            "up_spread": up_quote.get("spread"),
            "down_bid": down_quote.get("best_bid"),
            "down_ask": down_quote.get("best_ask"),
            "down_midpoint": down_quote.get("midpoint"),
            "down_spread": down_quote.get("spread"),
        }

        existing_round = prediction_rounds.get(cutoff_key, {})
        initial_prediction = existing_round.get("initial_prediction")
        initial_prob_up = existing_round.get("initial_prob_up")
        initial_prob_down = existing_round.get("initial_prob_down")
        initial_confidence = existing_round.get("initial_confidence")
        initial_timestamp = existing_round.get("initial_timestamp")
        initial_seconds_to_cutoff = existing_round.get("initial_seconds_to_cutoff")
        initial_action = existing_round.get("initial_recommended_action")
        initial_edge_up = existing_round.get("initial_edge_up")
        initial_edge_down = existing_round.get("initial_edge_down")

        if initial_prediction is None:
            initial_prediction = final_pred["prediction"]
            initial_prob_up = round(final_pred["prob_up"], 4)
            initial_prob_down = round(final_pred["prob_down"], 4)
            initial_confidence = round(final_pred["confidence"], 4)
            initial_timestamp = int(time.time())
            initial_seconds_to_cutoff = seconds_to_cutoff
            initial_action = recommended_action
            initial_edge_up = None if edge["edge_up"] is None else round(edge["edge_up"], 4)
            initial_edge_down = None if edge["edge_down"] is None else round(edge["edge_down"], 4)

        # Update live fields while preserving the initial 15m forecast.
        prediction_rounds[cutoff_key] = {
            "next_cutoff": next_cutoff,
            "window_open": window_open, # This is the active baseline
            "manual_baseline": manual_baselines.get(cutoff_key),
            "prediction": final_pred["prediction"],
            "prob_up": round(final_pred["prob_up"], 4),
            "prob_down": round(final_pred["prob_down"], 4),
            "confidence": round(final_pred["confidence"], 4),
            "model_version": final_pred.get("model_version", ACTIVE_MODEL),
            "model_stage": MODEL_STAGE,
            "active_model": ACTIVE_MODEL,
            "initial_prediction": initial_prediction,
            "initial_prob_up": initial_prob_up,
            "initial_prob_down": initial_prob_down,
            "initial_confidence": initial_confidence,
            "initial_timestamp": initial_timestamp,
            "initial_seconds_to_cutoff": initial_seconds_to_cutoff,
            "initial_recommended_action": initial_action,
            "initial_edge_up": initial_edge_up,
            "initial_edge_down": initial_edge_down,
            "baseline_source": baseline_source,
            "baseline_exact": baseline_exact,
            "baseline_action_allowed": baseline_action_allowed,
            "baseline_warning": None if baseline_exact else "Exact Polymarket/Chainlink baseline not available; using allowed price-feed proxy." if baseline_action_allowed else "Exact Polymarket/Chainlink baseline not available yet; action held at WAIT.",
            "resolution_source": (market or {}).get("resolution_source"),
            "base_prediction": {
                "prediction": base_pred["prediction"],
                "prob_up": round(base_pred["prob_up"], 4),
                "prob_down": round(base_pred["prob_down"], 4),
                "confidence": round(base_pred["confidence"], 4),
            },
            "feature_values": final_pred.get("feature_values") or base_pred["features"],
            "edge_up": None if edge["edge_up"] is None else round(edge["edge_up"], 4),
            "edge_down": None if edge["edge_down"] is None else round(edge["edge_down"], 4),
            "up_ask": edge["up_ask"],
            "down_ask": edge["down_ask"],
            "quote_summary": quote_summary,
            "raw_recommended_action": raw_recommended_action,
            "recommended_action": recommended_action,
            "current_price": current_price,
            "seconds_to_cutoff": seconds_to_cutoff,
            "seconds_bucket": seconds_bucket,
            "timestamp": int(time.time()),
            "actual_close": existing_round.get("actual_close"),
            "actual_outcome": existing_round.get("actual_outcome"),
            "outcome": existing_round.get("outcome")
        }
        save_predictions()

        return jsonify(prediction_rounds[cutoff_key])

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sync_history_baseline", methods=["POST"])
def sync_history_baseline():
    """Manual override for any round baseline (active or past)."""
    try:
        data = request.json
        price = float(data.get("price"))
        cutoff = str(data.get("cutoff")) # Expecting timestamp string
        
        # Also store in manual_baselines for the active one if applicable
        manual_baselines[cutoff] = price
        
        if cutoff in prediction_rounds:
            p = prediction_rounds[cutoff]
            p["window_open"] = price
            p["manual_baseline"] = price
            
            # Recalculate outcome if actual_close exists
            if p.get("actual_close"):
                is_up = p["actual_close"] > price
                p["actual_outcome"] = "UP" if is_up else "DOWN" if p["actual_close"] < price else "TIE"
                evaluated_prediction = p.get("initial_prediction") or p.get("prediction")
                if (evaluated_prediction == "UP" and is_up) or (evaluated_prediction == "DOWN" and not is_up):
                    p["outcome"] = "WIN"
                else:
                    p["outcome"] = "LOSS"
            
            save_predictions()
            return jsonify({"success": True, "baseline": price, "outcome": p.get("outcome")})
        else:
            # If round doesn't exist yet, we still store the manual baseline for when it's created
            return jsonify({"success": True, "baseline": price, "note": "Round entry not yet created, but baseline stored."})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/rounds")
def rounds():
    """Get history of rounds, sorted by newest first."""
    try:
        if db.db_enabled():
            rows = db.fetch_recent_rounds_v2(50)
            by_cutoff = {}
            for row in rows:
                cutoff = int(row["round_cutoff"])
                if cutoff in by_cutoff:
                    continue
                baseline = row.get("baseline")
                actual_close = row.get("actual_close")
                actual_outcome = row.get("outcome")
                prediction = row.get("prediction")
                evaluated_prediction = row.get("initial_prediction") or prediction
                hit = None
                if actual_outcome in {"UP", "DOWN"} and evaluated_prediction in {"UP", "DOWN"}:
                    hit = "WIN" if evaluated_prediction == actual_outcome else "LOSS"
                by_cutoff[cutoff] = {
                    "next_cutoff": cutoff,
                    "window_open": float(baseline) if baseline is not None else None,
                    "initial_prediction": row.get("initial_prediction"),
                    "initial_prob_up": row.get("initial_prob_up"),
                    "initial_observed_at": row.get("initial_observed_at"),
                    "prediction": prediction,
                    "prob_up": row.get("prob_up"),
                    "actual_close": float(actual_close) if actual_close is not None else None,
                    "actual_outcome": actual_outcome,
                    "outcome": hit,
                    "observed_at": row.get("observed_at"),
                    "source": db.db_backend(),
                    "baseline_source": row.get("baseline_source"),
                    "resolution_source": row.get("resolution_source"),
                    "model_version": row.get("model_version"),
                    "model_stage": row.get("model_stage"),
                }
            if by_cutoff:
                return jsonify(sorted(by_cutoff.values(), key=lambda x: x["next_cutoff"], reverse=True)[:50])
    except Exception as e:
        print(f"DB rounds failed: {e}")

    sorted_rounds = sorted(prediction_rounds.values(), key=lambda x: x["next_cutoff"], reverse=True)
    return jsonify(sorted_rounds)


@app.route("/api/stats")
def stats():
    """Calculate win rate from validated rounds."""
    try:
        if db.db_enabled():
            bets = db.fetch_recent_signals_v2(1000)
            source = f"{db.db_backend()}_strategy_performance_v2"
            validated_bets = [b for b in bets if b.get("result") in {"WIN", "LOSS"}]
            if validated_bets:
                wins = len([b for b in validated_bets if b["result"] == "WIN"])
                pnl = sum(float(b.get("pnl") or 0) for b in validated_bets)
                return jsonify({
                    "source": source,
                    "win_rate": round(wins / len(validated_bets) * 100, 1),
                    "total": len(validated_bets),
                    "wins": wins,
                    "losses": len(validated_bets) - wins,
                    "pnl": round(pnl, 2),
                })

        validated = [p for p in prediction_rounds.values() if p.get("outcome") is not None]
        if not validated:
            return jsonify({"source": "local_json", "win_rate": 0, "total": 0})
        
        wins = len([p for p in validated if p["outcome"] == "WIN"])
        return jsonify({
            "source": "local_json",
            "win_rate": round(wins / len(validated) * 100, 1),
            "total": len(validated),
            "wins": wins,
            "losses": len(validated) - wins
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/signals")
def signals():
    """Recent simulated entries from the configured DB."""
    try:
        limit = min(int(request.args.get("limit", 25)), 100)
        rows = db.fetch_recent_signals_v2(limit)
        source = "v2"
        enriched = []
        for row in rows:
            raw = row.get("raw") or {}
            resolved = raw.get("resolved") or {}
            enriched.append({
                "id": row.get("signal_id") or row.get("id"),
                "observed_at": row.get("observed_at"),
                "round_cutoff": row.get("round_cutoff"),
                "side": row.get("side"),
                "entry_price": row.get("entry_price"),
                "stake": row.get("stake"),
                "model_prob": row.get("model_prob"),
                "edge": row.get("edge"),
                "result": row.get("result"),
                "pnl": row.get("pnl"),
                "seconds_to_cutoff": row.get("seconds_to_cutoff") or raw.get("seconds_to_cutoff"),
                "seconds_bucket": row.get("seconds_bucket"),
                "btc_price": row.get("btc_price") or raw.get("btc_price"),
                "baseline": row.get("baseline") or raw.get("baseline"),
                "baseline_source": row.get("baseline_source"),
                "actual_close": row.get("actual_close") or resolved.get("actual_close"),
                "outcome": row.get("outcome") or resolved.get("outcome"),
                "model_version": row.get("model_version"),
                "model_stage": row.get("model_stage"),
                "strategy_version": row.get("strategy_version"),
                "source": source,
            })
        return jsonify(enriched)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/price")
def price():
    """Get recent price data for charting."""
    try:
        minutes = int(request.args.get("minutes", 120))
        minutes = min(minutes, 500)
        df = fetch_recent_candles(minutes)

        # Aggregate to market-window candles for the chart
        df['window'] = df['timestamp'] // WINDOW_MS
        candles_window = df.groupby('window').agg(
            time=('timestamp', 'first'),
            open=('open', 'first'),
            high=('high', 'max'),
            low=('low', 'min'),
            close=('close', 'last'),
            volume=('volume_usdt', 'sum'),
        ).reset_index(drop=True)

        # Convert timestamp to seconds
        candles_window['time'] = (candles_window['time'] / 1000).astype(int)

        return jsonify({
            "candles": candles_window.to_dict(orient="records"),
            "current_price": float(df.iloc[-1]['close']),
            "window_seconds": WINDOW_SECONDS,
            "market_interval": MARKET_INTERVAL,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/chart-data")
def chart_data():
    """Return recent 1-minute candles for the local BTC chart."""
    try:
        minutes = int(request.args.get("minutes", 240))
        minutes = max(60, min(minutes, 720))
        df = fetch_recent_candles(minutes)
        df = df.tail(minutes).copy()
        df["time"] = (df["timestamp"] / 1000).astype(int)
        return jsonify({
            "candles": df[["time", "open", "high", "low", "close", "volume_btc"]].to_dict(orient="records"),
            "current_price": float(df.iloc[-1]["close"]),
            "source": PRICE_SOURCE_LABEL,
            "market_interval": MARKET_INTERVAL,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/model-info")
def model_info():
    """Return model metrics and feature importance."""
    top_features = dict(list(feat_importance.items())[:15])
    market_metrics = {}
    if market_model is not None:
        market_metrics = market_model.get("metrics", {})
    return jsonify({
        "active_model": ACTIVE_MODEL,
        "active_model_alias": ACTIVE_MODEL,
        "active_model_version": active_model_version(),
        "base_model": "binance-hgb-v1",
        "market_model_available": market_model is not None,
        "metrics": metrics,
        "market_metrics": market_metrics,
        "feature_importance": top_features,
        "total_features": len(FEATURE_COLUMNS),
    })


@app.route("/api/model-performance")
def model_performance():
    """Dashboard-ready model and strategy performance from the configured DB."""
    now = time.time()
    if performance_cache["payload"] and now - performance_cache["ts"] < PERFORMANCE_CACHE_SECONDS:
        return jsonify(performance_cache["payload"])

    try:
        report_path = os.path.join("reports", "supabase_eda_report.json")
        report = {}
        if os.path.exists(report_path):
            with open(report_path, "r", encoding="utf-8") as f:
                report = json.load(f)

        model_cards = []
        bets = []
        if db.db_enabled():
            model_cards = db.fetch_model_performance_v2()
            bets = db.fetch_recent_signals_v2(1000)
            report = {**report, **(db.fetch_dataset_summary_v2() or {})}

        closed_bets = [b for b in bets if b.get("result") in {"WIN", "LOSS"}]
        wins = len([b for b in closed_bets if b.get("result") == "WIN"])
        pnl = sum(float(b.get("pnl") or 0) for b in closed_bets)

        payload = {
            "active_model": active_model_version(),
            "active_model_alias": ACTIVE_MODEL,
            "active_model_version": active_model_version(),
            "db_backend": db.db_backend(),
            "edge_threshold": EDGE_THRESHOLD,
            "report": report,
            "models": model_cards,
            "strategy": {
                "closed_bets": len(closed_bets),
                "wins": wins,
                "losses": len(closed_bets) - wins,
                "win_rate": round(wins / len(closed_bets) * 100, 2) if closed_bets else 0,
                "pnl": round(pnl, 4),
            },
            "training": {
                "base_metrics": metrics,
                "market_metrics": (market_model or {}).get("metrics", {}) if market_model else {},
                "market_model_available": market_model is not None,
            },
        }
        performance_cache["ts"] = now
        performance_cache["payload"] = payload
        return jsonify(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/dataset-health")
def dataset_health():
    """Basic DB row counts and latest model dataset status."""
    try:
        tables = [
            "rounds",
            "reference_prices",
            "decision_snapshots",
            "market_quotes",
            "feature_snapshots",
            "predictions_v2",
            "signals_v2",
            "round_results",
            "trade_results_v2",
            "dataset_versions",
            "model_runs",
        ]
        counts = {}
        for table in tables:
            try:
                counts[table] = db.count_rows(table)
            except Exception:
                counts[table] = None
        recent_results = db.fetch_recent_round_results(5)
        recent_bets = db.fetch_recent_signals_v2(5)
        return jsonify({
            "db_enabled": db.db_enabled(),
            "db_backend": db.db_backend(),
            "counts": counts,
            "recent_round_results": recent_results,
            "recent_signals": recent_bets,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/dashboard-data")
def dashboard_data():
    """Operational dashboard data with predictions, signals, rounds, and metadata."""
    try:
        prediction_limit = min(int(request.args.get("prediction_limit", 5000)), 20000)
        signal_limit = min(int(request.args.get("signal_limit", 5000)), 10000)
        round_limit = min(int(request.args.get("round_limit", 250)), 1000)

        predictions = db.fetch_dashboard_predictions(prediction_limit) if db.db_enabled() else []
        active_version = active_model_version()
        all_live_signals_rows = db.fetch_dashboard_signals(signal_limit) if db.db_enabled() else []
        live_active_signals_rows = [
            row for row in all_live_signals_rows
            if row.get("model_version") == active_version
        ]
        legacy_signals_rows = [
            row for row in all_live_signals_rows
            if row.get("model_version") != active_version
        ]
        backtest_run = db.fetch_latest_strategy_backtest_run(active_version, "dashboard-strategies-v1") if db.db_enabled() else None
        backtest_signals_rows = db.fetch_strategy_backtest_signals(active_version, signal_limit, "dashboard-strategies-v1") if db.db_enabled() else []
        active_forecasts_rows = _enrich_forecasts(predictions, active_version)
        model_test_predictions = _model_test_predictions(prediction_limit)
        live_blockers_summary = _summarize_live_blockers(active_forecasts_rows, live_active_signals_rows)
        signals_rows = backtest_signals_rows
        rounds_rows = db.fetch_recent_rounds_v2(round_limit) if db.db_enabled() else []
        model_cards = db.fetch_model_performance_v2() if db.db_enabled() else []
        dataset_summary = db.fetch_dataset_summary_v2() if db.db_enabled() else {}
        table_names = [
            "rounds",
            "reference_prices",
            "decision_snapshots",
            "market_quotes",
            "feature_snapshots",
            "predictions_v2",
            "signals_v2",
            "round_results",
            "trade_results_v2",
            "strategy_backtest_runs",
            "strategy_backtest_signals",
        ]
        counts = {}
        for table in table_names:
            try:
                counts[table] = db.count_rows(table)
            except Exception:
                counts[table] = None

        return jsonify(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "db_backend": db.db_backend(),
                "active_model": active_version,
                "active_model_alias": ACTIVE_MODEL,
                "active_model_version": active_version,
                "model_stage": MODEL_STAGE,
                "edge_threshold": EDGE_THRESHOLD,
                "signal_source": "strategy_lab",
                "signal_sources": {
                    "strategy_lab": {
                        "label": "Strategy Lab",
                        "description": "Historical replay plus new active-model forecasts for strategy comparison. Not live PnL.",
                        "signals": len(backtest_signals_rows),
                    },
                    "live_active": {
                        "label": "Actual Live Paper PnL",
                        "description": "Closed/live collector signals generated by the active model.",
                        "signals": len(live_active_signals_rows),
                    },
                    "backtest_active": {
                        "label": "Backtest Active Model",
                        "description": "Historical replay using the active model. Not live performance.",
                        "signals": len(backtest_signals_rows),
                    },
                    "all_live": {
                        "label": "All Live Audit",
                        "description": "All collector signals across model versions.",
                        "signals": len(all_live_signals_rows),
                    },
                },
                "active_backtest_run": backtest_run,
                "counts": counts,
                "predictions": predictions,
                "model_test_predictions": model_test_predictions,
                "signals": signals_rows,
                "active_model_forecasts": active_forecasts_rows,
                "actual_live_trades": live_active_signals_rows,
                "strategy_replay": {
                    "signals": backtest_signals_rows,
                    "source": "strategy_backtest_signals",
                    "run": backtest_run,
                    "dynamic_replay_hint": "Frontend adds resolved active-model forecasts not already represented in the backtest.",
                },
                "live_blockers_summary": live_blockers_summary,
                "live_active_signals": live_active_signals_rows,
                "backtest_active_signals": backtest_signals_rows,
                "all_live_signals": all_live_signals_rows,
                "legacy_signals": legacy_signals_rows,
                "rounds": rounds_rows,
                "models": model_cards,
                "dataset_summary": dataset_summary,
                "training_metrics": {
                    "base": metrics,
                    "market": (market_model or {}).get("metrics", {}) if market_model else {},
                },
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/live-signal-history")
def live_signal_history():
    """Lightweight payload for fast live Signal History refreshes."""
    try:
        prediction_limit = min(int(request.args.get("prediction_limit", 5000)), 5000)
        signal_limit = min(int(request.args.get("signal_limit", 5000)), 5000)
        active_version = active_model_version()

        if not db.db_enabled():
            return jsonify(
                {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "active_model_version": active_version,
                    "edge_threshold": EDGE_THRESHOLD,
                    "active_model_forecasts": [],
                    "actual_live_trades": [],
                    "live_active_signals": [],
                    "live_blockers_summary": {},
                }
            )

        try:
            predictions = db.fetch_live_signal_history_predictions(active_version, prediction_limit)
        except AttributeError:
            predictions = [
                row for row in db.fetch_dashboard_predictions(prediction_limit)
                if row.get("model_version") == active_version
            ]
        all_live_signals_rows = db.fetch_dashboard_signals(signal_limit)
        live_active_signals_rows = [
            row for row in all_live_signals_rows
            if row.get("model_version") == active_version
        ]
        active_forecasts_rows = _enrich_forecasts(predictions, active_version)

        return jsonify(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "active_model_version": active_version,
                "active_model": active_version,
                "edge_threshold": EDGE_THRESHOLD,
                "active_model_forecasts": active_forecasts_rows,
                "actual_live_trades": live_active_signals_rows,
                "live_active_signals": live_active_signals_rows,
                "live_blockers_summary": _summarize_live_blockers(active_forecasts_rows, live_active_signals_rows),
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/retrain", methods=["POST"])
def retrain():
    """Trigger model retraining."""
    try:
        from model import train_model
        train_model()
        load_model()
        return jsonify({"status": "success", "message": "Model retrained and loaded."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    load_model()
    print("\n" + "=" * 50)
    print("  Polymarket BTC Predictor - Server")
    print("  Dashboard: http://localhost:5000")
    print("=" * 50 + "\n")
    # Start validator thread at the very end
    threading.Thread(target=validate_old_predictions, daemon=True).start()
    app.run(
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT", "5000")),
        debug=False,
    )
