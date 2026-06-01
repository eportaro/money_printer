import argparse
import json
import os
from collections import defaultdict
from pathlib import Path

import pyodbc
from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]

TABLE_ORDER = [
    "rounds",
    "reference_prices",
    "decision_snapshots",
    "market_quotes",
    "feature_snapshots",
    "predictions_v2",
    "signals_v2",
    "round_results",
    "trade_results_v2",
    "dataset_versions",
    "model_runs",
]


def load_connections():
    local_env = dotenv_values(PROJECT_ROOT / ".env")
    docker_env = dotenv_values(PROJECT_ROOT / ".env.docker")
    target = docker_env.get("SQLSERVER_CONNECTION")
    return {
        "source": local_env.get("SQLSERVER_CONNECTION"),
        "target": target.replace("SERVER=sqlserver,1433", "SERVER=localhost,14333") if target else None,
    }


def connect(cs):
    return pyodbc.connect(cs, autocommit=True, timeout=20)


def columns(conn, table):
    rows = conn.cursor().execute(
        """
        select column_name
          from information_schema.columns
         where table_schema = 'dbo' and table_name = ?
         order by ordinal_position
        """,
        table,
    ).fetchall()
    return [row[0] for row in rows]


def rows(conn, table, cols):
    cur = conn.cursor()
    cur.execute(f"select {', '.join(f'[{c}]' for c in cols)} from dbo.[{table}]")
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def one(conn, query, params=()):
    cur = conn.cursor()
    cur.execute(query, params)
    row = cur.fetchone()
    if not row:
        return None
    cols = [c[0] for c in cur.description]
    return dict(zip(cols, row))


def execute(conn, query, params=()):
    conn.cursor().execute(query, params)


def exists_by_id(conn, table, id_value):
    if id_value is None:
        return None
    return one(conn, f"select cast(id as nvarchar(36)) as id from dbo.[{table}] where id = ?", (id_value,))


def update_by_id(conn, table, row, key="id"):
    cols = [c for c in row.keys() if c != key]
    if not cols:
        return
    set_clause = ", ".join(f"[{c}] = ?" for c in cols)
    execute(conn, f"update dbo.[{table}] set {set_clause} where [{key}] = ?", [row[c] for c in cols] + [row[key]])


def insert_row(conn, table, row):
    cols = list(row.keys())
    col_sql = ", ".join(f"[{c}]" for c in cols)
    placeholders = ", ".join("?" for _ in cols)
    execute(conn, f"insert into dbo.[{table}] ({col_sql}) values ({placeholders})", [row[c] for c in cols])


def upsert_id_row(conn, table, row):
    if exists_by_id(conn, table, row.get("id")):
        update_by_id(conn, table, row)
        return "updated"
    insert_row(conn, table, row)
    return "inserted"


def mapped(row, mapping, *fields):
    out = dict(row)
    for field in fields:
        if out.get(field) is not None:
            out[field] = mapping.get(str(out[field]).upper(), out[field])
    return out


def key(value):
    return str(value).upper() if value is not None else None


