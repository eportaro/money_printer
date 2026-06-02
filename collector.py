import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

import db
from features import compute_all_features
from model_runtime import (
    base_prediction,
    edge_from_probabilities,
    load_base_model,
    load_market_model,
    select_prediction,
)
from market_config import WINDOW_SECONDS, next_cutoff as configured_next_cutoff, round_id as configured_round_id, slug_candidates
from polymarket import PolymarketError, discover_btc_market, fetch_event_prices, fetch_market_quotes
from price_feed import active_symbol, fetch_recent_candles, oracle_price_at, source_label

load_dotenv()

SYMBOL = active_symbol()
PRICE_SOURCE_LABEL = source_label()
ACTIVE_MODEL = os.getenv("ACTIVE_MODEL", "market-aware-v1")
COLLECTOR_MODE = os.getenv("COLLECTOR_MODE", "legacy").lower()
COLLECTOR_VERSION = os.getenv("COLLECTOR_VERSION", "collector-v2")
FEATURE_SET_VERSION = os.getenv("FEATURE_SET_VERSION", "market-features-v1")
STRATEGY_VERSION = os.getenv("STRATEGY_VERSION", "edge-v1")
MODEL_STAGE = os.getenv("MODEL_STAGE", "production")
RESOLUTION_SOURCE = os.getenv("RESOLUTION_SOURCE", "https://data.chain.link/streams/btc-usd")
CLOSE_SOURCE = os.getenv("CLOSE_SOURCE", PRICE_SOURCE_LABEL)
RESOLUTION_CHECK_INTERVAL_SECONDS = int(os.getenv("RESOLUTION_CHECK_INTERVAL_SECONDS", "5"))
POLYMARKET_FINAL_PRICE_WAIT_SECONDS = int(os.getenv("POLYMARKET_FINAL_PRICE_WAIT_SECONDS", "300"))
POLYMARKET_MARKET_REFRESH_SECONDS = int(os.getenv("POLYMARKET_MARKET_REFRESH_SECONDS", "30"))
REQUIRE_EXACT_BASELINE_FOR_ACTION = os.getenv("REQUIRE_EXACT_BASELINE_FOR_ACTION", "true").lower() == "true"
ALLOW_PRICE_FEED_BASELINE_FOR_ACTION = os.getenv("ALLOW_PRICE_FEED_BASELINE_FOR_ACTION", "false").lower() == "true"
REQUIRE_EXACT_CLOSE_FOR_RESOLUTION = os.getenv("REQUIRE_EXACT_CLOSE_FOR_RESOLUTION", "true").lower() == "true"
PROVISIONAL_CLOSE_AFTER_SECONDS = int(os.getenv("PROVISIONAL_CLOSE_AFTER_SECONDS", "20"))
PROXY_BASELINE_MIN_ELAPSED_SECONDS = int(os.getenv("PROXY_BASELINE_MIN_ELAPSED_SECONDS", "60"))
BASELINE_MAX_FEED_DELTA_ABS = float(os.getenv("BASELINE_MAX_FEED_DELTA_ABS", "75"))
BASELINE_MAX_FEED_DELTA_PCT = float(os.getenv("BASELINE_MAX_FEED_DELTA_PCT", "0.12"))
# Looser guard for the resolution-time exact baseline/close overwrite: its only job
# is to block gross upstream corruption (e.g. a scraped wrong-event price ~$1.9k off),
# not to enforce tightness, so it allows legit gamma refinements (<~$150) through.
EXACT_BASELINE_MAX_DELTA_ABS = float(os.getenv("EXACT_BASELINE_MAX_DELTA_ABS", "400"))
EXACT_BASELINE_MAX_DELTA_PCT = float(os.getenv("EXACT_BASELINE_MAX_DELTA_PCT", "0.6"))
# How long to keep a round "provisional" waiting for an exact Polymarket close before
# finalizing it with the (reliable) price-feed close, so provisional rounds don't pile up.
EXACT_RECONCILE_GIVEUP_SECONDS = int(os.getenv("EXACT_RECONCILE_GIVEUP_SECONDS", "1800"))
EXACT_BASELINE_SOURCES = {"polymarket_gamma_event_metadata", "manual_sync"}
# Pyth oracle baseline (the source Polymarket settles on) for the live round.
ORACLE_BASELINE_SOURCE = "pyth_btc_usd_window_open"
ALLOW_ORACLE_BASELINE_FOR_ACTION = os.getenv("ALLOW_ORACLE_BASELINE_FOR_ACTION", "true").lower() == "true"

SCHEDULED_BUCKET_CACHE = {}
LAST_RESOLUTION_CHECK = 0
LAST_STRATEGY_LAB_REFRESH = 0


def get_next_cutoff():
    return configured_next_cutoff()


def expected_event_slugs(next_cutoff=None):
    next_cutoff = next_cutoff or get_next_cutoff()
    return set(slug_candidates(next_cutoff - WINDOW_SECONDS))


def load_model():
    return {
        "base": load_base_model(),
        "market": load_market_model(),
    }


