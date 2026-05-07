# Manual de Uso del Dashboard

Este panel no es un boton magico de compra. Es una mesa de decision: te muestra
que cree el modelo, cuanto cuesta entrar en Polymarket y como ha performado el
sistema con datos guardados.

## 1. Primero mira el baseline

El baseline es el precio que la ronda debe superar.

En la tarjeta de partida actual veras:

```text
BASELINE (OPEN)
```

Tambien veras una etiqueta de fuente:

- `BASELINE MANUAL`: lo sincronizaste tu con el boton `SYNC`.
- `BASELINE POLYMARKET API`: vino desde la API de Polymarket.
- `BASELINE BINANCE / NO CHAINLINK`: fue inferido desde Binance.
- `BASELINE BINANCE FALLBACK`: no se encontro el cierre exacto y se uso fallback.

Importante: Polymarket declara que estas rondas resuelven con Chainlink BTC/USD
Data Stream, no con Binance spot. La API publica de Gamma muestra la descripcion
y el orderbook, pero no expone el baseline visual exacto como campo estructurado.
Por eso, cuando la etiqueta diga Binance, compara contra Polymarket y usa `SYNC`
si ves diferencia.

## 2. Luego mira la accion

La tarjeta `EDGE / ACCION` puede mostrar:

- `WAIT`: no entrar. El modelo no ve suficiente ventaja contra el ask.
- `BUY UP`: el modelo ve valor comprando UP al ask actual.
- `BUY DOWN`: el modelo ve valor comprando DOWN al ask actual.

Regla actual:

```text
edge = probabilidad_modelo - ask_polymarket
```

El sistema solo genera compra si el edge supera:

```text
EDGE_THRESHOLD=0.07
```

## 3. Como leer edge

Ejemplo:

```text
UP 14.6% @0.84
```

Significa:

- El contrato UP cuesta `0.84`.
- El modelo cree que la probabilidad justa esta cerca de `0.986`.
- La diferencia aproximada es `14.6%`.

No significa que sea seguro. Significa que, segun el modelo, el precio ofrece
valor esperado positivo.

## 4. Como leer la confianza

La confianza es distancia frente a 50/50.

```text
prob_up = 0.60 -> confianza 20%
prob_up = 0.90 -> confianza 80%
```

Confianza alta al final de la ronda suele ser normal porque ya se ve si BTC esta
arriba o abajo del baseline. Por eso la metrica por fila puede inflarse.

## 5. Como leer el performance

### Por fila

Accuracy calculada sobre cada snapshot guardado.

Como el collector guarda varias muestras por ronda, una sola ronda facil puede
sumar muchos aciertos. Es util, pero no es la metrica mas honesta.

### Primera/ronda

Evalua la primera prediccion de cada ronda. Es mas dificil y mas honesta para
preguntar:

```text
Que tan bueno era el modelo apenas nacio la ronda?
```

### Ultima/ronda

Evalua la ultima prediccion antes del cierre. Tiende a ser alta porque falta
poco tiempo y el precio ya esta cerca del resultado.

### ROC-AUC

Mide si el modelo ordena bien los casos UP sobre DOWN. Mayor es mejor, pero no
garantiza rentabilidad.

### Win rate

Porcentaje de apuestas simuladas cerradas con `WIN`.

### PnL

Resultado economico simulado despues de pagar el ask. Esta es la metrica mas
cercana a la pregunta real:

```text
Si hubiera entrado, gane o perdi?
```

## 6. Como leer el historial

El historial ahora separa:

- `PREDICCION`: lo que dijo el modelo.
- `CIERRE`: precio usado para resolver.
- `OUTCOME`: lo que paso en el mercado, `UP` o `DOWN`.
- `ACIERTO`: si la prediccion gano o perdio.

Ejemplo:

```text
baseline: 79,987.42
cierre:   79,893.23
prediction: DOWN
outcome: DOWN
acierto: WIN
```

Aunque visualmente ambos empiezan con `79,8xx`, `79,893.23` es menor que
`79,987.42`, por eso DOWN gana.

## 7. Rutina recomendada para usarlo

1. Verifica que el baseline coincida con Polymarket.
2. Si no coincide, usa `SYNC`.
3. Mira `EDGE / ACCION`.
4. Si dice `WAIT`, no hay entrada segun el sistema.
5. Si dice `BUY UP` o `BUY DOWN`, revisa:
   - segundos restantes
   - spread/ask mostrado
   - PnL reciente
   - si el baseline esta sincronizado
6. Despues de la ronda, revisa `OUTCOME`, `ACIERTO` y senales simuladas.

## 8. Limitacion mas importante

La resolucion oficial usa Chainlink BTC/USD Data Stream. Para alineamiento
perfecto necesitariamos acceso realtime a Chainlink Data Streams. Esa API
requiere acceso/autenticacion especifica. Mientras no tengamos eso, el sistema
usa Binance como proxy y deja el boton `SYNC` para corregir visualmente el
baseline.