def merge(source_conn, target_conn, dry_run=False):
    common_columns = {
        table: [c for c in columns(source_conn, table) if c in set(columns(target_conn, table))]
        for table in TABLE_ORDER
    }
    stats = defaultdict(lambda: {"read": 0, "inserted": 0, "updated": 0, "mapped_existing": 0, "skipped": 0})
    id_map = {}
    snapshot_map = {}
    prediction_map = {}
    signal_map = {}

    def apply(action, fn):
        if dry_run:
            return action
        return fn()

    for table in TABLE_ORDER:
        table_rows = rows(source_conn, table, common_columns[table])
        stats[table]["read"] = len(table_rows)

        for row in table_rows:
            action = "skipped"

            if table == "rounds":
                existing = exists_by_id(target_conn, table, row["id"])
                if not existing:
                    existing = one(
                        target_conn,
                        "select cast(id as nvarchar(36)) as id from dbo.rounds where round_cutoff = ? or round_id = ?",
                        (row.get("round_cutoff"), row.get("round_id")),
                    )
                if existing:
                    id_map[key(row["id"])] = existing["id"]
                    row = {**row, "id": existing["id"]}
                    action = apply("updated", lambda r=row: update_by_id(target_conn, table, r))
                    stats[table]["mapped_existing"] += 1
                else:
                    id_map[key(row["id"])] = row["id"]
                    action = apply("inserted", lambda r=row: insert_row(target_conn, table, r))

            elif table == "reference_prices":
                row = mapped(row, id_map, "round_id")
                action = apply("upserted", lambda r=row: upsert_id_row(target_conn, table, r))

            elif table == "decision_snapshots":
                row = mapped(row, id_map, "round_id")
                existing = exists_by_id(target_conn, table, row["id"])
                if not existing and row.get("capture_reason") == "scheduled":
                    existing = one(
                        target_conn,
                        """
                        select cast(id as nvarchar(36)) as id
                          from dbo.decision_snapshots
                         where round_id = ? and seconds_bucket = ? and capture_reason = N'scheduled'
                        """,
                        (row.get("round_id"), row.get("seconds_bucket")),
                    )
                if existing:
                    snapshot_map[key(row["id"])] = existing["id"]
                    row = {**row, "id": existing["id"]}
                    action = apply("updated", lambda r=row: update_by_id(target_conn, table, r))
                    stats[table]["mapped_existing"] += 1
                else:
                    snapshot_map[key(row["id"])] = row["id"]
                    action = apply("inserted", lambda r=row: insert_row(target_conn, table, r))

            elif table == "market_quotes":
                row = mapped(row, id_map, "round_id")
                row = mapped(row, snapshot_map, "snapshot_id")
                existing = exists_by_id(target_conn, table, row["id"])
                if not existing:
                    existing = one(
                        target_conn,
                        "select cast(id as nvarchar(36)) as id from dbo.market_quotes where snapshot_id = ? and outcome = ?",
                        (row.get("snapshot_id"), row.get("outcome")),
                    )
                if existing:
                    row = {**row, "id": existing["id"]}
                    action = apply("updated", lambda r=row: update_by_id(target_conn, table, r))
                    stats[table]["mapped_existing"] += 1
                else:
                    action = apply("inserted", lambda r=row: insert_row(target_conn, table, r))

            elif table == "feature_snapshots":
                row = mapped(row, id_map, "round_id")
                row = mapped(row, snapshot_map, "snapshot_id")
                existing = exists_by_id(target_conn, table, row["id"])
                if not existing:
                    existing = one(
                        target_conn,
                        """
                        select cast(id as nvarchar(36)) as id
                          from dbo.feature_snapshots
                         where snapshot_id = ? and feature_set_version = ?
                        """,
                        (row.get("snapshot_id"), row.get("feature_set_version")),
                    )
                if existing:
                    row = {**row, "id": existing["id"]}
                    action = apply("updated", lambda r=row: update_by_id(target_conn, table, r))
                    stats[table]["mapped_existing"] += 1
                else:
                    action = apply("inserted", lambda r=row: insert_row(target_conn, table, r))

            elif table == "predictions_v2":
                row = mapped(row, id_map, "round_id")
                row = mapped(row, snapshot_map, "snapshot_id")
                existing = exists_by_id(target_conn, table, row["id"])
                if not existing:
                    existing = one(
                        target_conn,
                        """
                        select cast(id as nvarchar(36)) as id
                          from dbo.predictions_v2
                         where snapshot_id = ? and model_version = ? and model_stage = ?
                        """,
                        (row.get("snapshot_id"), row.get("model_version"), row.get("model_stage")),
                    )
                if existing:
                    prediction_map[key(row["id"])] = existing["id"]
                    row = {**row, "id": existing["id"]}
                    action = apply("updated", lambda r=row: update_by_id(target_conn, table, r))
                    stats[table]["mapped_existing"] += 1
                else:
                    prediction_map[key(row["id"])] = row["id"]
                    action = apply("inserted", lambda r=row: insert_row(target_conn, table, r))

            elif table == "signals_v2":
                row = mapped(row, id_map, "round_id")
                row = mapped(row, snapshot_map, "snapshot_id")
                row = mapped(row, prediction_map, "prediction_id")
                existing = exists_by_id(target_conn, table, row["id"])
                if not existing:
                    existing = one(
                        target_conn,
                        "select cast(id as nvarchar(36)) as id from dbo.signals_v2 where prediction_id = ? and strategy_version = ?",
                        (row.get("prediction_id"), row.get("strategy_version")),
                    )
                if existing:
                    signal_map[key(row["id"])] = existing["id"]
                    row = {**row, "id": existing["id"]}
                    action = apply("updated", lambda r=row: update_by_id(target_conn, table, r))
                    stats[table]["mapped_existing"] += 1
                else:
                    signal_map[key(row["id"])] = row["id"]
                    action = apply("inserted", lambda r=row: insert_row(target_conn, table, r))

            elif table == "round_results":
                existing = exists_by_id(target_conn, table, row["id"])
                if not existing:
                    existing = one(
                        target_conn,
                        "select cast(id as nvarchar(36)) as id from dbo.round_results where round_cutoff = ?",
                        (row.get("round_cutoff"),),
                    )
                if existing:
                    row = {**row, "id": existing["id"]}
                    action = apply("updated", lambda r=row: update_by_id(target_conn, table, r))
                    stats[table]["mapped_existing"] += 1
                else:
                    action = apply("inserted", lambda r=row: insert_row(target_conn, table, r))

            elif table == "trade_results_v2":
                row = mapped(row, id_map, "round_id")
                row = mapped(row, signal_map, "signal_id")
                existing = exists_by_id(target_conn, table, row["id"])
                if not existing:
                    existing = one(
                        target_conn,
                        "select cast(id as nvarchar(36)) as id from dbo.trade_results_v2 where signal_id = ?",
                        (row.get("signal_id"),),
                    )
                if existing:
                    row = {**row, "id": existing["id"]}
                    action = apply("updated", lambda r=row: update_by_id(target_conn, table, r))
                    stats[table]["mapped_existing"] += 1
                else:
                    action = apply("inserted", lambda r=row: insert_row(target_conn, table, r))

            elif table == "dataset_versions":
                existing = one(
                    target_conn,
                    "select cast(id as nvarchar(36)) as id from dbo.dataset_versions where dataset_version = ?",
                    (row.get("dataset_version"),),
                )
                if existing:
                    row = {**row, "id": existing["id"]}
                    action = apply("updated", lambda r=row: update_by_id(target_conn, table, r))
                    stats[table]["mapped_existing"] += 1
                else:
                    action = apply("inserted", lambda r=row: insert_row(target_conn, table, r))

            elif table == "model_runs":
                existing = one(
                    target_conn,
                    "select cast(id as nvarchar(36)) as id from dbo.model_runs where run_id = ?",
                    (row.get("run_id"),),
                )
                if existing:
                    row = {**row, "id": existing["id"]}
                    action = apply("updated", lambda r=row: update_by_id(target_conn, table, r))
                    stats[table]["mapped_existing"] += 1
                else:
                    action = apply("inserted", lambda r=row: insert_row(target_conn, table, r))

            if action in ("inserted", "upserted"):
                stats[table]["inserted"] += 1
            elif action == "updated":
                stats[table]["updated"] += 1
            else:
                stats[table]["skipped"] += 1

    return stats


def count_tables(conn):
    counts = {}
    for table in TABLE_ORDER:
        try:
            counts[table] = one(conn, f"select count(*) as total from dbo.[{table}]")["total"]
        except Exception as exc:
            counts[table] = f"error: {exc}"
    return counts


def main():
    parser = argparse.ArgumentParser(description="Merge local SQL Server PolymarketBot data into Docker SQL Server.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    connections = load_connections()
    if not connections["source"]:
        raise SystemExit("Missing SQLSERVER_CONNECTION in .env")
    if not connections["target"]:
        raise SystemExit("Missing SQLSERVER_CONNECTION in .env.docker")

    with connect(connections["source"]) as source, connect(connections["target"]) as target:
        before = {"source": count_tables(source), "target": count_tables(target)}
        stats = merge(source, target, dry_run=args.dry_run)
        after = {"source": count_tables(source), "target": count_tables(target)}

    print(json.dumps(
        {
            "dry_run": args.dry_run,
            "before": before,
            "merge_stats": stats,
            "after": after,
        },
        indent=2,
        default=str,
    ))


if __name__ == "__main__":
    main()
