# Guia Completa del Dashboard Polymarket BTC Bot

Este dashboard no es un boton automatico de compra. Es una mesa de decision para entender, en pocos segundos, cuatro cosas distintas:

1. Que cree el modelo.
2. Que esta pagando Polymarket.
3. Si existe edge real.
4. Si la estrategia activa permite operar o debe esperar.

La idea central es separar dos preguntas que parecen iguales, pero no lo son:

| Pregunta | Metrica | Donde vive |
|---|---|---|
| El modelo acerto UP/DOWN? | Model accuracy | `predictions_v2` + `round_results` |
| La operacion simulada gano dinero? | Trade PnL / win rate | `signals_v2` + `trade_results_v2` |

Un bucket puede tener muy buena accuracy y mal PnL si la estrategia compra una posicion cara, entra tarde o compra una senal contraria tipo longshot.

---

## 1. Como leer el dashboard en 10 segundos

Cuando abras `http://localhost:5000`, mira en este orden:

1. **Header**: estado general del sistema.
2. **Current Round Decision**: decision actual de la ronda.
3. **Signal Alignment**: si la senal sigue o contradice al modelo.
4. **Strategy Selector**: que reglas estas usando.
5. **Bucket Performance**: en que ventana historicamente conviene operar.
6. **Recent Performance / Drift**: si el sistema viene mejorando o empeorando.

La lectura rapida deberia sonar asi:

```text
BTC esta debajo del baseline.
El modelo cree DOWN.
Polymarket paga DOWN a 0.57.
La estrategia Directional Conservative no opera porque el edge no alcanza.
Mejor zona historica de trading: T-480 a T-240.
Evitar ultimos 60 segundos.
```

---

## 2. Header principal

El header muestra el estado general.

| Campo | Significado |
|---|---|
| BTC | Precio BTC actual segun el feed configurado. Hoy se usa Coinbase BTC-USD como referencia live. |
| Round closes | Tiempo restante hasta el cierre de la ronda actual de 15 minutos. |
| Model | Modelo activo usado para predecir. |
| Strategy | Estrategia seleccionada en la UI. |
| Sim PnL | PnL simulado acumulado de las senales cerradas. |
| Win rate | Porcentaje de senales simuladas ganadoras. |
| Signals | Cantidad de senales simuladas cerradas. |
| Last update | Hora local de la ultima actualizacion del dashboard. |
| LIVE | Indica que la web esta refrescando datos. No garantiza que el collector este perfecto, pero indica que el dashboard responde. |

Importante:

```text
Win rate bajo no siempre significa perdida.
PnL positivo puede existir con win rate bajo si algunas entradas pagaron mucho.
```

Ejemplo:

```text
Win rate: 40.9%
PnL: +$791.25
```

Eso puede pasar si se compraron contratos baratos, se perdieron muchos, pero los pocos wins pagaron bastante.

---

## 3. Current Round Decision

Esta es la seccion mas importante para operar.

### 3.1 Decision grande

Puede mostrar:

| Decision | Significado |
|---|---|
| BUY UP | La estrategia activa permite comprar UP. |
| BUY DOWN | La estrategia activa permite comprar DOWN. |
| WAIT | No hay senal comprable en este momento. |
| SKIP | Hay una senal bruta, pero la estrategia activa la bloquea por reglas. |

Ejemplo:

```text
Decision: SKIP
Reason: Contrarian signal not allowed by Directional Conservative strategy.
```

Esto significa:

- El modelo/edge detecto algo.
- Pero la estrategia conservadora no lo permite.
- Por tanto, para esa estrategia no se opera.

### 3.2 Badges superiores

| Badge | Significado |
|---|---|
| T-240, T-360, etc. | Bucket actual: segundos antes del cierre. |
| ALIGNED | La accion sigue la prediccion principal del modelo. |
| CONTRARIAN | La accion compra el lado contrario al modelo, normalmente longshot barato. |
| WAIT | No hay compra activa. |
| BASELINE EXACT | Baseline viene de Polymarket/metadata exacta o fuente segura. |
| BASELINE PROXY | Baseline todavia es Coinbase/proxy. La estrategia debe bloquear operacion. |

