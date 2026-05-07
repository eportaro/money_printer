import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.append(str(Path(__file__).resolve().parents[1]))

import db

REPORT_DIR = Path("reports")
REPORT_MD = REPORT_DIR / "supabase_eda_report.md"
REPORT_JSON = REPORT_DIR / "supabase_eda_report.json"

TABLES = [
    "round_snapshots",
    "polymarket_quotes",
    "model_predictions",
    "simulated_bets",
    "round_results",
    "polymarket_markets",
]


def to_number(df, columns):
    for col in columns:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def pct(value):
    if pd.isna(value):
        return None
    return round(float(value) * 100, 2)


def money(value):
    if pd.isna(value):
        return None
    return round(float(value), 4)


def markdown_table(df, max_rows=30):
    if df is None or df.empty:
        return "_No rows._"
    view = df.head(max_rows).copy()
    columns = list(view.columns)
    lines = [
        "| " + " | ".join(str(col) for col in columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for _, row in view.iterrows():
        values = []
        for col in columns:
            value = row[col]
            if pd.isna(value):
                value = ""
            values.append(str(value).replace("|", "\\|"))
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def fetch_view(name, limit=10000):
    return pd.DataFrame(db._get(name, {"select": "*", "order": "observed_at.asc", "limit": limit}))


def analyze():
    counts = {table: db.count_rows(table) for table in TABLES}

    modeling = fetch_view("modeling_snapshots")
    if modeling.empty:
        raise RuntimeError("modeling_snapshots returned no rows")

    modeling["observed_at"] = pd.to_datetime(modeling["observed_at"], utc=True)
    modeling = to_number(
        modeling,
        [
            "seconds_to_cutoff",
            "btc_price",
            "baseline",
            "dist_to_baseline",
            "dist_to_baseline_pct",
            "prob_up",
            "prob_down",
            "confidence",
            "edge_up",
            "edge_down",
            "target_up",
        ],
    )
    resolved = modeling[modeling["target_up"].notna()].copy()
    resolved["pred_up"] = (resolved["prob_up"] >= 0.5).astype(int)
    resolved["correct"] = resolved["pred_up"].eq(resolved["target_up"].astype(int))

    first_by_round = resolved.sort_values("observed_at").groupby("round_cutoff", as_index=False).first()
    last_by_round = resolved.sort_values("observed_at").groupby("round_cutoff", as_index=False).last()

    time_perf = resolved.copy()
    time_perf["seconds_bucket"] = pd.cut(
        time_perf["seconds_to_cutoff"],
        bins=[0, 30, 60, 90, 120, 180, 240, 300],
        include_lowest=True,
    )
    time_summary = (
        time_perf.groupby("seconds_bucket", observed=True)
        .agg(rows=("correct", "size"), rounds=("round_cutoff", "nunique"), accuracy=("correct", "mean"), avg_confidence=("confidence", "mean"))
        .reset_index()
    )
    time_summary["seconds_bucket"] = time_summary["seconds_bucket"].astype(str)
    time_summary["accuracy"] = (time_summary["accuracy"] * 100).round(2)
    time_summary["avg_confidence"] = (time_summary["avg_confidence"] * 100).round(2)

    bets = fetch_view("simulated_bet_performance")
    if not bets.empty:
        bets["observed_at"] = pd.to_datetime(bets["observed_at"], utc=True)
        bets = to_number(
            bets,
            ["entry_price", "stake", "model_prob", "edge", "pnl", "seconds_to_cutoff", "baseline", "btc_price", "actual_close"],
        )
    closed = bets[bets["result"].isin(["WIN", "LOSS"])].copy() if not bets.empty else pd.DataFrame()
    if not closed.empty:
        closed["win"] = closed["result"].eq("WIN")
        closed["cum_pnl"] = closed["pnl"].fillna(0).cumsum()
        closed["seconds_bucket"] = pd.cut(
            closed["seconds_to_cutoff"],
            bins=[0, 30, 60, 90, 120, 180, 240, 300],
            include_lowest=True,
        ).astype(str)
        closed["edge_bucket"] = pd.cut(
            closed["edge"],
            bins=[-1, 0.03, 0.05, 0.07, 0.10, 0.15, 1],
            include_lowest=True,
        ).astype(str)

        bet_by_time = (
            closed.groupby("seconds_bucket", observed=True)
            .agg(bets=("id", "size"), win_rate=("win", "mean"), pnl=("pnl", "sum"), avg_entry=("entry_price", "mean"))
            .reset_index()
        )
        bet_by_time["win_rate"] = (bet_by_time["win_rate"] * 100).round(2)
        bet_by_time["pnl"] = bet_by_time["pnl"].round(4)
        bet_by_time["avg_entry"] = bet_by_time["avg_entry"].round(4)

        bet_by_edge = (
            closed.groupby(["side", "edge_bucket"], observed=True)
            .agg(bets=("id", "size"), win_rate=("win", "mean"), pnl=("pnl", "sum"), avg_entry=("entry_price", "mean"))
            .reset_index()
        )
        bet_by_edge["win_rate"] = (bet_by_edge["win_rate"] * 100).round(2)
        bet_by_edge["pnl"] = bet_by_edge["pnl"].round(4)
        bet_by_edge["avg_entry"] = bet_by_edge["avg_entry"].round(4)

        threshold_rows = []
        for threshold in [0.03, 0.05, 0.07, 0.10, 0.15]:
            subset = closed[closed["edge"] >= threshold]
            threshold_rows.append(
                {
                    "threshold": threshold,
                    "bets": len(subset),
                    "win_rate": pct(subset["win"].mean()) if len(subset) else None,
                    "pnl": money(subset["pnl"].sum()) if len(subset) else 0.0,
                    "avg_entry": money(subset["entry_price"].mean()) if len(subset) else None,
                }
            )
        threshold_summary = pd.DataFrame(threshold_rows)
    else:
        bet_by_time = pd.DataFrame()
        bet_by_edge = pd.DataFrame()
        threshold_summary = pd.DataFrame()

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": counts,
        "modeling_rows": int(len(modeling)),
        "resolved_rows": int(len(resolved)),
        "unique_resolved_rounds": int(resolved["round_cutoff"].nunique()),
        "row_accuracy_pct": pct(resolved["correct"].mean()) if len(resolved) else None,
        "first_prediction_round_accuracy_pct": pct(first_by_round["correct"].mean()) if len(first_by_round) else None,
        "last_prediction_round_accuracy_pct": pct(last_by_round["correct"].mean()) if len(last_by_round) else None,
        "closed_bets": int(len(closed)),
        "bet_win_rate_pct": pct(closed["win"].mean()) if len(closed) else None,
        "bet_total_pnl": money(closed["pnl"].sum()) if len(closed) else None,
    }

    REPORT_DIR.mkdir(exist_ok=True)
    REPORT_JSON.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    lines = [
        "# Supabase EDA Report",
        "",
        f"Generated at UTC: `{summary['generated_at']}`",
        "",
        "## Database Counts",
        "",
        markdown_table(pd.Series(counts, name="rows").to_frame().reset_index().rename(columns={"index": "table"})),
        "",
        "## Prediction Quality",
        "",
        "| metric | value |",
        "|---|---:|",
        f"| modeling rows | {summary['modeling_rows']} |",
        f"| resolved rows | {summary['resolved_rows']} |",
        f"| unique resolved rounds | {summary['unique_resolved_rounds']} |",
        f"| row-level accuracy | {summary['row_accuracy_pct']}% |",
        f"| first prediction per round accuracy | {summary['first_prediction_round_accuracy_pct']}% |",
        f"| last prediction per round accuracy | {summary['last_prediction_round_accuracy_pct']}% |",
        "",
        "Row-level accuracy is inflated because the collector records many snapshots per 5-minute round. Unique-round metrics are more honest.",
        "",
        "## Accuracy By Seconds Remaining",
        "",
        markdown_table(time_summary),
        "",
        "## Simulated Trading",
        "",
        "| metric | value |",
        "|---|---:|",
        f"| closed bets | {summary['closed_bets']} |",
        f"| win rate | {summary['bet_win_rate_pct']}% |",
        f"| total PnL | {summary['bet_total_pnl']} |",
        "",
        "## Simulated Bets By Seconds Remaining",
        "",
        markdown_table(bet_by_time),
        "",
        "## Simulated Bets By Edge",
        "",
        markdown_table(bet_by_edge),
        "",
        "## What If We Raised EDGE_THRESHOLD?",
        "",
        markdown_table(threshold_summary),
        "",
        "## Interpretation",
        "",
        "- Accuracy is not enough. The strategy enters at Polymarket ask price, so spread and entry cost decide profitability.",
        "- A threshold can reduce noisy trades, but it cannot fix bad probability calibration alone.",
        "- The next serious model should include Polymarket quote features and should be evaluated by unique round and simulated ROI.",
    ]
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")

    print(json.dumps(summary, indent=2))
    print(f"Wrote {REPORT_MD}")
    print(f"Wrote {REPORT_JSON}")


if __name__ == "__main__":
    analyze()
