"""Lead-lag study: does Polymarket react to Coinbase spot with a delay?

Reads the tick_collector.py gzip JSONL files and answers, per day:

1. PREDICTIVE LAG: for every Polymarket UP-ask change, take the signed Coinbase
   mid return over the preceding tau seconds. If sign(spot move) predicts the
   direction of the next quote move well above 50%, Polymarket is following spot
   with >= tau seconds of delay -> that delay is tradable latency edge.
2. STALENESS: how often spot moves "materially" (default 0.03%, enough to shift
   the win probability late in a round) while the quote stays untouched for
   >= stale-seconds. Many long stale windows = the book is slow; none = the
   market makers are faster than this capture.

Usage:
    python scripts/analyze_leadlag.py --day 20260611 [--taus 1,2,5,10]
    python scripts/analyze_leadlag.py --day 20260611 --move-pct 0.03 --stale-seconds 3
"""

import argparse
import glob
import gzip
import json
import os
import sys
from bisect import bisect_left, bisect_right

import numpy as np

DATA_DIR = os.getenv("TICK_DATA_DIR", os.path.join("data", "ticks"))


def read_jsonl(pattern):
    rows = []
    for path in sorted(glob.glob(pattern)):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except EOFError:
            # A hard-killed collector leaves the last gzip member unterminated;
            # everything read up to that point is still valid.
            pass
    return rows


def coinbase_series(day):
    rows = read_jsonl(os.path.join(DATA_DIR, day, "cb-*.jsonl.gz"))
    ts, mid = [], []
    for r in rows:
        try:
            b, a = float(r["b"]), float(r["a"])
        except (KeyError, TypeError, ValueError):
            continue
        ts.append(r["t"])
        mid.append((b + a) / 2.0)
    return np.asarray(ts, dtype=np.int64), np.asarray(mid)


def pm_up_ask_changes(day):
    """(t, new_best_ask) for the UP token, from book snapshots."""
    rows = read_jsonl(os.path.join(DATA_DIR, day, "pm-*.jsonl.gz"))
    changes = []
    last_ask = None
    for r in rows:
        if r.get("e") == "round":
            last_ask = None  # new round, new book
            continue
        if r.get("side") != "UP" or r.get("e") != "book":
            continue
        asks = r.get("asks") or []
        if not asks:
            continue
        try:
            best_ask = min(float(level["price"]) for level in asks)
        except (KeyError, TypeError, ValueError):
            continue
        if last_ask is None or abs(best_ask - last_ask) > 1e-9:
            changes.append((r["t"], best_ask, last_ask))
            last_ask = best_ask
    return changes


def cb_return(ts, mid, t_end_ms, tau_seconds):
    """Signed CB mid return over [t_end - tau, t_end]."""
    j = bisect_right(ts, t_end_ms) - 1
    i = bisect_right(ts, t_end_ms - tau_seconds * 1000) - 1
    if i < 0 or j < 0 or i == j:
        return None
    if mid[i] == 0:
        return None
    return (mid[j] - mid[i]) / mid[i]


def predictive_lag(ts, mid, changes, taus):
    print("\nPREDICTIVE LAG: does the prior CB move predict the PM quote-move direction?")
    print(f"{'tau(s)':>7} {'n':>6} {'hit%':>6}   (>~55% on real n = PM lags spot by >= tau)")
    for tau in taus:
        hits, n = 0, 0
        for t, new_ask, old_ask in changes:
            if old_ask is None:
                continue
            r = cb_return(ts, mid, t, tau)
            if r is None or r == 0:
                continue
            quote_dir = np.sign(new_ask - old_ask)  # UP ask rises when spot rises
            if quote_dir == 0:
                continue
            n += 1
            if np.sign(r) == quote_dir:
                hits += 1
        rate = 100.0 * hits / n if n else float("nan")
        print(f"{tau:>7} {n:>6} {rate:>5.1f}%")


def staleness(ts, mid, changes, move_pct, stale_seconds):
    """Material spot moves with no PM quote update within stale_seconds after."""
    if not len(ts):
        return
    change_ts = [t for t, _, _ in changes]
    window_ms = 1000
    stale_ms = stale_seconds * 1000
    events = 0
    stale = 0
    last_event_t = 0
    for j in range(1, len(ts)):
        if ts[j] - last_event_t < window_ms:
            continue
        r = cb_return(ts, mid, ts[j], 5)
        if r is None or abs(r) * 100 < move_pct:
            continue
        events += 1
        last_event_t = ts[j]
        k = bisect_left(change_ts, ts[j])
        next_change = change_ts[k] if k < len(change_ts) else None
        if next_change is None or next_change - ts[j] > stale_ms:
            stale += 1
    rate = 100.0 * stale / events if events else float("nan")
    print(f"\nSTALENESS: spot moved >={move_pct}% (5s) -> PM UP-ask silent for >{stale_seconds}s")
    print(f"  material spot moves: {events}   stale (no quote reaction): {stale} ({rate:.1f}%)")
    print("  (high % = slow makers = exploitable; ~0% = no latency edge at this resolution)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--day", required=True, help="YYYYMMDD folder under data/ticks/")
    parser.add_argument("--taus", default="1,2,5,10")
    parser.add_argument("--move-pct", type=float, default=0.03)
    parser.add_argument("--stale-seconds", type=float, default=3.0)
    args = parser.parse_args()

    ts, mid = coinbase_series(args.day)
    changes = pm_up_ask_changes(args.day)
    print(f"day={args.day}  cb_ticks={len(ts)}  pm_up_ask_changes={len(changes)}")
    if not len(ts) or not changes:
        sys.exit("not enough data yet — let tick_collector run longer")

    taus = [float(x) for x in args.taus.split(",") if x.strip()]
    predictive_lag(ts, mid, changes, taus)
    staleness(ts, mid, changes, args.move_pct, args.stale_seconds)


if __name__ == "__main__":
    main()