Regla practica:

```text
Si ves BASELINE PROXY, no operes manualmente basado en esa ronda.
```

---

## 4. Card: Baseline / BTC Position

Muestra:

- Baseline de la ronda.
- BTC actual.
- Distancia al baseline.
- Distancia porcentual.
- Si BTC esta arriba o abajo.

Ejemplo:

```text
Baseline: $75,158.81
BTC actual: $75,120.00
Distance: -$38.81 (-0.05%)
BTC is currently below the round baseline.
```

Lectura:

- Si BTC cierra debajo del baseline, gana DOWN.
- Si BTC cierra arriba del baseline, gana UP.

Ojo:

El baseline de Polymarket viene de Chainlink/Data Streams. El precio live del dashboard puede venir de Coinbase como referencia. Por eso el sistema intenta usar baseline exacto de Polymarket para resolver y evaluar.

---

## 5. Card: Model Prediction

Muestra:

- Prediccion principal: UP o DOWN.
- Probabilidad UP.
- Probabilidad DOWN.
- Confianza.
- Modelo activo.

Ejemplo:

```text
Prediction: DOWN
UP: 21.0%
DOWN: 79.0%
Confidence: 58.0%
The model currently thinks DOWN is more likely.
```

Interpretacion:

- El modelo cree que DOWN es mas probable.
- Eso no significa automaticamente comprar DOWN.
- Para comprar, tambien debe haber precio/edge favorable.

---

## 6. Card: Market Pricing

Muestra lo que esta pagando Polymarket:

| Campo | Significado |
|---|---|
| UP price | Ask o precio de entrada aproximado para comprar UP. |
| DOWN price | Ask o precio de entrada aproximado para comprar DOWN. |
| Spread | Diferencia bid/ask. Spread alto es peor para operar. |
| Midpoint | Precio medio estimado entre bid y ask. |

Ejemplo:

```text
UP 0.44 / DOWN 0.57
Polymarket is pricing UP at 0.44 and DOWN at 0.57.
```

Lectura:

- Comprar a 0.57 significa pagar 57 centavos por contrato.
- Si ganas, cobras 1.00.
- Si pierdes, pierdes lo pagado.

---

## 7. Card: Edge

Edge compara la probabilidad del modelo contra el precio de mercado.

Formula conceptual:

```text
edge = probabilidad_modelo - precio_entrada
```

Ejemplo direccional:

```text
Modelo cree DOWN: 70%
Polymarket vende DOWN: 0.57
Edge DOWN = 70% - 57% = +13%
```

Eso es una oportunidad potencial.

Ejemplo contrarian:

```text
Modelo cree DOWN: 84%
UP tiene solo 16% de probabilidad segun modelo
Pero Polymarket vende UP a 0.02
Edge UP = 16% - 2% = +14%
```

Aqui la accion podria ser comprar UP aunque el modelo crea DOWN. Eso se llama longshot/value bet. Puede tener edge matematico, pero baja probabilidad de ganar.

---

## 8. Passed Rules / Failed Rules

Esta parte explica por que la estrategia compro, espero o bloqueo.

Ejemplo:

```text
Passed:
- edge >= 5%
- bucket T-360 allowed
- model probability >= 55%

Failed:
- signal must align with prediction
```

Lectura:

La senal tenia edge y estaba en un buen bucket, pero era contraria. Si la estrategia activa no permite contrarian, la decision final sera `SKIP`.

---

## 9. Signal Alignment

Esta seccion es clave.

| Caso | Alignment |
|---|---|
| Prediction UP + BUY_UP | ALIGNED |
| Prediction DOWN + BUY_DOWN | ALIGNED |
| Prediction DOWN + BUY_UP | CONTRARIAN |
| Prediction UP + BUY_DOWN | CONTRARIAN |
| WAIT | WAIT |
| No hay senal | NO_SIGNAL |

### ALIGNED

