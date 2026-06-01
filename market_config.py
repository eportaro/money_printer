import os

from dotenv import load_dotenv

load_dotenv()


def _positive_int(name, default):
    try:
        value = int(os.getenv(name, str(default)))
        return value if value > 0 else default
    except ValueError:
        return default


WINDOW_SECONDS = _positive_int("WINDOW_SECONDS", 900)
WINDOW_MS = WINDOW_SECONDS * 1000
WINDOW_MINUTES = max(1, WINDOW_SECONDS // 60)
MARKET_INTERVAL = os.getenv("POLYMARKET_INTERVAL", f"{WINDOW_MINUTES}m").strip().lower()


def round_start(timestamp=None):
    import time

    now = int(time.time()) if timestamp is None else int(timestamp)
    return (now // WINDOW_SECONDS) * WINDOW_SECONDS


def next_cutoff(timestamp=None):
    return round_start(timestamp) + WINDOW_SECONDS


def round_id(window_start):
    return f"btc-{MARKET_INTERVAL}-{int(window_start)}"


def slug_candidates(window_start):
    window_start = int(window_start)
    prefixes = [
        os.getenv("POLYMARKET_EVENT_PREFIX", "").strip(),
        f"btc-updown-{MARKET_INTERVAL}",
        f"btc-up-or-down-{MARKET_INTERVAL}",
    ]
    seen = set()
    slugs = []
    for prefix in prefixes:
        if not prefix or prefix in seen:
            continue
        seen.add(prefix)
        slugs.append(f"{prefix}-{window_start}")
    return slugs
