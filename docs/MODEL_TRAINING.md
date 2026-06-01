# Model Training and Versioning Guide

Este documento explica como se entrena el modelo del bot, como se versiona y que falta mejorar para convertir la data real capturada en SQL Server en un modelo mas confiable.

## Estado actual

Hay tres familias de modelos:

| Modelo | Archivo | Fuente | Estado |
|---|---|---|---|
| `binance-hgb-v1` | `model_artifacts/model.pkl` | 30 dias de Binance 1m | Base tecnico |
| `historical-sim-extra_trees-v1` | `model_artifacts/model_supabase.pkl` antes de activar v2 | Simulacion historica 15m tipo Polymarket | Reemplazado |
| `extra_trees-v2-20260529002918` | `model_artifacts/model_supabase.pkl` | SQL Server real fusionado local + Docker | Produccion actual |
| `extra_trees-v2-*` | `model_artifacts/extra_trees-v2-*.pkl` | SQL Server real capturado por el collector | Candidatos/versiones |

El modelo activo live se carga desde:

```text
model_artifacts/model_supabase.pkl
```

Por seguridad, `scripts/train_model_v2.py` ahora NO reemplaza ese archivo salvo que se use `--activate`.

## Base canonica actual

Docker es ahora la base recomendada para correr el bot 24/7 localmente:

```text
SQL Server Docker: localhost,14333
Database: PolymarketBot
```

La data vieja del SQL Server local no se borro. Se fusiono hacia Docker con:

```bat
venv\Scripts\python.exe scripts\merge_sqlserver_data.py
```

Conteos despues de fusionar y reiniciar:

```text
rounds: 134
decision_snapshots: 1755
market_quotes: 3510
feature_snapshots: 1755
predictions_v2: 1756
signals_v2: 88
round_results: 132
trade_results_v2: 88
```

Conteos despues de agregar backtest versionado del modelo activo:

```text
strategy_backtest_runs: 1
strategy_backtest_signals: 1,065
```

## Ultimo reentrenamiento activado

Se genero y activo un modelo con toda la data disponible fusionada:

```text
model_version: extra_trees-v2-20260529002918
dataset_version: ds_20260529_002916_827fa3b4a463ddcc
algorithm: extra_trees
activated: true
```

Metricas:

```text
accuracy: 82.18%
roc_auc: 0.9090
brier_score: 0.1188
log_loss: 0.3578
```

Dataset usado:

```text
rows: 1,728 snapshots resueltos
unique_rounds: 132 rondas
train_rows: 1,296
test_rows: 432
target_up_rate: 50.98%
feature_count: 102
periodo: 2026-05-27 05:35 UTC -> 2026-05-28 23:59 UTC
```

Lectura importante:

```text
Este modelo esta activo en model_artifacts/model_supabase.pkl.
132 rondas todavia es poca muestra, asi que se debe seguir acumulando data.
```

## Comandos

Entrenar candidato sin activar:

```bat
venv\Scripts\python.exe scripts\train_model_v2.py --min-rounds 500 --limit 50000 --notes "candidate from SQL Server real data"
```

Entrenar aunque haya poca muestra, solo para experimento:

```bat
venv\Scripts\python.exe scripts\train_model_v2.py --min-rounds 5 --limit 2000 --notes "quick candidate"
```

Entrenar y activar para live:

```bat
venv\Scripts\python.exe scripts\train_model_v2.py --min-rounds 500 --limit 50000 --activate --notes "promoted production model"
```

Entrenar y activar usando Docker como base canonica:

```bat
docker compose exec app python scripts/train_model_v2.py --min-rounds 500 --limit 50000 --activate --notes "production retrain from Docker SQL Server"
```

Despues de activar, reinicia `server.py` y `collector.py` para que carguen el nuevo `model_supabase.pkl`.

Despues de activar un modelo tambien puedes recalcular el backtest de estrategias para comparar contra historia. El dashboard separa esa lectura de la performance live real:

```bat
docker compose exec app python scripts/backtest_active_model.py --model-version active --strategy-set-version dashboard-strategies-v1
docker compose restart app collector
```

## Versionado

Cada entrenamiento crea:

| Objeto | Para que sirve |
|---|---|
| `dataset_versions` | Registra que data se uso para entrenar. |
| `model_runs` | Registra algoritmo, metricas, artifact y stage. |
| `model_artifacts/<model_version>.pkl` | Modelo versionado. |
| `model_artifacts/<model_version>_metrics.json` | Metricas y feature importance. |

Cada backtest de estrategia crea:

| Objeto | Para que sirve |
|---|---|
| `strategy_backtest_runs` | Registra una corrida de backtest para un `model_version`. |
| `strategy_backtest_signals` | Guarda las señales simuladas recalculadas para ese modelo y estrategia. |
| `strategy_backtest_performance_v2` | Vista para que el dashboard lea performance del modelo activo. |

Stages:

| Stage | Significado |
|---|---|
| `candidate` | Modelo entrenado para comparar, no live. |
| `production` | Modelo activado para live. |
| `archived` | Modelo anterior retirado. |

## Como entrena `train_model_v2.py`

Fuente:

```text
dbo.training_decision_snapshots
```

