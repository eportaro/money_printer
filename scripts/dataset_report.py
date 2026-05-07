import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

import db

TABLES = [
    "round_snapshots",
    "polymarket_quotes",
    "model_predictions",
    "simulated_bets",
    "round_results",
    "polymarket_markets",
]


def main():
    report = {
        "db_enabled": db.db_enabled(),
        "counts": {table: db.count_rows(table) for table in TABLES},
        "recent_round_results": db.fetch_recent_round_results(5),
        "recent_simulated_bets": db.fetch_recent_simulated_bets(10),
    }
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
