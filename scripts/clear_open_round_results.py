import json
import os
import time

import pyodbc
from dotenv import load_dotenv


load_dotenv()


def connect():
    conn_str = os.getenv("SQLSERVER_CONNECTION")
    if not conn_str:
        raise RuntimeError("SQLSERVER_CONNECTION is not configured")
    return pyodbc.connect(conn_str)


def main():
    now = int(time.time())
    with connect() as conn:
        cursor = conn.cursor()
        rows = cursor.execute(
            """
            select id, round_cutoff
              from dbo.rounds
             where round_cutoff >= ?
                or (status = N'resolved' and round_cutoff >= ?)
            """,
            now - 15,
            now - 900,
        ).fetchall()

        cutoffs = sorted({int(row.round_cutoff) for row in rows})
        deleted_results = 0
        updated_rounds = 0
        for cutoff in cutoffs:
            cursor.execute("delete from dbo.round_results where round_cutoff = ?", cutoff)
            deleted_results += cursor.rowcount or 0
            cursor.execute(
                """
                update dbo.rounds
                   set status = N'open',
                       resolved_at = null,
                       close_source = null,
                       updated_at = sysutcdatetime()
                 where round_cutoff = ?
                """,
                cutoff,
            )
            updated_rounds += cursor.rowcount or 0
        conn.commit()

    print(json.dumps({"cutoffs": cutoffs, "deleted_results": deleted_results, "updated_rounds": updated_rounds}, indent=2))


if __name__ == "__main__":
    main()
