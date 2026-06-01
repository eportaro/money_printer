import json
import os
import threading
import uuid
from datetime import datetime
from decimal import Decimal

from dotenv import load_dotenv

load_dotenv()

_thread_local = threading.local()


def enabled():
    return os.getenv("DB_BACKEND", "").lower() == "sqlserver"


def connection_string():
    return os.getenv(
        "SQLSERVER_CONNECTION",
        "DRIVER={ODBC Driver 18 for SQL Server};"
        "SERVER=localhost;"
        "DATABASE=PolymarketBot;"
        "Trusted_Connection=yes;"
        "Encrypt=yes;"
        "TrustServerCertificate=yes;",
    )


def _connect():
    import pyodbc

    conn = getattr(_thread_local, "conn", None)
    if conn is not None:
        try:
            conn.cursor().execute("SELECT 1")
            return conn
        except Exception:
            _thread_local.conn = None
    _thread_local.conn = pyodbc.connect(connection_string(), autocommit=True)
    return _thread_local.conn


def _jsonable(value):
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _json_text(value):
    return json.dumps(_jsonable(value or {}), default=str)


def _as_row_dict(cursor, row):
    cols = [col[0] for col in cursor.description]
    parsed = {}
    json_columns = {"raw", "features", "filters", "metrics", "hyperparameters"}
    for col, value in zip(cols, row):
        value = _jsonable(value)
        if col in json_columns and isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        parsed[col] = value
    return parsed


def _fetch_one(query, params=()):
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(query, params)
        row = cur.fetchone()
        return _as_row_dict(cur, row) if row else None


def _fetch_all(query, params=()):
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(query, params)
        return [_as_row_dict(cur, row) for row in cur.fetchall()]


def _execute(query, params=()):
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(query, params)


def _insert_returning_id(query, params=()):
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(query, params)
        row = cur.fetchone()
        return str(row[0]) if row else None


def count_rows(table):
    row = _fetch_one(f"select count(*) as total from dbo.{table}")
    return int(row["total"]) if row else None


def insert_dataset_version(row):
    existing = _fetch_one(
        "select cast(id as nvarchar(36)) as id from dbo.dataset_versions where dataset_version = ?",
        (row["dataset_version"],),
    )
    params = (
        row["source_query_hash"],
        int(row["row_count"]),
        int(row["round_count"]),
        row.get("start_time"),
        row.get("end_time"),
        _json_text(row.get("filters", {})),
        row.get("parquet_path"),
        _json_text(row.get("raw", {})),
    )
    if existing:
        _execute(
            """
            update dbo.dataset_versions
               set source_query_hash = ?, row_count = ?, round_count = ?,
                   start_time = ?, end_time = ?, filters = ?, parquet_path = ?, raw = ?
             where id = ?
            """,
            (*params, existing["id"]),
        )
        return existing["id"]
    return _insert_returning_id(
        """
        insert into dbo.dataset_versions
            (dataset_version, source_query_hash, row_count, round_count, start_time,
             end_time, filters, parquet_path, raw)
        output inserted.id
        values (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (row["dataset_version"], *params),
    )


def insert_model_run(row):
    existing = _fetch_one(
        "select cast(id as nvarchar(36)) as id from dbo.model_runs where run_id = ?",
        (row["run_id"],),
    )
    params = (
        row["model_version"],
        row.get("dataset_version"),
        row.get("feature_set_version"),
        row["algorithm"],
        _json_text(row.get("hyperparameters", {})),
        _json_text(row.get("metrics", {})),
        row.get("artifact_path"),
        row.get("model_stage", "candidate"),
        row.get("notes"),
    )
    if existing:
        _execute(
            """
            update dbo.model_runs
               set model_version = ?, dataset_version = ?, feature_set_version = ?,
                   algorithm = ?, hyperparameters = ?, metrics = ?, artifact_path = ?,
                   model_stage = ?, notes = ?
             where id = ?
            """,
            (*params, existing["id"]),
        )
        return existing["id"]
    return _insert_returning_id(
        """
        insert into dbo.model_runs
            (run_id, model_version, dataset_version, feature_set_version, algorithm,
             hyperparameters, metrics, artifact_path, model_stage, notes)
        output inserted.id
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (row["run_id"], *params),
    )


