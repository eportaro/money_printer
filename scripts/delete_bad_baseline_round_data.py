import argparse
import json
import os

import pyodbc
from dotenv import load_dotenv


load_dotenv()


def connect():
    conn_str = os.getenv("SQLSERVER_CONNECTION")
    if not conn_str:
        raise RuntimeError("SQLSERVER_CONNECTION is not configured")
    return pyodbc.connect(conn_str)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cutoff", type=int, required=True)
    parser.add_argument("--bad-source", default="polymarket_previous_final_price")
    args = parser.parse_args()

    with connect() as conn:
        cursor = conn.cursor()
        snapshot_rows = cursor.execute(
            """
            select ds.id
              from dbo.decision_snapshots ds
              join dbo.predictions_v2 p on p.snapshot_id = ds.id
              join dbo.rounds r on r.id = ds.round_id
             where r.round_cutoff = ?
               and json_value(p.raw, '$.baseline_source') = ?
            """,
            args.cutoff,
            args.bad_source,
        ).fetchall()
        snapshot_ids = [row.id for row in snapshot_rows]

        deleted = {
            "trade_results_v2": 0,
            "signals_v2": 0,
            "predictions_v2": 0,
            "feature_snapshots": 0,
            "market_quotes": 0,
            "decision_snapshots": 0,
        }

        for snapshot_id in snapshot_ids:
            signal_rows = cursor.execute("select id from dbo.signals_v2 where snapshot_id = ?", snapshot_id).fetchall()
            signal_ids = [row.id for row in signal_rows]
            for signal_id in signal_ids:
                cursor.execute("delete from dbo.trade_results_v2 where signal_id = ?", signal_id)
                deleted["trade_results_v2"] += cursor.rowcount or 0

            cursor.execute("delete from dbo.signals_v2 where snapshot_id = ?", snapshot_id)
            deleted["signals_v2"] += cursor.rowcount or 0
            cursor.execute("delete from dbo.predictions_v2 where snapshot_id = ?", snapshot_id)
            deleted["predictions_v2"] += cursor.rowcount or 0
            cursor.execute("delete from dbo.feature_snapshots where snapshot_id = ?", snapshot_id)
            deleted["feature_snapshots"] += cursor.rowcount or 0
            cursor.execute("delete from dbo.market_quotes where snapshot_id = ?", snapshot_id)
            deleted["market_quotes"] += cursor.rowcount or 0
            cursor.execute("delete from dbo.decision_snapshots where id = ?", snapshot_id)
            deleted["decision_snapshots"] += cursor.rowcount or 0

        conn.commit()

    print(json.dumps({"cutoff": args.cutoff, "bad_source": args.bad_source, "snapshots": len(snapshot_ids), "deleted": deleted}, indent=2))


if __name__ == "__main__":
    main()
