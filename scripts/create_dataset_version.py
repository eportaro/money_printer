import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import db

load_dotenv()


DEFAULT_SELECT = "*"


def fetch_training_rows(limit):
    params = {
        "select": DEFAULT_SELECT,
        "target_up": "not.is.null",
        "order": "observed_at.asc",
        "limit": limit,
    }
    return db._get("training_decision_snapshots", params)


def dataset_hash(filters):
    payload = json.dumps(filters, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def main():
    parser = argparse.ArgumentParser(description="Create a reproducible dataset version from training_decision_snapshots.")
    parser.add_argument("--limit", type=int, default=int(os.getenv("DATASET_EXPORT_LIMIT", "50000")))
    parser.add_argument("--version", default=None)
    parser.add_argument("--out-dir", default="datasets")
    args = parser.parse_args()

    if not db.db_enabled():
        raise SystemExit("Supabase is not configured. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.")

    rows = fetch_training_rows(args.limit)
    frame = pd.DataFrame(rows)
    if frame.empty:
        raise SystemExit("training_decision_snapshots returned no resolved rows.")

    filters = {
        "view": "training_decision_snapshots",
        "target_up": "not null",
        "order": "observed_at.asc",
        "limit": args.limit,
    }
    query_hash = dataset_hash(filters)
    version = args.version or f"ds_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{query_hash}"

    os.makedirs(args.out_dir, exist_ok=True)
    csv_path = os.path.join(args.out_dir, f"{version}.csv")
    frame.to_csv(csv_path, index=False)

    row_count = int(len(frame))
    round_count = int(frame["round_cutoff"].nunique()) if "round_cutoff" in frame else 0
    start_time = frame["observed_at"].min() if "observed_at" in frame else None
    end_time = frame["observed_at"].max() if "observed_at" in frame else None

    db.insert_dataset_version(
        {
            "dataset_version": version,
            "source_query_hash": query_hash,
            "row_count": row_count,
            "round_count": round_count,
            "start_time": start_time,
            "end_time": end_time,
            "filters": filters,
            "parquet_path": csv_path,
            "raw": {"format": "csv", "note": "Column kept as parquet_path for schema compatibility."},
        }
    )

    print(json.dumps({
        "dataset_version": version,
        "row_count": row_count,
        "round_count": round_count,
        "path": csv_path,
    }, indent=2))


if __name__ == "__main__":
    main()