def insert_strategy_backtest_run(row):
    existing = _fetch_one(
        "select cast(id as nvarchar(36)) as id from dbo.strategy_backtest_runs where run_id = ?",
        (row["run_id"],),
    )
    params = (
        row["model_version"],
        row.get("dataset_version"),
        row["strategy_set_version"],
        int(row.get("source_row_count", 0)),
        int(row.get("resolved_round_count", 0)),
        int(row.get("signal_count", 0)),
        row.get("started_at"),
        row.get("completed_at"),
        _json_text(row.get("metrics", {})),
        _json_text(row.get("raw", {})),
    )
    if existing:
        _execute(
            """
            update dbo.strategy_backtest_runs
               set model_version = ?, dataset_version = ?, strategy_set_version = ?,
                   source_row_count = ?, resolved_round_count = ?, signal_count = ?,
                   started_at = ?, completed_at = ?, metrics = ?, raw = ?
             where id = ?
            """,
            (*params, existing["id"]),
        )
        return existing["id"]
    return _insert_returning_id(
        """
        insert into dbo.strategy_backtest_runs
            (run_id, model_version, dataset_version, strategy_set_version,
             source_row_count, resolved_round_count, signal_count, started_at,
             completed_at, metrics, raw)
        output inserted.id
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (row["run_id"], *params),
    )


def update_strategy_backtest_run(run_id, updates):
    allowed = {
        "source_row_count",
        "resolved_round_count",
        "signal_count",
        "completed_at",
        "metrics",
        "raw",
    }
    fields = []
    params = []
    for key, value in updates.items():
        if key not in allowed:
            continue
        fields.append(f"{key} = ?")
        params.append(_json_text(value) if key in {"metrics", "raw"} else value)
    if not fields:
        return
    _execute(
        f"update dbo.strategy_backtest_runs set {', '.join(fields)} where run_id = ?",
        (*params, run_id),
    )


def insert_strategy_backtest_signal(row):
    return _insert_returning_id(
        """
        insert into dbo.strategy_backtest_signals
            (backtest_run_id, snapshot_id, round_id, observed_at, round_cutoff,
             seconds_to_cutoff, seconds_bucket, model_version, model_stage,
             strategy_version, action, side, prediction, prob_up, prob_down,
             confidence, entry_price, model_prob, edge, stake, alignment,
             result, pnl, roi, actual_close, outcome, reason, raw)
        output inserted.id
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["backtest_run_id"],
            row["snapshot_id"],
            row["round_id"],
            row.get("observed_at"),
            int(row["round_cutoff"]),
            row.get("seconds_to_cutoff"),
            row.get("seconds_bucket"),
            row["model_version"],
            row.get("model_stage", "backtest"),
            row["strategy_version"],
            row["action"],
            row["side"],
            row["prediction"],
            row["prob_up"],
            row["prob_down"],
            row["confidence"],
            row["entry_price"],
            row["model_prob"],
            row["edge"],
            row.get("stake", 1),
            row["alignment"],
            row["result"],
            row.get("pnl"),
            row.get("roi"),
            row.get("actual_close"),
            row.get("outcome"),
            row.get("reason"),
            _json_text(row.get("raw", {})),
        ),
    )


def fetch_latest_strategy_backtest_run(model_version, strategy_set_version=None):
    if strategy_set_version:
        return _fetch_one(
            """
            select top (1) *
              from dbo.strategy_backtest_runs
             where model_version = ? and strategy_set_version = ? and completed_at is not null
             order by completed_at desc, started_at desc
            """,
            (model_version, strategy_set_version),
        )
    return _fetch_one(
        """
        select top (1) *
          from dbo.strategy_backtest_runs
         where model_version = ? and completed_at is not null
         order by completed_at desc, started_at desc
        """,
        (model_version,),
    )