def configured_decision_buckets():
    default = "895,840,720,600,480,360,240,180,120,90,60,30,15,5"
    raw = os.getenv("DECISION_BUCKETS", default)
    buckets = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            buckets.append(int(item))
        except ValueError:
            continue
    return sorted(set(buckets), reverse=True)


def nearest_decision_bucket(seconds_to_cutoff):
    """Return the largest scheduled bucket <= seconds_to_cutoff for context."""
    for b in configured_decision_buckets():
        if seconds_to_cutoff >= b:
            return b
    return configured_decision_buckets()[-1]


def trim_quote_raw(quote):
    top_n = int(os.getenv("STORE_ORDERBOOK_TOP_N", "3"))
    raw = dict(quote.get("raw") or {})
    if "bids_top" in raw:
        raw["bids_top"] = raw["bids_top"][:top_n]
    if "asks_top" in raw:
        raw["asks_top"] = raw["asks_top"][:top_n]
    return raw


def v2_round_id(window_start):
    return configured_round_id(window_start)


def scheduled_bucket_to_capture(round_id, seconds_to_cutoff):
    if round_id not in SCHEDULED_BUCKET_CACHE:
        SCHEDULED_BUCKET_CACHE[round_id] = db.fetch_captured_scheduled_buckets(round_id)
    captured = SCHEDULED_BUCKET_CACHE[round_id]
    for bucket in sorted(configured_decision_buckets()):
        if seconds_to_cutoff <= bucket and bucket not in captured:
            return bucket
    return None


def adaptive_capture_reason(seconds_to_cutoff, dist_pct, edge_up, edge_down):
    if os.getenv("ADAPTIVE_CAPTURE_ENABLED", "true").lower() != "true":
        return None
    max_edge = max([v for v in [edge_up, edge_down] if v is not None], default=None)
    edge_threshold = float(os.getenv("ADAPTIVE_EDGE_THRESHOLD", "0.07"))
    near_baseline_pct = float(os.getenv("ADAPTIVE_NEAR_BASELINE_PCT", "0.03"))
    if max_edge is not None and max_edge >= edge_threshold:
        return "adaptive_edge"
    if dist_pct is not None and abs(dist_pct) <= near_baseline_pct:
        return "adaptive_near_baseline"
    if seconds_to_cutoff <= 30:
        return "adaptive_late_round"
    return None


