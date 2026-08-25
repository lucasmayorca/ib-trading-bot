# IB Trading Bot — Dashboard de análisis técnico para Interactive Brokers

Bot y dashboard web que escanea ~100 acciones y ~100 ETFs de NYSE/NASDAQ, calcula
**MACD + RSI + Koncorde**, valida cada setup contra 5 años de backtest y arma un
análisis chartista completo (figuras técnicas, Fibonacci, velas japonesas) sobre
cada activo.

Corre de dos formas: **local** (tu TWS + tu máquina) o **cloud** (un bridge liviano
en tu máquina que alimenta un dashboard multi-usuario en Railway).

---

## Qué hace

**Señales.** El sistema exige que los 3 indicadores se alineen para una señal
ejecutable (3/3), y expone estados granulares intermedios — `COMPRA/VENTA`,
`COMPRA/VENTA INMINENTE` (2/3 con zonas coherentes), `VIRANDO A…`, `ZONA DE
SOBRE(VENTA|COMPRA)`, `NEUTRAL`.

**Señales sobre cierres confirmados.** Durante la sesión la vela del día está a
medio formar: evaluar los giros ahí hace parpadear las recomendaciones cada 5
minutos. Todo el análisis corre sobre **cierres diarios confirmados**, acorde a un
horizonte swing (mediana ~31 días por trade).

**Backtest calibrado, no win-rate crudo.** Cooldown para no solapar trades, coste
round-trip por operación, y una confianza que es significancia estadística del
expectancy (no "win-rate × volumen"). Las recomendaciones se rankean por *edge
esperado*.

**Niveles con fundamento.** Entrada, objetivo y stop salen de estructura real:
pisos/techos horizontales con toques, canal de regresión ±2σ, medias móviles,
ATR y niveles Fibonacci — nunca de un porcentaje fijo.

**Figuras técnicas validadas contra la historia.** Doble/triple techo-suelo,
hombro-cabeza-hombro, triángulos/cuñas, banderas, rupturas de nivel y divergencias
RSI, detectadas sobre pivotes mayores del gráfico de 5 años. Cada tipo de figura se
mide contra su **baseline aleatorio** y solo las que demuestran edge publican
objetivo y pesan en el score; las neutras quedan como contexto y las de edge
negativo no se emiten.

**Velas japonesas para el timing.** Martillo, envolventes, estrellas, harami,
marubozu, doji y más — con estado de confirmación (el cierre siguiente valida,
niega o deja pendiente) y encuadre explícito contra la tesis: si la vela va en
contra, lo dice.

**Mi Cartera.** Posiciones de IB con veredicto por posición (MANTENER / REDUCIR /
VENDER / SUMAR) que pondera el sistema *y* la estructura del gráfico, narrativa
integral desde la óptica del tenedor, SL/TP reales activos en IB dibujados en el
chart, y las **órdenes cargadas sin ejecutar**.

**Options Lab.** 15 estrategias valuadas con la cadena de opciones real
(bid/ask/IV de mercado), Greeks, Monte Carlo para probabilidad de ganancia,
scoring por valor esperado y la orden lista para pegar en el broker.

**Trades Históricos.** Round-trips cerrados (acciones, opciones y spreads) con el
gráfico del trade y una tesis autogenerada de la entrada y la salida.

---

## Arrancar local

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python vista_web.py
```

Dashboard en http://localhost:5050. Requiere TWS o IB Gateway abierto con la API
habilitada (puerto 7497 paper / 7496 live). **Sin TWS también arranca**: cae a
yfinance para los históricos.

| Comando | Qué corre |
|---|---|
| `python vista_web.py` | Dashboard web (escáner, ETFs, cartera, opciones, trades) |
| `python bot.py` | Bot de terminal con confirmación manual de órdenes |
| `python vista_analisis.py` | Watchlist en terminal |

---

## Arrancar en la nube

Ver **[SETUP_CLOUD.md](SETUP_CLOUD.md)**: deploy en Railway + PostgreSQL, registro
de usuario, y el bridge que corre en tu máquina para leer tu TWS.

---

## Arquitectura

```
Local                                  Cloud (Railway)
┌──────────────────┐                   ┌──────────────────────────┐
│ TWS / IB Gateway │◄── ibapi ────────►│  cloud/server.py         │
│                  │                   │  Flask + SocketIO        │
│ bridge/main.py   │── WebSocket ─────►│  + PostgreSQL            │
│ (paquete pip)    │   analysis_batch  │                          │
└──────────────────┘   portfolio_data  │  Navegador ◄── HTTPS ────┤
                                       └──────────────────────────┘
```

El dashboard cloud reusa el **mismo** HTML del local (paridad exacta) e inyecta
las pestañas propias del modo cloud. Las figuras, velas y el enriquecimiento se
calculan server-side, así que mejorarlos **no** requiere reinstalar el bridge.

---

## Archivos clave

| Archivo | Rol |
|---|---|
| `vista_web.py` | Dashboard Flask completo (API + UI) |
| `indicators.py` | MACD, RSI, Koncorde (matemática pura) |
| `signals.py` | Generación de señales BUY/SELL (lógica pura) |
| `patterns.py` | Motor de figuras técnicas + Fibonacci + velas japonesas |
| `backtester.py` | Motor de backtest con confianza calibrada |
| `calibration.py` | Contraste predicho vs. realizado por fuerza de señal |
| `portfolio.py` | Tracking de cartera, órdenes y ejecuciones de IB |
| `options_lab.py` | Motor de estrategias de opciones (Black-Scholes, Greeks) |
| `market_pulse.py` | Pulso del mercado (SPY) |
| `enrichment.py` | Beta, RS, RVOL, analistas, insiders, short interest |
| `scanner.py` | Escáner de mayor volumen |
| `config.py` | Todos los parámetros centralizados |
| `bridge/` | Paquete pip que corre en la máquina del usuario (modo cloud) |
| `cloud/` | Servidor multi-usuario (auth, DB, bridge WebSocket) |
| `CLAUDE.md` | Documentación técnica profunda: decisiones, gotchas y por qué |

---

## Notas

- Los nombres de variables del Koncorde están en castellano (`marron`, `verde`,
  `azul`) siguiendo el Pine Script original.
- `CLAUDE.md` documenta las decisiones de diseño y los bugs que costaron
  debugging — vale leerlo antes de tocar el motor de figuras o el stack de charts.
- Este software es para análisis. No es asesoramiento de inversión.