def fetch_strategy_backtest_signals(model_version, limit=5000, strategy_set_version=None):
    run = fetch_latest_strategy_backtest_run(model_version, strategy_set_version)
    if not run:
        return []
    return _fetch_all(
        f"""
        select top ({int(limit)}) *
          from dbo.strategy_backtest_performance_v2
         where run_id = ?
         order by observed_at desc
        """,
        (run["run_id"],),
    )


def promote_model_version(model_version):
    _execute(
        """
        update dbo.model_runs
           set model_stage = N'archived'
         where model_stage = N'production'
           and model_version <> ?
        """,
        (model_version,),
    )
    _execute(
        """
        update dbo.model_runs
           set model_stage = N'production'
         where model_version = ?
        """,
        (model_version,),
    )


def fetch_training_decision_snapshots(limit=50000):
    return _fetch_all(
        f"""
        select top ({int(limit)}) *
          from dbo.training_decision_snapshots
         where target_up is not null
         order by observed_at asc
        """
    )


def fetch_model_performance_v2():
    return _fetch_all(
        """
        select *
          from dbo.model_performance_by_version
         order by model_stage asc, model_version asc
        """
    )


def fetch_dataset_summary_v2():
    row = _fetch_one(
        """
        with base as (
            select
                *,
                case when prediction = outcome then 1.0 else 0.0 end as correct,
                row_number() over(partition by round_cutoff order by seconds_to_cutoff desc, observed_at asc) as first_rn,
                row_number() over(partition by round_cutoff order by seconds_to_cutoff asc, observed_at desc) as last_rn
            from dbo.training_decision_snapshots
            where target_up is not null and outcome in (N'UP', N'DOWN')
        )
        select
            count(*) as modeling_rows,
            count(distinct round_cutoff) as unique_resolved_rounds,
            cast(round(avg(correct) * 100, 2) as decimal(8,2)) as row_accuracy_pct,
            cast(round(avg(case when first_rn = 1 then correct end) * 100, 2) as decimal(8,2)) as first_prediction_round_accuracy_pct,
            cast(round(avg(case when last_rn = 1 then correct end) * 100, 2) as decimal(8,2)) as last_prediction_round_accuracy_pct,
            min(observed_at) as start_time,
            max(observed_at) as end_time
        from base
        """
    )
    return row or {}


