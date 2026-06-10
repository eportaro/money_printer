"""Persistence facade.

The bot persists exclusively to SQL Server (db_sqlserver.py). This module keeps
the `db.` call sites stable and is the single place to swap backends if that is
ever needed again. The old Supabase REST backend and the v1 tables
(round_snapshots / simulated_bets / model_predictions / polymarket_markets)
were removed; SQL Server schema 004 (rounds/decision_snapshots/.../v2) is the
only data model.
"""

import db_sqlserver as _sql


def db_backend():
    return "sqlserver"


def sqlserver_enabled():
    return True


def db_enabled():
    return _sql.enabled()


# ── generic ──
count_rows = _sql.count_rows

# ── rounds / snapshots / quotes / features (v2) ──
upsert_round_v2 = _sql.upsert_round_v2
update_round_v2 = _sql.update_round_v2
update_round_baseline_snapshots = _sql.update_round_baseline_snapshots
insert_reference_price = _sql.insert_reference_price
insert_decision_snapshot = _sql.insert_decision_snapshot
insert_market_quote_v2 = _sql.insert_market_quote_v2
upsert_feature_snapshot = _sql.upsert_feature_snapshot
fetch_captured_scheduled_buckets = _sql.fetch_captured_scheduled_buckets
fetch_unresolved_rounds_v2 = _sql.fetch_unresolved_rounds_v2
fetch_recent_rounds_v2 = _sql.fetch_recent_rounds_v2

# ── predictions / signals / results (v2) ──
insert_prediction_v2 = _sql.insert_prediction_v2
insert_signal_v2 = _sql.insert_signal_v2
upsert_trade_result_v2 = _sql.upsert_trade_result_v2
upsert_round_result = _sql.upsert_round_result
fetch_recent_round_results = _sql.fetch_recent_round_results
fetch_open_signals_v2 = _sql.fetch_open_signals_v2
fetch_recent_signals_v2 = _sql.fetch_recent_signals_v2

# ── dashboard ──
fetch_dashboard_predictions = _sql.fetch_dashboard_predictions
fetch_live_signal_history_predictions = _sql.fetch_live_signal_history_predictions
fetch_dashboard_signals = _sql.fetch_dashboard_signals
fetch_model_performance_v2 = _sql.fetch_model_performance_v2
fetch_dataset_summary_v2 = _sql.fetch_dataset_summary_v2

# ── MLOps: datasets / model runs / strategy backtests ──
fetch_training_decision_snapshots = _sql.fetch_training_decision_snapshots
insert_dataset_version = _sql.insert_dataset_version
insert_model_run = _sql.insert_model_run
promote_model_version = _sql.promote_model_version
insert_strategy_backtest_run = _sql.insert_strategy_backtest_run
update_strategy_backtest_run = _sql.update_strategy_backtest_run
insert_strategy_backtest_signal = _sql.insert_strategy_backtest_signal
fetch_latest_strategy_backtest_run = _sql.fetch_latest_strategy_backtest_run
fetch_strategy_backtest_signals = _sql.fetch_strategy_backtest_signals
