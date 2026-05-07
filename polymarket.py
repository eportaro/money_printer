import json
import os
import re
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"


class PolymarketError(RuntimeError):
    pass


def _get(url, params=None, timeout=15):
    resp = requests.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _loads(value, default=None):
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def _as_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def _parse_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _extract_baseline(text):
    if not text:
        return None
    matches = re.findall(r"\$?\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]{4,}(?:\.[0-9]+)?)", text)
    if not matches:
        return None
    numbers = []
    for match in matches:
        value = _as_float(match.replace(",", ""))
        if value and value > 1000:
            numbers.append(value)
    return numbers[0] if numbers else None


def _looks_like_five_min_market(text):
    if re.search(r"\b5\s*(-|\s)?(m|min|mins|minute|minutes)\b", text):
        return True
    if re.search(r"\b0?[0-9]{1,2}:[0-5][0-9]\s*-\s*0?[0-9]{1,2}:[0-5][0-9]\b", text):
        return True
    return False


def _token_map(market):
    outcomes = _loads(market.get("outcomes"), [])
    token_ids = _loads(market.get("clobTokenIds"), [])
    if not token_ids:
        token_ids = _loads(market.get("tokenIds"), [])

    mapping = {}
    for idx, outcome in enumerate(outcomes or []):
        if idx >= len(token_ids):
            continue
        key = str(outcome).strip().upper()
        if key in {"YES", "UP", "ARRIBA", "ALCISTA"}:
            mapping["UP"] = str(token_ids[idx])
        elif key in {"NO", "DOWN", "ABAJO", "BAJISTA"}:
            mapping["DOWN"] = str(token_ids[idx])

    if "UP" not in mapping and len(token_ids) >= 1:
        mapping["UP"] = str(token_ids[0])
    if "DOWN" not in mapping and len(token_ids) >= 2:
        mapping["DOWN"] = str(token_ids[1])
    return mapping, outcomes


def normalize_market(market, event=None):
    text = " ".join(
        str(x or "")
        for x in [
            market.get("question"),
            market.get("title"),
            market.get("description"),
            event.get("title") if event else None,
            event.get("description") if event else None,
        ]
    )
    tokens, outcomes = _token_map(market)
    return {
        "event_id": str(event.get("id")) if event and event.get("id") else None,
        "market_id": str(market.get("id")) if market.get("id") else None,
        "condition_id": market.get("conditionId") or market.get("condition_id"),
        "question": market.get("question") or market.get("title"),
        "slug": market.get("slug"),
        "event_slug": event.get("slug") if event else None,
        "start_time": _parse_datetime(market.get("startDate") or market.get("startTime")),
        "end_time": _parse_datetime(market.get("endDate") or market.get("endTime")),
        "baseline": _extract_baseline(text),
        "resolution_source": market.get("resolutionSource"),
        "token_up": tokens.get("UP"),
        "token_down": tokens.get("DOWN"),
        "outcomes": outcomes,
        "raw": {"market": market, "event": event},
        "active": bool(market.get("active", True)),
    }


def get_event_by_slug(slug):
    if not slug:
        return None
    try:
        return _get(f"{GAMMA_URL}/events/slug/{slug}")
    except Exception:
        rows = _get(f"{GAMMA_URL}/events", {"slug": slug})
        return rows[0] if rows else None


def get_market_by_slug(slug):
    if not slug:
        return None
    try:
        return _get(f"{GAMMA_URL}/markets/slug/{slug}")
    except Exception:
        rows = _get(f"{GAMMA_URL}/markets", {"slug": slug})
        return rows[0] if rows else None


