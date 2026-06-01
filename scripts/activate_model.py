import argparse
import json
import os
import pickle
import shutil
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = PROJECT_ROOT / "model_artifacts"

load_dotenv(PROJECT_ROOT / ".env")


def load_artifact(path):
    with open(path, "rb") as f:
        artifact = pickle.load(f)
    if not isinstance(artifact, dict) or not artifact.get("model_version"):
        raise SystemExit(f"Invalid model artifact: {path}")
    return artifact


def main():
    parser = argparse.ArgumentParser(description="Promote a versioned model artifact to the live model_supabase.pkl alias.")
    parser.add_argument("model_version", help="Model version, for example extra_trees-v2-20260528232224")
    parser.add_argument("--stage-db", action="store_true", help="Mark this model_version as production in model_runs.")
    args = parser.parse_args()

    artifact_path = MODEL_DIR / f"{args.model_version}.pkl"
    metrics_path = MODEL_DIR / f"{args.model_version}_metrics.json"
    if not artifact_path.exists():
        raise SystemExit(f"Model artifact not found: {artifact_path}")

    artifact = load_artifact(artifact_path)
    if artifact.get("model_version") != args.model_version:
        raise SystemExit(
            f"Artifact model_version mismatch: expected {args.model_version}, got {artifact.get('model_version')}"
        )

    live_path = MODEL_DIR / "model_supabase.pkl"
    live_metrics_path = MODEL_DIR / "metrics_supabase.json"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    if live_path.exists():
        backup = MODEL_DIR / f"model_supabase.backup_{stamp}.pkl"
        shutil.copy2(live_path, backup)
    else:
        backup = None

    shutil.copy2(artifact_path, live_path)
    if metrics_path.exists():
        shutil.copy2(metrics_path, live_metrics_path)

    if args.stage_db:
        import sys

        sys.path.insert(0, str(PROJECT_ROOT))
        import db

        if db.db_enabled() and hasattr(db, "promote_model_version"):
            db.promote_model_version(args.model_version)

    print(json.dumps({
        "activated_model_version": args.model_version,
        "live_artifact": str(live_path),
        "live_metrics": str(live_metrics_path) if live_metrics_path.exists() else None,
        "backup": str(backup) if backup else None,
        "stage_db": bool(args.stage_db),
    }, indent=2))


if __name__ == "__main__":
    main()
