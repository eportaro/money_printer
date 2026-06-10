"""Tick-level capture: Coinbase BTC-USD trades + Polymarket CLOB quote updates.

Purpose: measure the spot -> Polymarket lead-lag, the one edge hypothesis the
5-second polling collector cannot observe. If Polymarket quotes for the BTC 15m
up/down market react to spot moves with a delay of seconds, that delay is the
edge; if they don't, the latency door closes too.

Writes compact gzip JSONL per source per hour under data/ticks/YYYYMMDD/:
    cb-HH.jsonl.gz   one line per Coinbase ticker change (dedup on price/bid/ask)
    pm-HH.jsonl.gz   one line per Polymarket book/price_change/trade event

Every line carries t (epoch ms at receive) so the two streams can be joined.
Polymarket tokens rotate every round; the PM task re-discovers the market and
reconnects at each 15m boundary. Run via: docker compose --profile ticks up -d

Analysis: scripts/analyze_leadlag.py
"""

import asyncio
import gzip
import json
import os
import shutil
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from market_config import WINDOW_SECONDS, next_cutoff, round_start
from polymarket import discover_btc_market

load_dotenv()

import websockets

COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
POLYMARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PRODUCT_ID = os.getenv("COINBASE_PRODUCT_ID", "BTC-USD")
DATA_DIR = os.getenv("TICK_DATA_DIR", os.path.join("data", "ticks"))
MIN_FREE_BYTES = int(os.getenv("TICK_MIN_FREE_BYTES", str(2 * 1024**3)))
ORDERBOOK_TOP_N = int(os.getenv("TICK_ORDERBOOK_TOP_N", "3"))
RECONNECT_DELAY_SECONDS = 3


def now_ms():
    return int(time.time() * 1000)


class TickWriter:
    """Hour-rotated gzip JSONL writer with a free-disk guard."""

    def __init__(self, source):
        self.source = source
        self.fh = None
        self.hour_key = None
        self.disk_warned_at = 0

    def _path(self, hour_key):
        day, hour = hour_key
        folder = os.path.join(DATA_DIR, day)
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, f"{self.source}-{hour}.jsonl.gz")

    def write(self, record):
        if shutil.disk_usage(".").free < MIN_FREE_BYTES:
            if time.time() - self.disk_warned_at > 60:
                print(f"[{self.source}] disk almost full; dropping ticks")
                self.disk_warned_at = time.time()
            return
        now = datetime.now(timezone.utc)
        hour_key = (now.strftime("%Y%m%d"), now.strftime("%H"))
        if hour_key != self.hour_key:
            self.close()
            self.fh = gzip.open(self._path(hour_key), "at", encoding="utf-8")
            self.hour_key = hour_key
        self.fh.write(json.dumps(record, separators=(",", ":")) + "\n")

    def flush(self):
        if self.fh:
            self.fh.flush()

    def close(self):
        if self.fh:
            try:
                self.fh.close()
            except Exception:
                pass
            self.fh = None


async def coinbase_stream():
    writer = TickWriter("cb")
    try:
        await _coinbase_loop(writer)
    finally:
        writer.close()


async def _coinbase_loop(writer):
    last = {}
    while True:
        try:
            async with websockets.connect(COINBASE_WS, ping_interval=20, max_size=2**22) as ws:
                await ws.send(json.dumps({
                    "type": "subscribe",
                    "product_ids": [PRODUCT_ID],
                    "channels": ["ticker"],
                }))
                print(f"[cb] subscribed to {PRODUCT_ID} ticker")
                last_flush = time.time()
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") != "ticker":
                        continue
                    key = (msg.get("price"), msg.get("best_bid"), msg.get("best_ask"))
                    if key == last.get("key"):
                        continue
                    last["key"] = key
                    writer.write({
                        "t": now_ms(),
                        "xt": msg.get("time"),
                        "p": msg.get("price"),
                        "b": msg.get("best_bid"),
                        "a": msg.get("best_ask"),
                        "s": msg.get("last_size"),
                        "sd": msg.get("side"),
                    })
                    if time.time() - last_flush > 5:
                        writer.flush()
                        last_flush = time.time()
        except Exception as exc:
            print(f"[cb] stream error: {exc}; reconnecting in {RECONNECT_DELAY_SECONDS}s")
            writer.flush()
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)


