# Polymarket BTC 5m Research Bot

Sistema experimental para observar mercados BTC Up/Down de 5 minutos en
Polymarket, capturar datos de Binance y Polymarket, guardar telemetria en
Supabase, generar predicciones UP/DOWN y evaluar si una entrada simulada habria
ganado despues de pagar el precio real de entrada.

> Aviso: esto es investigacion y backtesting. No es asesoramiento financiero.
> Un win rate historico no garantiza resultados futuros, y una estrategia puede
> acertar muchas rondas y aun asi perder dinero si compra caro.

## Idea Principal

Cada ronda de Polymarket tiene un baseline/corte. La pregunta operativa es:

```text
Al cierre de la ventana de 5 minutos, BTC estara arriba o abajo del baseline?
```

Pero la pregunta de trading es mas exigente:

```text
La probabilidad estimada por el modelo es mayor que el precio ask que debo pagar
en Polymarket, despues de considerar spread, liquidez y tiempo restante?
```

Por eso el proyecto separa tres capas:

- Modelo: estima `P(UP)` y `P(DOWN)`.
- Mercado: captura bid/ask, spread, midpoint y tamanos del orderbook de Polymarket.
- Estrategia: calcula edge y decide `WAIT`, `BUY_UP` o `BUY_DOWN`.

## Estado Actual

El sistema ya tiene:

- Dashboard Flask en `http://localhost:5000`.
- Collector Docker que guarda snapshots cada pocos segundos.
- Supabase como almacenamiento principal.
- Vistas SQL para modelado.
- Notebook de analisis en `notebooks/01_supabase_eda.ipynb`.
- Runner no interactivo en `scripts/analyze_supabase.py`.
- Modelo original entrenado con Binance.
- Modelo Supabase/Polymarket en `model_artifacts/model_supabase.pkl`.

El modelo activo del dashboard se controla con:

```text
ACTIVE_MODEL=market-aware-v1
```

Con la configuracion actual, la web usa `market-aware-v1`. Ese modelo toma
features tecnicas, quotes de Polymarket y la salida del modelo Binance como
senal auxiliar. Si `model_supabase.pkl` no existe o falla, el sistema cae al
baseline `model.pkl`.

## Arquitectura

```text
Binance API
  |
  | 1m candles: OHLCV, trades, taker buy volume
  v
features.py
  |
  | indicadores tecnicos + contexto de ventana 5m
  v
model_runtime.py
  |
  | selecciona binance-hgb-v1 o market-aware-v1
  v
collector.py / server.py
  |
  +-------------------- Polymarket Gamma API
  |                       descubre mercado BTC 5m activo
  |
  +-------------------- Polymarket CLOB API
                          captura orderbook UP/DOWN
  |
  v
edge = model_probability - polymarket_best_ask
  |
  v
Supabase
  |
  v
Dashboard, reportes, Jupyter, entrenamiento futuro
```

## Componentes

| Archivo | Proposito |
|---|---|
| `server.py` | Backend Flask, dashboard, APIs live, persistencia best-effort de predicciones. |
| `collector.py` | Proceso continuo que captura snapshots, quotes, predicciones, apuestas simuladas y resultados. |
| `db.py` | Cliente REST de Supabase/PostgREST. Inserta, consulta, actualiza y borra registros. |
| `polymarket.py` | Descubre mercados BTC 5m y lee orderbooks desde APIs publicas de Polymarket. |
| `features.py` | Construye indicadores tecnicos sobre velas Binance. |
| `model.py` | Entrenamiento historico desde Binance 1m. Produce `model.pkl`. |
| `model_runtime.py` | Carga modelos, arma features live y selecciona la prediccion activa. |
| `scripts/train_from_supabase.py` | Entrenamiento experimental usando `modeling_snapshots`. |
| `scripts/analyze_supabase.py` | Ejecuta el analisis del dataset y genera reporte Markdown/JSON. |
| `scripts/dataset_report.py` | Reporte rapido de conteos y registros recientes. |
| `scripts/cleanup_from_cutoff.py` | Limpieza de datos anteriores a un timestamp de corte. |
| `dashboard/` | UI web: prediccion, TradingView, historial, senales simuladas, stats. |
| `migrations/` | SQL para crear tablas y vistas en Supabase. |
| `notebooks/` | Laboratorio Jupyter para EDA y modelado. |

