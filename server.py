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
import requests
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

try:
    from polymarket import discover_btc_5m_market, fetch_market_quotes
except Exception:
    discover_btc_5m_market = None
    fetch_market_quotes = None

load_dotenv()

# ─── Config ───
MODEL_DIR = "model_artifacts"
BINANCE_URL = "https://api.binance.com/api/v3/klines"
SYMBOL = os.getenv("BINANCE_SYMBOL", "BTCUSDT")
PREDICTIONS_FILE = "predictions_db.json"
MAX_HISTORY = 200
EDGE_THRESHOLD = float(os.getenv("EDGE_THRESHOLD", "0.03"))
ACTIVE_MODEL = os.getenv("ACTIVE_MODEL", "market-aware-v1")

app = Flask(__name__, static_folder="dashboard", static_url_path="")

# ─── Load Model & State ───
model = None
market_model = None
metrics = {}
feat_importance = {}
active_poly_market = None


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


def get_active_poly_market():
    global active_poly_market
    expected_slug = f"btc-updown-5m-{(int(time.time()) // 300) * 300}"
    if active_poly_market is not None and active_poly_market.get("event_slug") != expected_slug:
        active_poly_market = None
    if active_poly_market is not None:
        return active_poly_market
    if discover_btc_5m_market is None:
        return None
    try:
        active_poly_market = discover_btc_5m_market()
        db.upsert_market(active_poly_market)
        print(f"Polymarket market detected: {active_poly_market.get('question')}")
    except Exception as e:
        print(f"Polymarket market discovery failed: {e}")
        active_poly_market = None
    return active_poly_market


def persist_live_prediction(df, df_feat, payload, quotes=None, market=None):
    """Best-effort edge enrichment and persistence. Keeps JSON fallback untouched."""
    payload.setdefault("edge_up", None)
    payload.setdefault("edge_down", None)
    payload.setdefault("recommended_action", "WAIT")
    payload.setdefault("db_snapshot_id", None)
    payload.setdefault("db_prediction_id", None)

    try:
        next_cutoff = payload["next_cutoff"]
        window_start = next_cutoff - 300
        current_price = payload["current_price"]
        baseline = payload["window_open"]
        dist = current_price - baseline
        snapshot_id = db.insert_round_snapshot({
            "observed_at": datetime.now(timezone.utc),
            "round_cutoff": next_cutoff,
            "window_start": window_start,
            "seconds_to_cutoff": max(0, next_cutoff - int(time.time())),
            "symbol": SYMBOL,
            "btc_price": current_price,
            "baseline": baseline,
            "dist_to_baseline": dist,
            "dist_to_baseline_pct": (dist / baseline * 100) if baseline else None,
            "source": "binance",
            "raw": {"latest_candle": df.iloc[-1].to_dict()},
        })

        market = market or get_active_poly_market()
        quotes = quotes or []
        if quotes:
            for quote in quotes:
                db.insert_quote(quote)
        elif market and fetch_market_quotes is not None:
            try:
                quotes = fetch_market_quotes(market, round_cutoff=next_cutoff)
                for quote in quotes:
                    db.insert_quote(quote)
            except Exception as e:
                print(f"Polymarket quote persistence failed: {e}")

        prob_up = payload["prob_up"]
        edge = edge_from_probabilities(prob_up, quotes, EDGE_THRESHOLD)
        edge_up = edge["edge_up"]
        edge_down = edge["edge_down"]
        action = edge["recommended_action"]
        up_ask = edge["up_ask"]
        down_ask = edge["down_ask"]

        features = payload.get("feature_values") or df_feat[FEATURE_COLUMNS].iloc[-1:].fillna(0).iloc[0].to_dict()
        prediction_id = db.insert_prediction({
            "observed_at": datetime.now(timezone.utc),
            "round_cutoff": next_cutoff,
            "model_version": payload.get("model_version", ACTIVE_MODEL),
            "prediction": payload["prediction"],
            "prob_up": prob_up,
            "prob_down": payload.get("prob_down", 1.0 - prob_up),
            "confidence": payload["confidence"],
            "edge_up": edge_up,
            "edge_down": edge_down,
            "recommended_action": action,
            "feature_values": features,
            "source_snapshot_id": snapshot_id,
            "raw": {"payload": payload, "quotes": quotes, "base_prediction": payload.get("base_prediction"), "active_model": ACTIVE_MODEL},
        })

        payload["edge_up"] = None if edge_up is None else round(edge_up, 4)
        payload["edge_down"] = None if edge_down is None else round(edge_down, 4)
        payload["up_ask"] = up_ask
        payload["down_ask"] = down_ask
        payload["recommended_action"] = action
        payload["db_snapshot_id"] = snapshot_id
        payload["db_prediction_id"] = prediction_id
    except Exception as e:
        print(f"Prediction persistence failed: {e}")


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
                    df = fetch_recent_candles(10)
                    matching = df[df['timestamp'] >= cutoff * 1000]
                    if not matching.empty:
                        # Use the open price of the cutoff candle as the final close price
                        close_price = float(matching.iloc[0]['open'])
                        p["actual_close"] = close_price
                        
                        # Use the actual baseline (manual or calculated)
                        baseline = p.get("manual_baseline") or p["window_open"]
                        is_up = close_price > baseline
                        
                        if (p["prediction"] == "UP" and is_up) or (p["prediction"] == "DOWN" and not is_up):
                            p["outcome"] = "WIN"
                        else:
                            p["outcome"] = "LOSS"
                        db.upsert_round_result({
                            "round_cutoff": cutoff,
                            "baseline": baseline,
                            "actual_close": close_price,
                            "outcome": "UP" if is_up else "DOWN" if close_price < baseline else "TIE",
                            "raw": p,
                        })
                        changed = True
            
            if changed:
                save_predictions()
                
        except Exception as e:
            print(f"Error in validator: {e}")
        time.sleep(30)