def compact_pm_event(msg, side_by_token):
    event_type = msg.get("event_type")
    token = str(msg.get("asset_id") or "")
    base = {
        "t": now_ms(),
        "e": event_type,
        "side": side_by_token.get(token),
        "xt": msg.get("timestamp"),
    }
    if event_type == "book":
        base["bids"] = (msg.get("bids") or msg.get("buys") or [])[-ORDERBOOK_TOP_N:]
        base["asks"] = (msg.get("asks") or msg.get("sells") or [])[-ORDERBOOK_TOP_N:]
    elif event_type == "price_change":
        base["ch"] = msg.get("changes") or [{
            "price": msg.get("price"), "size": msg.get("size"), "side": msg.get("side"),
        }]
    elif event_type == "last_trade_price":
        base["p"] = msg.get("price")
        base["s"] = msg.get("size")
        base["sd"] = msg.get("side")
    else:
        return None
    return base


async def polymarket_round_stream(writer):
    """Stream one round's market; returns at the round boundary to resubscribe."""
    window_start = round_start()
    cutoff = next_cutoff()
    market = await asyncio.to_thread(discover_btc_market, window_start)
    token_up = market.get("token_up")
    token_down = market.get("token_down")
    if not token_up or not token_down:
        print(f"[pm] no tokens for {market.get('event_slug')}; retrying soon")
        await asyncio.sleep(5)
        return
    side_by_token = {str(token_up): "UP", str(token_down): "DOWN"}
    writer.write({
        "t": now_ms(),
        "e": "round",
        "event_slug": market.get("event_slug"),
        "window_start": window_start,
        "cutoff": cutoff,
        "token_up": str(token_up),
        "token_down": str(token_down),
    })
    print(f"[pm] subscribing {market.get('event_slug')} (cutoff {cutoff})")
    async with websockets.connect(POLYMARKET_WS, ping_interval=10, max_size=2**22) as ws:
        await ws.send(json.dumps({"assets_ids": [str(token_up), str(token_down)], "type": "market"}))
        last_flush = time.time()
        # Hold a little past the cutoff to capture settlement-time quotes.
        deadline = cutoff + 10
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(1, deadline - time.time()))
            except asyncio.TimeoutError:
                break
            if raw == "PONG":
                continue
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            events = payload if isinstance(payload, list) else [payload]
            for msg in events:
                if not isinstance(msg, dict):
                    continue
                record = compact_pm_event(msg, side_by_token)
                if record:
                    writer.write(record)
            if time.time() - last_flush > 5:
                writer.flush()
                last_flush = time.time()
        writer.flush()


async def polymarket_stream():
    writer = TickWriter("pm")
    try:
        while True:
            try:
                await polymarket_round_stream(writer)
            except Exception as exc:
                print(f"[pm] stream error: {exc}; reconnecting in {RECONNECT_DELAY_SECONDS}s")
                writer.flush()
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)
    finally:
        writer.close()


async def main():
    print(f"Tick collector writing to {DATA_DIR} (window {WINDOW_SECONDS}s)")
    # Close gzip members cleanly on docker stop (SIGTERM) so files stay readable.
    try:
        import signal

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: [t.cancel() for t in asyncio.all_tasks(loop)])
    except (ImportError, NotImplementedError):
        pass  # Windows: no add_signal_handler; Ctrl+C raises KeyboardInterrupt instead
    await asyncio.gather(coinbase_stream(), polymarket_stream())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("tick collector stopped")