## Fuentes De Datos

### Binance

Se usa la API publica de Binance para `BTCUSDT`:

```text
https://api.binance.com/api/v3/klines
```

Datos capturados:

- open, high, low, close
- volumen BTC
- volumen USDT
- numero de trades
- taker buy base
- taker buy quote

Uso principal:

- calcular features tecnicas
- inferir baseline si Polymarket no entrega uno explicito
- resolver resultados de ronda cuando termina la ventana

### Polymarket

Se usan APIs publicas:

- Gamma API: descubrir mercado/evento activo.
- CLOB API: leer orderbook de cada token UP/DOWN.

Datos capturados:

- condition id
- event slug
- question
- token UP
- token DOWN
- best bid
- best ask
- midpoint
- spread
- bid size
- ask size
- last trade price
- top 10 bids/asks en `raw`

Uso principal:

- saber a que precio real podrias entrar
- calcular edge
- evaluar si el modelo compensa el costo de mercado

## Flujo De Datos En Vivo

Cada ciclo del collector:

1. Calcula la ronda activa y el `round_cutoff`.
2. Descarga velas recientes de Binance.
3. Detecta el mercado BTC 5m correspondiente en Polymarket.
4. Guarda un snapshot de BTC en `round_snapshots`.
5. Guarda quotes UP/DOWN en `polymarket_quotes`.
6. Ejecuta el modelo activo definido por `ACTIVE_MODEL`.
7. Guarda la prediccion en `model_predictions`.
8. Calcula edge:

```text
edge_up = prob_up - up_best_ask
edge_down = prob_down - down_best_ask
```

9. Si el edge supera `EDGE_THRESHOLD`, guarda una entrada simulada en
   `simulated_bets`.
10. Cuando una ronda termina, resuelve `round_results` y actualiza PnL de bets.

## Supabase

Configura `.env`:

```text
SUPABASE_URL=https://PROJECT_REF.supabase.co
SUPABASE_SERVICE_ROLE_KEY=...
```

Usa la service role key solo en backend/local/Docker. Nunca la pongas en
frontend.

Ejecuta en Supabase SQL Editor:

```text
migrations/001_supabase_schema.sql
migrations/002_modeling_views.sql
```

Si modificas las vistas, vuelve a ejecutar `002_modeling_views.sql`.

La version actual de `modeling_snapshots` incluye `model_version`. Si tu
dashboard muestra todas las filas como `historical`, vuelve a ejecutar
`migrations/002_modeling_views.sql` en Supabase para que la vista exponga esa
columna.

## Tablas

### `polymarket_markets`

Catalogo de mercados detectados.

| Columna | Proposito |
|---|---|
| `condition_id` | Identificador unico del mercado CLOB. |
| `question` | Texto del mercado. |
| `slug`, `event_slug` | Slugs usados para descubrir/sincronizar rondas. |
| `baseline` | Baseline si se pudo extraer. Actualmente puede venir nulo. |
| `token_up`, `token_down` | Token IDs de contratos UP/DOWN. |
| `raw` | Payload original recortado para auditoria. |

### `round_snapshots`

Fotos del estado BTC durante una ronda. Es la tabla central de observacion.

| Columna | Proposito |
|---|---|
| `observed_at` | Momento exacto de captura. |
| `round_cutoff` | Timestamp unix del cierre de la ronda. |
| `window_start` | Inicio de la ventana de 5 minutos. |
| `seconds_to_cutoff` | Segundos restantes cuando se tomo la decision. |
| `btc_price` | Precio BTC observado desde Binance. |
| `baseline` | Precio a vencer. |
| `dist_to_baseline` | Diferencia absoluta entre BTC y baseline. |
| `dist_to_baseline_pct` | Diferencia porcentual. |
| `market_condition_id` | Link al mercado Polymarket. |

### `polymarket_quotes`

Top-of-book por lado UP/DOWN.

| Columna | Proposito |
|---|---|
| `outcome` | `UP` o `DOWN`. |
| `best_bid` | Mejor precio de compra disponible. |
| `best_ask` | Precio real al que entrarias si compras. |
| `midpoint` | Punto medio entre bid y ask. |
| `spread` | Costo/friccion del mercado. |
| `bid_size`, `ask_size` | Liquidez visible al mejor precio. |
| `raw` | Top 10 bids/asks y metadata CLOB. |

