import json
import os
import re
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from market_config import MARKET_INTERVAL, WINDOW_SECONDS, round_start, slug_candidates

load_dotenv()

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"
USER_AGENT = "Mozilla/5.0"


class PolymarketError(RuntimeError):
    pass


def _get(url, params=None, timeout=15):
    resp = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": USER_AGENT})
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


def _event_metadata(event):
    if not event:
        return {}
    metadata = event.get("eventMetadata") or event.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            return {}
    return metadata if isinstance(metadata, dict) else {}


def _metadata_price(metadata, *keys):
    for key in keys:
        value = _as_float(metadata.get(key))
        if value is not None:
            return value
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


def _looks_like_interval_market(text, interval_minutes=None):
    interval_minutes = interval_minutes or max(1, WINDOW_SECONDS // 60)
    if re.search(rf"\b{interval_minutes}\s*(-|\s)?(m|min|mins|minute|minutes)\b", text):
        return True
    if f"{interval_minutes}m" in text:
        return True
    for start_h, start_m, end_h, end_m in re.findall(r"\b(0?[0-9]{1,2}):([0-5][0-9])\s*-\s*(0?[0-9]{1,2}):([0-5][0-9])\b", text):
        start_total = int(start_h) * 60 + int(start_m)
        end_total = int(end_h) * 60 + int(end_m)
        diff = (end_total - start_total) % (24 * 60)
        if diff == interval_minutes:
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
    metadata = _event_metadata(event)
    metadata_baseline = _metadata_price(metadata, "priceToBeat", "price_to_beat", "baseline")
    metadata_final_price = _metadata_price(metadata, "finalPrice", "final_price", "actualClose", "actual_close")
    baseline = metadata_baseline if metadata_baseline is not None else _extract_baseline(text)
    return {
        "event_id": str(event.get("id")) if event and event.get("id") else None,
        "market_id": str(market.get("id")) if market.get("id") else None,
        "condition_id": market.get("conditionId") or market.get("condition_id"),
        "question": market.get("question") or market.get("title"),
        "slug": market.get("slug"),
        "event_slug": event.get("slug") if event else None,
        "start_time": _parse_datetime(market.get("startDate") or market.get("startTime")),
        "end_time": _parse_datetime(market.get("endDate") or market.get("endTime")),
        "baseline": baseline,
        "baseline_source": "polymarket_gamma_event_metadata" if metadata_baseline is not None else "polymarket_text_extract" if baseline is not None else None,
        "final_price": metadata_final_price,
        "event_metadata": metadata,
        "resolution_source": market.get("resolutionSource") or (event.get("resolutionSource") if event else None),
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


def _extract_event_metadata_from_page(slug, page_slug=None):
    page_slug = page_slug or slug
    url = f"https://polymarket.com/event/{page_slug}"
    resp = requests.get(url, timeout=20, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    text = resp.text
    markers = [f'"ticker":"{slug}"', f'"slug":"{slug}"', f'"/api/event/slug","{slug}"']
    positions = []
    for marker in markers:
        start = 0
        while True:
            pos = text.find(marker, start)
            if pos < 0:
                break
            positions.append(pos)
            start = pos + len(marker)

    for pos in sorted(set(positions)):
        chunk_start = max(0, pos - 30000)
        chunk_end = min(len(text), pos + 50000)
        chunk = text[chunk_start:chunk_end]
        matches = list(re.finditer(r'"eventMetadata":(\{[^}]*\})', chunk))
        if not matches:
            continue
        matches.sort(key=lambda match: abs((chunk_start + match.start()) - pos))
        for match in matches:
            try:
                metadata = json.loads(match.group(1))
            except Exception:
                continue
            if metadata:
                return metadata

    return {}


def fetch_event_prices(slug, page_slug=None, include_page=True):
    event = get_event_by_slug(slug)
    metadata = _event_metadata(event)
    if not include_page:
        return {
            "event_slug": slug,
            "price_to_beat": _metadata_price(metadata, "priceToBeat", "price_to_beat", "baseline"),
            "final_price": _metadata_price(metadata, "finalPrice", "final_price", "actualClose", "actual_close"),
            "raw": metadata,
        }

    page_slugs = []
    if page_slug:
        page_slugs.append(page_slug)
    page_slugs.append(slug)
    match = re.match(r"(.+)-(\d+)$", slug)
    if match:
        start = int(match.group(2))
        prefix = match.group(1)
        page_slugs.extend([f"{prefix}-{start + WINDOW_SECONDS}", f"{prefix}-{start + 2 * WINDOW_SECONDS}"])

    for candidate_page_slug in page_slugs:
        if metadata and _metadata_price(metadata, "priceToBeat", "price_to_beat", "baseline") is not None and _metadata_price(metadata, "finalPrice", "final_price", "actualClose", "actual_close") is not None:
            break
        page_metadata = _extract_event_metadata_from_page(slug, page_slug=candidate_page_slug)
        if page_metadata:
            metadata = {**page_metadata, **metadata}
    return {
        "event_slug": slug,
        "price_to_beat": _metadata_price(metadata, "priceToBeat", "price_to_beat", "baseline"),
        "final_price": _metadata_price(metadata, "finalPrice", "final_price", "actualClose", "actual_close"),
        "raw": metadata,
    }


def infer_baseline_from_previous_event(event_slug):
    if not event_slug:
        return None
    match = re.match(r"(.+)-(\d+)$", event_slug)
    if not match:
        return None
    previous_slug = f"{match.group(1)}-{int(match.group(2)) - WINDOW_SECONDS}"
    prices = fetch_event_prices(previous_slug, page_slug=event_slug)
    final_price = prices.get("final_price")
    if final_price is None:
        return None
    return {
        "baseline": final_price,
        "baseline_source": "polymarket_previous_final_price",
        "previous_event_slug": previous_slug,
        "raw": prices.get("raw"),
    }


def enrich_market_baseline(normalized):
    if not normalized or not normalized.get("event_slug"):
        return normalized

    try:
        prices = fetch_event_prices(normalized.get("event_slug"), include_page=False)
    except Exception:
        prices = {}

    price_to_beat = prices.get("price_to_beat")
    if price_to_beat is not None:
        normalized.update({
            "baseline": price_to_beat,
            "baseline_source": "polymarket_gamma_event_metadata",
            "event_metadata": prices.get("raw") or normalized.get("event_metadata"),
            "raw": {**(normalized.get("raw") or {}), "event_prices": prices.get("raw")},
        })
        return normalized

    if normalized.get("baseline") is not None:
        return normalized

    if os.getenv("USE_POLYMARKET_PREVIOUS_FINAL_BASELINE", "false").lower() == "true":
        previous = infer_baseline_from_previous_event(normalized.get("event_slug"))
        if previous:
            normalized.update(previous)
    return normalized


def discover_btc_market(window_start=None):
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
                normalized = normalize_market(markets[0], event)
                return enrich_market_baseline(normalized)

    current_start = int(window_start) if window_start is not None else round_start()
    for offset in [0, WINDOW_SECONDS, -WINDOW_SECONDS, 2 * WINDOW_SECONDS, -2 * WINDOW_SECONDS]:
        for slug in slug_candidates(current_start + offset):
            try:
                event = get_event_by_slug(slug)
            except Exception:
                continue
            markets = event.get("markets") or []
            if markets:
                normalized = normalize_market(markets[0], event)
                return enrich_market_baseline(normalized)

    query = os.getenv("POLYMARKET_SEARCH_QUERY", f"BTC arriba abajo {MARKET_INTERVAL}")
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
            interval_match = _looks_like_interval_market(market_text)
            directional = any(x in market_text for x in ["up", "down", "above", "below", "arriba", "abajo"])
            query_match = sum(1 for term in query_terms if term in market_text)
            if btcish and interval_match and directional:
                candidates.append((query_match, event, market))

    if not candidates:
        raise PolymarketError(f"No active BTC {MARKET_INTERVAL} Polymarket market found. Set POLYMARKET_EVENT_SLUG or POLYMARKET_MARKET_SLUG.")

    candidates.sort(key=lambda item: item[0], reverse=True)
    _, event, market = candidates[0]
    normalized = normalize_market(market, event)
    normalized = enrich_market_baseline(normalized)
    if not normalized["token_up"] or not normalized["token_down"]:
        raise PolymarketError("Discovered market has no CLOB token ids. Set a more specific slug.")
    return normalized


def discover_btc_5m_market():
    return discover_btc_market()


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