Significa:

```text
La operacion sigue la direccion principal del modelo.
```

Ejemplo:

```text
Prediction: DOWN
Action: BUY_DOWN
Alignment: ALIGNED
```

### CONTRARIAN

Significa:

```text
La estrategia esta comprando el lado barato contra la prediccion principal.
```

Ejemplo:

```text
Prediction: DOWN
Action: BUY_UP
Alignment: CONTRARIAN
```

Este fue el problema que vimos en T-60:

- El modelo acertaba muy bien.
- Pero algunas senales compraban el lado contrario porque estaba barato.
- Resultado: alta accuracy, pero trades perdedores.

---

## 10. Strategy Selector

Permite elegir como interpretar el edge.

### 10.1 Directional Conservative

Reglas:

- BUY_UP solo si `prediction = UP`.
- BUY_DOWN solo si `prediction = DOWN`.
- No permite contrarian.
- Buckets permitidos: T-480, T-360, T-240.
- Probabilidad minima: 55%.
- Edge minimo: 5%.
- Evita ultimos 120 segundos.

Uso recomendado:

```text
Es la estrategia mas sana para operar manualmente mientras validamos el sistema.
```

### 10.2 Directional Aggressive

Reglas:

- Sigue la direccion principal del modelo.
- Buckets permitidos: T-600, T-480, T-360, T-240, T-180.
- Probabilidad minima: 50%.
- Edge minimo: 3%.
- No permite contrarian por defecto.

Uso:

```text
Acepta mas senales, pero con mayor riesgo.
```

### 10.3 Value Bet / Longshot

Reglas:

- Puede comprar el lado barato aunque contradiga al modelo.
- Edge minimo: 10%.
- Entry price maximo: 0.25.
- Permite contrarian.

Uso:

```text
Busca apuestas baratas con valor esperado positivo.
```

Riesgo:

```text
Puede perder muchas veces seguidas.
```

### 10.4 No Last Minute

Reglas:

- Excluye T-120, T-90, T-60, T-30, T-15, T-5.
- Busca evitar ejecucion tardia, spreads raros y senales poco operables.

Uso:

```text
Buena estrategia si ves que los ultimos segundos tienen accuracy alta pero PnL malo.
```

### 10.5 Custom Strategy

Permite ajustar:

- Buckets permitidos.
- Min probability.
- Min edge.
- Max entry.
- Excluir ultimos X segundos.
- Permitir o bloquear contrarian.

Uso:

```text
Sirve para experimentar sin tocar codigo.
```

Importante:

```text
Custom Strategy no se guarda en SQL Server como estrategia oficial.
Se guarda en el navegador para que no pierdas la configuracion al refrescar.
```

La base de datos sigue guardando rondas, snapshots, quotes, features, predicciones, senales simuladas del collector y resultados. El dashboard toma esa data y calcula la lectura de cada estrategia encima. Si quieres que cada estrategia custom quede auditada en base de datos, hay que implementar una tabla tipo `strategy_configs` y un proceso de backtest/replay que escriba resultados por estrategia.

---

## 11. Strategy Performance

Compara estrategias como si cada una hubiera filtrado las senales historicas.

Columnas:

| Columna | Significado |
|---|---|
| Strategy | Nombre de la estrategia. |
| Signals | Cantidad de senales que esa estrategia habria permitido. |
| Win Rate | Porcentaje de operaciones ganadoras. |
| Total PnL | Ganancia/perdida simulada total. |
| Avg ROI | Retorno promedio por trade. |
| Best Bucket | Bucket con mejor PnL para esa estrategia. |
| Recent PnL | PnL en la ventana reciente seleccionada. |
| Alignment | Porcentaje de senales alineadas. |

Importante:

```text
Una estrategia con pocas senales puede verse muy buena por suerte.
```

Si ves `LOW SAMPLE`, no saques conclusiones fuertes.

---

## 12. Recent Performance / Drift

Mide si el performance reciente se esta desviando del historico.

Filtros:

