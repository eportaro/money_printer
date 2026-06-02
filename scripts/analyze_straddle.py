"""
Backtest the "collect both legs cheap" straddle strategy on historical quote data.

Idea (the user's observation): in a binary UP/DOWN round exactly one side pays $1.
If during the round you can buy UP for p_up and DOWN for p_down at different moments
such that p_up + p_down < 1, you profit risk-free. Even buying only the legs that
get cheap (limit order at threshold T) can be +EV if the price oscillates around the
baseline often enough that BOTH sides become cheap.

Model (no hindsight): place a resting limit BUY at price T on each side. A leg "fills"
if that side's best_ask touches <= T at any captured bucket; you pay T. Hold to
settlement: the winning side you hold pays $1.

NOTE: uses scheduled-bucket snapshots (discrete), so it UNDER-counts real continuous
chances. Ignores fees and assumes fills (liquidity caveat: check ask_size separately).
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import db_sqlserver as dbs


def fetch():
    return dbs._fetch_all(
        """
        select r.round_cutoff as cutoff, rr.outcome as outcome,
               max(case when mq.outcome = N'UP' then mq.best_ask end) as up_ask,
               max(case when mq.outcome = N'DOWN' then mq.best_ask end) as down_ask
        from dbo.decision_snapshots ds
        join dbo.rounds r on r.id = ds.round_id
        join dbo.round_results rr on rr.round_cutoff = r.round_cutoff and rr.outcome in (N'UP', N'DOWN')
        join dbo.market_quotes mq on mq.snapshot_id = ds.id
        group by r.round_cutoff, rr.outcome, ds.id
        """
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--thresholds", default="0.05,0.10,0.15,0.20,0.25")
    parser.add_argument("--stake", type=float, default=1.0, help="shares per leg")
    args = parser.parse_args()

    rows = fetch()
    by_round = defaultdict(lambda: {"outcome": None, "up": [], "down": []})
    for r in rows:
        rec = by_round[int(r["cutoff"])]
        rec["outcome"] = r["outcome"]
        if r["up_ask"] is not None:
            rec["up"].append(float(r["up_ask"]))
        if r["down_ask"] is not None:
            rec["down"].append(float(r["down_ask"]))

    rounds = list(by_round.values())
    print(f"resolved rounds analysed: {len(rounds)}\n")
    print(f"{'T':>6} {'both%':>7} {'oneLeg%':>8} {'net':>10} {'pnl/round':>10} {'roi':>8} {'bothLeg_net':>12}")
    for T in [float(x) for x in args.thresholds.split(",")]:
        both = one = 0
        net = cost_total = 0.0
        both_net = 0.0
        for rec in rounds:
            up_fill = any(a <= T for a in rec["up"])
            dn_fill = any(a <= T for a in rec["down"])
            cost = T * (int(up_fill) + int(dn_fill)) * args.stake
            payout = 0.0
            if up_fill and rec["outcome"] == "UP":
                payout += args.stake
            if dn_fill and rec["outcome"] == "DOWN":
                payout += args.stake
            pnl = payout - cost
            net += pnl
            cost_total += cost
            if up_fill and dn_fill:
                both += 1
                both_net += pnl
            elif up_fill or dn_fill:
                one += 1
        n = len(rounds)
        roi = (net / cost_total) if cost_total else 0.0
        print(f"{T:>6.2f} {100*both/n:>6.1f}% {100*one/n:>7.1f}% {net:>10.2f} {net/n:>10.4f} {100*roi:>7.1f}% {both_net:>12.2f}")
    print("\nbothLeg_net = profit only from rounds where BOTH legs filled (the risk-free pairs).")
    print("If both% is high and net>0, the oscillation edge is real and tradable.")


if __name__ == "__main__":
    main()