Esa vista une:

- `rounds`
- `decision_snapshots`
- `market_quotes`
- `feature_snapshots`
- `predictions_v2`
- `round_results`

Target:

```text
target_up = 1 si actual_close > baseline
target_up = 0 si actual_close < baseline
```

Split:

```text
Temporal split
75% mas antiguo para train
25% mas reciente para test
```

Esto es mejor que mezclar aleatoriamente porque respeta el orden del mercado.

Modelos probados:

- Dummy baseline
- Logistic Regression
- Random Forest
- Extra Trees
- HistGradientBoosting

Seleccion actual:

```text
El script elige el modelo con mejor brier_score y log_loss.
```

Eso prioriza probabilidades bien calibradas, no solo accuracy.

## Features actuales

El candidato SQL usa 102 columnas aproximadamente:

### Contexto de ronda

- `seconds_to_cutoff`
- `seconds_bucket`
- `btc_price`
- `baseline`
- `dist_to_baseline`
- `dist_to_baseline_pct`

### Indicadores tecnicos

- SMA/EMA 5, 10, 20, 50
- RSI
- MACD
- Stochastic
- Williams %R
- CCI
- ROC
- Bollinger Bands
- ATR
- Rolling volatility
- OBV
- MFI
- VWAP
- returns 1/5/10/15/30
- candle body/shadows
- hour/day cyclic features

### Polymarket quotes

- UP/DOWN best bid
- UP/DOWN best ask
- midpoint
- spread
- bid/ask size
- last trade price

## Oportunidades de mejora

### 1. Mas data real

La prioridad numero uno no es cambiar algoritmo, sino acumular mas rondas reales.

Recomendacion minima:

```text
500 rondas resueltas para primer modelo serio
2,000+ rondas para empezar a comparar estrategias con confianza
```

### 2. Features nuevas

Buenas candidatas:

- distancia al baseline en unidades de ATR
- velocidad de acercamiento/alejamiento del baseline
- numero de cruces del baseline dentro de la ronda
- tiempo desde ultimo cruce del baseline
- max favorable excursion desde inicio de ronda
- max adverse excursion desde inicio de ronda
- pendiente de precio en los ultimos 1/3/5 minutos
- volatilidad realizada dentro de la ronda
- quote imbalance: bid_size / ask_size por lado
- spread relativo
- diferencia entre probabilidad del modelo y midpoint de mercado
- edge ajustado por spread
- cambio de odds desde bucket anterior
- consenso: direccion del modelo base vs mercado

### 3. Calibracion de probabilidades

Para trading importa mucho que `prob_up = 0.62` signifique algo cercano a 62%.

Mejoras:

- calibracion isotonic
- Platt scaling
- calibration curve por bucket
- brier score por bucket

### 4. Evaluacion separada por uso

No usar una sola accuracy global.

Separar:

- initial 15m accuracy
- accuracy por bucket
- accuracy por modelo
- PnL por estrategia
- PnL por alignment
- performance reciente vs historica

### 5. Walk-forward validation

El siguiente salto serio es entrenar por ventanas:

```text
train dias 1-7 -> test dia 8
train dias 2-8 -> test dia 9
...
```

Eso simula mejor el uso real.

## Regla operativa

No activar un modelo solo porque tiene mayor accuracy.

Checklist para activar:

1. Tiene al menos 500 rondas reales.
2. Mejora brier/log_loss frente al modelo actual.
3. No empeora `Initial 15m accuracy`.
4. No empeora PnL de la estrategia conservadora.
5. No depende de pocos buckets o pocas señales.
6. Recent performance no esta deteriorando.

## Separacion correcta de metricas

`signals_v2` y `trade_results_v2` son performance live historica del collector. Representan lo que el sistema genero en vivo con el modelo que estaba activo en ese momento.

El dashboard ahora separa tres lecturas:

```text
Active Model Forecast
  -> predictions_v2 filtrado por active_model_version

Strategy Lab
  -> strategy_backtest_signals + replay dinamico de forecasts resueltos nuevos

Actual Live Paper PnL
  -> signals_v2 + trade_results_v2 filtrado por active_model_version
```

`Strategy Lab` abre por defecto para que el dashboard no quede vacio cuando un modelo nuevo todavia no emitio BUY signals. Esa vista sirve para comparar estrategias y buckets, pero no es dinero live real.

`Actual Live Paper PnL` responde cuanto esta ganando/perdiendo el modelo activo desde que empezo a generar señales live. Si aparece en cero, revisa `Why no live trades?`; normalmente significa que las predicciones existen, pero el collector esta dejando la accion en `WAIT`.

El backtest historico queda versionado en:

```text
strategy_backtest_runs
strategy_backtest_signals
strategy_backtest_performance_v2
```

Flujo correcto:

```text
1. Entrenar modelo.
2. Activar modelo.
3. Dashboard mide forecast quality con `predictions_v2`.
4. Dashboard mide live PnL real con `signals_v2 + trade_results_v2`.
5. Opcionalmente ejecutar backtest del modelo activo.
6. Usar el selector del dashboard para comparar Strategy Lab vs Actual Live Paper PnL vs Backtest Active Model vs All Live Audit.
```