def upsert_round_v2(round_data):
    existing = _fetch_one(
        "select cast(id as nvarchar(36)) as id from dbo.rounds where round_cutoff = ?",
        (int(round_data["round_cutoff"]),),
    )
    raw = _json_text(round_data.get("raw", {}))
    if existing:
        _execute(
            """
            update dbo.rounds
               set round_id = ?, event_slug = ?, condition_id = ?, window_start = ?,
                   baseline = ?, baseline_source = ?, resolution_source = ?, close_source = ?,
                   status = ?, resolved_at = ?, raw = ?, updated_at = sysutcdatetime()
             where id = ?
            """,
            (
                round_data["round_id"],
                round_data.get("event_slug"),
                round_data.get("condition_id"),
                int(round_data["window_start"]),
                round_data.get("baseline"),
                round_data.get("baseline_source", "coinbase_btc_usd_prev_close"),
                round_data.get("resolution_source"),
                round_data.get("close_source"),
                round_data.get("status", "open"),
                round_data.get("resolved_at"),
                raw,
                existing["id"],
            ),
        )
        return existing["id"]

    return _insert_returning_id(
        """
        insert into dbo.rounds
            (round_id, event_slug, condition_id, window_start, round_cutoff, baseline,
             baseline_source, resolution_source, close_source, status, resolved_at, raw)
        output inserted.id
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            round_data["round_id"],
            round_data.get("event_slug"),
            round_data.get("condition_id"),
            int(round_data["window_start"]),
            int(round_data["round_cutoff"]),
            round_data.get("baseline"),
            round_data.get("baseline_source", "coinbase_btc_usd_prev_close"),
            round_data.get("resolution_source"),
            round_data.get("close_source"),
            round_data.get("status", "open"),
            round_data.get("resolved_at"),
            raw,
        ),
    )


def update_round_v2(round_id, updates):
    allowed = {
        "status",
        "resolved_at",
        "close_source",
        "baseline",
        "baseline_source",
        "resolution_source",
        "raw",
    }
    parts = []
    params = []
    for key, value in updates.items():
        if key not in allowed:
            continue
        parts.append(f"{key} = ?")
        params.append(_json_text(value) if key == "raw" else value)
    if not parts:
        return None
    params.append(round_id)
    _execute(f"update dbo.rounds set {', '.join(parts)}, updated_at = sysutcdatetime() where id = ?", params)
    return round_id


def update_round_baseline_snapshots(round_id, baseline, baseline_source):
    _execute(
        """
        update dbo.decision_snapshots
           set baseline = ?,
               dist_to_baseline = btc_price - ?,
               dist_to_baseline_pct = case when ? = 0 then null else ((btc_price - ?) / ?) * 100.0 end,
               baseline_source = ?
         where round_id = ?
        """,
        (
            baseline,
            baseline,
            baseline,
            baseline,
            baseline,
            baseline_source,
            round_id,
        ),
    )


def insert_reference_price(row):
    return _insert_returning_id(
        """
        insert into dbo.reference_prices
            (round_id, observed_at, source, symbol, price, seconds_to_cutoff, purpose, raw)
        output inserted.id
        values (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row.get("round_id"),
            row.get("observed_at"),
            row.get("source", "coinbase_btc_usd"),
            row.get("symbol", "BTC-USD"),
            row["price"],
            row.get("seconds_to_cutoff"),
            row["purpose"],
            _json_text(row.get("raw", {})),
        ),
    )


def insert_decision_snapshot(row):
    if row.get("capture_reason") == "scheduled":
        existing = _fetch_one(
            """
            select cast(id as nvarchar(36)) as id
              from dbo.decision_snapshots
             where round_id = ? and seconds_bucket = ? and capture_reason = N'scheduled'
            """,
            (row["round_id"], int(row["seconds_bucket"])),
        )
        if existing:
            return existing["id"]
    return _insert_returning_id(
        """
        insert into dbo.decision_snapshots
            (round_id, observed_at, seconds_to_cutoff, seconds_bucket, capture_reason,
             btc_price, baseline, dist_to_baseline, dist_to_baseline_pct, baseline_source,
             collector_version, raw)
        output inserted.id
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["round_id"],
            row.get("observed_at"),
            int(row["seconds_to_cutoff"]),
            int(row["seconds_bucket"]),
            row.get("capture_reason", "scheduled"),
            row["btc_price"],
            row.get("baseline"),
            row.get("dist_to_baseline"),
            row.get("dist_to_baseline_pct"),
            row.get("baseline_source"),
            row.get("collector_version", "collector-v2"),
            _json_text(row.get("raw", {})),
        ),
    )


def insert_market_quote_v2(row):
    existing = _fetch_one(
        "select cast(id as nvarchar(36)) as id from dbo.market_quotes where snapshot_id = ? and outcome = ?",
        (row["snapshot_id"], row["outcome"]),
    )
    if existing:
        return existing["id"]
    return _insert_returning_id(
        """
        insert into dbo.market_quotes
            (snapshot_id, round_id, observed_at, outcome, token_id, best_bid, best_ask,
             midpoint, spread, bid_size, ask_size, last_trade_price, book_hash, raw)
        output inserted.id
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["snapshot_id"],
            row["round_id"],
            row.get("observed_at"),
            row["outcome"],
            row.get("token_id"),
            row.get("best_bid"),
            row.get("best_ask"),
            row.get("midpoint"),
            row.get("spread"),
            row.get("bid_size"),
            row.get("ask_size"),
            row.get("last_trade_price"),
            row.get("book_hash"),
            _json_text(row.get("raw", {})),
        ),
    )


