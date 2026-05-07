# Supabase EDA Report

Generated at UTC: `2026-05-07T16:31:39.442915+00:00`

## Database Counts

| table | rows |
| --- | --- |
| round_snapshots | 713 |
| polymarket_quotes | 1412 |
| model_predictions | 701 |
| simulated_bets | 436 |
| round_results | 108 |
| polymarket_markets | 108 |

## Prediction Quality

| metric | value |
|---|---:|
| modeling rows | 701 |
| resolved rows | 691 |
| unique resolved rounds | 107 |
| row-level accuracy | 72.65% |
| first prediction per round accuracy | 57.94% |
| last prediction per round accuracy | 88.79% |

Row-level accuracy is inflated because the collector records many snapshots per 5-minute round. Unique-round metrics are more honest.

## Accuracy By Seconds Remaining

| seconds_bucket | rows | rounds | accuracy | avg_confidence |
| --- | --- | --- | --- | --- |
| (-0.001, 30.0] | 75 | 71 | 94.67 | 98.42 |
| (30.0, 60.0] | 64 | 64 | 87.5 | 97.74 |
| (60.0, 90.0] | 73 | 70 | 76.71 | 59.51 |
| (90.0, 120.0] | 59 | 58 | 79.66 | 56.83 |
| (120.0, 180.0] | 150 | 105 | 70.67 | 47.65 |
| (180.0, 240.0] | 140 | 104 | 65.0 | 33.26 |
| (240.0, 300.0] | 130 | 106 | 57.69 | 22.37 |

## Simulated Trading

| metric | value |
|---|---:|
| closed bets | 436 |
| win rate | 44.04% |
| total PnL | 83.9996 |

## Simulated Bets By Seconds Remaining

| seconds_bucket | bets | win_rate | pnl | avg_entry |
| --- | --- | --- | --- | --- |
| (-0.001, 30.0] | 22 | 81.82 | 84.5042 | 0.7186 |
| (120.0, 180.0] | 110 | 41.82 | 109.0549 | 0.3774 |
| (180.0, 240.0] | 98 | 43.88 | -45.5166 | 0.437 |
| (240.0, 300.0] | 99 | 45.45 | -45.208 | 0.4588 |
| (30.0, 60.0] | 31 | 70.97 | -7.5456 | 0.7545 |
| (60.0, 90.0] | 48 | 27.08 | 153.2626 | 0.2146 |
| (90.0, 120.0] | 28 | 17.86 | -164.5519 | 0.2504 |

## Simulated Bets By Edge

| side | edge_bucket | bets | win_rate | pnl | avg_entry |
| --- | --- | --- | --- | --- | --- |
| DOWN | (0.03, 0.05] | 41 | 43.9 | -80.8429 | 0.4866 |
| DOWN | (0.05, 0.07] | 24 | 41.67 | -83.1663 | 0.4708 |
| DOWN | (0.07, 0.1] | 33 | 54.55 | 95.5098 | 0.4688 |
| DOWN | (0.1, 0.15] | 23 | 21.74 | -134.5429 | 0.4078 |
| DOWN | (0.15, 1.0] | 17 | 52.94 | 1.0925 | 0.5 |
| UP | (0.03, 0.05] | 42 | 57.14 | 74.6767 | 0.4621 |
| UP | (0.05, 0.07] | 40 | 47.5 | -56.0556 | 0.5075 |
| UP | (0.07, 0.1] | 67 | 31.34 | -260.2767 | 0.4001 |
| UP | (0.1, 0.15] | 77 | 45.45 | -25.0226 | 0.3891 |
| UP | (0.15, 1.0] | 72 | 45.83 | 552.6276 | 0.3499 |

## What If We Raised EDGE_THRESHOLD?

| threshold | bets | win_rate | pnl | avg_entry |
| --- | --- | --- | --- | --- |
| 0.03 | 436.0 | 44.04 | 83.9996 | 0.4272 |
| 0.05 | 353.0 | 42.49 | 90.1658 | 0.4162 |
| 0.07 | 289.0 | 41.87 | 229.3877 | 0.399 |
| 0.1 | 189.0 | 43.39 | 394.1546 | 0.3864 |
| 0.15 | 89.0 | 47.19 | 553.7201 | 0.3785 |

## Interpretation

- Accuracy is not enough. The strategy enters at Polymarket ask price, so spread and entry cost decide profitability.
- A threshold can reduce noisy trades, but it cannot fix bad probability calibration alone.
- The next serious model should include Polymarket quote features and should be evaluated by unique round and simulated ROI.