- All time.
- Today.
- Last 24h.
- Last 12h.
- Last 6h.
- Last 20 rounds.
- Last 10 rounds.

Estados:

| Estado | Significado |
|---|---|
| IMPROVING | Reciente mejor que historico. |
| STABLE | Reciente consistente con historico. |
| DETERIORATING | Reciente peor que historico. |
| LOW SAMPLE | Muy pocos datos para concluir. |
| WATCH | Hay que observar mas. |

Ejemplo:

```text
All-time PnL: +$791.25
Recent PnL: -$90.00
Status: DETERIORATING
```

Lectura:

El sistema gano historicamente, pero ultimamente viene mal. Conviene bajar riesgo o esperar mas data.

---

## 13. Model Performance

Esta seccion evalua al modelo, no a la estrategia.

### Active Model Summary

Muestra:

- Modelo activo.
- Total de predicciones resueltas.
- Accuracy global.
- Accuracy reciente.
- Confianza promedio.

Lectura:

```text
El modelo puede acertar, pero la estrategia puede perder si compra mal.
```

### Accuracy by Bucket

Tabla por bucket:

| Columna | Significado |
|---|---|
| Bucket | Segundos antes del cierre. |
| Predictions | Predicciones resueltas en ese bucket. |
| Correct | Cuantas acertaron UP/DOWN. |
| Accuracy | Accuracy pura del modelo. |
| Avg Confidence | Confianza promedio. |
| Signal PnL | PnL de las senales generadas en ese bucket. |
| Comment | Lectura rapida. |

Ejemplo real que vimos:

```text
T-60:
Predictions: 37
Correct: 34
Accuracy: 91.9%
Signal PnL: -$60.00
Comment: Accurate, but trades lost due to contrarian/late signals.
```

Conclusion:

```text
T-60 predice bien, pero no necesariamente conviene operar ahi.
```

---

## 14. Timeframe / Bucket Performance

Esta es la seccion para decidir donde operar.

Columnas:

| Columna | Significado |
|---|---|
| Bucket | Tiempo antes del cierre. |
| Predictions | Cantidad de predicciones resueltas. |
| Accuracy | Accuracy pura del modelo en ese bucket. |
| Signals | Trades simulados generados en ese bucket. |
| Win Rate | Win rate de esos trades. |
| Total PnL | PnL simulado en ese bucket. |
| Avg ROI | ROI promedio. |
| Avg Entry | Precio promedio de entrada. |
| Aligned % | Porcentaje de senales alineadas. |
| Recommendation | GOOD / WATCH / AVOID / LOW_SAMPLE. |

### Como leer GOOD / WATCH / AVOID

| Recomendacion | Significado |
|---|---|
| GOOD | PnL positivo y muestra razonable. |
| WATCH | Resultado mixto o necesita mas datos. |
| AVOID | PnL negativo o senales problematicas. |
| LOW_SAMPLE | Muy pocas senales para confiar. |

Regla mental:

```text
Para operar, mira PnL por bucket antes que accuracy por bucket.
```

---

## 15. Graficos

### PnL by Bucket

Muestra donde se gana o pierde dinero simulado.

Lectura:

```text
Si T-360 tiene mucho verde y T-60 rojo, T-360 es mejor zona operativa.
```

### Accuracy by Bucket

Muestra donde el modelo acierta mas.

Lectura:

```text
Es normal que T-30/T-15/T-5 tengan alta accuracy porque falta poco.
```

### Alignment PnL

Separa PnL de:

- ALIGNED.
- CONTRARIAN.
- WAIT/NO_SIGNAL si aplica.

Lectura:

```text
Si CONTRARIAN pierde mucho, conviene usar Directional Conservative.
```

---

## 16. Signal History

Tabla de operaciones simuladas.

Columnas:

| Columna | Significado |
|---|---|
| Time | Hora de la senal. |
| Bucket | Momento dentro de la ronda. |
| Prediction | Lo que creia el modelo. |
| Action | Lo que la logica de edge queria comprar. |
| Alignment | ALIGNED o CONTRARIAN. |
| Entry | Precio de entrada simulado. |
| Model Prob | Probabilidad asignada al lado comprado. |
| Edge | Ventaja estimada. |
| Result | WIN / LOSS / OPEN. |
| PnL | Ganancia o perdida simulada. |
| Reason | Explicacion simple. |