def upsert_feature_snapshot(row):
    existing = _fetch_one(
        "select cast(id as nvarchar(36)) as id from dbo.feature_snapshots where snapshot_id = ? and feature_set_version = ?",
        (row["snapshot_id"], row["feature_set_version"]),
    )
    if existing:
        return existing["id"]
    return _insert_returning_id(
        """
        insert into dbo.feature_snapshots
            (snapshot_id, round_id, observed_at, feature_set_version, features)
        output inserted.id
        values (?, ?, ?, ?, ?)
        """,
        (
            row["snapshot_id"],
            row["round_id"],
            row.get("observed_at"),
            row["feature_set_version"],
            _json_text(row.get("features", {})),
        ),
    )


def insert_prediction_v2(row):
    existing = _fetch_one(
        """
        select cast(id as nvarchar(36)) as id
          from dbo.predictions_v2
         where snapshot_id = ? and model_version = ? and model_stage = ?
        """,
        (row["snapshot_id"], row["model_version"], row.get("model_stage", "production")),
    )
    if existing:
        return existing["id"]
    return _insert_returning_id(
        """
        insert into dbo.predictions_v2
            (snapshot_id, round_id, observed_at, model_version, model_stage, prediction,
             prob_up, prob_down, confidence, edge_up, edge_down, recommended_action,
             feature_set_version, raw)
        output inserted.id
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["snapshot_id"],
            row["round_id"],
            row.get("observed_at"),
            row["model_version"],
            row.get("model_stage", "production"),
            row["prediction"],
            row["prob_up"],
            row["prob_down"],
            row["confidence"],
            row.get("edge_up"),
            row.get("edge_down"),
            row.get("recommended_action"),
            row.get("feature_set_version"),
            _json_text(row.get("raw", {})),
        ),
    )


def insert_signal_v2(row):
    existing = _fetch_one(
        "select cast(id as nvarchar(36)) as id from dbo.signals_v2 where prediction_id = ? and strategy_version = ?",
        (row["prediction_id"], row["strategy_version"]),
    )
    if existing:
        return existing["id"]
    return _insert_returning_id(
        """
        insert into dbo.signals_v2
            (prediction_id, snapshot_id, round_id, observed_at, strategy_version, action,
             side, entry_price, model_prob, edge, stake, raw)
        output inserted.id
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["prediction_id"],
            row["snapshot_id"],
            row["round_id"],
            row.get("observed_at"),
            row["strategy_version"],
            row["action"],
            row.get("side"),
            row.get("entry_price"),
            row.get("model_prob"),
            row.get("edge"),
            row.get("stake", 1),
            _json_text(row.get("raw", {})),
        ),
    )