load_predictions()


def fetch_recent_candles(n=200):
    """Fetch the last N 1-minute candles from Binance."""
    params = {"symbol": SYMBOL, "interval": "1m", "limit": n}
    resp = requests.get(BINANCE_URL, params=params, timeout=15)
    resp.raise_for_status()
    raw = resp.json()
    candles = []
    for c in raw:
        candles.append({
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
        })
    return pd.DataFrame(candles)


def get_next_cutoff():
    """Get the next Polymarket 5-minute cutoff timestamp."""
    now = int(time.time())
    next_cut = ((now // 300) + 1) * 300
    return next_cut


def get_current_window_start():
    """Get the start of the current 5-minute window."""
    now = int(time.time())
    return (now // 300) * 300


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
            interp = f"Volatilidad 5min: {rolling_vol:.4f}%. Mercado muy movido → más riesgo pero más oportunidad. Predicciones menos confiables."
        elif rolling_vol > 0.02:
            vol_level = "MODERADA"
            interp = f"Volatilidad 5min: {rolling_vol:.4f}%. Movimiento normal del mercado. Buen entorno para predicciones."
        else:
            vol_level = "BAJA"
            interp = f"Volatilidad 5min: {rolling_vol:.4f}%. Mercado tranquilo → movimientos pequeños. Las predicciones son más confiables pero las oportunidades menores."
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
        conclusion_parts.append(f"Los indicadores técnicos favorecen una subida en los próximos 5 minutos.")
        if confidence > 0.3:
            conclusion_parts.append("El modelo ML también respalda esta dirección con confianza moderada-alta.")
        else:
            conclusion_parts.append("Sin embargo, la confianza del modelo es baja — proceder con precaución.")
    elif bearish_count > bullish_count + 2:
        verdict = "BAJISTA"
        risk = "MEDIO"
        conclusion_parts.append(f"📊 VEREDICTO: BAJISTA — {bearish_count} de {total} señales son bajistas.")
        conclusion_parts.append(f"Los indicadores técnicos apuntan a una caída en los próximos 5 minutos.")
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
    """Live prediction for the current/next 5-minute round."""
    if model is None:
        return jsonify({"error": "Model not loaded. Train first."}), 503

    try:
        df = fetch_recent_candles(200)
        current_price = float(df.iloc[-1]['close'])
        next_cutoff = get_next_cutoff()
        cutoff_key = str(next_cutoff)
        
        market = get_active_poly_market()

        # Window open (Baseline)
        # Polymarket "Precio a superar" is often the CLOSE of the last minute
        window_start = next_cutoff - 300
        window_start_ms = window_start * 1000
        
        # We look for the candle ending at exactly window_start
        # Which is the one with timestamp (window_start - 60)
        prev_candle_ts = (window_start - 60) * 1000
        prev_candle = df[df['timestamp'] == prev_candle_ts]
        
        if not prev_candle.empty:
            window_open = float(prev_candle.iloc[0]['close'])
            baseline_source = "binance_prev_close"
        else:
            # Fallback to the open of the first candle of the window
            window_candles = df[df['timestamp'] >= window_start_ms]
            window_open = float(window_candles.iloc[0]['open']) if len(window_candles) > 0 else current_price
            baseline_source = "binance_window_open"

        if market and market.get("baseline"):
            window_open = float(market["baseline"])
            baseline_source = "polymarket_gamma"
        
        # Check for manual override
        if cutoff_key in manual_baselines:
            window_open = manual_baselines[cutoff_key]
            baseline_source = "manual_sync"
        
        # Features & base prediction
        df_feat = compute_all_features(df)
        
        # CRITICAL: Overwrite the window-open used in features if we have a manual sync
        if cutoff_key in manual_baselines:
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

        # Update current round
        prediction_rounds[cutoff_key] = {
            "next_cutoff": next_cutoff,
            "window_open": window_open, # This is the active baseline
            "manual_baseline": manual_baselines.get(cutoff_key),
            "prediction": final_pred["prediction"],
            "prob_up": round(final_pred["prob_up"], 4),
            "prob_down": round(final_pred["prob_down"], 4),
            "confidence": round(final_pred["confidence"], 4),
            "model_version": final_pred.get("model_version", ACTIVE_MODEL),
            "active_model": ACTIVE_MODEL,
            "baseline_source": baseline_source,
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
            "recommended_action": edge["recommended_action"],
            "current_price": current_price,
            "timestamp": int(time.time()),
            "outcome": None
        }
        persist_live_prediction(df, df_feat, prediction_rounds[cutoff_key], quotes=quotes, market=market)
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
                if (p["prediction"] == "UP" and is_up) or (p["prediction"] == "DOWN" and not is_up):
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
            try:
                rows = db._get(
                    "modeling_snapshots",
                    {
                        "select": "observed_at,round_cutoff,baseline,prediction,prob_up,actual_close,outcome,target_up",
                        "order": "observed_at.desc",
                        "limit": 500,
                    },
                )
            except Exception:
                rows = []

            by_cutoff = {}
            for row in rows:
                cutoff = int(row["round_cutoff"])
                if cutoff in by_cutoff:
                    continue
                baseline = row.get("baseline")
                actual_close = row.get("actual_close")
                actual_outcome = row.get("outcome")
                prediction = row.get("prediction")
                hit = None
                if actual_outcome in {"UP", "DOWN"} and prediction in {"UP", "DOWN"}:
                    hit = "WIN" if prediction == actual_outcome else "LOSS"
                by_cutoff[cutoff] = {
                    "next_cutoff": cutoff,
                    "window_open": float(baseline) if baseline is not None else None,
                    "prediction": prediction,
                    "prob_up": row.get("prob_up"),
                    "actual_close": float(actual_close) if actual_close is not None else None,
                    "actual_outcome": actual_outcome,
                    "outcome": hit,
                    "observed_at": row.get("observed_at"),
                    "source": "supabase",
                }
            if by_cutoff:
                return jsonify(sorted(by_cutoff.values(), key=lambda x: x["next_cutoff"], reverse=True)[:50])
    except Exception as e:
        print(f"Supabase rounds failed: {e}")

    sorted_rounds = sorted(prediction_rounds.values(), key=lambda x: x["next_cutoff"], reverse=True)
    return jsonify(sorted_rounds)


@app.route("/api/stats")
def stats():
    """Calculate win rate from validated rounds."""
    try:
        if db.db_enabled():
            bets = db.fetch_recent_simulated_bets(1000)
            validated_bets = [b for b in bets if b.get("result") in {"WIN", "LOSS"}]
            if validated_bets:
                wins = len([b for b in validated_bets if b["result"] == "WIN"])
                pnl = sum(float(b.get("pnl") or 0) for b in validated_bets)
                return jsonify({
                    "source": "supabase_simulated_bets",
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
    """Recent simulated entries from Supabase."""
    try:
        limit = min(int(request.args.get("limit", 25)), 100)
        rows = db.fetch_recent_simulated_bets(limit)
        enriched = []
        for row in rows:
            raw = row.get("raw") or {}
            resolved = raw.get("resolved") or {}
            enriched.append({
                "id": row.get("id"),
                "observed_at": row.get("observed_at"),
                "round_cutoff": row.get("round_cutoff"),
                "side": row.get("side"),
                "entry_price": row.get("entry_price"),
                "stake": row.get("stake"),
                "model_prob": row.get("model_prob"),
                "edge": row.get("edge"),
                "result": row.get("result"),
                "pnl": row.get("pnl"),
                "seconds_to_cutoff": raw.get("seconds_to_cutoff"),
                "btc_price": raw.get("btc_price"),
                "baseline": raw.get("baseline"),
                "actual_close": resolved.get("actual_close"),
                "outcome": resolved.get("outcome"),
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

        # Aggregate to 5-minute candles for the chart
        df['window'] = df['timestamp'] // 300_000
        candles_5m = df.groupby('window').agg(
            time=('timestamp', 'first'),
            open=('open', 'first'),
            high=('high', 'max'),
            low=('low', 'min'),
            close=('close', 'last'),
            volume=('volume_usdt', 'sum'),
        ).reset_index(drop=True)

        # Convert timestamp to seconds
        candles_5m['time'] = (candles_5m['time'] / 1000).astype(int)

        return jsonify({
            "candles": candles_5m.to_dict(orient="records"),
            "current_price": float(df.iloc[-1]['close']),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/history")
def history():
    """Return prediction history with outcomes."""
    now = int(time.time())
    enriched = []
    for h in prediction_history[-50:]:
        entry = dict(h)
        # Check if this window has completed
        if now > h["next_cutoff"]:
            entry["completed"] = True
            # We'd need the actual close price to determine outcome
            # For now, mark as completed
        else:
            entry["completed"] = False
        enriched.append(entry)
    return jsonify(enriched)


@app.route("/api/model-info")
def model_info():
    """Return model metrics and feature importance."""
    top_features = dict(list(feat_importance.items())[:15])
    market_metrics = {}
    if market_model is not None:
        market_metrics = market_model.get("metrics", {})
    return jsonify({
        "active_model": ACTIVE_MODEL,
        "base_model": "binance-hgb-v1",
        "market_model_available": market_model is not None,
        "metrics": metrics,
        "market_metrics": market_metrics,
        "feature_importance": top_features,
        "total_features": len(FEATURE_COLUMNS),
    })


@app.route("/api/model-performance")
def model_performance():
    """Dashboard-ready model and strategy performance from Supabase."""
    try:
        report_path = os.path.join("reports", "supabase_eda_report.json")
        report = {}
        if os.path.exists(report_path):
            with open(report_path, "r", encoding="utf-8") as f:
                report = json.load(f)

        rows = []
        if db.db_enabled():
            predictions = db._get(
                "model_predictions",
                {
                    "select": "id,observed_at,round_cutoff,model_version,prediction,prob_up,confidence",
                    "order": "observed_at.asc",
                    "limit": 5000,
                },
            )
            results = db._get(
                "round_results",
                {
                    "select": "round_cutoff,outcome",
                    "order": "round_cutoff.asc",
                    "limit": 5000,
                },
            )
            outcome_by_cutoff = {int(r["round_cutoff"]): r.get("outcome") for r in results}
            for row in predictions:
                outcome = outcome_by_cutoff.get(int(row["round_cutoff"]))
                if outcome in {"UP", "DOWN"}:
                    row["target_up"] = 1 if outcome == "UP" else 0
                    rows.append(row)

        model_rows = pd.DataFrame(rows)
        model_cards = []
        if not model_rows.empty:
            model_rows["target_up"] = pd.to_numeric(model_rows["target_up"], errors="coerce")
            model_rows["prob_up"] = pd.to_numeric(model_rows["prob_up"], errors="coerce")
            model_rows["pred_up"] = (model_rows["prob_up"] >= 0.5).astype(int)
            model_rows["correct"] = model_rows["pred_up"].eq(model_rows["target_up"].astype(int))
            for version, group in model_rows.groupby("model_version", dropna=False):
                version_name = version or "unknown"
                first = group.sort_values("observed_at").groupby("round_cutoff", as_index=False).first()
                last = group.sort_values("observed_at").groupby("round_cutoff", as_index=False).last()
                model_cards.append({
                    "model_version": version_name,
                    "rows": int(len(group)),
                    "unique_rounds": int(group["round_cutoff"].nunique()),
                    "row_accuracy": round(float(group["correct"].mean() * 100), 2),
                    "first_round_accuracy": round(float(first["correct"].mean() * 100), 2) if len(first) else None,
                    "last_round_accuracy": round(float(last["correct"].mean() * 100), 2) if len(last) else None,
                })

        bets = []
        if db.db_enabled():
            bets = db.fetch_recent_simulated_bets(1000)
        closed_bets = [b for b in bets if b.get("result") in {"WIN", "LOSS"}]
        wins = len([b for b in closed_bets if b.get("result") == "WIN"])
        pnl = sum(float(b.get("pnl") or 0) for b in closed_bets)

        return jsonify({
            "active_model": ACTIVE_MODEL,
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
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/dataset-health")
def dataset_health():
    """Basic Supabase row counts and latest model dataset status."""
    try:
        tables = [
            "round_snapshots",
            "polymarket_quotes",
            "model_predictions",
            "simulated_bets",
            "round_results",
            "polymarket_markets",
        ]
        counts = {table: db.count_rows(table) for table in tables}
        recent_results = db.fetch_recent_round_results(5)
        recent_bets = db.fetch_recent_simulated_bets(5)
        return jsonify({
            "db_enabled": db.db_enabled(),
            "counts": counts,
            "recent_round_results": recent_results,
            "recent_simulated_bets": recent_bets,
        })
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
