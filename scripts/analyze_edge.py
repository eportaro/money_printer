"""
The profit map: empirical EV of BUYING a binary leg at its ask price.

For every captured snapshot there are two buyable legs: UP at up_ask, DOWN at down_ask.
At settlement the leg pays $1 if its side won. So a buy at price `a` has realized
payoff `1{won}` and EV = P(win | a) - a. We bin by the ask you actually pay and measure
the realized win rate vs the price. Bins where win_rate > ask are systematically
+EV (the market mis-sells that probability). This is model-free.

Tests the favorite-longshot bias and locates any tradable edge by price and by bucket.
"""
import argparse
import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import db_sqlserver as dbs


def fetch():
    return dbs._fetch_all(
        """
        select ds.seconds_bucket as bucket, rr.outcome as outcome,
               max(case when mq.outcome = N'UP' then mq.best_ask end) as up_ask,
               max(case when mq.outcome = N'DOWN' then mq.best_ask end) as down_ask,
               max(case when mq.outcome = N'UP' then mq.ask_size end) as up_sz,
               max(case when mq.outcome = N'DOWN' then mq.ask_size end) as down_sz
        from dbo.decision_snapshots ds
        join dbo.rounds r on r.id = ds.round_id
        join dbo.round_results rr on rr.round_cutoff = r.round_cutoff and rr.outcome in (N'UP', N'DOWN')
        join dbo.market_quotes mq on mq.snapshot_id = ds.id
        group by ds.id, ds.seconds_bucket, rr.outcome
        """
    )


def legs(rows, min_bucket=None, min_size=0.0):
    out = []
    for r in rows:
        if min_bucket is not None and (r["bucket"] is None or int(r["bucket"]) < min_bucket):
            continue
        for side, ask, sz in (("UP", r["up_ask"], r["up_sz"]), ("DOWN", r["down_ask"], r["down_sz"])):
            if ask is None:
                continue
            if min_size and (sz is None or float(sz) < min_size):
                continue
            out.append((float(ask), 1.0 if r["outcome"] == side else 0.0))
    return out


def table(legs_list, label):
    edges = [0, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 1.01]
    print(f"\n=== {label}  (legs={len(legs_list)}) ===")
    print(f"{'price bin':>12} {'n':>6} {'avg_ask':>8} {'win_rate':>9} {'EV/contract':>12} {'ROI':>8}")
    arr = np.array(legs_list)
    if len(arr) == 0:
        print("  (no data)")
        return
    asks, wins = arr[:, 0], arr[:, 1]
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (asks >= lo) & (asks < hi)
        if m.sum() == 0:
            continue
        a = asks[m].mean()
        wr = wins[m].mean()
        ev = wr - a
        roi = ev / a if a > 0 else 0
        flag = "  <== +EV" if ev > 0.01 else ""
        print(f"{lo:>5.2f}-{hi:<5.2f} {int(m.sum()):>6} {a:>8.3f} {wr:>9.3f} {ev:>+12.3f} {100*roi:>7.1f}%{flag}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-size", type=float, default=0.0, help="require ask_size >= this (liquidity)")
    args = parser.parse_args()
    rows = fetch()
    table(legs(rows), "ALL buckets")
    table(legs(rows, min_bucket=240), "EARLY only (T-240 and earlier, real uncertainty)")
    table(legs(rows, min_bucket=600), "VERY EARLY (T-600 and earlier)")
    if args.min_size > 0:
        table(legs(rows, min_size=args.min_size), f"ALL buckets, ask_size >= {args.min_size}")


if __name__ == "__main__":
    main()
