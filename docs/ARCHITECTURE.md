# Architecture Notes

The goal is not only to predict whether BTC closes above or below a 5-minute baseline. The useful trading question is:

```text
Is Polymarket underpricing one side right now, at the price where we can actually enter?
```

That means the system must separate three layers:

1. Probability model: estimates `P(UP)` and `P(DOWN)`.
2. Market data layer: captures Polymarket bid/ask, spread, liquidity and last traded price.
3. Strategy layer: decides whether the model probability is enough better than the entry price.

## Data Flow

```text
Binance 1m candles
      |
      v
technical features + current 5m window context
      |
      v
model probability P(UP)
      |
      +---------- Polymarket orderbook snapshots
      |                     |
      v                     v
edge calculation: probability - ask price
      |
      v
WAIT / BUY_UP / BUY_DOWN
      |
      v
Supabase tables for backtesting and calibration
```

## Why Supabase

JSON files are fine for a prototype, but this system needs to ask temporal questions:

- What did we know when there were 90 seconds left?
- What was the best ask for UP at that exact moment?
- Did our probability beat the actual entry price?
- Which features matter only when the baseline distance is small?
- What happens if we only trade when spread is under 0.03?

Those questions need relational storage with timestamps. Supabase gives us hosted Postgres, easy SQL access and enough room to grow into analytics.

## Model Improvements

The current model predicts the window outcome from Binance features. The next model should be trained from decision snapshots:

```text
round_id + observed_at + seconds_remaining + btc_features + polymarket_quotes + outcome
```

Important feature groups:

- Baseline context: distance to baseline, percent distance, seconds remaining.
- Price momentum: short-window returns, RSI, MACD, Bollinger position.
- Volatility: rolling volatility, ATR, candle range.
- Volume/order flow: Binance volume, taker buy ratio, VWAP distance.
- Polymarket pricing: UP ask, DOWN ask, midpoint, spread, last trade.
- Polymarket microstructure: top-of-book size, quote movement, liquidity imbalance.

The model should be evaluated with:

- Brier score and log loss for probability quality.
- Calibration curves by probability bucket.
- ROI and drawdown for simulated bets.
- Performance by seconds remaining.
- Performance by spread/liquidity bucket.
- Performance by distance to baseline bucket.

Accuracy alone is not enough. A 55% win rate can lose money if entries are expensive; a 48% win rate can be profitable if bought at very mispriced odds.