### `model_predictions`

Cada prediccion emitida por el modelo.

| Columna | Proposito |
|---|---|
| `prediction` | `UP` o `DOWN`. |
| `prob_up`, `prob_down` | Probabilidades del modelo. |
| `confidence` | Distancia a 50/50. |
| `edge_up`, `edge_down` | Probabilidad menos ask de Polymarket. |
| `recommended_action` | `WAIT`, `BUY_UP` o `BUY_DOWN`. |
| `feature_values` | Features tecnicas calculadas en ese instante. |
| `source_snapshot_id` | Link al snapshot usado. |
| `raw` | Payload y quotes asociados. |

### `round_results`

Resultado final por ronda unica.

| Columna | Proposito |
|---|---|
| `round_cutoff` | Ronda resuelta. |
| `baseline` | Baseline usado para evaluar. |
| `actual_close` | Precio de cierre/resolucion inferido. |
| `outcome` | `UP`, `DOWN` o `TIE`. |

Esta tabla es critica para entrenamiento supervisado.

### `simulated_bets`

Entradas simuladas generadas cuando el edge supera el umbral.

| Columna | Proposito |
|---|---|
| `side` | Lado comprado: `UP` o `DOWN`. |
| `entry_price` | Precio ask pagado. |
| `stake` | Tamano teorico de apuesta. |
| `model_prob` | Probabilidad usada para entrar. |
| `edge` | Ventaja estimada al momento de entrar. |
| `result` | `OPEN`, `WIN` o `LOSS`. |
| `pnl` | PnL simulado. |
| `raw` | Snapshot completo de la decision y resolucion. |

## Vistas De Modelado

### `modeling_snapshots`

Dataset limpio para ML. Une:

- `model_predictions`
- `round_snapshots`
- `round_results`

Incluye:

- features tecnicas
- `model_version`
- segundos restantes
- precio y baseline
- probabilidad del modelo anterior
- edge
- outcome final
- `target_up`
- `prediction_raw` con quotes asociadas

Uso:

```bash
python scripts/train_from_supabase.py
```

### `simulated_bet_performance`

Vista orientada a performance de estrategia. Extrae desde `simulated_bets.raw`:

- segundos restantes
- baseline
- btc_price
- actual_close
- outcome

### `model_bucket_performance`

Agrupa apuestas por:

- bucket de tiempo restante
- bucket de edge
- lado UP/DOWN

Sirve para responder:

```text
En que escenarios realmente conviene entrar?
```

## Modelos

Hay dos artefactos principales:

```text
model_artifacts/model.pkl
model_artifacts/model_supabase.pkl
```

El dashboard usa `ACTIVE_MODEL`. Por defecto:

```text
ACTIVE_MODEL=market-aware-v1
```

### `binance-hgb-v1`

Artefacto:

```text
model_artifacts/model.pkl
```

Entrenamiento:

```bash
python model.py
```

Fuente:

```text
Binance BTCUSDT 1m candles
```

Target:

```text
close de ventana 5m > open/baseline de ventana 5m
```

Algoritmo:

```text
HistGradientBoostingClassifier
CalibratedClassifierCV
```

Familias de features:

- distancia a baseline
- minuto dentro de ventana
- retornos 1/5/10/15/30
- SMA/EMA y cruces
- RSI, MACD, stochastic, Williams %R, CCI, ROC
- ATR, Bollinger Bands, rolling volatility
- OBV, MFI, VWAP, volumen relativo, taker buy ratio
- cuerpo/sombras de vela
- hora y dia de semana codificados como seno/coseno

Limitacion clave:

```text
El modelo original aprende direccion de BTC, no rentabilidad despues de comprar
en Polymarket.
```

Por eso puede verse bien en accuracy y mal en PnL.

### `market-aware-v1`

Artefacto:

```text
model_artifacts/model_supabase.pkl
```

Este es el modelo activo por defecto. Para construir su feature vector live:

1. Calcula features tecnicas desde Binance.
2. Ejecuta `binance-hgb-v1` para obtener una probabilidad base.
3. Captura quotes UP/DOWN desde Polymarket.
4. Calcula edge base contra el ask.
5. Arma las 82 columnas esperadas por `model_supabase.pkl`.
6. Emite la prediccion final `market-aware-v1`.

