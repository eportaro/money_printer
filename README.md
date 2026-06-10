# Polymarket BTC 15m Prediction Bot

Bot para seguir mercados BTC UP/DOWN de 15 minutos en Polymarket. El dashboard tiene dos usos distintos:

- Prediccion inicial de ronda: se guarda al inicio de cada ronda y sirve para medir si el modelo realmente anticipa 15 minutos.
- Senal live: cambia durante la ronda y compara probabilidad del modelo contra el ask real de Polymarket para recomendar `BUY_UP`, `BUY_DOWN` o `WAIT`.

## Guia del dashboard

La guia completa de lectura operativa del dashboard esta en:

```text
docs/USER_MANUAL.md
```

Ese documento explica cada seccion de la interfaz, la diferencia entre accuracy y PnL, estrategias, buckets, alignment, drift y como tomar decisiones sin mezclar metricas.

La guia de entrenamiento, versionado y activacion de modelos esta en:

```text
docs/MODEL_TRAINING.md
```

## Estado actual

- Horizonte: 15 minutos (`WINDOW_SECONDS=900`).
- Precio live y baseline proxy: Coinbase `BTC-USD`.
- Modelo base: entrenado con 30 dias de velas Binance `BTCUSDT` 1m.
- Modelo activo: `hist_gradient_boosting-v2-20260531080901` (HistGradientBoosting, ROC-AUC honesto 0.708 con split por ronda, 199 features, ~354 rondas al entrenar). Nota: la accuracy global (~63%) esta inflada por buckets tardios; la metrica anticipatoria real es la "Initial-15m accuracy" (~51%). Ver `docs/MODEL_TRAINING.md`.
- Persistencia recomendada: SQL Server Docker `localhost,14333` / `PolymarketBot`.
- Collector: captura en buckets programados, no en cada tick adaptivo.
- Dashboard: `http://localhost:5000`.

> **Nota de arquitectura**: el modelo market-aware (`model_supabase.pkl`) se activa cuando `ACTIVE_MODEL=market-aware-v1` en el `.env`. En inferencia, las features técnicas se envían con prefijo `feat__` y las quotes de Polymarket con nombres `up_*`/`down_*` para coincidir con el schema de entrenamiento.

## Estructura

```text
collector.py                  # Ingesta de rondas, snapshots, quotes, predicciones y resultados
tick_collector.py             # Captura tick-a-tick websocket (Coinbase + Polymarket CLOB) para lead-lag
server.py                     # Flask API + dashboard
model.py                      # Entrena el modelo base con 30 dias de Binance
features.py                   # Indicadores tecnicos
microstructure.py             # Features de microestructura (perp basis/funding, book imbalance)
model_runtime.py              # Carga modelo base y modelo market-aware
price_feed.py                 # Coinbase/Binance candles + oraculo Pyth
polymarket.py                 # Descubrimiento de mercados y quotes Polymarket
db.py / db_sqlserver.py       # Persistencia SQL Server (db.py es fachada)
market_config.py              # Ventanas 15m y slugs esperados
migrations/004_sqlserver_schema.sql
scripts/train_model_v2.py     # Entrena modelo market-aware desde SQL Server
scripts/strategy_walkforward.py  # Walk-forward honesto de estrategias (con fees reales)
scripts/analyze_leadlag.py    # Mide el lag spot->Polymarket con la data de tick_collector
dashboard/                    # Frontend
```

## Setup local con SQL Server

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
sqlcmd -S localhost -E -C -i migrations\004_sqlserver_schema.sql
python model.py
python server.py
```

En otra terminal:

```powershell
venv\Scripts\activate
python collector.py
```

Para uso normal en Windows tambien puedes abrir:

```text
start.bat
```

Ese script levanta `collector.py` en una ventana y `server.py` en otra. Para detener ambos:

```text
stop_bot.bat
```

El `.env` relevante:

```env
PRICE_SOURCE=coinbase
COINBASE_PRODUCT_ID=BTC-USD
BINANCE_SYMBOL=BTCUSDT
WINDOW_SECONDS=900
POLYMARKET_INTERVAL=15m

SQLSERVER_CONNECTION=DRIVER={ODBC Driver 18 for SQL Server};SERVER=localhost;DATABASE=PolymarketBot;Trusted_Connection=yes;Encrypt=no;TrustServerCertificate=yes;

