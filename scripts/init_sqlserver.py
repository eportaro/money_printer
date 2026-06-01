import os
import re
import time
from pathlib import Path

import pyodbc
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = PROJECT_ROOT / "migrations" / "004_sqlserver_schema.sql"

load_dotenv(PROJECT_ROOT / ".env")


def connection_string(database=None):
    raw = os.environ["SQLSERVER_CONNECTION"]
    if database is None:
        return raw
    if re.search(r"(?i)(^|;)DATABASE=", raw):
        return re.sub(r"(?i)(^|;)DATABASE=[^;]*", rf"\1DATABASE={database}", raw)
    return raw + f";DATABASE={database};"


def wait_for_server(timeout=120):
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            with pyodbc.connect(connection_string("master"), autocommit=True, timeout=5) as conn:
                conn.cursor().execute("select 1")
                return
        except Exception as exc:
            last_error = exc
            print(f"SQL Server not ready yet: {exc}", flush=True)
            time.sleep(4)
    raise SystemExit(f"SQL Server did not become ready: {last_error}")


def execute_batches(conn, sql_text):
    batches = re.split(r"(?im)^\s*go\s*$", sql_text)
    cursor = conn.cursor()
    for batch in batches:
        batch = batch.strip()
        if not batch:
            continue
        cursor.execute(batch)


def main():
    wait_for_server()
    db_name = os.getenv("SQLSERVER_DATABASE", "PolymarketBot")
    with pyodbc.connect(connection_string("master"), autocommit=True) as conn:
        conn.cursor().execute(
            f"if db_id(N'{db_name}') is null create database [{db_name}]"
        )
    with pyodbc.connect(connection_string(db_name), autocommit=True) as conn:
        execute_batches(conn, MIGRATION.read_text(encoding="utf-8"))
    print(f"SQL Server database ready: {db_name}", flush=True)


if __name__ == "__main__":
    main()