El modelo viejo no se toma como verdad unica. En `market-aware-v1` es solo una
feature mas, igual que RSI, spread o distancia al baseline.

## Entrenamiento Desde Supabase

Entrenamiento:

```bash
python scripts/train_from_supabase.py
```

Salida:

```text
model_artifacts/model_supabase.pkl
model_artifacts/metrics_supabase.json
```

Este script usa:

- features tecnicas guardadas en `feature_values`
- contexto de ronda
- probabilidad del modelo anterior
- edge
- features de quotes Polymarket si `prediction_raw` existe en la vista

Metricas guardadas:

- accuracy
- ROC-AUC
- Brier score
- log loss
- numero de filas
- numero de rondas unicas
- cantidad de features
- si uso features de Polymarket

Importante:

```text
No uses random split para confiar en un modelo temporal.
```

El split debe ser cronologico. Mas adelante conviene hacer walk-forward:

1. Entrenar con periodo A.
2. Validar en periodo B.
3. Mover ventana.
4. Medir ROI, drawdown y calibracion.

## Por Que Accuracy Puede Mentir

El collector guarda una muestra cada pocos segundos. En una misma ronda puede
haber muchas predicciones.

Si una ronda fue facil y el modelo acerto, eso suma muchas filas correctas.
Por eso hay dos metricas:

- row-level accuracy: precision por snapshot.
- unique-round accuracy: precision por ronda unica.

Para trading, lo mas importante es:

- PnL simulado
- win rate de bets cerradas
- performance por segundos restantes
- performance por edge
- performance por spread/liquidez

## Edge Y Umbral

Configuracion:

```text
EDGE_THRESHOLD=0.07
```

Regla:

```text
BUY_UP si prob_up - up_best_ask >= EDGE_THRESHOLD
BUY_DOWN si prob_down - down_best_ask >= EDGE_THRESHOLD
WAIT si no hay suficiente ventaja
```

El umbral anterior era `0.03`. Si hay muchas entradas y PnL negativo, subir a
`0.07` reduce ruido y fuerza mejores oportunidades. No soluciona calibracion por
si solo; solo filtra.

## Docker

Levantar dashboard:

```bash
docker compose up -d --build app
```

Levantar dashboard + collector:

```bash
docker compose --profile collector up -d --build
```

Ver estado:

```bash
docker compose ps
```

Ver logs del collector:

```bash
docker compose logs --tail=80 collector
```

Usar menu Windows:

```bat
run_docker.bat
```

El dashboard queda en:

```text
http://localhost:5000
```

Para que Supabase siga llenandose, el servicio `collector` debe estar activo.

## Jupyter Y Analisis

Notebook:

```text
notebooks/01_supabase_eda.ipynb
```

Instalacion recomendada con Python 3.10/3.11:

```bash
pip install -r requirements-dev.txt
jupyter lab notebooks
```

En Windows:

```bat
run_jupyter.bat
```

Si tu Python local no soporta JupyterLab, ejecuta el analisis equivalente:

```bash
python scripts/analyze_supabase.py
```

Genera:

```text
reports/supabase_eda_report.md
reports/supabase_eda_report.json
```

El reporte muestra:

- conteos por tabla
- accuracy por fila
- accuracy por primera prediccion de ronda
- accuracy por ultima prediccion de ronda
- win rate y PnL de apuestas simuladas
- performance por segundos restantes
- performance por edge
- simulacion de umbrales `EDGE_THRESHOLD`

## Manual De Uso

Lee [docs/USER_MANUAL.md](docs/USER_MANUAL.md) para interpretar el dashboard:

- que significa baseline
- cuando usar `SYNC`
- como leer `BUY UP`, `BUY DOWN` y `WAIT`
- diferencia entre `PREDICCION`, `OUTCOME` y `ACIERTO`
- como interpretar row accuracy, first-round accuracy, win rate y PnL

## Scripts Utiles

```bash
python scripts/dataset_report.py
```

Muestra conteos y registros recientes.

```bash
python scripts/analyze_supabase.py
```

Analisis tipo notebook sin UI.

```bash
python scripts/train_from_supabase.py
```

Entrena artefacto experimental desde Supabase.

```bash
python scripts/cleanup_from_cutoff.py
```

Limpia datos anteriores al cutoff configurado en el script.

