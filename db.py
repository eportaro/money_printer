import json
import os
from datetime import datetime
from decimal import Decimal
from urllib.parse import urljoin

import requests
from dotenv import load_dotenv

load_dotenv()


def db_backend():
    return os.getenv("DB_BACKEND", "supabase").strip().lower()


def sqlserver_enabled():
    return db_backend() == "sqlserver"


def _sqlserver():
    import db_sqlserver

    return db_sqlserver


def supabase_url():
    return os.getenv("SUPABASE_URL", "").rstrip("/")


def supabase_key():
    return os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_ANON_KEY")


def db_enabled():
    if sqlserver_enabled():
        return True
    return bool(supabase_url() and supabase_key())


def _headers(prefer="return=representation"):
    return {
        "apikey": supabase_key(),
        "Authorization": f"Bearer {supabase_key()}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }


def _endpoint(table):
    return urljoin(f"{supabase_url()}/", f"rest/v1/{table}")


def _jsonable(value):
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _post(table, row, params=None, prefer="return=representation"):
    if not db_enabled():
        return None
    resp = requests.post(
        _endpoint(table),
        params=params or {},
        headers=_headers(prefer),
        data=json.dumps(_jsonable(row)),
        timeout=20,
    )
    if not resp.ok:
        raise requests.HTTPError(f"{resp.status_code} {resp.reason}: {resp.text}", response=resp)
    data = resp.json() if resp.text else []
    if isinstance(data, list) and data:
        return data[0].get("id")
    if isinstance(data, dict):
        return data.get("id")
    return None


def _get(table, params=None):
    if not db_enabled():
        return []
    resp = requests.get(
        _endpoint(table),
        params=params or {},
        headers=_headers(),
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def _patch(table, row, params=None, prefer="return=representation"):
    if not db_enabled():
        return None
    resp = requests.patch(
        _endpoint(table),
        params=params or {},
        headers=_headers(prefer),
        data=json.dumps(_jsonable(row)),
        timeout=20,
    )
    if not resp.ok:
        raise requests.HTTPError(f"{resp.status_code} {resp.reason}: {resp.text}", response=resp)
    data = resp.json() if resp.text else []
    if isinstance(data, list) and data:
        return data[0].get("id")
    if isinstance(data, dict):
        return data.get("id")
    return None


def _delete(table, params=None, prefer="return=minimal"):
    if not db_enabled():
        return None
    resp = requests.delete(
        _endpoint(table),
        params=params or {},
        headers=_headers(prefer),
        timeout=20,
    )
    if not resp.ok:
        raise requests.HTTPError(f"{resp.status_code} {resp.reason}: {resp.text}", response=resp)
    return True


def insert_round_snapshot(snapshot):
    row = {
        "observed_at": snapshot.get("observed_at"),
        "round_cutoff": snapshot["round_cutoff"],
        "window_start": snapshot["window_start"],
        "seconds_to_cutoff": snapshot["seconds_to_cutoff"],
        "symbol": snapshot.get("symbol", "BTCUSDT"),
        "btc_price": snapshot["btc_price"],
        "baseline": snapshot.get("baseline"),
        "dist_to_baseline": snapshot.get("dist_to_baseline"),
        "dist_to_baseline_pct": snapshot.get("dist_to_baseline_pct"),
        "source": snapshot.get("source", "binance"),
        "market_condition_id": snapshot.get("market_condition_id"),
        "raw": snapshot.get("raw", {}),
    }
    return _post("round_snapshots", row)


def upsert_market(market):
    if not db_enabled() or not market or not market.get("condition_id"):
        return None
    row = {
        "event_id": market.get("event_id"),
        "market_id": market.get("market_id"),
        "condition_id": market.get("condition_id"),
        "question": market.get("question"),
        "slug": market.get("slug"),
        "event_slug": market.get("event_slug"),
        "start_time": market.get("start_time"),
        "end_time": market.get("end_time"),
        "baseline": market.get("baseline"),
        "token_up": market.get("token_up"),
        "token_down": market.get("token_down"),
        "outcomes": market.get("outcomes"),
        "raw": market.get("raw", {}),
        "active": market.get("active", True),
    }
    return _post(
        "polymarket_markets",
        row,
        params={"on_conflict": "condition_id"},
        prefer="resolution=merge-duplicates,return=representation",
    )


def insert_quote(quote):
    row = {
        "observed_at": quote.get("observed_at"),
        "round_cutoff": quote.get("round_cutoff"),
        "market_condition_id": quote.get("market_condition_id"),
        "token_id": quote["token_id"],
        "outcome": quote["outcome"],
        "best_bid": quote.get("best_bid"),
        "best_ask": quote.get("best_ask"),
        "midpoint": quote.get("midpoint"),
        "spread": quote.get("spread"),
        "last_trade_price": quote.get("last_trade_price"),
        "bid_size": quote.get("bid_size"),
        "ask_size": quote.get("ask_size"),
        "book_hash": quote.get("book_hash"),
        "raw": quote.get("raw", {}),
    }
    return _post("polymarket_quotes", row)


def insert_prediction(prediction):
    row = {
        "observed_at": prediction.get("observed_at"),
        "round_cutoff": prediction["round_cutoff"],
        "model_version": prediction.get("model_version", "local-hgb-v1"),
        "prediction": prediction["prediction"],
        "prob_up": prediction["prob_up"],
        "prob_down": prediction["prob_down"],
        "confidence": prediction["confidence"],
        "edge_up": prediction.get("edge_up"),
        "edge_down": prediction.get("edge_down"),
        "recommended_action": prediction.get("recommended_action"),
        "feature_values": prediction.get("feature_values", {}),
        "source_snapshot_id": prediction.get("source_snapshot_id"),
        "raw": prediction.get("raw", {}),
    }
    return _post("model_predictions", row)


def upsert_round_result(result):
    if sqlserver_enabled():
        return _sqlserver().upsert_round_result(result)
    row = {
        "round_cutoff": result["round_cutoff"],
        "resolved_at": result.get("resolved_at") or datetime.utcnow().isoformat(),
        "baseline": result.get("baseline"),
        "actual_close": result["actual_close"],
        "outcome": result["outcome"],
        "raw": result.get("raw", {}),
    }
    return _post(
        "round_results",
        row,
        params={"on_conflict": "round_cutoff"},
        prefer="resolution=merge-duplicates,return=representation",
    )


def insert_simulated_bet(bet):
    row = {
        "observed_at": bet.get("observed_at"),
        "round_cutoff": bet["round_cutoff"],
        "side": bet["side"],
        "entry_price": bet["entry_price"],
        "stake": bet.get("stake", 1),
        "model_prob": bet.get("model_prob"),
        "edge": bet.get("edge"),
        "result": bet.get("result", "OPEN"),
        "pnl": bet.get("pnl"),
        "model_prediction_id": bet.get("model_prediction_id"),
        "raw": bet.get("raw", {}),
    }
    return _post("simulated_bets", row)


def update_simulated_bet(bet_id, updates):
    return _patch("simulated_bets", updates, params={"id": f"eq.{bet_id}"})


def fetch_open_simulated_bets(limit=500):
    return _get(
        "simulated_bets",
        {
            "select": "*",
            "result": "eq.OPEN",
            "order": "observed_at.asc",
            "limit": limit,
        },
    )


def fetch_recent_simulated_bets(limit=25):
    rows = _get(
        "simulated_bets",
        {
            "select": "*",
            "order": "observed_at.desc",
            "limit": limit,
        },
    )
    return json.loads(json.dumps(rows, default=str))


def fetch_recent_snapshots(limit=1000):
    rows = _get(
        "round_snapshots",
        {
            "select": "*",
            "order": "observed_at.desc",
            "limit": limit,
        },
    )
    return json.loads(json.dumps(rows, default=str))


def fetch_recent_round_results(limit=200):
    if sqlserver_enabled():
        return _sqlserver().fetch_recent_round_results(limit)
    rows = _get(
        "round_results",
        {
            "select": "*",
            "order": "round_cutoff.desc",
            "limit": limit,
        },
    )
    return json.loads(json.dumps(rows, default=str))


def fetch_recent_predictions(limit=50):
    rows = _get(
        "model_predictions",
        {
            "select": "*",
            "order": "observed_at.desc",
            "limit": limit,
        },
    )
    return json.loads(json.dumps(rows, default=str))


def delete_before(table, column, cutoff_value):
    return _delete(table, params={column: f"lt.{cutoff_value}"})


def count_rows(table):
    if sqlserver_enabled():
        return _sqlserver().count_rows(table)
    if not db_enabled():
        return None
    resp = requests.get(
        _endpoint(table),
        params={"select": "id", "limit": 1},
        headers={
            **_headers(),
            "Prefer": "count=exact",
            "Range": "0-0",
        },
        timeout=20,
    )
    resp.raise_for_status()
    content_range = resp.headers.get("Content-Range", "")
    if "/" in content_range:
        total = content_range.rsplit("/", 1)[-1]
        return None if total == "*" else int(total)
    return None


# --- MLOps v2 helpers -----------------------------------------------------

def upsert_round_v2(round_data):
    if sqlserver_enabled():
        return _sqlserver().upsert_round_v2(round_data)
    """Create/update the canonical Polymarket round and return its UUID."""
    row = {
        "round_id": round_data["round_id"],
        "event_slug": round_data.get("event_slug"),
        "condition_id": round_data.get("condition_id"),
        "window_start": round_data["window_start"],
        "round_cutoff": round_data["round_cutoff"],
        "baseline": round_data.get("baseline"),
        "baseline_source": round_data.get("baseline_source", "binance_prev_close"),
        "resolution_source": round_data.get("resolution_source"),
        "close_source": round_data.get("close_source"),
        "status": round_data.get("status", "open"),
        "resolved_at": round_data.get("resolved_at"),
        "raw": round_data.get("raw", {}),
        "updated_at": datetime.utcnow().isoformat(),
    }
    return _post(
        "rounds",
        row,
        params={"on_conflict": "round_cutoff"},
        prefer="resolution=merge-duplicates,return=representation",
    )


def update_round_v2(round_id, updates):
    if sqlserver_enabled():
        return _sqlserver().update_round_v2(round_id, updates)
    updates = {**updates, "updated_at": datetime.utcnow().isoformat()}
    return _patch("rounds", updates, params={"id": f"eq.{round_id}"})


def update_round_baseline_snapshots(round_id, baseline, baseline_source):
    if sqlserver_enabled():
        return _sqlserver().update_round_baseline_snapshots(round_id, baseline, baseline_source)
    return _patch(
        "decision_snapshots",
        {
            "baseline": baseline,
            "baseline_source": baseline_source,
        },
        params={"round_id": f"eq.{round_id}"},
    )


def insert_reference_price(row):
    if sqlserver_enabled():
        return _sqlserver().insert_reference_price(row)
    return _post(
        "reference_prices",
        {
            "round_id": row.get("round_id"),
            "observed_at": row.get("observed_at"),
            "source": row.get("source", "binance"),
            "symbol": row.get("symbol", "BTCUSDT"),
            "price": row["price"],
            "seconds_to_cutoff": row.get("seconds_to_cutoff"),
            "purpose": row["purpose"],
            "raw": row.get("raw", {}),
        },
        prefer="return=minimal",
    )


def insert_decision_snapshot(row):
    if sqlserver_enabled():
        return _sqlserver().insert_decision_snapshot(row)
    return _post(
        "decision_snapshots",
        {
            "round_id": row["round_id"],
            "observed_at": row.get("observed_at"),
            "seconds_to_cutoff": row["seconds_to_cutoff"],
            "seconds_bucket": row["seconds_bucket"],
            "capture_reason": row.get("capture_reason", "scheduled"),
            "btc_price": row["btc_price"],
            "baseline": row.get("baseline"),
            "dist_to_baseline": row.get("dist_to_baseline"),
            "dist_to_baseline_pct": row.get("dist_to_baseline_pct"),
            "baseline_source": row.get("baseline_source"),
            "collector_version": row.get("collector_version", "collector-v2"),
            "raw": row.get("raw", {}),
        },
    )


def insert_market_quote_v2(row):
    if sqlserver_enabled():
        return _sqlserver().insert_market_quote_v2(row)
    return _post(
        "market_quotes",
        {
            "snapshot_id": row["snapshot_id"],
            "round_id": row["round_id"],
            "observed_at": row.get("observed_at"),
            "outcome": row["outcome"],
            "token_id": row.get("token_id"),
            "best_bid": row.get("best_bid"),
            "best_ask": row.get("best_ask"),
            "midpoint": row.get("midpoint"),
            "spread": row.get("spread"),
            "bid_size": row.get("bid_size"),
            "ask_size": row.get("ask_size"),
            "last_trade_price": row.get("last_trade_price"),
            "book_hash": row.get("book_hash"),
            "raw": row.get("raw", {}),
        },
        params={"on_conflict": "snapshot_id,outcome"},
        prefer="resolution=merge-duplicates,return=minimal",
    )


def upsert_feature_snapshot(row):
    if sqlserver_enabled():
        return _sqlserver().upsert_feature_snapshot(row)
    return _post(
        "feature_snapshots",
        {
            "snapshot_id": row["snapshot_id"],
            "round_id": row["round_id"],
            "observed_at": row.get("observed_at"),
            "feature_set_version": row["feature_set_version"],
            "features": row.get("features", {}),
        },
        params={"on_conflict": "snapshot_id,feature_set_version"},
        prefer="resolution=merge-duplicates,return=minimal",
    )


def insert_prediction_v2(row):
    if sqlserver_enabled():
        return _sqlserver().insert_prediction_v2(row)
    return _post(
        "predictions_v2",
        {
            "snapshot_id": row["snapshot_id"],
            "round_id": row["round_id"],
            "observed_at": row.get("observed_at"),
            "model_version": row["model_version"],
            "model_stage": row.get("model_stage", "production"),
            "prediction": row["prediction"],
            "prob_up": row["prob_up"],
            "prob_down": row["prob_down"],
            "confidence": row["confidence"],
            "edge_up": row.get("edge_up"),
            "edge_down": row.get("edge_down"),
            "recommended_action": row.get("recommended_action"),
            "feature_set_version": row.get("feature_set_version"),
            "raw": row.get("raw", {}),
        },
        params={"on_conflict": "snapshot_id,model_version,model_stage"},
        prefer="resolution=merge-duplicates,return=representation",
    )


def insert_signal_v2(row):
    if sqlserver_enabled():
        return _sqlserver().insert_signal_v2(row)
    return _post(
        "signals_v2",
        {
            "prediction_id": row["prediction_id"],
            "snapshot_id": row["snapshot_id"],
            "round_id": row["round_id"],
            "observed_at": row.get("observed_at"),
            "strategy_version": row["strategy_version"],
            "action": row["action"],
            "side": row.get("side"),
            "entry_price": row.get("entry_price"),
            "model_prob": row.get("model_prob"),
            "edge": row.get("edge"),
            "stake": row.get("stake", 1),
            "raw": row.get("raw", {}),
        },
        params={"on_conflict": "prediction_id,strategy_version"},
        prefer="resolution=merge-duplicates,return=representation",
    )


def upsert_trade_result_v2(row):
    if sqlserver_enabled():
        return _sqlserver().upsert_trade_result_v2(row)
    return _post(
        "trade_results_v2",
        {
            "signal_id": row["signal_id"],
            "round_id": row["round_id"],
            "result": row["result"],
            "pnl": row.get("pnl"),
            "roi": row.get("roi"),
            "resolved_at": row.get("resolved_at") or datetime.utcnow().isoformat(),
            "raw": row.get("raw", {}),
        },
        params={"on_conflict": "signal_id"},
        prefer="resolution=merge-duplicates,return=minimal",
    )


def insert_dataset_version(row):
    if sqlserver_enabled():
        return _sqlserver().insert_dataset_version(row)
    return _post(
        "dataset_versions",
        row,
        params={"on_conflict": "dataset_version"},
        prefer="resolution=merge-duplicates,return=minimal",
    )


def insert_model_run(row):
    if sqlserver_enabled():
        return _sqlserver().insert_model_run(row)
    return _post(
        "model_runs",
        row,
        params={"on_conflict": "run_id"},
        prefer="resolution=merge-duplicates,return=minimal",
    )


def promote_model_version(model_version):
    if sqlserver_enabled():
        return _sqlserver().promote_model_version(model_version)
    return None


def insert_strategy_backtest_run(row):
    if sqlserver_enabled():
        return _sqlserver().insert_strategy_backtest_run(row)
    return None


def update_strategy_backtest_run(run_id, updates):
    if sqlserver_enabled():
        return _sqlserver().update_strategy_backtest_run(run_id, updates)
    return None


def insert_strategy_backtest_signal(row):
    if sqlserver_enabled():
        return _sqlserver().insert_strategy_backtest_signal(row)
    return None


def fetch_latest_strategy_backtest_run(model_version, strategy_set_version=None):
    if sqlserver_enabled():
        return _sqlserver().fetch_latest_strategy_backtest_run(model_version, strategy_set_version)
    return None


def fetch_strategy_backtest_signals(model_version, limit=5000, strategy_set_version=None):
    if sqlserver_enabled():
        return _sqlserver().fetch_strategy_backtest_signals(model_version, limit, strategy_set_version)
    return []


def fetch_round_v2_by_cutoff(round_cutoff):
    rows = _get(
        "rounds",
        {
            "select": "*",
            "round_cutoff": f"eq.{int(round_cutoff)}",
            "limit": 1,
        },
    )
    return rows[0] if rows else None


def fetch_captured_scheduled_buckets(round_id):
    if sqlserver_enabled():
        return _sqlserver().fetch_captured_scheduled_buckets(round_id)
    rows = _get(
        "decision_snapshots",
        {
            "select": "seconds_bucket",
            "round_id": f"eq.{round_id}",
            "capture_reason": "eq.scheduled",
            "limit": 100,
        },
    )
    return {int(row["seconds_bucket"]) for row in rows}


def fetch_unresolved_rounds_v2(limit=200):
    if sqlserver_enabled():
        return _sqlserver().fetch_unresolved_rounds_v2(limit)
    return _get(
        "rounds",
        {
            "select": "*",
            "status": "eq.open",
            "order": "round_cutoff.asc",
            "limit": limit,
        },
    )


def fetch_open_signals_v2(limit=500):
    if sqlserver_enabled():
        return _sqlserver().fetch_open_signals_v2(limit)
    return _get(
        "strategy_performance_v2",
        {
            "select": "*",
            "result": "is.null",
            "order": "observed_at.asc",
            "limit": limit,
        },
    )


def fetch_recent_signals_v2(limit=25):
    if sqlserver_enabled():
        return _sqlserver().fetch_recent_signals_v2(limit)
    rows = _get(
        "strategy_performance_v2",
        {
            "select": "*",
            "order": "observed_at.desc",
            "limit": limit,
        },
    )
    return json.loads(json.dumps(rows, default=str))


def fetch_recent_rounds_v2(limit=50):
    if sqlserver_enabled():
        return _sqlserver().fetch_recent_rounds_v2(limit)
    rows = _get(
        "training_decision_snapshots",
        {
            "select": "observed_at,round_id,round_cutoff,baseline,baseline_source,resolution_source,prediction,prob_up,actual_close,outcome,target_up,model_version,model_stage",
            "order": "observed_at.desc",
            "limit": max(limit * 10, 100),
        },
    )
    return json.loads(json.dumps(rows, default=str))


def fetch_training_decision_snapshots(limit=50000):
    if sqlserver_enabled():
        return _sqlserver().fetch_training_decision_snapshots(limit)
    return _get(
        "training_decision_snapshots",
        {
            "select": "*",
            "target_up": "not.is.null",
            "order": "observed_at.asc",
            "limit": limit,
        },
    )


def fetch_dashboard_predictions(limit=5000):
    if sqlserver_enabled():
        return _sqlserver().fetch_dashboard_predictions(limit)
    return fetch_training_decision_snapshots(limit)


def fetch_live_signal_history_predictions(model_version, limit=1500):
    if sqlserver_enabled():
        return _sqlserver().fetch_live_signal_history_predictions(model_version, limit)
    rows = fetch_dashboard_predictions(limit)
    return [row for row in rows if row.get("model_version") == model_version]


def fetch_dashboard_signals(limit=1000):
    if sqlserver_enabled():
        return _sqlserver().fetch_dashboard_signals(limit)
    return fetch_recent_signals_v2(limit)


def fetch_model_performance_v2():
    if sqlserver_enabled():
        return _sqlserver().fetch_model_performance_v2()
    return _get(
        "model_performance_by_version",
        {
            "select": "*",
            "order": "model_stage.asc,model_version.asc",
        },
    )


def fetch_dataset_summary_v2():
    if sqlserver_enabled():
        return _sqlserver().fetch_dataset_summary_v2()
    rows = _get(
        "training_decision_snapshots",
        {
            "select": "observed_at,round_cutoff,prediction,outcome,target_up,seconds_to_cutoff",
            "target_up": "not.is.null",
            "order": "observed_at.asc",
            "limit": 50000,
        },
    )
    if not rows:
        return {}
    total = len(rows)
    correct = [1 for row in rows if row.get("prediction") == row.get("outcome")]
    by_round = {}
    for row in rows:
        by_round.setdefault(int(row["round_cutoff"]), []).append(row)
    first_hits = []
    last_hits = []
    for round_rows in by_round.values():
        first = sorted(round_rows, key=lambda row: (-int(row.get("seconds_to_cutoff") or 0), row.get("observed_at") or ""))[0]
        last = sorted(round_rows, key=lambda row: (int(row.get("seconds_to_cutoff") or 0), row.get("observed_at") or ""), reverse=False)[0]
        first_hits.append(1 if first.get("prediction") == first.get("outcome") else 0)
        last_hits.append(1 if last.get("prediction") == last.get("outcome") else 0)
    return {
        "modeling_rows": total,
        "unique_resolved_rounds": len(by_round),
        "row_accuracy_pct": round(sum(correct) / total * 100, 2) if total else None,
        "first_prediction_round_accuracy_pct": round(sum(first_hits) / len(first_hits) * 100, 2) if first_hits else None,
        "last_prediction_round_accuracy_pct": round(sum(last_hits) / len(last_hits) * 100, 2) if last_hits else None,
        "start_time": rows[0].get("observed_at"),
        "end_time": rows[-1].get("observed_at"),
    }
