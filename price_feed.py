import os
import time

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

BINANCE_URL = "https://api.binance.com/api/v3/klines"
COINBASE_URL = "https://api.exchange.coinbase.com/products/{product_id}/candles"

PRICE_SOURCE = os.getenv("PRICE_SOURCE", "coinbase").strip().lower()
BINANCE_SYMBOL = os.getenv("BINANCE_SYMBOL", "BTCUSDT")
COINBASE_PRODUCT_ID = os.getenv("COINBASE_PRODUCT_ID", "BTC-USD")
COINBASE_MAX_CANDLES_PER_REQUEST = 290


PYTH_HERMES_URL = "https://hermes.pyth.network/v2/updates/price/{ts}"
PYTH_BTC_USD_FEED_ID = os.getenv(
    "PYTH_BTC_USD_FEED_ID",
    "e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43",
)
_ORACLE_CACHE = {}


def oracle_price_at(ts):
    """BTC/USD price from the Pyth benchmark oracle at unix `ts`.

    Pyth is the same oracle Polymarket settles these BTC up/down markets on, so this
    reproduces the round's `priceToBeat` within a few dollars (vs ~$25 for a CEX proxy).
    Returns float or None. Cached per timestamp (a round's baseline is fixed)."""
    ts = int(ts)
    if ts in _ORACLE_CACHE:
        return _ORACLE_CACHE[ts]
    try:
        resp = requests.get(
            PYTH_HERMES_URL.format(ts=ts),
            params={"ids[]": PYTH_BTC_USD_FEED_ID},
            headers={"User-Agent": "polymarket-btc-bot/1.0"},
            timeout=10,
        )
        resp.raise_for_status()
        parsed = resp.json().get("parsed") or []
        if not parsed:
            return None
        price = parsed[0]["price"]
        value = float(int(price["price"]) * (10 ** int(price["expo"])))
        _ORACLE_CACHE[ts] = value
        if len(_ORACLE_CACHE) > 500:
            _ORACLE_CACHE.pop(next(iter(_ORACLE_CACHE)))
        return value
    except Exception as exc:
        print(f"Pyth oracle fetch failed for ts={ts}: {exc}")
        return None


def active_symbol():
    if PRICE_SOURCE == "coinbase":
        return COINBASE_PRODUCT_ID
    return BINANCE_SYMBOL


def source_label():
    if PRICE_SOURCE == "coinbase":
        return "coinbase_btc_usd"
    return "binance_btc_usdt"


def fetch_recent_candles(n=200):
    if PRICE_SOURCE == "coinbase":
        return fetch_coinbase_candles(n)
    return fetch_binance_candles(n)


def fetch_binance_candles(n=200):
    params = {"symbol": BINANCE_SYMBOL, "interval": "1m", "limit": n}
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


def fetch_coinbase_candles(n=200):
    product_id = COINBASE_PRODUCT_ID
    rows = []
    end = int(time.time())
    remaining = max(1, int(n))

    while remaining > 0:
        # Coinbase rejects ranges that can produce more than 300 buckets.
        # Keep margin for inclusive start/end boundaries.
        chunk = min(remaining, COINBASE_MAX_CANDLES_PER_REQUEST)
        start = end - chunk * 60
        params = {
            "granularity": 60,
            "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)),
            "end": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(end)),
        }
        resp = requests.get(
            COINBASE_URL.format(product_id=product_id),
            params=params,
            headers={"User-Agent": "polymarket-btc-bot/1.0"},
            timeout=15,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        rows.extend(batch)
        oldest = min(int(row[0]) for row in batch)
        end = oldest - 60
        remaining -= len(batch)
        if len(batch) < chunk:
            break

    by_time = {int(row[0]): row for row in rows}
    candles = []
    for ts in sorted(by_time)[-n:]:
        # Coinbase format: [time, low, high, open, close, volume]
        row = by_time[ts]
        close = float(row[4])
        volume_btc = float(row[5])
        candles.append(
            {
                "timestamp": ts * 1000,
                "open": float(row[3]),
                "high": float(row[2]),
                "low": float(row[1]),
                "close": close,
                "volume_btc": volume_btc,
                "volume_usdt": close * volume_btc,
                "num_trades": 0,
                "taker_buy_base": 0.0,
                "taker_buy_quote": 0.0,
            }
        )

    return pd.DataFrame(candles)