## Endpoints

| Endpoint | Proposito |
|---|---|
| `GET /` | Dashboard web. |
| `GET /api/predict` | Prediccion live actual. |
| `GET /api/price?minutes=120` | Velas/precios recientes. |
| `GET /api/rounds` | Historial de rondas. |
| `GET /api/stats` | Stats de bets simuladas desde Supabase. |
| `GET /api/signals` | Senales simuladas recientes. |
| `GET /api/model-info` | Metricas y features del modelo activo. |
| `GET /api/model-performance` | Performance de modelos, estrategia, PnL y dataset. |
| `GET /api/dataset-health` | Conteos y salud de Supabase. |
| `POST /api/sync_history_baseline` | Sincronizacion manual de baseline. |
| `POST /api/retrain` | Reentrena modelo Binance y recarga artefactos. |

## Variables De Entorno

| Variable | Default | Proposito |
|---|---:|---|
| `APP_HOST` | `0.0.0.0` | Host Flask. |
| `APP_PORT` | `5000` | Puerto Flask. |
| `ACTIVE_MODEL` | `market-aware-v1` | Modelo live: `market-aware-v1` con fallback a Binance. |
| `SUPABASE_URL` | vacio | URL del proyecto Supabase. |
| `SUPABASE_SERVICE_ROLE_KEY` | vacio | Key backend para PostgREST. |
| `POLYMARKET_EVENT_SLUG` | vacio | Fijar evento si discovery falla. |
| `POLYMARKET_MARKET_SLUG` | vacio | Fijar mercado si discovery falla. |
| `POLYMARKET_SEARCH_QUERY` | `BTC arriba abajo 5 m` | Query de discovery. |
| `COLLECTOR_INTERVAL_SECONDS` | `5` | Frecuencia de captura. |
| `COLLECTOR_STAKE_SIZE` | `10` | Stake simulado por entrada. |
| `EDGE_THRESHOLD` | `0.07` | Edge minimo para simular compra. |
| `BINANCE_SYMBOL` | `BTCUSDT` | Simbolo Binance. |
| `MIN_MODELING_ROWS` | `500` | Minimo para entrenar desde Supabase. |

## Storage Y Saturacion

El mayor riesgo de almacenamiento es guardar orderbooks completos cada pocos
segundos. Por eso el sistema guarda:

- columnas resumidas en `polymarket_quotes`
- top 10 bids/asks en `raw`

Estimacion simple si capturas cada 5 segundos:

```text
12 snapshots por minuto
720 snapshots por hora
17,280 snapshots por dia
```

Quotes suelen ser 2 por snapshot:

```text
34,560 quote rows por dia
```

Para investigacion esta bien. Mas adelante conviene:

- particionar por fecha si crece mucho
- borrar raw profundo antiguo
- agregar tablas agregadas por ronda
- guardar solo snapshots cercanos a decisiones reales

## Limitaciones Conocidas

- El baseline exacto de Polymarket puede no venir explicitamente en la API; se
  infiere con Binance cuando falta.
- Binance y Polymarket pueden no estar perfectamente sincronizados.
- `model.pkl` no fue entrenado con costos de mercado.
- La precision por fila sobreestima rendimiento por rondas repetidas.
- El discovery de Polymarket puede romperse si cambian slugs/formato.
- No hay suite formal de tests.
- JupyterLab puede no instalarse en Python 3.14; usa Python 3.10/3.11 o el
  runner `scripts/analyze_supabase.py`.

## Roadmap Recomendado

1. Dejar el collector corriendo 24/7.
2. Mantener `EDGE_THRESHOLD=0.07` mientras juntamos mas evidencia.
3. Usar `scripts/analyze_supabase.py` cada cierto tiempo para revisar PnL real.
4. Reejecutar `migrations/002_modeling_views.sql` cuando cambie la vista.
5. Entrenar `model_supabase.pkl` con mas rondas resueltas.
6. Agregar validacion walk-forward.
7. Crear features de microestructura:
   - spread UP/DOWN
   - liquidez ask/bid
   - quote imbalance
   - cambios de quote entre snapshots
   - distancia al baseline por bucket de tiempo
8. Evaluar estrategia por ROI y drawdown, no solo accuracy.
9. Solo promover el modelo Supabase a produccion cuando gane fuera de muestra.