ADAPTIVE_CAPTURE_ENABLED=false
ACTIVE_MODEL=market-aware-v1
# Estrategia live value-aligned-v1 (la unica walk-forward-positiva con fees):
EDGE_THRESHOLD=0.10
SIGNAL_MAX_ENTRY_PRICE=0.40
SIGNAL_REQUIRE_ALIGNED=true
SIGNAL_EXCLUDE_BELOW_SECONDS=120
MIN_ASK_SIZE=50
```

Para la captura tick-a-tick (estudio de latencia spot→Polymarket):

```powershell
docker compose --profile ticks up -d
python scripts\analyze_leadlag.py --day 20260611
```

## Arranque con Docker

La ruta recomendada para no depender de ventanas CMD abiertas es Docker:

```text
run_docker.bat
```

O por consola:

```powershell
docker compose --profile collector up -d --build
```

Esto levanta:

- `sqlserver`: SQL Server 2022 en Docker.
- `migrate`: crea `PolymarketBot` y aplica `migrations/004_sqlserver_schema.sql`.
- `app`: dashboard Flask en `http://localhost:5000`.
- `collector`: ingesta continua de rondas/snapshots.

Para ver logs:

```powershell
docker compose logs -f app collector
```

Para detener:

```text
stop_docker.bat
```

O:

```powershell
docker compose --profile collector down
```

Nota: Docker usa `.env.docker` y un SQL Server dentro de Docker. La data historica que estaba en tu SQL Server local fue fusionada hacia Docker con `scripts\merge_sqlserver_data.py`; desde ahora Docker debe tratarse como la base canonica para no volver a partir la data.

## Buckets de decision

El collector guarda snapshots en estos momentos antes del cierre de la ronda de 15m:

`895, 840, 720, 600, 480, 360, 240, 180, 120, 90, 60, 30, 15, 5`

Para trading manual, el dashboard live es mas util entre `T-120s` y `T-30s`: ya hay bastante movimiento dentro de la ronda y todavia suele haber tiempo para apostar. La tabla historica mide aparte la prediccion inicial para saber si el modelo sirve a 15 minutos reales.

## Entrenamiento

Modelo base:

```powershell
python model.py
```

Esto baja 30 dias de Binance 1m y entrena `model_artifacts/model.pkl`.

Modelo market-aware con datos propios:

```powershell
python scripts\train_model_v2.py
```

Este script usa rondas cerradas de SQL Server (`training_decision_snapshots`). Por defecto exige muchas rondas resueltas (`MIN_UNIQUE_ROUNDS=500`) para evitar entrenar un modelo falso con muy poca data.

Por seguridad, ahora genera un candidato versionado y NO reemplaza el modelo live salvo que uses `--activate`:

```powershell
python scripts\train_model_v2.py --min-rounds 500 --limit 50000
python scripts\train_model_v2.py --min-rounds 500 --limit 50000 --activate
```

Si estas usando Docker, entrena dentro del contenedor para usar la base Docker fusionada:

```powershell
docker compose exec app python scripts/train_model_v2.py --min-rounds 500 --limit 50000 --activate
docker compose exec app python scripts/backtest_active_model.py --model-version active --strategy-set-version dashboard-strategies-v1
docker compose restart app collector
```

El dashboard abre por defecto en `Strategy Lab`: usa el backtest versionado y replay dinamico para comparar estrategias y buckets sin quedarse vacio cuando un modelo nuevo aun no emitio señales live. La lectura de dinero real simulado esta separada en `Actual Live Paper PnL`, que usa `signals_v2 + trade_results_v2` filtrado por el modelo activo.

El modelo live se carga desde:

```text
model_artifacts/model_supabase.pkl
```

Luego se puede cambiar:

```env
ACTIVE_MODEL=market-aware-v1
```

Modelo market-aware historico/simulado:

```powershell
python scripts\train_historical_polymarket_sim.py
```

Este usa velas historicas 1m, reconstruye rondas de 15 minutos y simula quotes UP/DOWN tipo Polymarket con distancia al baseline, volatilidad reciente y spread. Sirve para arrancar con un modelo market-aware antes de tener cientos de rondas live guardadas. La data live de SQL Server sigue siendo importante porque despues reemplaza la simulacion por odds reales.

## Verificaciones utiles

```powershell
Invoke-RestMethod http://localhost:5000/api/dataset-health | ConvertTo-Json -Depth 6
Invoke-RestMethod http://localhost:5000/api/model-performance | ConvertTo-Json -Depth 6
python scripts\train_model_v2.py --min-rounds 5 --limit 1000
```

Si el trainer dice `not_enough_resolved_rounds`, no es error: significa que falta dejar correr el collector para juntar rondas cerradas.
