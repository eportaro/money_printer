import json
import os
from datetime import datetime
from decimal import Decimal
from urllib.parse import urljoin

import requests
from dotenv import load_dotenv

load_dotenv()


def supabase_url():
    return os.getenv("SUPABASE_URL", "").rstrip("/")


def supabase_key():
    return os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_ANON_KEY")


def db_enabled():
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
        "stake": bet.get("stake", 10),
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
