import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

import db

CUTOFF_ISO = "2026-05-07T07:35:00+00:00"
CUTOFF_TS = 1778139300


def cleanup_supabase():
    tables = [
        ("simulated_bets", "round_cutoff", CUTOFF_TS),
        ("model_predictions", "round_cutoff", CUTOFF_TS),
        ("polymarket_quotes", "round_cutoff", CUTOFF_TS),
        ("round_snapshots", "round_cutoff", CUTOFF_TS),
        ("round_results", "round_cutoff", CUTOFF_TS),
        ("polymarket_markets", "created_at", CUTOFF_ISO),
    ]
    before = {table: db.count_rows(table) for table, _, _ in tables}
    for table, column, cutoff in tables:
        db.delete_before(table, column, cutoff)
    after = {table: db.count_rows(table) for table, _, _ in tables}
    return {"cutoff_iso": CUTOFF_ISO, "cutoff_ts": CUTOFF_TS, "before": before, "after": after}


def cleanup_local_json():
    path = Path("predictions_db.json")
    if not path.exists():
        return {"file": str(path), "before": 0, "after": 0}
    data = json.loads(path.read_text(encoding="utf-8"))
    cleaned = [row for row in data if int(row.get("next_cutoff", 0)) >= CUTOFF_TS]
    path.write_text(json.dumps(cleaned, indent=2), encoding="utf-8")
    return {"file": str(path), "before": len(data), "after": len(cleaned)}


if __name__ == "__main__":
    print(json.dumps({"supabase": cleanup_supabase(), "local_json": cleanup_local_json()}, indent=2))