def discover_btc_5m_market():
    market_slug = os.getenv("POLYMARKET_MARKET_SLUG")
    if market_slug:
        market = get_market_by_slug(market_slug)
        if market:
            return normalize_market(market)

    event_slug = os.getenv("POLYMARKET_EVENT_SLUG")
    if event_slug:
        event = get_event_by_slug(event_slug)
        if event:
            markets = event.get("markets") or []
            if markets:
                return normalize_market(markets[0], event)

    # Polymarket BTC 5m slugs commonly follow btc-updown-5m-{window_start_unix}.
    current_start = (int(time.time()) // 300) * 300
    for offset in [0, 300, -300, 600, -600]:
        slug = f"btc-updown-5m-{current_start + offset}"
        try:
            event = get_event_by_slug(slug)
        except Exception:
            continue
        markets = event.get("markets") or []
        if markets:
            return normalize_market(markets[0], event)

    query = os.getenv("POLYMARKET_SEARCH_QUERY", "BTC arriba abajo 5 m")
    events = _get(
        f"{GAMMA_URL}/events",
        {
            "active": "true",
            "closed": "false",
            "order": "volume_24hr",
            "ascending": "false",
            "limit": 100,
        },
    )
    candidates = []
    query_terms = [t.lower() for t in re.split(r"\s+", query) if t.strip()]
    for event in events:
        markets = event.get("markets") or []
        haystack = " ".join(
            str(x or "")
            for x in [event.get("title"), event.get("slug"), event.get("description")]
        ).lower()
        for market in markets:
            market_text = " ".join(
                str(x or "")
                for x in [haystack, market.get("question"), market.get("slug"), market.get("description")]
            ).lower()
            btcish = "btc" in market_text or "bitcoin" in market_text
            five_min = _looks_like_five_min_market(market_text)
            directional = any(x in market_text for x in ["up", "down", "above", "below", "arriba", "abajo"])
            query_match = sum(1 for term in query_terms if term in market_text)
            if btcish and five_min and directional:
                candidates.append((query_match, event, market))

    if not candidates:
        raise PolymarketError("No active BTC 5m Polymarket market found. Set POLYMARKET_EVENT_SLUG or POLYMARKET_MARKET_SLUG.")

    candidates.sort(key=lambda item: item[0], reverse=True)
    _, event, market = candidates[0]
    normalized = normalize_market(market, event)
    if not normalized["token_up"] or not normalized["token_down"]:
        raise PolymarketError("Discovered market has no CLOB token ids. Set a more specific slug.")
    return normalized


def get_order_book(token_id):
    return _get(f"{CLOB_URL}/book", {"token_id": token_id})


def summarize_book(book, outcome, round_cutoff=None, market_condition_id=None, token_id=None):
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    best_bid = max(bids, key=lambda row: _as_float(row.get("price")) or -1) if bids else {}
    best_ask = min(asks, key=lambda row: _as_float(row.get("price")) or 2) if asks else {}
    bid_price = _as_float(best_bid.get("price"))
    ask_price = _as_float(best_ask.get("price"))
    midpoint = (bid_price + ask_price) / 2 if bid_price is not None and ask_price is not None else None
    spread = ask_price - bid_price if bid_price is not None and ask_price is not None else None
    return {
        "observed_at": datetime.now(timezone.utc),
        "round_cutoff": round_cutoff,
        "market_condition_id": market_condition_id or book.get("market"),
        "token_id": str(book.get("asset_id") or token_id),
        "outcome": outcome,
        "best_bid": bid_price,
        "best_ask": ask_price,
        "midpoint": midpoint,
        "spread": spread,
        "last_trade_price": _as_float(book.get("last_trade_price")),
        "bid_size": _as_float(best_bid.get("size")),
        "ask_size": _as_float(best_ask.get("size")),
        "book_hash": book.get("hash"),
        "raw": {
            "market": book.get("market"),
            "asset_id": book.get("asset_id"),
            "timestamp": book.get("timestamp"),
            "hash": book.get("hash"),
            "last_trade_price": book.get("last_trade_price"),
            "tick_size": book.get("tick_size"),
            "min_order_size": book.get("min_order_size"),
            "bids_top": sorted(bids, key=lambda row: _as_float(row.get("price")) or -1, reverse=True)[:10],
            "asks_top": sorted(asks, key=lambda row: _as_float(row.get("price")) or 2)[:10],
        },
    }


def fetch_market_quotes(market, round_cutoff=None):
    quotes = []
    for outcome, token_key in [("UP", "token_up"), ("DOWN", "token_down")]:
        token_id = market.get(token_key)
        if not token_id:
            continue
        book = get_order_book(token_id)
        quotes.append(
            summarize_book(
                book,
                outcome,
                round_cutoff=round_cutoff,
                market_condition_id=market.get("condition_id"),
                token_id=token_id,
            )
        )
    return quotes
