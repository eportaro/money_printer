import argparse
import json
import os
import sys
import time
from pathlib import Path

import pyodbc
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket import fetch_event_prices, infer_baseline_from_previous_event


load_dotenv()


def connect():
    conn_str = os.getenv("SQLSERVER_CONNECTION")
    if not conn_str:
        raise RuntimeError("SQLSERVER_CONNECTION is not configured")
    return pyodbc.connect(conn_str)


def outcome_for(actual_close, baseline):
    if actual_close > baseline:
        return "UP"
    if actual_close < baseline:
        return "DOWN"
    return "TIE"


def upsert_round_result(cursor, cutoff, baseline, actual_close, raw):
    outcome = outcome_for(actual_close, baseline)
    row = cursor.execute(
        "select id from dbo.round_results where round_cutoff = ?",
        int(cutoff),
    ).fetchone()
    raw_json = json.dumps(raw, default=str)
    if row:
        cursor.execute(
            """
            update dbo.round_results
               set baseline = ?, actual_close = ?, outcome = ?, raw = ?,
                   resolved_at = sysutcdatetime()
             where id = ?
            """,
            float(baseline),
            float(actual_close),
            outcome,
            raw_json,
            row.id,
        )
    else:
        cursor.execute(
            """
            insert into dbo.round_results (round_cutoff, baseline, actual_close, outcome, raw)
            values (?, ?, ?, ?, ?)
            """,
            int(cutoff),
            float(baseline),
            float(actual_close),
            outcome,
            raw_json,
        )
    return outcome


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--sleep", type=float, default=0.15)
    args = parser.parse_args()

    updated_rounds = 0
    updated_snapshots = 0
    updated_results = 0
    skipped = 0

    with connect() as conn:
        cursor = conn.cursor()
        rows = cursor.execute(
            f"""
            select top ({int(args.limit)})
                   id, round_id, event_slug, round_cutoff, baseline, baseline_source, status, close_source
              from dbo.rounds
             where event_slug is not null
             order by round_cutoff desc
            """
        ).fetchall()

        for row in rows:
            event_slug = row.event_slug
            try:
                # gamma-only: the HTML page scraper can return a wrong-event priceToBeat.
                prices = fetch_event_prices(event_slug, include_page=False)
                baseline = prices.get("price_to_beat")
                baseline_source = "polymarket_gamma_event_metadata" if baseline is not None else None
                if baseline is None:
                    previous = infer_baseline_from_previous_event(event_slug)
                    if previous:
                        baseline = previous["baseline"]
                        baseline_source = previous["baseline_source"]
                        prices["previous_event_slug"] = previous.get("previous_event_slug")
                        prices["raw"] = previous.get("raw")

                final_price = prices.get("final_price")
                if baseline is None and final_price is None:
                    skipped += 1
                    continue

                raw = {
                    "backfill": "polymarket_event_metadata",
                    "event_slug": event_slug,
                    "metadata": prices.get("raw"),
                    "previous_event_slug": prices.get("previous_event_slug"),
                }

                if baseline is not None:
                    cursor.execute(
                        """
                        update dbo.rounds
                           set baseline = ?, baseline_source = ?, raw = json_modify(coalesce(raw, '{}'), '$.polymarket_metadata_backfill', json_query(?)),
                               updated_at = sysutcdatetime()
                         where id = ?
                        """,
                        float(baseline),
                        baseline_source,
                        json.dumps(raw, default=str),
                        row.id,
                    )
                    updated_rounds += cursor.rowcount or 0

                    cursor.execute(
                        """
                        update dbo.decision_snapshots
                           set baseline = ?,
                               dist_to_baseline = btc_price - ?,
                               dist_to_baseline_pct = case when ? = 0 then null else ((btc_price - ?) / ?) * 100.0 end,
                               baseline_source = ?
                         where round_id = ?
                        """,
                        float(baseline),
                        float(baseline),
                        float(baseline),
                        float(baseline),
                        float(baseline),
                        baseline_source,
                        row.id,
                    )
                    updated_snapshots += cursor.rowcount or 0

                round_due = int(time.time()) > int(row.round_cutoff) + 15
                if baseline is not None and final_price is not None and round_due:
                    outcome = upsert_round_result(
                        cursor,
                        row.round_cutoff,
                        baseline,
                        final_price,
                        {
                            **raw,
                            "close_source": "polymarket_gamma_final_price",
                            "outcome": outcome_for(final_price, baseline),
                        },
                    )
                    cursor.execute(
                        """
                        update dbo.rounds
                           set status = N'resolved',
                               resolved_at = sysutcdatetime(),
                               close_source = N'polymarket_gamma_final_price',
                               raw = json_modify(coalesce(raw, '{}'), '$.actual_close', ?),
                               updated_at = sysutcdatetime()
                         where id = ?
                        """,
                        float(final_price),
                        row.id,
                    )
                    updated_results += 1
                    print(f"{event_slug}: baseline={baseline:.8f} final={final_price:.8f} outcome={outcome}")
                elif baseline is not None:
                    cursor.execute(
                        """
                        update dbo.rounds
                           set status = N'open',
                               resolved_at = null,
                               close_source = null,
                               updated_at = sysutcdatetime()
                         where id = ?
                           and (close_source is null or close_source <> N'polymarket_gamma_final_price')
                        """,
                        row.id,
                    )
                    cursor.execute(
                        """
                        delete from dbo.round_results
                         where round_cutoff = ?
                           and (
                               raw is null
                               or json_value(raw, '$.close_source') is null
                               or json_value(raw, '$.close_source') <> N'polymarket_gamma_final_price'
                           )
                        """,
                        int(row.round_cutoff),
                    )
                    print(f"{event_slug}: baseline={baseline:.8f} final=pending")

                conn.commit()
                if args.sleep:
                    time.sleep(args.sleep)
            except Exception as exc:
                skipped += 1
                print(f"{event_slug}: skipped ({exc})")

    print(
        json.dumps(
            {
                "rounds_updated": updated_rounds,
                "snapshots_updated": updated_snapshots,
                "results_updated": updated_results,
                "skipped": skipped,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