def adaptive_bucket(seconds_to_cutoff):
    interval = max(1, int(os.getenv("COLLECTOR_INTERVAL_SECONDS", "5")))
    return max(0, int(seconds_to_cutoff // interval * interval))


def infer_price_feed_baseline(df, next_cutoff):
    window_start = next_cutoff - WINDOW_SECONDS
    prev_candle_ts = (window_start - 60) * 1000
    prev_candle = df[df["timestamp"] == prev_candle_ts]
    if not prev_candle.empty:
        return float(prev_candle.iloc[0]["close"])

    window_candles = df[df["timestamp"] >= window_start * 1000]
    if not window_candles.empty:
        return float(window_candles.iloc[0]["open"])
    return float(df.iloc[-1]["close"])


def baseline_from_market_or_feed(market, df, next_cutoff):
    feed_baseline = infer_price_feed_baseline(df, next_cutoff)
    feed_source = f"{PRICE_SOURCE_LABEL}_prev_close"

    # 1) Exact Polymarket priceToBeat from gamma (best, but usually only populated
    #    at/after the round closes; absent during the live round).
    if market and market.get("baseline") is not None:
        market_baseline = float(market["baseline"])
        market_source = market.get("baseline_source") or "polymarket_gamma_event_metadata"
        delta = abs(market_baseline - feed_baseline)
        delta_pct = (delta / feed_baseline * 100) if feed_baseline else 0
        if delta <= BASELINE_MAX_FEED_DELTA_ABS and delta_pct <= BASELINE_MAX_FEED_DELTA_PCT:
            return market_baseline, market_source
        print(
            f"Rejected Polymarket baseline {market_baseline:.2f} from {market_source}; "
            f"feed baseline is {feed_baseline:.2f} (delta {delta:.2f}, {delta_pct:.3f}%)."
        )

    # 2) Live round: read the Pyth oracle (what Polymarket settles on) at the window
    #    open. Matches the real priceToBeat within a few dollars vs ~$25 for the CEX proxy.
    if os.getenv("USE_ORACLE_BASELINE", "true").lower() == "true":
        oracle_baseline = oracle_price_at(next_cutoff - WINDOW_SECONDS)
        if oracle_baseline is not None:
            return oracle_baseline, ORACLE_BASELINE_SOURCE

    # 3) Last resort: price-feed previous close.
    return feed_baseline, feed_source


def baseline_is_exact(source):
    return source in EXACT_BASELINE_SOURCES


def baseline_allows_action(source):
    if baseline_is_exact(source):
        return True
    if ALLOW_ORACLE_BASELINE_FOR_ACTION and source == ORACLE_BASELINE_SOURCE:
        return True
    return ALLOW_PRICE_FEED_BASELINE_FOR_ACTION and source == f"{PRICE_SOURCE_LABEL}_prev_close"


def update_exact_baseline_if_available(round_row, exact_prices):
    exact_baseline = exact_prices.get("price_to_beat")
    if exact_baseline is None:
        return None, round_row.get("baseline_source")
    baseline = float(exact_baseline)
    current = round_row.get("baseline")
    # Sanity guard: never overwrite a captured baseline with an exact value that is
    # implausibly far from it. Protects round results + snapshots from bad upstream data.
    if current is not None:
        current_val = float(current)
        delta = abs(baseline - current_val)
        delta_pct = (delta / current_val * 100) if current_val else 0.0
        if not (delta <= EXACT_BASELINE_MAX_DELTA_ABS and delta_pct <= EXACT_BASELINE_MAX_DELTA_PCT):
            print(
                f"Rejected exact baseline {baseline:.2f} for round {round_row.get('round_id')}; "
                f"captured baseline {current_val:.2f} (delta {delta:.2f}, {delta_pct:.3f}%)."
            )
            return None, round_row.get("baseline_source")
    source = "polymarket_gamma_event_metadata"
    current_source = round_row.get("baseline_source")
    if current is None or abs(float(current) - baseline) > 0.000001 or current_source != source:
        db.update_round_v2(
            round_row["id"],
            {
                "baseline": baseline,
                "baseline_source": source,
                "raw": {
                    **(round_row.get("raw") or {}),
                    "event_metadata": exact_prices.get("raw"),
                    "baseline_backfilled_at": datetime.now(timezone.utc).isoformat(),
                },
            },
        )
        if hasattr(db, "update_round_baseline_snapshots"):
            db.update_round_baseline_snapshots(round_row["id"], baseline, source)
    return baseline, source


def exact_prices_for_round(round_row):
    slug = round_row.get("event_slug")
    if not slug:
        raw_market = (round_row.get("raw") or {}).get("market") or {}
        slug = raw_market.get("event_slug")
    if not slug:
        return {}
    try:
        # gamma-only: the HTML page scraper can return a wrong-event priceToBeat.
        return fetch_event_prices(slug, include_page=False)
    except Exception as exc:
        print(f"Polymarket event metadata fetch failed for {slug}: {exc}")
        return {"event_slug": slug}


def compute_model_prediction(model, df, baseline=None, current_price=None):
    df_feat = compute_all_features(df)
    if baseline is not None and current_price is not None and "dist_to_window_open" in df_feat.columns:
        df_feat.loc[df_feat.index[-1], "dist_to_window_open"] = (current_price - baseline) / (baseline + 1e-10) * 100
    return base_prediction(model["base"], df_feat)


def edge_from_quotes(prediction, quotes):
    threshold = float(os.getenv("EDGE_THRESHOLD", "0.03"))
    edge = edge_from_probabilities(prediction["prob_up"], quotes, threshold)
    return edge["edge_up"], edge["edge_down"], edge["recommended_action"]


def quote_by_side(quotes, side):
    return next((q for q in quotes if q["outcome"] == side), None)


def simulated_pnl(stake, entry_price, won):
    if not won:
        return -float(stake)
    if not entry_price:
        return 0.0
    return float(stake) * ((1.0 / float(entry_price)) - 1.0)


def resolve_open_simulated_bets():
    if not db.db_enabled():
        return {"checked": 0, "resolved": 0}

    now = int(time.time())
    open_bets = db.fetch_open_simulated_bets()
    resolved = 0
    if not open_bets:
        return {"checked": 0, "resolved": 0}

    df = fetch_recent_candles(20)
    for bet in open_bets:
        cutoff = int(bet["round_cutoff"])
        if now <= cutoff + 15:
            continue

        matching = df[df["timestamp"] >= cutoff * 1000]
        if matching.empty:
            continue

        actual_close = float(matching.iloc[0]["open"])
        raw = bet.get("raw") or {}
        baseline = raw.get("baseline")
        if baseline is None:
            baseline = raw.get("snapshot", {}).get("baseline")
        if baseline is None:
            continue

        baseline = float(baseline)
        outcome = "UP" if actual_close > baseline else "DOWN" if actual_close < baseline else "TIE"
        won = bet["side"] == outcome
        pnl = simulated_pnl(bet.get("stake", 10), bet.get("entry_price"), won)
        db.update_simulated_bet(
            bet["id"],
            {
                "result": "WIN" if won else "LOSS",
                "pnl": round(pnl, 4),
                "raw": {
                    **raw,
                    "resolved": {
                        "actual_close": actual_close,
                        "baseline": baseline,
                        "outcome": outcome,
                        "resolved_at": datetime.now(timezone.utc).isoformat(),
                    },
                },
            },
        )
        db.upsert_round_result(
            {
                "round_cutoff": cutoff,
                "baseline": baseline,
                "actual_close": actual_close,
                "outcome": outcome,
                "raw": {"source": "collector", "bet_id": bet["id"]},
            }
        )
        resolved += 1

    return {"checked": len(open_bets), "resolved": resolved}


def resolve_recent_round_results():
    if not db.db_enabled():
        return {"checked": 0, "resolved": 0}

    now = int(time.time())
    snapshots = db.fetch_recent_snapshots(1000)
    if not snapshots:
        return {"checked": 0, "resolved": 0}

    existing = {int(r["round_cutoff"]) for r in db.fetch_recent_round_results(500)}
    by_cutoff = {}
    for snapshot in snapshots:
        cutoff = int(snapshot["round_cutoff"])
        if cutoff in existing or now <= cutoff + 15:
            continue
        by_cutoff.setdefault(cutoff, []).append(snapshot)

    if not by_cutoff:
        return {"checked": 0, "resolved": 0}

    df = fetch_recent_candles(500)
    resolved = 0
    for cutoff, rows in by_cutoff.items():
        rows.sort(key=lambda row: row["observed_at"])
        baseline = next((row.get("baseline") for row in rows if row.get("baseline") is not None), None)
        if baseline is None:
            continue

        matching = df[df["timestamp"] >= cutoff * 1000]
        if matching.empty:
            continue

        baseline = float(baseline)
        actual_close = float(matching.iloc[0]["open"])
        outcome = "UP" if actual_close > baseline else "DOWN" if actual_close < baseline else "TIE"
        db.upsert_round_result(
            {
                "round_cutoff": cutoff,
                "baseline": baseline,
                "actual_close": actual_close,
                "outcome": outcome,
                "raw": {
                    "source": "collector_snapshots",
                    "snapshots_seen": len(rows),
                    "first_snapshot_id": rows[0].get("id"),
                    "last_snapshot_id": rows[-1].get("id"),
                },
            }
        )
        resolved += 1

    return {"checked": len(by_cutoff), "resolved": resolved}


def resolve_recent_round_results_v2():
    if not db.db_enabled():
        return {"checked": 0, "resolved": 0, "trades_resolved": 0}

    now = int(time.time())
    rounds = db.fetch_unresolved_rounds_v2(300)
    due_rounds = [row for row in rounds if now > int(row["round_cutoff"]) + 15]
    if not due_rounds:
        return {"checked": len(rounds), "resolved": 0, "trades_resolved": 0}

    df = fetch_recent_candles(500)
    resolved = 0
    trades_resolved = 0
    open_signals = db.fetch_open_signals_v2(1000)
    signals_by_round = {}
    for signal in open_signals:
        signals_by_round.setdefault(signal.get("round_id"), []).append(signal)

    for round_row in due_rounds:
        cutoff = int(round_row["round_cutoff"])
        baseline = round_row.get("baseline")
        if baseline is None:
            db.update_round_v2(round_row["id"], {"status": "skipped", "raw": {**(round_row.get("raw") or {}), "skip_reason": "missing_baseline"}})
            continue

        matching = df[df["timestamp"] >= cutoff * 1000]
        if matching.empty:
            continue

        exact_prices = exact_prices_for_round(round_row)
        exact_baseline, exact_baseline_source = update_exact_baseline_if_available(round_row, exact_prices)
        if exact_baseline is not None:
            baseline = exact_baseline
        baseline = float(baseline)

        feed_close = float(matching.iloc[0]["open"])
        exact_close = exact_prices.get("final_price")
        if exact_close is not None:
            ec = float(exact_close)
            delta = abs(ec - feed_close)
            delta_pct = (delta / feed_close * 100) if feed_close else 0.0
            if not (delta <= EXACT_BASELINE_MAX_DELTA_ABS and delta_pct <= EXACT_BASELINE_MAX_DELTA_PCT):
                print(
                    f"Rejected exact close {ec:.2f} for round {round_row.get('round_id')}; "
                    f"feed close {feed_close:.2f} (delta {delta:.2f}, {delta_pct:.3f}%)."
                )
                exact_close = None

        round_status = round_row.get("status") or "open"
        was_provisional = round_status == "provisional"
        has_exact_close = exact_close is not None
        give_up_on_exact = now > cutoff + EXACT_RECONCILE_GIVEUP_SECONDS
        if not has_exact_close:
            if now <= cutoff + PROVISIONAL_CLOSE_AFTER_SECONDS:
                continue
            if was_provisional and not give_up_on_exact:
                continue

        actual_close = float(exact_close) if has_exact_close else feed_close
        is_provisional = not has_exact_close and not give_up_on_exact
        close_source_used = "polymarket_gamma_final_price" if has_exact_close else f"provisional_{CLOSE_SOURCE}_close"
        baseline_source_used = exact_baseline_source or round_row.get("baseline_source")
        outcome = "UP" if actual_close > baseline else "DOWN" if actual_close < baseline else "TIE"
        resolved_at = datetime.now(timezone.utc).isoformat()
        existing_raw = round_row.get("raw") or {}
        raw_resolution = {
            "source": "collector_v2",
            "close_source": close_source_used,
            "baseline_source": baseline_source_used,
            "event_metadata": exact_prices.get("raw"),
            "round_id": round_row["id"],
            "provisional": is_provisional,
            "previous_status": round_status,
        }
        if has_exact_close and was_provisional:
            raw_resolution["reconciled_from_provisional"] = True
        db.upsert_round_result(
            {
                "round_cutoff": cutoff,
                "baseline": baseline,
                "actual_close": actual_close,
                "outcome": outcome,
                "raw": raw_resolution,
            }
        )
        db.insert_reference_price(
            {
                "round_id": round_row["id"],
                "observed_at": resolved_at,
                "source": close_source_used,
                "symbol": SYMBOL,
                "price": actual_close,
                "seconds_to_cutoff": 0,
                "purpose": "close",
                "raw": {"round_cutoff": cutoff, "outcome": outcome},
            }
        )
        db.update_round_v2(
            round_row["id"],
            {
                "status": "provisional" if is_provisional else "resolved",
                "resolved_at": resolved_at,
                "close_source": close_source_used,
                "baseline": baseline,
                "baseline_source": baseline_source_used,
                "raw": {
                    **existing_raw,
                    "actual_close": actual_close,
                    "outcome": outcome,
                    "event_metadata": exact_prices.get("raw"),
                    "close_source": close_source_used,
                    "provisional": is_provisional,
                    "reconciled_at": resolved_at if has_exact_close and was_provisional else existing_raw.get("reconciled_at"),
                },
            },
        )
        resolved += 1

        for signal in signals_by_round.get(round_row["round_id"], []):
            won = signal.get("side") == outcome
            entry_price = float(signal.get("entry_price") or 0)
            stake = float(signal.get("stake") or 10)
            pnl = simulated_pnl(stake, entry_price, won) if outcome != "TIE" else 0.0
            roi = pnl / stake if stake else None
            db.upsert_trade_result_v2(
                {
                    "signal_id": signal["signal_id"],
                    "round_id": round_row["id"],
                    "result": "TIE" if outcome == "TIE" else "WIN" if won else "LOSS",
                    "pnl": round(pnl, 4),
                    "roi": None if roi is None else round(roi, 6),
                    "resolved_at": resolved_at,
                    "raw": {
                        "baseline": baseline,
                        "actual_close": actual_close,
                        "outcome": outcome,
                        "close_source": close_source_used,
                        "provisional": is_provisional,
                        "reconciled_from_provisional": has_exact_close and was_provisional,
                    },
                }
            )
            trades_resolved += 1

    return {"checked": len(due_rounds), "resolved": resolved, "trades_resolved": trades_resolved}


def refresh_strategy_lab():
    global LAST_STRATEGY_LAB_REFRESH
    if os.getenv("STRATEGY_LAB_AUTO_REFRESH", "true").lower() != "true":
        return {"skipped": True, "reason": "disabled"}
    cooldown = int(os.getenv("STRATEGY_LAB_REFRESH_COOLDOWN_SECONDS", "120"))
    now = time.time()
    if now - LAST_STRATEGY_LAB_REFRESH < cooldown:
        return {"skipped": True, "reason": "cooldown"}
    LAST_STRATEGY_LAB_REFRESH = now
    result = subprocess.run(
        [
            sys.executable,
            "scripts/backtest_active_model.py",
            "--model-version",
            "active",
            "--strategy-set-version",
            "dashboard-strategies-v1",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=int(os.getenv("STRATEGY_LAB_REFRESH_TIMEOUT_SECONDS", "300")),
    )
    if result.returncode:
        return {
            "ok": False,
            "returncode": result.returncode,
            "stderr": result.stderr[-1200:],
        }
    return {"ok": True, "stdout": result.stdout[-1200:]}


def collect_once_v2(model=None, market=None):
    model = model or load_model()
    df = fetch_recent_candles(200)
    current_price = float(df.iloc[-1]["close"])
    next_cutoff = get_next_cutoff()
    window_start = next_cutoff - WINDOW_SECONDS
    seconds_to_cutoff = max(0, next_cutoff - int(time.time()))

    if market is None or market.get("event_slug") not in expected_event_slugs(next_cutoff):
        market = discover_btc_market(window_start=window_start)

    baseline, baseline_source = baseline_from_market_or_feed(market, df, next_cutoff)
    dist = current_price - baseline if baseline else None
    dist_pct = (dist / baseline * 100) if baseline else None

    round_pk = db.upsert_round_v2(
        {
            "round_id": v2_round_id(window_start),
            "event_slug": market.get("event_slug"),
            "condition_id": market.get("condition_id"),
            "window_start": window_start,
            "round_cutoff": next_cutoff,
            "baseline": baseline,
            "baseline_source": baseline_source,
            "resolution_source": market.get("resolution_source") or RESOLUTION_SOURCE,
            "status": "open",
            "raw": {
                "market": {
                    "question": market.get("question"),
                    "slug": market.get("slug"),
                    "event_slug": market.get("event_slug"),
                },
                "collector_version": COLLECTOR_VERSION,
            },
        }
    )

    if not round_pk:
        return {"skipped": True, "reason": "db_disabled_or_round_not_created"}

    if baseline is not None and hasattr(db, "update_round_baseline_snapshots"):
        db.update_round_baseline_snapshots(round_pk, baseline, baseline_source)

    quotes = []
    try:
        quotes = fetch_market_quotes(market, round_cutoff=next_cutoff)
    except Exception as exc:
        print(f"Polymarket quote capture failed: {exc}")

    base_pred = compute_model_prediction(model, df, baseline=baseline, current_price=current_price)
    base_edge_up, base_edge_down, _base_action = edge_from_quotes(base_pred, quotes)
    context = {
        "seconds_to_cutoff": seconds_to_cutoff,
        "seconds_bucket": nearest_decision_bucket(seconds_to_cutoff),
        "btc_price": current_price,
        "baseline": baseline,
        "dist_to_baseline": dist,
        "dist_to_baseline_pct": dist_pct,
        "base_prob_up": base_pred["prob_up"],
        "base_prob_down": base_pred["prob_down"],
        "base_confidence": base_pred["confidence"],
        "base_edge_up": base_edge_up,
        "base_edge_down": base_edge_down,
    }
    pred = select_prediction(ACTIVE_MODEL, base_pred, model.get("market"), context, quotes)
    edge_up, edge_down, action = edge_from_quotes(pred, quotes)
    baseline_exact = baseline_is_exact(baseline_source)
    baseline_action_allowed = baseline_allows_action(baseline_source)
    if REQUIRE_EXACT_BASELINE_FOR_ACTION and not baseline_action_allowed:
        action = "WAIT"
    elif not baseline_exact and seconds_to_cutoff > WINDOW_SECONDS - PROXY_BASELINE_MIN_ELAPSED_SECONDS:
        action = "WAIT"

    seconds_bucket = scheduled_bucket_to_capture(round_pk, seconds_to_cutoff)
    capture_reason = "scheduled" if seconds_bucket is not None else adaptive_capture_reason(
        seconds_to_cutoff,
        dist_pct,
        edge_up,
        edge_down,
    )
    if capture_reason != "scheduled":
        seconds_bucket = adaptive_bucket(seconds_to_cutoff) if capture_reason else None

    if seconds_bucket is None:
        return {
            "round_cutoff": next_cutoff,
            "seconds_to_cutoff": seconds_to_cutoff,
            "skipped": True,
            "reason": "no_bucket_due",
        }

    observed_at = datetime.now(timezone.utc)
    snapshot_id = db.insert_decision_snapshot(
        {
            "round_id": round_pk,
            "observed_at": observed_at,
            "seconds_to_cutoff": seconds_to_cutoff,
            "seconds_bucket": seconds_bucket,
            "capture_reason": capture_reason,
            "btc_price": current_price,
            "baseline": baseline,
            "dist_to_baseline": dist,
            "dist_to_baseline_pct": dist_pct,
            "baseline_source": baseline_source,
            "collector_version": COLLECTOR_VERSION,
            "raw": {
                "symbol": SYMBOL,
                "round_cutoff": next_cutoff,
                "window_start": window_start,
                "market_condition_id": market.get("condition_id"),
            },
        }
    )

    db.insert_reference_price(
        {
            "round_id": round_pk,
            "observed_at": observed_at,
            "source": PRICE_SOURCE_LABEL,
            "symbol": SYMBOL,
            "price": current_price,
            "seconds_to_cutoff": seconds_to_cutoff,
            "purpose": "snapshot",
            "raw": {"round_cutoff": next_cutoff, "snapshot_id": snapshot_id, "capture_reason": capture_reason},
        }
    )

    for quote in quotes:
        db.insert_market_quote_v2(
            {
                **quote,
                "snapshot_id": snapshot_id,
                "round_id": round_pk,
                "observed_at": observed_at,
                "raw": trim_quote_raw(quote),
            }
        )

    # Store ONLY the raw technical indicators (clean inputs). Storing the market
    # model's full feature_values would re-inject context/quotes/model-outputs and
    # cause feat__ duplication + leakage in training. See train_model_v2.flatten_features.
    features = base_pred.get("features", {})
    db.upsert_feature_snapshot(
        {
            "snapshot_id": snapshot_id,
            "round_id": round_pk,
            "observed_at": observed_at,
            "feature_set_version": FEATURE_SET_VERSION,
            "features": features,
        }
    )

    prediction_id = db.insert_prediction_v2(
        {
            "snapshot_id": snapshot_id,
            "round_id": round_pk,
            "observed_at": observed_at,
            "model_version": pred.get("model_version", ACTIVE_MODEL),
            "model_stage": MODEL_STAGE,
            "prediction": pred["prediction"],
            "prob_up": pred["prob_up"],
            "prob_down": pred["prob_down"],
            "confidence": pred["confidence"],
            "edge_up": edge_up,
            "edge_down": edge_down,
            "recommended_action": action,
            "feature_set_version": FEATURE_SET_VERSION,
            "raw": {
                "base_prediction": base_pred,
                "active_model": ACTIVE_MODEL,
                "baseline_source": baseline_source,
                "baseline_exact": baseline_exact,
                "baseline_action_allowed": baseline_action_allowed,
                "capture_reason": capture_reason,
            },
        }
    )

    min_ask_size = float(os.getenv("MIN_ASK_SIZE", "0"))
    signal_id = None
    if action in {"BUY_UP", "BUY_DOWN"}:
        side = "UP" if action == "BUY_UP" else "DOWN"
        quote = quote_by_side(quotes, side)
        ask_size = float(quote.get("ask_size") or 0) if quote else 0
        if quote and quote.get("best_ask") is not None and (min_ask_size <= 0 or ask_size >= min_ask_size):
            signal_id = db.insert_signal_v2(
                {
                    "prediction_id": prediction_id,
                    "snapshot_id": snapshot_id,
                    "round_id": round_pk,
                    "observed_at": observed_at,
                    "strategy_version": STRATEGY_VERSION,
                    "action": action,
                    "side": side,
                    "entry_price": quote["best_ask"],
                    "stake": float(os.getenv("COLLECTOR_STAKE_SIZE", "1")),
                    "model_prob": pred["prob_up"] if side == "UP" else pred["prob_down"],
                    "edge": edge_up if side == "UP" else edge_down,
                    "raw": {
                        "quote": {**quote, "raw": trim_quote_raw(quote)},
                        "capture_reason": capture_reason,
                        "seconds_to_cutoff": seconds_to_cutoff,
                        "ask_size": ask_size,
                    },
                }
            )

    if capture_reason == "scheduled":
        SCHEDULED_BUCKET_CACHE.setdefault(round_pk, set()).add(seconds_bucket)

    return {
        "mode": "v2",
        "round_cutoff": next_cutoff,
        "seconds_to_cutoff": seconds_to_cutoff,
        "seconds_bucket": seconds_bucket,
        "capture_reason": capture_reason,
        "btc_price": current_price,
        "baseline": baseline,
        "baseline_source": baseline_source,
        "baseline_exact": baseline_exact,
        "baseline_action_allowed": baseline_action_allowed,
        "prediction": pred["prediction"],
        "prob_up": round(pred["prob_up"], 4),
        "edge_up": None if edge_up is None else round(edge_up, 4),
        "edge_down": None if edge_down is None else round(edge_down, 4),
        "action": action,
        "snapshot_id": snapshot_id,
        "prediction_id": prediction_id,
        "signal_id": signal_id,
    }


def collect_once(model=None, market=None):
    if COLLECTOR_MODE == "scheduled_buckets":
        return collect_once_v2(model=model, market=market)

    model = model or load_model()
    df = fetch_recent_candles(200)
    current_price = float(df.iloc[-1]["close"])
    next_cutoff = get_next_cutoff()
    window_start = next_cutoff - WINDOW_SECONDS
    seconds_to_cutoff = next_cutoff - int(time.time())

    if market is None or market.get("event_slug") not in expected_event_slugs(next_cutoff):
        market = discover_btc_market(window_start=window_start)
    db.upsert_market(market)

    baseline, baseline_source = baseline_from_market_or_feed(market, df, next_cutoff)
    dist = current_price - baseline if baseline else None
    dist_pct = (dist / baseline * 100) if baseline else None

    snapshot = {
        "observed_at": datetime.now(timezone.utc),
        "round_cutoff": next_cutoff,
        "window_start": window_start,
        "seconds_to_cutoff": seconds_to_cutoff,
        "symbol": SYMBOL,
        "btc_price": current_price,
        "baseline": baseline,
        "dist_to_baseline": dist,
        "dist_to_baseline_pct": dist_pct,
        "source": PRICE_SOURCE_LABEL,
        "market_condition_id": market.get("condition_id"),
        "raw": {"market": market, "baseline_source": baseline_source},
    }
    snapshot_id = db.insert_round_snapshot(snapshot)

    quotes = []
    try:
        quotes = fetch_market_quotes(market, round_cutoff=next_cutoff)
        for quote in quotes:
            db.insert_quote(quote)
    except Exception as exc:
        print(f"Polymarket quote capture failed: {exc}")

    base_pred = compute_model_prediction(model, df, baseline=baseline, current_price=current_price)
    base_edge_up, base_edge_down, _base_action = edge_from_quotes(base_pred, quotes)
    context = {
        "seconds_to_cutoff": seconds_to_cutoff,
        "seconds_bucket": nearest_decision_bucket(seconds_to_cutoff),
        "btc_price": current_price,
        "baseline": baseline,
        "dist_to_baseline": dist,
        "dist_to_baseline_pct": dist_pct,
        "base_prob_up": base_pred["prob_up"],
        "base_prob_down": base_pred["prob_down"],
        "base_confidence": base_pred["confidence"],
        "base_edge_up": base_edge_up,
        "base_edge_down": base_edge_down,
    }
    pred = select_prediction(ACTIVE_MODEL, base_pred, model.get("market"), context, quotes)
    edge_up, edge_down, action = edge_from_quotes(pred, quotes)
    baseline_exact = baseline_is_exact(baseline_source)
    baseline_action_allowed = baseline_allows_action(baseline_source)
    if REQUIRE_EXACT_BASELINE_FOR_ACTION and not baseline_action_allowed:
        action = "WAIT"
    elif not baseline_exact and seconds_to_cutoff > WINDOW_SECONDS - PROXY_BASELINE_MIN_ELAPSED_SECONDS:
        action = "WAIT"
    prediction_id = db.insert_prediction(
        {
            "observed_at": datetime.now(timezone.utc),
            "round_cutoff": next_cutoff,
            "model_version": pred.get("model_version", ACTIVE_MODEL),
            "prediction": pred["prediction"],
            "prob_up": pred["prob_up"],
            "prob_down": pred["prob_down"],
            "confidence": pred["confidence"],
            "edge_up": edge_up,
            "edge_down": edge_down,
            "recommended_action": action,
            "feature_values": pred.get("feature_values") or pred.get("features", {}),
            "source_snapshot_id": snapshot_id,
            "raw": {
                "quotes": quotes,
                "base_prediction": base_pred,
                "active_model": ACTIVE_MODEL,
                "baseline_source": baseline_source,
                "baseline_exact": baseline_exact,
                "baseline_action_allowed": baseline_action_allowed,
            },
        }
    )

    if action in {"BUY_UP", "BUY_DOWN"}:
        side = "UP" if action == "BUY_UP" else "DOWN"
        quote = quote_by_side(quotes, side)
        if quote and quote.get("best_ask") is not None:
            db.insert_simulated_bet(
                {
                    "observed_at": datetime.now(timezone.utc),
                    "round_cutoff": next_cutoff,
                    "side": side,
                    "entry_price": quote["best_ask"],
                    "stake": float(os.getenv("COLLECTOR_STAKE_SIZE", "1")),
                    "model_prob": pred["prob_up"] if side == "UP" else pred["prob_down"],
                    "edge": edge_up if side == "UP" else edge_down,
                    "result": "OPEN",
                    "model_prediction_id": prediction_id,
                    "raw": {
                        "quote": quote,
                        "prediction": pred,
                        "base_prediction": base_pred,
                        "baseline": baseline,
                        "btc_price": current_price,
                        "seconds_to_cutoff": seconds_to_cutoff,
                        "window_start": window_start,
                        "market": {
                            "question": market.get("question"),
                            "event_slug": market.get("event_slug"),
                            "condition_id": market.get("condition_id"),
                            "resolution_source": market.get("resolution_source"),
                        },
                        "baseline_source": baseline_source,
                    },
                }
            )

    return {
        "round_cutoff": next_cutoff,
        "btc_price": current_price,
        "baseline": baseline,
        "baseline_source": baseline_source,
        "baseline_exact": baseline_exact,
        "baseline_action_allowed": baseline_action_allowed,
        "prediction": pred["prediction"],
        "prob_up": round(pred["prob_up"], 4),
        "edge_up": None if edge_up is None else round(edge_up, 4),
        "edge_down": None if edge_down is None else round(edge_down, 4),
        "action": action,
        "snapshot_id": snapshot_id,
        "prediction_id": prediction_id,
    }


def run_forever():
    global LAST_RESOLUTION_CHECK
    if not db.db_enabled():
        print("WARNING: No database backend is configured. Collector will run without persistence.")
    model = load_model()
    market = None
    market_fetched_at = 0
    interval = int(os.getenv("COLLECTOR_INTERVAL_SECONDS", "5"))
    while True:
        try:
            if COLLECTOR_MODE == "scheduled_buckets":
                now = time.time()
                if now - LAST_RESOLUTION_CHECK >= RESOLUTION_CHECK_INTERVAL_SECONDS:
                    round_resolution = resolve_recent_round_results_v2()
                    LAST_RESOLUTION_CHECK = now
                    if round_resolution["resolved"] or round_resolution.get("trades_resolved"):
                        print(f"Resolved v2 results: {round_resolution}")
                        strategy_lab = refresh_strategy_lab()
                        print(f"Strategy Lab refresh: {strategy_lab}")
            else:
                round_resolution = resolve_recent_round_results()
                if round_resolution["resolved"]:
                    print(f"Resolved round results: {round_resolution}")
                resolved = resolve_open_simulated_bets()
                if resolved["resolved"]:
                    print(f"Resolved simulated bets: {resolved}")
            current_cutoff = get_next_cutoff()
            current_window_start = current_cutoff - WINDOW_SECONDS
            expected_slugs = expected_event_slugs(current_cutoff)
            market_refresh_due = (time.time() - market_fetched_at) >= POLYMARKET_MARKET_REFRESH_SECONDS
            if market is None or market.get("event_slug") not in expected_slugs or market.get("baseline") is None or market_refresh_due:
                market = discover_btc_market(window_start=current_window_start)
                market_fetched_at = time.time()
                print(f"Tracking Polymarket market: {market.get('question')} ({market.get('slug')})")
            result = collect_once(model=model, market=market)
            print(json.dumps(result, default=str))
        except PolymarketError as exc:
            print(f"Polymarket discovery failed: {exc}")
            market = None
        except Exception as exc:
            print(f"Collector error: {exc}")
        time.sleep(interval)


if __name__ == "__main__":
    run_forever()
