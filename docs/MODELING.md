# Modeling Workflow

The current production model is a baseline model trained from Binance 1-minute candles. It predicts whether the current 5-minute BTC window will finish above or below its inferred baseline.

The next model should be trained from Supabase observations, not directly from raw Binance candles.

## Current Model

Source:

```text
Binance BTCUSDT 1m candles
```

Target:

```text
window close > window open
```

Algorithm:

```text
HistGradientBoostingClassifier
CalibratedClassifierCV
```

Feature families:

- Baseline/window context
- Trend indicators
- Momentum indicators
- Volatility indicators
- Volume/order-flow indicators from Binance
- Time features

## New Dataset

The collector now writes decision-time observations to Supabase:

- `round_snapshots`: BTC state and baseline context.
- `polymarket_quotes`: UP/DOWN orderbook state.
- `model_predictions`: model probability and features at that moment.
- `simulated_bets`: signals that crossed the edge threshold.
- `round_results`: final result per 5-minute round.

For training, prefer the `modeling_snapshots` view from `migrations/002_modeling_views.sql`.

## Why Not Train On Raw Tables Directly

Raw tables are append-only telemetry. Model training needs a stable, reproducible dataset:

- one row per decision time
- clear target
- known feature columns
- no future leakage
- consistent joins between prediction time and final outcome

That is why we keep raw capture tables separate from modeling views/tables.

## Next Training Plan

1. Let the collector run long enough to collect many resolved rounds.
2. Use `modeling_snapshots` where `target_up is not null`.
3. Add Polymarket quote features:
   - UP ask
   - DOWN ask
   - spread
   - midpoint
   - bid/ask size
   - implied market probability
4. Split by time, not random rows.
5. Tune hyperparameters with walk-forward validation.
6. Evaluate both probability quality and trading quality:
   - log loss
   - Brier score
   - calibration by probability bucket
   - simulated ROI
   - drawdown
   - win rate by seconds remaining
   - win rate by edge bucket

## Jupyter Workflow

Use `notebooks/01_supabase_eda.ipynb` as the modeling lab. It connects to the
same `.env` as the app, reads `modeling_snapshots` and
`simulated_bet_performance`, then shows:

- dataset row counts
- resolved rows versus open rows
- accuracy by seconds remaining
- simulated PnL
- edge bucket performance
- a flattened feature matrix for experiments
- a simple walk-forward model check

Install notebook dependencies with:

```text
pip install -r requirements-dev.txt
```

Then start Jupyter:

```text
jupyter lab
```

Keep `scripts/train_from_supabase.py` as the repeatable training path. The
notebook is for exploration; the script is for producing artifacts.

## Storage Notes

The biggest storage risk is saving full orderbooks every few seconds. The app now stores only top-of-book summary plus top 10 bid/ask levels in `raw`, which is enough for analysis without bloating Supabase too quickly.