Ejemplo:

```text
Prediction: DOWN
Action: BUY_UP
Alignment: CONTRARIAN
Entry: 0.01
Result: LOSS
Reason: Longshot bet.
```

Lectura:

El modelo probablemente tenia razon con DOWN, pero la estrategia compro UP porque estaba barato. Eso puede ser racional como value bet, pero es riesgoso.

---

## 17. Round History

Tabla de rondas cerradas o abiertas.

Columnas:

| Columna | Significado |
|---|---|
| Cutoff | Hora de cierre de la ronda. |
| Baseline | Precio a superar. |
| Initial Prediction | Primera prediccion de la ronda. |
| Final Prediction | Ultima prediccion guardada antes del cierre. |
| Actual Close | Cierre real de Polymarket/Chainlink cuando existe. |
| Outcome | UP o DOWN. |
| Initial Correct? | Si la prediccion inicial acerto. |
| Final Correct? | Si la prediccion final acerto. |
| Trade Taken? | Si hubo senales simuladas. |
| Trade PnL | PnL total de esas senales. |

Importante:

```text
Initial Prediction mide capacidad real de anticipar 15 minutos.
Final Prediction mide lectura cerca del cierre.
Trade PnL mide si convenia operar.
```

No mezcles esas tres cosas.

---

## 18. Caso especial: por que T-60 puede tener 91% accuracy y 6 losses

Esto ya paso en tus datos.

Resultado real:

```text
T-60 predicciones: 37
Correctas: 34
Accuracy: 91.9%

T-60 senales: 6
Wins: 0
Losses: 6
```

No es contradiccion.

Lo que paso:

```text
El modelo predijo correctamente el outcome.
Pero la logica de edge compro el lado contrario porque estaba barato.
```

Ejemplo:

```text
Prediction: DOWN
Outcome: DOWN
Prediccion correcta: SI

Action: BUY_UP
Trade result: LOSS
```

Conclusion:

```text
El modelo hizo bien su trabajo.
La estrategia de trade eligio una apuesta contraria tipo longshot y perdio.
```

Por eso la UI ahora separa:

- Model accuracy.
- Trade performance.
- Alignment.
- Strategy.

---

## 19. Que estrategia usar ahora

Mientras el sistema sigue juntando datos, mi recomendacion es:

```text
Usar Directional Conservative como lectura principal.
```

Por que:

- Evita contrarian.
- Evita ultimos 120 segundos.
- Opera buckets historicamente mas sanos: T-480, T-360, T-240.
- Exige probabilidad minima y edge minimo.

Usaria Value Bet / Longshot solo para investigacion, no como regla principal, hasta tener mucha mas muestra.

---

## 20. Que evitar

Evita operar cuando:

- El dashboard diga `BASELINE PROXY`.
- La decision diga `SKIP`.
- Alignment sea `CONTRARIAN` y no estas buscando longshots.
- El bucket este en T-60, T-30, T-15 o T-5 y la estrategia reciente venga perdiendo.
- El performance reciente diga `DETERIORATING`.
- La muestra sea `LOW SAMPLE`.
- El spread sea alto o el precio se mueva demasiado rapido.

---

## 21. Que significa que la PC se apague

Si apagas la PC:

- El collector deja de capturar snapshots.
- Se pierden order books y senales de esos buckets.
- Baseline y cierre se pueden recuperar parcialmente despues desde Polymarket.
- Pero no se recupera perfectamente el estado del order book historico en cada segundo.

Impacto:

```text
El modelo puede seguir funcionando porque tiene historico de precios.
Pero la evaluacion real de estrategia queda con huecos.
```

Si quieres data continua y seria, conviene correrlo en un servidor 24/7.

---

## 21.1 El modelo se autoentrena?