def upsert_trade_result_v2(row):
    existing = _fetch_one(
        "select cast(id as nvarchar(36)) as id from dbo.trade_results_v2 where signal_id = ?",
        (row["signal_id"],),
    )
    raw = _json_text(row.get("raw", {}))
    if existing:
        _execute(
            "update dbo.trade_results_v2 set result = ?, pnl = ?, roi = ?, resolved_at = ?, raw = ? where id = ?",
            (
                row["result"],
                row.get("pnl"),
                row.get("roi"),
                row.get("resolved_at") or datetime.utcnow(),
                raw,
                existing["id"],
            ),
        )
        return existing["id"]
    return _insert_returning_id(
        """
        insert into dbo.trade_results_v2
            (signal_id, round_id, result, pnl, roi, resolved_at, raw)
        output inserted.id
        values (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["signal_id"],
            row["round_id"],
            row["result"],
            row.get("pnl"),
            row.get("roi"),
            row.get("resolved_at") or datetime.utcnow(),
            raw,
        ),
    )


def fetch_captured_scheduled_buckets(round_id):
    rows = _fetch_all(
        "select seconds_bucket from dbo.decision_snapshots where round_id = ? and capture_reason = N'scheduled'",
        (round_id,),
    )
    return {int(row["seconds_bucket"]) for row in rows}


def fetch_unresolved_rounds_v2(limit=200):
    return _fetch_all(
        f"select top ({int(limit)}) * from dbo.rounds where status in (N'open', N'provisional') order by round_cutoff asc"
    )


def upsert_round_result(result):
    existing = _fetch_one("select cast(id as nvarchar(36)) as id from dbo.round_results where round_cutoff = ?", (int(result["round_cutoff"]),))
    raw = _json_text(result.get("raw", {}))
    if existing:
        _execute(
            "update dbo.round_results set baseline = ?, actual_close = ?, outcome = ?, raw = ?, resolved_at = sysutcdatetime() where id = ?",
            (result.get("baseline"), result["actual_close"], result["outcome"], raw, existing["id"]),
        )
        return existing["id"]
    return _insert_returning_id(
        """
        insert into dbo.round_results (round_cutoff, baseline, actual_close, outcome, raw)
        output inserted.id values (?, ?, ?, ?, ?)
        """,
        (int(result["round_cutoff"]), result.get("baseline"), result["actual_close"], result["outcome"], raw),
    )


def fetch_recent_round_results(limit=200):
    return _fetch_all(f"select top ({int(limit)}) * from dbo.round_results order by round_cutoff desc")


def fetch_open_signals_v2(limit=500):
    return _fetch_all(
        f"""
        select top ({int(limit)}) sp.*
        from dbo.strategy_performance_v2 sp
        join dbo.rounds r on r.round_id = sp.round_id
        where sp.result is null or r.status = N'provisional'
        order by sp.observed_at asc
        """
    )


def fetch_recent_signals_v2(limit=25):
    return _fetch_all(f"select top ({int(limit)}) * from dbo.strategy_performance_v2 order by observed_at desc")


def fetch_recent_rounds_v2(limit=50):
    return _fetch_all(
        f"""
        with ranked as (
            select
                ds.observed_at,
                r.round_id,
                r.round_cutoff,
                coalesce(r.baseline, ds.baseline) as baseline,
                r.baseline_source,
                r.resolution_source,
                p.prediction,
                p.prob_up,
                rr.actual_close,
                rr.outcome,
                case when rr.outcome = N'UP' then 1 when rr.outcome = N'DOWN' then 0 else null end as target_up,
                p.model_version,
                p.model_stage,
                row_number() over(partition by r.round_cutoff order by ds.seconds_to_cutoff desc, ds.observed_at asc) as initial_rn,
                row_number() over(partition by r.round_cutoff order by ds.seconds_to_cutoff asc, ds.observed_at desc) as final_rn
            from dbo.decision_snapshots ds
            join dbo.rounds r on r.id = ds.round_id
            left join dbo.predictions_v2 p on p.snapshot_id = ds.id and p.model_stage = N'production'
            left join dbo.round_results rr on rr.round_cutoff = r.round_cutoff
        ),
        per_round as (
            select
                round_id,
                round_cutoff,
                max(case when final_rn = 1 then observed_at end) as observed_at,
                max(case when final_rn = 1 then baseline end) as baseline,
                max(case when final_rn = 1 then baseline_source end) as baseline_source,
                max(case when final_rn = 1 then resolution_source end) as resolution_source,
                max(case when initial_rn = 1 then prediction end) as initial_prediction,
                max(case when initial_rn = 1 then prob_up end) as initial_prob_up,
                max(case when initial_rn = 1 then observed_at end) as initial_observed_at,
                max(case when final_rn = 1 then prediction end) as prediction,
                max(case when final_rn = 1 then prob_up end) as prob_up,
                max(case when final_rn = 1 then actual_close end) as actual_close,
                max(case when final_rn = 1 then outcome end) as outcome,
                max(case when final_rn = 1 then target_up end) as target_up,
                max(case when final_rn = 1 then model_version end) as model_version,
                max(case when final_rn = 1 then model_stage end) as model_stage
            from ranked
            group by round_id, round_cutoff
        )
        select top ({int(limit)})
            observed_at,
            r.round_id,
            r.round_cutoff,
            baseline,
            baseline_source,
            resolution_source,
            initial_prediction,
            initial_prob_up,
            initial_observed_at,
            prediction,
            prob_up,
            actual_close,
            outcome,
            target_up,
            model_version,
            model_stage
        from per_round r
        order by round_cutoff desc
        """
    )


def fetch_dashboard_predictions(limit=5000):
    return _fetch_all(
        f"""
        with quote_pivot as (
            select
                snapshot_id,
                max(case when outcome = N'UP' then best_bid end) as up_best_bid,
                max(case when outcome = N'UP' then best_ask end) as up_best_ask,
                max(case when outcome = N'UP' then midpoint end) as up_midpoint,
                max(case when outcome = N'UP' then spread end) as up_spread,
                max(case when outcome = N'UP' then ask_size end) as up_ask_size,
                max(case when outcome = N'DOWN' then best_bid end) as down_best_bid,
                max(case when outcome = N'DOWN' then best_ask end) as down_best_ask,
                max(case when outcome = N'DOWN' then midpoint end) as down_midpoint,
                max(case when outcome = N'DOWN' then spread end) as down_spread,
                max(case when outcome = N'DOWN' then ask_size end) as down_ask_size
            from dbo.market_quotes
            group by snapshot_id
        )
        select top ({int(limit)})
            cast(ds.id as nvarchar(36)) as snapshot_id,
            ds.observed_at,
            r.round_id,
            cast(r.id as nvarchar(36)) as round_pk,
            r.round_cutoff,
            ds.seconds_to_cutoff,
            ds.seconds_bucket,
            r.status as round_status,
            r.close_source,
            r.resolved_at as round_resolved_at,
            coalesce(ds.baseline, r.baseline) as baseline,
            coalesce(ds.baseline_source, r.baseline_source) as baseline_source,
            ds.btc_price,
            p.model_version,
            p.model_stage,
            p.prediction,
            p.prob_up,
            p.prob_down,
            p.confidence,
            p.edge_up,
            p.edge_down,
            p.recommended_action,
            p.raw as prediction_raw,
            qp.up_best_bid,
            qp.up_best_ask,
            qp.up_midpoint,
            qp.up_spread,
            qp.up_ask_size,
            qp.down_best_bid,
            qp.down_best_ask,
            qp.down_midpoint,
            qp.down_spread,
            qp.down_ask_size,
            rr.actual_close,
            rr.outcome,
            case when rr.outcome = N'UP' then 1 when rr.outcome = N'DOWN' then 0 else null end as target_up
        from dbo.decision_snapshots ds
        join dbo.rounds r on r.id = ds.round_id
        join dbo.predictions_v2 p on p.snapshot_id = ds.id and p.model_stage = N'production'
        left join quote_pivot qp on qp.snapshot_id = ds.id
        left join dbo.round_results rr on rr.round_cutoff = r.round_cutoff
        order by ds.observed_at desc
        """
    )


def fetch_live_signal_history_predictions(model_version, limit=1500):
    return _fetch_all(
        f"""
        with quote_pivot as (
            select
                snapshot_id,
                max(case when outcome = N'UP' then best_bid end) as up_best_bid,
                max(case when outcome = N'UP' then best_ask end) as up_best_ask,
                max(case when outcome = N'UP' then midpoint end) as up_midpoint,
                max(case when outcome = N'UP' then spread end) as up_spread,
                max(case when outcome = N'UP' then ask_size end) as up_ask_size,
                max(case when outcome = N'DOWN' then best_bid end) as down_best_bid,
                max(case when outcome = N'DOWN' then best_ask end) as down_best_ask,
                max(case when outcome = N'DOWN' then midpoint end) as down_midpoint,
                max(case when outcome = N'DOWN' then spread end) as down_spread,
                max(case when outcome = N'DOWN' then ask_size end) as down_ask_size
            from dbo.market_quotes
            group by snapshot_id
        )
        select top ({int(limit)})
            cast(ds.id as nvarchar(36)) as snapshot_id,
            ds.observed_at,
            r.round_id,
            cast(r.id as nvarchar(36)) as round_pk,
            r.round_cutoff,
            ds.seconds_to_cutoff,
            ds.seconds_bucket,
            r.status as round_status,
            r.close_source,
            r.resolved_at as round_resolved_at,
            coalesce(ds.baseline, r.baseline) as baseline,
            coalesce(ds.baseline_source, r.baseline_source) as baseline_source,
            ds.btc_price,
            p.model_version,
            p.model_stage,
            p.prediction,
            p.prob_up,
            p.prob_down,
            p.confidence,
            p.edge_up,
            p.edge_down,
            p.recommended_action,
            p.raw as prediction_raw,
            qp.up_best_bid,
            qp.up_best_ask,
            qp.up_midpoint,
            qp.up_spread,
            qp.up_ask_size,
            qp.down_best_bid,
            qp.down_best_ask,
            qp.down_midpoint,
            qp.down_spread,
            qp.down_ask_size,
            rr.actual_close,
            rr.outcome,
            case when rr.outcome = N'UP' then 1 when rr.outcome = N'DOWN' then 0 else null end as target_up
        from dbo.decision_snapshots ds
        join dbo.rounds r on r.id = ds.round_id
        join dbo.predictions_v2 p on p.snapshot_id = ds.id and p.model_stage = N'production'
        left join quote_pivot qp on qp.snapshot_id = ds.id
        left join dbo.round_results rr on rr.round_cutoff = r.round_cutoff
        where p.model_version = ?
        order by ds.observed_at desc
        """,
        (model_version,),
    )


def fetch_dashboard_signals(limit=1000):
    return _fetch_all(
        f"""
        select top ({int(limit)})
            s.id as signal_id,
            s.observed_at,
            r.round_id,
            r.round_cutoff,
            ds.seconds_to_cutoff,
            ds.seconds_bucket,
            r.status as round_status,
            r.close_source,
            r.resolved_at as round_resolved_at,
            ds.btc_price,
            coalesce(ds.baseline, r.baseline) as baseline,
            coalesce(ds.baseline_source, r.baseline_source) as baseline_source,
            p.model_version,
            p.model_stage,
            p.prediction,
            p.prob_up,
            p.prob_down,
            p.confidence,
            p.edge_up,
            p.edge_down,
            p.recommended_action,
            s.strategy_version,
            s.action,
            s.side,
            s.entry_price,
            s.stake,
            s.model_prob,
            s.edge,
            tr.result,
            tr.pnl,
            tr.roi,
            tr.resolved_at,
            rr.actual_close,
            rr.outcome
        from dbo.signals_v2 s
        join dbo.rounds r on r.id = s.round_id
        join dbo.decision_snapshots ds on ds.id = s.snapshot_id
        join dbo.predictions_v2 p on p.id = s.prediction_id
        left join dbo.trade_results_v2 tr on tr.signal_id = s.id
        left join dbo.round_results rr on rr.round_cutoff = r.round_cutoff
        order by s.observed_at desc
        """
    )
