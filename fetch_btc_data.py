"""
Bitcoin 1-Minute OHLCV Data Collector (últimos 7 días)
======================================================
Fuente: Binance Public API (sin API key)
Formato: CSV con columnas OHLCV + indicadores base para análisis técnico

Uso:
    python fetch_btc_data.py

Salida:
    btc_1m_7d_YYYYMMDD_HHMMSS.csv
"""

import requests
import csv
import time
from datetime import datetime, timezone

# ──────────────────────────────────────────────
# Configuración
# ──────────────────────────────────────────────
SYMBOL = "BTCUSDT"
INTERVAL = "1m"
DAYS = 7
LIMIT_PER_REQUEST = 1000  # máximo permitido por Binance
BASE_URL = "https://api.binance.com/api/v3/klines"

# Cálculos temporales
MS_PER_MINUTE = 60_000
TOTAL_MINUTES = DAYS * 24 * 60  # 10,080
now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
start_ms = now_ms - (TOTAL_MINUTES * MS_PER_MINUTE)


def fetch_klines(symbol: str, interval: str, start_time: int, end_time: int, limit: int = 1000) -> list:
    """Obtiene velas (klines) de Binance."""
    params = {
        "symbol": symbol,
        "interval": interval,
        "startTime": start_time,
        "endTime": end_time,
        "limit": limit,
    }
    response = requests.get(BASE_URL, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def collect_all_klines() -> list[dict]:
    """
    Recopila todas las velas de 1 minuto para los últimos 7 días,
    paginando automáticamente.
    """
    all_candles = []
    current_start = start_ms
    request_num = 0
    total_requests = (TOTAL_MINUTES // LIMIT_PER_REQUEST) + 1

    print(f"📊 Recopilando datos de {SYMBOL} | Intervalo: {INTERVAL}")
    print(f"📅 Período: últimos {DAYS} días ({TOTAL_MINUTES:,} velas esperadas)")
    print(f"🔄 Estimación: ~{total_requests} peticiones a Binance API\n")

    while current_start < now_ms:
        request_num += 1
        print(f"  → Petición {request_num}/{total_requests}...", end=" ", flush=True)

        try:
            raw = fetch_klines(SYMBOL, INTERVAL, current_start, now_ms, LIMIT_PER_REQUEST)
        except requests.exceptions.RequestException as e:
            print(f"❌ Error: {e}")
            print("  ⏳ Esperando 10s antes de reintentar...")
            time.sleep(10)
            continue

        if not raw:
            print("Sin más datos.")
            break

        for candle in raw:
            all_candles.append({
                "timestamp": candle[0],
                "datetime_utc": datetime.fromtimestamp(candle[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "open": float(candle[1]),
                "high": float(candle[2]),
                "low": float(candle[3]),
                "close": float(candle[4]),
                "volume_btc": float(candle[5]),
                "close_time": candle[6],
                "volume_usdt": float(candle[7]),
                "num_trades": int(candle[8]),
                "taker_buy_base": float(candle[9]),
                "taker_buy_quote": float(candle[10]),
            })

        print(f"✅ {len(raw)} velas recibidas (total: {len(all_candles):,})")

        # Avanzar el cursor al siguiente bloque
        last_open_time = raw[-1][0]
        current_start = last_open_time + MS_PER_MINUTE

        # Pausa para respetar rate limits
        time.sleep(0.3)

    return all_candles


def save_to_csv(candles: list[dict], filename: str):
    """Guarda las velas en un archivo CSV."""
    if not candles:
        print("⚠️ No hay datos para guardar.")
        return

    fieldnames = [
        "timestamp",
        "datetime_utc",
        "open",
        "high",
        "low",
        "close",
        "volume_btc",
        "volume_usdt",
        "num_trades",
        "taker_buy_base",
        "taker_buy_quote",
    ]

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for candle in candles:
            row = {k: candle[k] for k in fieldnames}
            writer.writerow(row)

    print(f"\n💾 Archivo guardado: {filename}")
    print(f"   Filas: {len(candles):,}")
    print(f"   Columnas: {', '.join(fieldnames)}")


def print_summary(candles: list[dict]):
    """Muestra un resumen rápido de los datos recopilados."""
    if not candles:
        return

    closes = [c["close"] for c in candles]
    volumes = [c["volume_usdt"] for c in candles]
    trades = [c["num_trades"] for c in candles]

    print("\n" + "=" * 55)
    print("📈 RESUMEN DE DATOS RECOPILADOS")
    print("=" * 55)
    print(f"  Par:             {SYMBOL}")
    print(f"  Intervalo:       {INTERVAL}")
    print(f"  Total velas:     {len(candles):,}")
    print(f"  Desde:           {candles[0]['datetime_utc']} UTC")
    print(f"  Hasta:           {candles[-1]['datetime_utc']} UTC")
    print(f"  ─────────────────────────────────────────")
    print(f"  Precio mínimo:   ${min(closes):,.2f}")
    print(f"  Precio máximo:   ${max(closes):,.2f}")
    print(f"  Precio actual:   ${closes[-1]:,.2f}")
    print(f"  Variación 7d:    {((closes[-1] - closes[0]) / closes[0] * 100):+.2f}%")
    print(f"  ─────────────────────────────────────────")
    print(f"  Vol. total USDT: ${sum(volumes):,.0f}")
    print(f"  Trades totales:  {sum(trades):,}")
    print("=" * 55)


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("  🪙 BITCOIN DATA COLLECTOR — Binance API")
    print("=" * 55 + "\n")

    candles = collect_all_klines()

    # Nombre del archivo con timestamp
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"btc_1m_7d_{ts}.csv"

    save_to_csv(candles, filename)
    print_summary(candles)

    print(f"\n✅ Listo. Usa el CSV para análisis técnico con pandas, ta-lib, etc.")
    print(f"   Ejemplo rápido en Python:")
    print(f'     import pandas as pd')
    print(f'     df = pd.read_csv("{filename}", parse_dates=["datetime_utc"])')
    print(f'     df.set_index("datetime_utc", inplace=True)')
    print(f'     print(df.describe())')