No. Hoy el modelo no se autoentrena solo.

El flujo actual es:

```text
Collector corriendo
  -> guarda nueva data en SQL Server
  -> esa data sirve para auditoria y futuro reentrenamiento
  -> pero NO reemplaza automaticamente el modelo activo
```

Para cambiar el modelo activo hay que correr un script de entrenamiento y guardar un nuevo artefacto en `model_artifacts/`.

Modelos actuales:

| Modelo | Archivo | Fuente de entrenamiento | Uso |
|---|---|---|---|
| `binance-hgb-v1` | `model_artifacts/model.pkl` | 30 dias de Binance 1m | Modelo base tecnico |
| `historical-sim-extra_trees-v1` | `model_artifacts/model_supabase.pkl` | simulacion historica 15m tipo Polymarket | Modelo activo market-aware |

La data capturada ahora en SQL Server sirve para hacer un modelo mejor despues, con baseline exacto, buckets reales, quotes reales de Polymarket, senales simuladas y resultados reales.

---

## 21.2 Que precision muestra el dashboard?

Cuando el dashboard dice `Model accuracy`, normalmente mide todas las predicciones resueltas disponibles en `predictions_v2`, no solo la prediccion inicial.

Por eso se separan:

| Metrica | Significado |
|---|---|
| Initial 15m accuracy | Primera prediccion de la ronda, la mas importante para saber si el modelo anticipa 15 minutos. |
| Final/live accuracy | Ultima prediccion antes del cierre; suele ser mas alta porque queda menos incertidumbre. |
| Accuracy by bucket | Precision del modelo en cada ventana T-895, T-480, T-60, etc. |
| Trade PnL by bucket | Si las senales simuladas ganaron dinero en cada bucket. |

Para evaluar capacidad predictiva real a 15 minutos, mira `Initial 15m accuracy`.

---

## 22. Recomendacion de servidor

Para un proyecto serio:

- VPS pequeño 24/7.
- Collector siempre encendido.
- SQL Server o PostgreSQL central.
- Backups diarios.
- Dashboard accesible por navegador.
- Health check para reiniciar procesos.

Tu PC quedaria solo para mirar el dashboard y decidir.

---

## 23. Glosario rapido

| Termino | Significado |
|---|---|
| Round | Ronda de Polymarket de 15 minutos. |
| Cutoff | Hora de cierre. |
| Baseline | Precio que BTC debe superar. |
| Outcome | Resultado final: UP o DOWN. |
| Snapshot | Captura en un bucket antes del cierre. |
| Bucket | Tiempo restante: T-480, T-360, T-60, etc. |
| Prediction | Direccion que cree el modelo. |
| Probability | Probabilidad estimada por el modelo. |
| Confidence | Distancia frente a 50/50. |
| Market price | Precio/ask de Polymarket. |
| Edge | Probabilidad del modelo menos precio de mercado. |
| Signal | Trade simulado generado por la estrategia. |
| Alignment | Si la senal sigue o contradice la prediccion. |
| Longshot | Compra barata con baja probabilidad, pero posible edge. |
| PnL | Ganancia/perdida simulada. |
| ROI | Retorno sobre stake. |
| Drift | Cambio reciente del rendimiento frente al historico. |

---

## 24. Rutina recomendada antes de operar manualmente

1. Mira `BASELINE EXACT`.
2. Mira `Current Round Decision`.
3. Si dice `WAIT` o `SKIP`, no operar.
4. Si dice `BUY_UP` o `BUY_DOWN`, revisa alignment.
5. Prefiere `ALIGNED`.
6. Revisa bucket.
7. Prefiere buckets con PnL positivo historico.
8. Revisa Recent Performance.
9. Si dice `DETERIORATING`, baja riesgo o espera.
10. Despues de la ronda, revisa Round History y Signal History.

La pregunta final no es:

```text
El modelo tiene alta accuracy?
```

La pregunta final es:

```text
La estrategia activa, en este bucket, con esta alineacion, ha ganado dinero de forma consistente?
```
