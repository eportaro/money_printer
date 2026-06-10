"""Microstructure features: the only feature family with edge potential left.

The 57 candle-TA features carry zero importance in every trained model (the
market already prices them). These come from sources the TA set cannot see:

- Binance USDT-M perp premium/funding (premiumIndex): leveraged-flow pressure.
- Coinbase L2 top-of-book imbalance and depth: who is leaning on the spot book.

Captured best-effort per decision snapshot (REST, ~3 calls per bucket). On any
failure the feature is simply absent -> NaN -> imputed at training, so a flaky
endpoint can never break the collector. Stored in feature_snapshots alongside
the TA set and whitelisted for training via MICRO_FEATURE_COLUMNS.
"""

import os

import requests

BINANCE_PREMIUM_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
COINBASE_BOOK_URL = "https://api.exchange.coinbase.com/products/{product_id}/book"

MICRO_FEATURE_COLUMNS = [
    "micro_perp_basis_pct",      # (perp mark - spot index) / index * 100
    "micro_funding_rate",        # last funding rate (8h, signed)
    "micro_book_imbalance_10",   # bid_vol/(bid_vol+ask_vol), top 10 levels
    "micro_book_imbalance_50",   # same, top 50 levels
    "micro_spread_bps",          # spot top-of-book spread in bps
    "micro_bid_depth_10",        # BTC within top 10 bid levels
    "micro_ask_depth_10",        # BTC within top 10 ask levels
]

_TIMEOUT = int(os.getenv("MICRO_FETCH_TIMEOUT_SECONDS", "5"))
_HEADERS = {"User-Agent": "polymarket-btc-bot/1.0"}


def _binance_perp_features(symbol):
    resp = requests.get(BINANCE_PREMIUM_URL, params={"symbol": symbol}, timeout=_TIMEOUT, headers=_HEADERS)
    resp.raise_for_status()
    data = resp.json()
    mark = float(data["markPrice"])
    index = float(data["indexPrice"])
    return {
        "micro_perp_basis_pct": (mark - index) / index * 100 if index else None,
        "micro_funding_rate": float(data.get("lastFundingRate") or 0),
    }


def _coinbase_book_features(product_id):
    resp = requests.get(
        COINBASE_BOOK_URL.format(product_id=product_id),
        params={"level": 2},
        timeout=_TIMEOUT,
        headers=_HEADERS,
    )
    resp.raise_for_status()
    data = resp.json()
    bids = data.get("bids") or []
    asks = data.get("asks") or []
    if not bids or not asks:
        return {}

    def depth(levels, n):
        return sum(float(size) for _, size, *_ in levels[:n])

    bid10, ask10 = depth(bids, 10), depth(asks, 10)
    bid50, ask50 = depth(bids, 50), depth(asks, 50)
    best_bid, best_ask = float(bids[0][0]), float(asks[0][0])
    mid = (best_bid + best_ask) / 2
    return {
        "micro_book_imbalance_10": bid10 / (bid10 + ask10) if bid10 + ask10 else None,
        "micro_book_imbalance_50": bid50 / (bid50 + ask50) if bid50 + ask50 else None,
        "micro_spread_bps": (best_ask - best_bid) / mid * 10000 if mid else None,
        "micro_bid_depth_10": bid10,
        "micro_ask_depth_10": ask10,
    }


def fetch_micro_features():
    """Best-effort dict of MICRO_FEATURE_COLUMNS values; missing keys on failure."""
    if os.getenv("MICRO_FEATURES_ENABLED", "true").lower() != "true":
        return {}
    features = {}
    try:
        features.update(_binance_perp_features(os.getenv("BINANCE_PERP_SYMBOL", "BTCUSDT")))
    except Exception as exc:
        print(f"micro: binance perp fetch failed: {exc}")
    try:
        features.update(_coinbase_book_features(os.getenv("COINBASE_PRODUCT_ID", "BTC-USD")))
    except Exception as exc:
        print(f"micro: coinbase book fetch failed: {exc}")
    return {k: v for k, v in features.items() if v is not None}
