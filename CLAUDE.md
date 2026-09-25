## Overview
Automated trading bot connected to Interactive Brokers TWS. Scans top ~75 NYSE/NASDAQ stocks by volume, calculates MACD + RSI + Koncorde indicators, and executes bracket orders when all three align.

## Tech Stack
- **Language**: Python 3.14.0
- **Broker API**: ibapi 9.81.1.post1 (Interactive Brokers)
- **Web Dashboard**: Flask 3.1.3
- **Data**: pandas, numpy, yfinance, scipy
- **Virtual env**: `/venv/`

## Entry Points
| Command | What it runs |
|---------|-------------|
| `python vista_web.py` | Web dashboard on http://localhost:5050 |
| `python bot.py` | Terminal bot with manual order confirmation |
| `python vista_analisis.py` | Terminal watchlist dashboard |

## IB Connection
- Host: 127.0.0.1
- Port 7497 (paper trading) / 7496 (live)
- TWS/IB Gateway must be running with API enabled
- Client IDs: bot=3, scanner=13, vista=4

## Key Files
| File | Purpose |
|------|---------|
| `bot.py` | Main orchestrator - scan, calculate, execute |
| `indicators.py` | MACD, RSI, Koncorde calculation (pure math) |
| `signals.py` | BUY/SELL signal generation (pure logic) |
| `scanner.py` | Market scanner (top N stocks by volume) |
| `config.py` | All parameters centralized |
| `portfolio.py` | Portfolio tracking & history |
| `vista_web.py` | Flask dashboard (5500+ lines) |
| `trades_imported.json` | Imported trade history from IB (BUY/SELL fills) |
| `backtester.py` | Historical backtest engine |
| `patterns.py` | Motor compartido de figuras técnicas + Fibonacci (chartista, por reglas) |
| `options_lab.py` | Options strategy engine (Black-Scholes, Greeks, IV analysis) |

## Signal Logic

### BUY (all must align):
1. MACD histogram negative but rising (turning up)
2. RSI < 30 (oversold)
3. Koncorde marron below media but rising

### SELL (all must align):
1. MACD histogram positive but falling (turning down)
2. RSI > 70 (overbought)
3. Koncorde marron above media but falling

### Score: base 1pt/indicator + bonuses for extreme conditions

### Signal Labels (granular trend detection)
`generate_signal()` returns `signal` (BUY/SELL/HOLD for order logic) and `signal_label` (descriptive trend):
- **COMPRA / COMPRA FUERTE**: 3/3 buy conditions (fuerte = strength >= 4)
- **VENTA / VENTA FUERTE**: 3/3 sell conditions (fuerte = strength >= 4)
- **COMPRA INMINENTE**: 2/3 buy conditions met **+ zonas coherentes** (`_zones_coherent_buy`:
  hist MACD ≤ 0, RSI < 45, marrón < media — el cuadro debe verse sobrevendido, patrón "SNPS").
  2/3 sin coherencia de zona se degrada a VIRANDO A COMPRA (evita "compra" con MACD/RSI por
  las nubes y un solo giro técnico acompañando).
- **VENTA INMINENTE**: 2/3 sell conditions met **+ zonas coherentes** (`_zones_coherent_sell`:
  hist ≥ 0, RSI > 55, marrón > media). Espejo en `bridge/signals.py` — mantener paridad.
- **Top Recomendaciones solo lista** señales completas (BUY/SELL 3/3) o INMINENTE coherente;
  VIRANDO/ZONA/NEUTRAL no se recomiendan (gate en `_score_stock` y en el fallback de
  `compute_top3`) — calidad antes que cantidad: si no hay 5 setups reales, se muestran menos.
- **VIRANDO A COMPRA**: 1/3 buy or multiple bullish hints (RSI < 40, MACD/Koncorde turning)
- **VIRANDO A VENTA**: 1/3 sell or multiple bearish hints (RSI > 60, MACD/Koncorde turning)
- **ZONA DE SOBREVENTA**: RSI < 35, no conditions met
- **ZONA DE SOBRECOMPRA**: RSI > 65, no conditions met
- **NEUTRAL**: no clear direction

`signal_label` is display-only; `signal` (BUY/SELL/HOLD) drives order execution in `bot.py`.
The thesis (`_generate_thesis`) and rationale (`_generate_rationale`) use `signal_label` for direction consistency.
`_label_is_bearish(label)` (vista_web.py) is the shared bearish/bullish check used by `_compute_price_levels`
(entry/target/stop), `_score_stock` (win_rate/avg_return component), `_generate_rationale` (qué lado del backtest
se narra) y the recommendation/portfolio deep-analysis win_rate/avg_return fields — always branch on `signal_label`
here, not raw `signal`, since INMINENTE/VIRANDO/ZONA labels can be directional while `signal` is still HOLD.
- **TRAMPA DE SUBCADENAS (2026-09, costó un bug en los dos espejos)**: `"SOBREVENTA"` contiene `"VENTA"` y
  `"SOBRECOMPRA"` contiene `"COMPRA"`. Testear la subcadena suelta hacía que **ZONA DE SOBREVENTA** (RSI<35,
  lectura ALCISTA) se clasificara como bajista — target por debajo del precio, stats del lado sell, UI en rojo —
  y que `fstr()` pintara de **verde alcista** una ZONA DE SOBRECOMPRA. La regla correcta excluye el prefijo:
  `("VENTA" in label and "SOBREVENTA" not in label) or "SOBRECOMPRA" in label`. El espejo JS `_labelIsBearish`
  hace lo mismo con indexOf — **mantener paridad**; cualquier chequeo direccional nuevo debe llamar a estas dos
  funciones, nunca reimplementar el test con substrings.

## Configuration (config.py)
- `SCAN_COUNT = 100` acciones y ETFs. El scanner de IB devuelve máx ~50 filas por
  suscripción, así que `scanner._merge_to_count()` fusiona el top-volumen en vivo (≤50)
  con la lista curada de respaldo hasta completar 100 únicos (con/sin TWS). Fallbacks:
  `FALLBACK_STOCKS`=100, `FALLBACK_ETFS`=113. El bridge (`bridge/main.py`) tiene sus
  propias copias (self-contained) también a 100 — `get_stock_list()[:100]`, `get_etf_list()[:100]`
- **Universos acciones/ETF DISJUNTOS (2026-09)**: para IB un ETF **es** `secType="STK"`, así que
  se colaban por dos vías y el mismo símbolo (p.ej. USO) aparecía en los DOS escáneres, analizado
  dos veces por ciclo en momentos distintos → **dos scores distintos para el mismo activo**
  (cada pasada ve un snapshot propio: barra parcial, fuente IB/yfinance, o un fallo que deja
  el store con el valor del ciclo anterior). Vías tapadas: (1) `scanner.get_top_volume_stocks()`
  usa `instrument="STK"` y el `MOST_ACTIVE` de IB devuelve ETFs mezclados → `_strip_etfs()`
  (basado en `is_etf_symbol` / `FALLBACK_ETFS`) filtra el vivo **y el cache en disco**;
  (2) el bridge mergea las tenencias en la watchlist de acciones filtrando `secType=="STK"` →
  `split_held(positions)` manda los ETFs conocidos al universo de ETFs y deja en acciones sólo
  los que NO están en `FALLBACK_ETFS` (p.ej. IBIT, que el bridge no cubre) para que igual tengan
  chart en Mi Cartera — ese era el motivo original del merge; (3) red de seguridad en
  `vista_web.main()`: se restan de `stock_list` los símbolos que están en `etf_list`.
  **`_IB_HIST_FAILS` es POR LOOP** (`_IB_HIST_FAILS` acciones / `_IB_HIST_FAILS_ETF` ETFs,
  `fetch_historical(..., breaker=)`): era un global compartido entre dos threads paralelos, así
  que el corte de circuito de un loop arrastraba al otro a yfinance a mitad de pasada (o se lo
  reseteaba) → un símbolo podía analizarse con barras de IB en un tab y de yfinance en el otro.
- `SCAN_INTERVAL_SECONDS = 300` (5 min)
- **`SIGNALS_INTRADAY_REFRESH_MINUTES = 60` (2026-09, reemplaza a `SIGNALS_CONFIRMED_CLOSE_ONLY`)**:
  el análisis mira la vela de HOY y se recalcula **una vez por hora**, no en cada ciclo de 5 min.
  Historia: primero se evaluaba con la barra viva en cada pasada → las condiciones de giro son
  comparaciones de última barra (`hist[-1] > hist[-2]`, marrón vs media, RSI) y sobre una vela a
  medio formar parpadeaban (las recomendaciones entraban y salían del Top varias veces por hora).
  El parche de 2026-08 fue descartar la barra viva, pero eso dejó el análisis clavado en el cierre
  de AYER durante toda la jornada: una rueda muy volátil no movía nada hasta el día siguiente.
  Solución actual: **barra viva + época**. `signal_epoch()` (vista_web, espejo en `bridge/main.py`
  — mantener paridad, hay test) devuelve `YYYY-MM-DD#HHH` alineado al RELOJ mientras la rueda está
  abierta (10:00, 11:00, … ET) y `YYYY-MM-DD#cierre` fuera del horario. Los loops (`run_analysis`,
  `run_etf_analysis` y los dos del bridge) saltean el símbolo cuyo análisis cacheado ya tiene la
  época vigente, así que dentro de la hora nada se recalcula y el resultado no puede parpadear;
  además ahorra ~200 pedidos históricos por pasada. Fuera del horario (pre-market, post-cierre, fin
  de semana) todas las horas comparten la época de la última rueda cerrada: se re-analiza **una
  sola vez** después de las 16:00 ET para tomar el cierre definitivo y después nada hasta la
  apertura. Un análisis fallido (`None`) nunca es "fresco" → se reintenta en la pasada siguiente,
  no en una hora. `SIGNALS_INTRADAY_REFRESH_MINUTES = 0` restaura el modo viejo (solo cierres
  confirmados, sin gate). Cada análisis viaja con `epoch`, `as_of` (fecha de la barra usada) y
  **`live_bar`** (la vela todavía se está formando).
  **Contrapartida asumida y documentada**: el backtest solo ve barras CERRADAS, así que durante la
  rueda la señal en vivo corre sobre una barra que sus estadísticas nunca midieron — la vela puede
  revertir antes del cierre. Medido: las stats del backtest salen **idénticas** en los dos modos
  (la última barra no puede abrir un trade que resuelva), lo que cambia es la señal de hoy. Ejemplo
  real 2026-09-23 11:00 ET: AMD "VIRANDO A VENTA" (cierre de ayer) vs **VENTA FUERTE** (rueda en
  curso); SPY y NVDA NEUTRAL vs VIRANDO A VENTA.
  `_drop_partial_bar` ahora solo descarta la vela de hoy cuando el refresco está en 0 — pero en
  **los dos modos** descarta una última barra con cierre NaN (yfinance la devuelve antes de la
  apertura y envenena indicadores y precio).
  El footer dice "Señales: rueda en curso, 11:00 ET" o "Señales: cierre del <fecha>"
  (`signals_label` en `/api/data` y `/api/etf-data`; **`signals_as_of` sigue siendo la FECHA** —
  es la referencia que compara `_asOfBadge`, no tocarla).
  **OJO: `bot.py` (el bot que ejecuta órdenes) siempre usó la barra viva** — ahora el dashboard y
  el bot miran lo mismo, pero el bot no tiene gate horario.
  ⚠ El gate y la barra viva están también en `bridge/main.py` ⇒ **requiere reinstalar el bridge**
  (`rm -rf ~/.ib-bridge && curl -sL .../install-bridge.sh | bash`); hasta entonces el cloud sigue
  mostrando el cierre confirmado del día anterior.
- `MAX_PER_TRADE = 5000` USD
- `STOP_LOSS_PCT = 3.0`, `TAKE_PROFIT_PCT = 8.0`
- `MAX_OPEN_POSITIONS = 10`
- MACD: 12/26/9, RSI: 14/21, Koncorde EMA: 255
- Backtest: `BACKTEST_COST_PCT=0.10` (round-trip), `BACKTEST_COOLDOWN=True`,
  `BACKTEST_ROBUST_TRADES=12` (muestra para confianza plena), `BACKTEST_TREND_SMA=200`

## Web Dashboard (vista_web.py)
- Default chart period: 1Y (scanner, top recommendations, portfolio)
- **Stack de gráficos sincronizados (`scRenderStack`/`scBuild`, 2026-07)**: velas + MACD + RSI +
  Koncorde son 4 paneles **Lightweight Charts** apilados que comparten eje de tiempo, rango visible
  (`subscribeVisibleLogicalRangeChange`) y crosshair (`subscribeCrosshairMove`+`setCrosshairPosition`).
  Al pasar el cursor por cualquier panel se marca el mismo instante en los otros y cada header muestra
  la lectura de valores (`fmt(i)` → O/H/L/C, Hist/MACD/Señal, RSI, Verde/Marrón/Azul/Media). **Chart.js
  se eliminó** (los indicadores ya no son canvas): un único motor reutilizable sirve a los 6 sitios
  (`scStackHTML(key,legend)` para el markup, `_scReg[key]` para el registro): escáner acciones
  (`scan_<idx>`), rec acciones (`rec_<idx>`), escáner ETF (`etf_<idx>`), rec ETF (`etfrec_<idx>`),
  Mi Cartera (`port_<sym>`) y Trades Históricos (`th_<idx>`). Alineación: todos los paneles usan las
  MISMAS marcas de tiempo (índice-paralelo a `ohlc`; nulls → whitespace para no desalinear el rango
  lógico) y se igualan los anchos de escala (`rightPriceScale.minimumWidth`).
  **Gotchas del stack (costaron debugging)**: (1) una vela con OHLC null (NaN del server —
  `_clean` de `to_json` mapea NaN→null; típico: barra parcial del día en curso de yfinance) NO es
  whitespace para LW (null ≠ undefined) y rompe la serie en silencio → panel de precio vacío y eje
  sin ticks mientras MACD/RSI pintan (sus arrays limpian NaN→0). `scBuild` mapea velas inválidas a
  `{time}` (whitespace, mantiene alineación) y `_fetch_trade_chart_data` hace dropna del OHLC.
  (2) si el stack se construye con el contenedor oculto/ancho 0, `fitContent` se pierde y queda el
  barSpacing default anclado a la derecha (80% del panel vacío a la izquierda) — `_scRefitIfDegenerate`
  (llamado desde `_scEqualize`) lo detecta y re-encuadra sin tocar zoom/pan legítimos.
- **Frecuencia de velas por ventana** (`SC_INTRADAY`/`SC_DAILY_BARS` en el JS): ALL/5Y/1Y/3M → **diario**
  (ya no semanal — `toWeekly` se retiró; con LW el detalle renderiza ~1300 velas diarias sin problema),
  1M → 1h, 1W → 30min, 1D → 15min. Intradía vía `/api/bars/<sym>/<1h|30m|15m>` ("4h" legado): IB si está
  conectado, **fallback yfinance** (`_bars_payload_yf`). **El endpoint ahora devuelve indicadores**
  (`{ohlc, macd, rsi, koncorde}`) calculados sobre las mismas barras intradía con calentamiento amplio
  (`_attach_bar_indicators` + `indicators.calculate_all`, redondeo NaN-safe vía `_safe_round`) — así los
  4 paneles siguen sincronizados también en intradía (antes intradía solo tenía velas). El cloud usa el
  mismo `_bars_payload_yf` (importado de `vista_web`) para paridad.
- `compute_top3()` muestra `config.TOP_RECOMMENDATIONS` (5) recomendaciones en cada scanner
  (acciones y ETFs, local y cloud). El nombre `top3`/`renderTop3` se conserva por historia;
  el render itera sobre la longitud del array, no asume 3.
- **Mi Cartera — veredicto y narrativa coherentes (2026-07)**: `_compute_position_verdict` pondera,
  además del sistema, la **estructura del gráfico** (techos/pisos reales vía `_find_sr_levels`,
  canal de regresión ±2σ, asimetría de aguantar una lectura bajista: caída proyectada vs. distancia
  a la invalidación) y expone `verdict["tech"]` para que `_generate_position_recommendation` cite
  LOS MISMOS niveles (no recomputa). VENTA INMINENTE con `bear > bull` ⇒ headline "HOLD — ALERTA DE
  VENTA" y los factores dominantes del reason salen del lado bajista (no citar factores alcistas
  contradiciendo la tesis). La narrativa habla desde la óptica del TENEDOR largo: con label bajista
  el target de abajo es "caída proyectada" y el stop de arriba es el techo que ANULA la lectura
  (nunca "piso de invalidación"); el stop de protección del largo se calcula aparte (piso fuerte
  cercano −0.5·ATR, o 2·ATR bajo el precio) porque el `stop_sug` bajista queda ARRIBA del precio y
  no sirve como stop de la posición. Usa `position["precio_actual"]` (vivo de IB) para "hoy cotiza"
  y P&L, no `data["price"]` (cierre del análisis) — antes mezclaba y los % no cuadraban. La
  tendencia de fondo lee JUNTOS precio-vs-SMA200 y el cross ("alcista... aunque death cross"), no
  "alcista (death cross)". **JS**: todo signo/etiqueta/color de objetivo sale de
  `_labelIsBearish(signal_label)` (espejo del Python, incluye SOBRECOMPRA) — NUNCA de
  `signal==='SELL'`, que mostraba "+12%" verde en una VENTA INMINENTE (signal HOLD). El P&L en $
  lleva signo explícito (antes `Math.abs` sin '-'). El panel de posición rotula "Riesgo a la baja"
  / "Invalidación (techo)" cuando el label es bajista (misma corrección aplicada en Top
  Recomendaciones y ETF Top Recomendaciones: "Stop Loss" plano por "Invalidación (techo)" cuando
  `_labelIsBearish` da bajista).
- **Órdenes pendientes en IBKR (2026-07)**: `reqAllOpenOrders` ya se pedía (`portfolio.fetch_open_orders`)
  pero solo se leía para extraer SL/TP de posiciones YA existentes (`extract_sl_tp_by_symbol`) — una
  orden de ENTRADA cargada en IB sin fill (sin posición todavía, p.ej. un LMT de compra que no llegó
  al precio) se pedía y parseaba pero se descartaba en silencio (el campo `entry_limit` quedaba
  calculado y sin usar). `extract_pending_entries(open_orders, held_symbols)` (`portfolio.py`) cierra
  ese hueco: toma las órdenes con `parent_id==0` (no son hijas SL/TP de un bracket, ver
  `bot.py create_bracket_order` — el parent nace con `parentId=0`, los hijos con `parentId=<id del
  parent>`) para símbolos SIN posición. `analyze_portfolio()` pide `open_orders` **antes** del early
  return de "sin posiciones" (antes solo se pedía dentro del branch con posiciones activas, así que
  una cartera vacía con una orden cargada nunca la mostraba) y arma `pending_orders` reusando
  `build_position_analysis_fn` sobre un stub de posición (`cantidad=0`, `costo_promedio=0`) — el
  pipeline de veredicto/narrativa ya tolera esos campos en 0/None con gracia. `cloud/server.py`
  duplica el wiring (mismo `extract_pending_entries` importado de `portfolio.py`, no hace falta
  duplicar la función) usando `_build_cloud_position_analysis` + `analysis.get(sym)` del store
  alimentado por el bridge — el bridge ya reenvía `open_orders` completo en `portfolio_data`, cero
  cambios ahí. **UI**: sección nueva "Órdenes pendientes en IBKR" en Mi Cartera (`renderPendingOrdersList`,
  oculta si `pending_orders` está vacío), un accordion por orden con el MISMO stack de charts
  sincronizado (`scRenderStack` key `pend_<sym>`) que las posiciones reales — sin veredicto/CTA
  (mostrar `verdict_reason`/`_generate_position_recommendation` sería engañoso: esa narrativa asume
  que ya se entró, dice "Entraste en X" — para pendientes solo se usa `rec.thesis`, el racional
  técnico neutro del escáner). `_portAnalDecorate` dibuja además la línea de la orden pendiente
  (ámbar, `pos.is_pending && pos.pending_order.price`) y, para posiciones reales, el SL/TP **real**
  activo en IB (`pos.stop_loss`/`pos.take_profit`, punteado) — antes esos dos niveles solo se
  mostraban como texto en el panel lateral, nunca en el gráfico (el Target/Stop sólido del chart es
  la sugerencia del sistema, no la orden real cargada).
- **Tabla del scanner (rediseño 2026-07)**: header en dos niveles — fila de grupos (`.lh-groups`:
  Activo · Precio vs media móvil · Momentum · Backtest 5A · Tend., con `grid-column:span N`) sobre
  la fila de columnas (`.lh-cols`). El grid de 18 columnas vive en `.lh-groups,.lh-cols,.stock-row`
  (min-width 1020px); el sort sigue anclado a `#list-header [data-col=...]` (spans anidados OK).
  Celdas compuestas: `fsym` (ticker + $vol 20d), `fpx` (precio + Δ vs cierre anterior), `fstr`
  (fuerza + micro-barra coloreada por dirección del label), `frsi` (píldora por zona `.rsi-os/osl/
  obl/ob`), `fv` (valor + `.okdot` si la condición de giro se cumple, tooltip = detail), `fcond`
  (3 puntos `.cond-dot`), `fconf` (número + micro-barra). Todos con tooltips `title`. Los headers
  están duplicados para acciones (`sortListBy`) y ETFs (`sortEtfListBy`) — cambiar ambos.
- Columnas "30D" y "1A" (grupo Tend.) en ambos scanners: mini sparklines SVG (`trendSparkCell(r,
  nDays,label)`, default 30) de los cierres de `chart.ohlc`, verde/roja según el cambio; sortables
  por `_trendPct(r,nDays)` (`data-col="trend"` / `"trend1y"`). El 1A usa 252 cierres con downsample
  a ~60 puntos (252 pts × 200 filas inflan el DOM). Las filas "sin datos" llevan celdas vacías
  extra para cuadrar el grid.
- **Enriquecimiento (grupos "Mercado" y "Wall Street", 2026-07)**: módulo `enrichment.py` compartido
  por local y cloud (server-side; el bridge NO se toca — cero reinstalls). Bloque `ext` por símbolo
  en `/api/data` y `/api/etf-data`. Mercado (beta propio 12m diario vs SPY vía cov/var + RS 30d +
  RVOL con proyección de sesión): UNA descarga batcheada `yf.download` del universo + SPY, refresco
  15 min (`refresh_market_metrics`). Wall Street (consenso analistas `recommendationMean` + precio
  objetivo, insiders 90d, `shortPercentOfFloat`): fetch por símbolo TTL 24h, thread de fondo con
  rate-limit 0.6s, persistido en `enrichment_cache.json` (local; cloud sin disco — Railway efímero).
  Celdas JS: `fbeta/frvol/fanalyst/fins/fshort` (tooltips explicativos); sort cols `beta/rvol/
  analyst/insiders/short_int`. ETFs muestran `---` en Wall Street (sin cobertura). Arranque:
  `enrichment.start_background(symbols_getter, persist_path)` en `main()` local y a nivel módulo en
  `cloud/server.py`.
- **Top Recomendaciones colapsable**: arranca comprimida (`_top3Collapsed=true`) mostrando solo la
  barra de título + chips `#N SYM ±obj%` (`_t3Chips`); clic en el título expande/colapsa
  (`toggleTop3Sec`/`toggleEtfTop3Sec` re-renderizan desde `_top3Data`/`_etfTop3Data`). El resumen
  de cada tarjeta muestra **Objetivo** (ganancia esperada, `recObjetivo`: `target_pct` con signo
  según dirección del label) antes de Score/Fuerza/WR/R/R.
- **Empty states**: los headers de tabla (`#list-header`/`#etf-list-header`) nacen con
  `display:none` y `update()`/`updateEtf()` los muestran solo cuando `total>0` — sin header
  flotando sobre el spinner. `.tab-loading` es una tarjeta (surface + borde + min-height) usada
  por todos los tabs.
- **La barra de contadores por signal_label ya NO existe** (se retiró del DOM). Su cálculo quedó vivo como
  código muerto en `_renderStockList`/`_renderEtfList` hasta 2026-09; de esos contadores solo sobrevive `total`,
  que decide si se muestra el header de la tabla.
- Thesis includes: signal label + direction, indicator status (MACD hist, RSI level, Koncorde vs media), moving averages (SMA200/50/20 + golden/death cross), institutional flow (Koncorde azul), target with consistent direction, fundamentals
- Portfolio "Composicion por Tipo" and "Distribucion por Sector" sections removed
- **Theme: "Cobalto Suizo" (light)** — white surfaces on warm-grey bg (#f4f4f1), cobalt accent (#2456e6),
  buy green #0b7a4b, sell red #c22436, hold amber #b45309. Tokens live in the `:root{...}` block of
  `DASHBOARD_HTML`; chart colors (Lightweight Charts / canvas payoff) are passed via JS literals,
  NOT CSS — when changing palette, sweep both. Dark-theme colors must not be reintroduced (user explicitly
  chose light background for readability).

## Figuras Técnicas (`patterns.py`, 2026-08)
- **Motor compartido** de figuras chartistas por reglas, extraído del detector que nació en
  `market_pulse.py` (que ahora es un wrapper fino sobre `patterns.detect`) y generalizado a TODOS
  los análisis: escáner acciones/ETF, Top Recomendaciones, Mi Cartera y el pulso. Server-side puro
  (numpy, sin IO) sobre el `chart` que ya viaja en cada análisis — **el bridge NO se toca**: el
  cloud computa `attach_to_analysis` al recibir cada `analysis_batch`/`etf_analysis_batch`.
- **ANCLAJE UNIFICADO por zigzag (2026-08, crítico — feedback "no entiendo el anclaje")**: TODOS
  los detectores se construyen sobre los MISMOS pivotes mayores (`_zigzag`, window 5, swing mínimo
  max(2.5·ATR, 4% del precio)) sobre el histórico completo — antes usaban `_pivots_arr(window=3,
  lookback=252)`, extremos locales de ±3 barras que en un gráfico de 5 años son ruido y anclaban
  figuras en cualquier wiggle. Consecuencias: "N toques" = N swings mayores al mismo precio (no
  pivotes menores agrupados), y las ventanas de recencia se ampliaron (dobles/HCH n−70, triángulo
  n−160, divergencia n−90) porque los pivotes mayores son más espaciados. El fib usa el MISMO
  zigzag con umbral más grueso (`0.20·rango` del gráfico) para que un impulso dominante no se parta
  en pedazos por rebotes intermedios. **Escala por ventana**: cada figura lleva `scale`
  (`major`=estructura del gráfico global; `short`=banderas/divergencias/velas). `_scScaleFilter` en
  `scRenderStack` dibuja SOLO `major` en ALL/5Y (3 velas en 5Y son 3px — ilegible y sin sentido) y
  todo en 3M/1Y; nada en intradía. El recuadro de velas tiene ancho mínimo 14px para ser visible.
- **Ventana de análisis = 5 AÑOS para TODOS los detectores (2026-08, "para todos los patrones")**:
  la estructura se busca sobre el histórico completo y solo el elemento que CIERRA la figura debe
  ser reciente (así sigue siendo accionable). Cambios: doble/triple techo-suelo ya no compara los
  últimos 2-3 pivotes de la lista sino que agrupa TODOS los pivotes mayores de 5Y que estén al
  mismo nivel (tol max(0.6·ATR, 1.5%), toques separados ≥20 ruedas, span ≤250 ruedas, último toque
  ≤70 ruedas) — antes un doble techo H1+H3 con un H2 más bajo en medio no se veía; HCH de 220 →
  histórico completo (span ≤400, hombro derecho ≤70 ruedas); triángulos/cuñas de 160 → 400 ruedas;
  divergencia RSI de 90 → 250 ruedas. Con ventanas más amplias aparecen más figuras, así que se
  subieron los umbrales: profundidad doble/triple ≥ max(3·ATR, 5%), altura inicial de triángulo
  ≥ max(5·ATR, 8%), palo de bandera ≥ max(6·ATR, 10%). Barrido de 18 large caps: 13 con figura
  (11 `major`), con anclas datadas a meses de distancia (MSFT doble suelo 02/26→07/26, XOM HCH inv
  05/26→06/26→08/26). Las cuñas rotas siguen siendo el eslabón débil según `validate_universe`
  (~26% hit-rate) — su peso en score/veredicto queda chico a propósito hasta tener más evidencia.
- **Detectores**: ruptura de nivel (3+ toques), doble/TRIPLE techo-suelo, hombro-cabeza-hombro
  (+ invertido), triángulos/cuñas (incluye "rota" ≤8 ruedas), banderas de continuación (palo
  ≥3.5·ATR + consolidación ≤55% del palo), cruce dorado/muerte (`include_cross`, solo pulso),
  divergencia RSI/precio, canal/estructura fallback (`include_fallback`, solo pulso). Cada figura:
  `{name, direction, status (en formacion/por confirmar/confirmada/vigente), key_level, breakout,
  invalidation, target (objetivo MEDIDO), priority, text}`. **Fibonacci por SWING SIN QUIEBRES sobre 5 años (2026-08, cuarta
  iteración — feedback "es correcto que el fibo de CPRT esté puesto ahí?" / "agranda la ventana a
  las ruedas de 5 años")**: (1) **terminal** = el máximo o el mínimo de las últimas 250 ruedas, el
  que cierre el swing más grande (así el retroceso está VIVO; debe estar a ≤150 ruedas). (2)
  **origen** = desde el terminal se camina hacia atrás sobre **TODO el histórico (5Y, sin tope)**
  hasta que el precio operó más allá de ese nivel; el origen es el extremo opuesto de ese tramo.
  Resultado: el ancla superior es un techo estructural real — en un barrido de 12 símbolos, 12/12
  anclaron en el máximo de 5 años. Descartes: swing < max(4·ATR, 8% del precio), retr fuera de
  [−35%, 105%]. **Historia de bugs — no volver atrás**: (1) ventana fija 180r → anclaba en el borde
  a mitad de la caída (TEAM $242); (2) zigzag de pivotes mayores → fragmentaba el impulso y elegía
  un tramo interno (USO $102→$142 ignorando $154.08; SLV $80.86 ignorando el pico $109.83); (3)
  ventana auto-expansiva de 250r → el extremo no quedaba "pegado al borde" (índice 6, no ≤2) y no
  expandía: CPRT ancló $50.11 a mitad de la caída en vez de su techo real $64.38 de 433 ruedas
  atrás; (4) tope de 500r en la búsqueda del origen → TEAM anclaba $326 en vez de $483.13.
  `at`/`relevant`/`retr_pct`/`start_off`/`end_off` como antes; el texto nombra los anclajes con
  fecha ("del techo $64.38 (27-11-2024) al piso $26.81 (23-07-2026)").
  **Convicción
  (2026-08, feedback: "patrones muy fuertes del gráfico global, no cualquier cosa")**: doble/triple
  profundidad ≥ max(2.5·ATR, 3.5%), HCH ≥ max(3·ATR, 5%) con cabeza ≥1.2·ATR, triángulo altura
  inicial ≥ max(4·ATR, 6%), bandera palo ≥ max(5·ATR, 8%), ruptura solo niveles de 3+ toques,
  divergencia RSI ≥5 pts. Velas: deriva previa ≥1.2·ATR, rango ≥0.8·ATR, marubozu ≥1.6·ATR,
  doji ≥1.2·ATR, envolvente previa ≥0.5·ATR, harami madre ≥1.4·ATR, máx 2 reportadas — en un
  barrido de 10 large caps: velas 1/10 (antes 3/10), figuras 6/10 prominentes.
- **Dónde pega cada cosa**: `analyze_symbol` adjunta `sig["pattern"]/sig["fib"]` (payload de
  `/api/data`, `/api/etf-data`, top3, deep analysis de cartera). `_compute_price_levels` suma el
  objetivo medido de la figura (prio 0, solo si su dirección coincide con el label) y los niveles
  fib (prio 1) como candidatos NOMBRADOS de target/entrada vía `_pick_directional_target(extra=)` —
  la ventana [0.6,1.8]·mov_esperado sigue mandando. Tesis línea "Figura tecnica:", racional bullets
  "Figura:"/"Fibonacci:", veredicto factores 9e (figura ±8/5/4/3 según status) y 9f (fib retroceso
  profundo ±3), `_score_stock` bonus acotado (+5 confirmada alineada, +2.5 en formación, −3
  confirmada en contra). **UI**: chips violeta `.rec-thesis-fig` (tooltip = texto), línea en
  `buildTechSummary`, y `_patternPriceLines` dibuja ruptura/anulación/objetivo (violeta) + fib
  38.2/50/61.8 (gris punteado) en TODOS los stacks (scan/etf/rec/etfrec/port/pend).
- **Geometría dibujada (2026-08)**: cada figura lleva `draw` — `segments` [{x0,y0,x1,y1,dash,w}]
  y `points` [{x,y,label,pos}] con **x = offset desde la última barra** (0 = hoy). El JS los
  convierte con `_figDrawLines` (interpola el segmento a un array right-aligned → `decorate.lines`,
  el mismo mecanismo del canal del pulso: solo vistas diarias, `!timeVis`, scBuild paddea por la
  izquierda) y `_figDrawMarkers` (offset→time vía ohlc; círculos violeta "T"/"H"/"C"). Geometrías:
  doble/triple = nivel tocado (dash) + neckline + puntos T; HCH = neckline + puntos H/C/H;
  triángulo/cuña = las 2 rectas del fit, **cada una desde SU primer pivote** (extrapolar al pivote
  más viejo del otro lado la dibuja lejos de las velas); bandera = palo (w2) + canal (dash);
  divergencia = recta entre los 2 pivotes de precio. Ruptura/cruce/canal-fallback NO llevan draw
  (la priceLine horizontal ya es la figura). Conectado en los 6 decorates + `_mpDecorate` (pulso);
  `_recDecorate` y `_portAnalDecorate` ahora reciben `ch` para mapear offsets a times de markers.
- **Relevancia + hover (2026-08, feedback del usuario)**: las figuras solo se adjuntan/dibujan
  cuando están en su **punto de decisión** (`patterns._is_critical`: confirmada/por confirmar/
  vigente siempre — los detectores ya descartan figuras viejas —, en formación solo si el precio
  está a ≤1.5·ATR de la ruptura o anulación). `attach_to_analysis` filtra por `critical` (el pulso
  NO filtra: su main sigue siendo el de mayor prioridad, muestra contexto siempre). Fibonacci lleva
  `relevant` (precio apoyado en un nivel o a ≤1.2·ATR de alguno): gatea el dibujo de las líneas fib
  (ámbar `#a16207`, la del `at` dashed + axis label) y la mención en tesis/racional; los niveles
  fib SIEMPRE siguen siendo candidatos de target en `_compute_price_levels` (targets son a dónde
  VA el precio, no dónde está). **Hover en scBuild**: cada priceLine/línea superpuesta lleva `tip`
  (y las superpuestas `label`, del `lbl` por segmento del server); al pasar el cursor la línea se
  engrosa +1px (`applyOptions`) y aparece `.sc-figtip` (tooltip violeta absoluto en `.sc-pane-body`)
  con el detalle. Detección: `coordinateToPrice` con umbral ~7px; para series superpuestas se
  compara el valor interpolado en el índice del crosshair. La ruptura se renombró
  "Ruptura de techo"/"Perdida de piso" y su texto dice QUÉ nivel rompe (antes "Ruptura alcista"
  no explicaba nada).
- **Dibujo estilo investing.com (2026-08)**: primitive `_scTextLabels` (clon del patrón
  `_scZoneBands`, canvas con `logicalToCoordinate`+`priceToCoordinate`, zOrder top, halo blanco)
  rotula texto SOBRE el chart: el nombre de la figura al inicio de su primer trazo y cada nivel
  fib como "61.8% · 689.11". **Fibonacci ya no es priceLine full-width**: `_fibDrawLines` dibuja
  los 7 niveles (0-100%, ámbar; el `at` sólido y más grueso) como segmentos DESDE el inicio del
  impulso (`fib.start_off`/`end_off`, offsets del server) hasta hoy — solo el nivel apoyado
  conserva etiqueta en el eje. La neckline del HCH une los DOS VALLES reales (recta inclinada
  extrapolada a hoy, índices `i_v1/i_v2`), no un promedio horizontal. `decorate.labels`
  ([{off,price,text,color,bold}]) viaja por los 6 decorates + `_mpDecorate`; scBuild convierte
  off→índice lógico y ancla al borde izquierdo si el inicio quedó fuera de la vista. Detección
  SIEMPRE sobre el histórico diario completo (5Y); el dibujo aparece en todas las vistas diarias
  (ALL/5Y/1Y/3M, los arrays son sufijos del mismo eje) y el preset de período sigue en 1Y.
  **En las vistas intradía (1M/1W/1D) las figuras NO se consideran en absoluto** (decisión del
  usuario: figuras de menor plazo no son fiables): los trazos/labels ya se omitían por `timeVis`
  y las priceLines de figura/fib llevan `fig:true` para que scBuild también las saltee ahí —
  solo Entrada/Target/Stop del sistema persisten en intradía.
- **Validación y POLÍTICA DE EDGE** (metodología; la evidencia vigente está en el ítem siguiente):
  `patterns.validate_universe` recorre la historia detectando figuras confirmadas con target y mide
  si el precio alcanzó el objetivo **antes** que la invalidación en ≤40 ruedas. Clave metodológica:
  el hit-rate NO se compara contra 50% sino contra el **baseline aleatorio** de cada figura
  (`d_inval/(d_target+d_inval)`, ruina del jugador — el target casi siempre está más lejos que la
  invalidación); ambos se promedian solo sobre eventos **resueltos**. `edge = hit_rate − baseline`.
  El tie-break intrabar es **conservador**: si target e invalidación se tocan en la MISMA barra manda
  la invalidación (misma regla que el backtest usa para SL vs TP). En la práctica no mueve nada —
  **0 colisiones en 6.935 eventos** —, pero queda como seguro estructural.
- **MEDICIÓN VIGENTE — universo completo, 2026-09-18**: 217 símbolos (100 acciones + 118 ETFs de las
  listas del scanner; solo falta MMC, que yfinance no sirve) × **10 años** → **6.935 eventos, 4.742
  resueltos**. Detectores actuales y modo crudo `detect(apply_policy=False)` (la medición original
  corría sobre la detección ya filtrada, así que las figuras suprimidas no podían re-medirse nunca).

  | Figura | edge | res | IC95 | sig |
  |---|---|---|---|---|
  | Bandera alcista | **+17.0** | 195 | [+9.9, +24.0] | **SÍ** |
  | Triple suelo | **+14.4** | 162 | [+8.0, +20.9] | **SÍ** |
  | Doble suelo | **+9.8** | 653 | [+6.4, +13.1] | **SÍ** |
  | Triángulo asc roto / HCH inv / Triple techo / Cuña desc rota | +2.6 / +1.4 / +1.0 / +0.3 | | | no |
  | Triángulo sim roto / Cuña asc rota / Doble techo / Triángulo desc roto | −1.2 / −2.1 / −3.0 / −4.5 | | | solo cuña asc |
  | Bandera bajista | **−14.6** | 100 | [−22.1, −7.1] | **SÍ** |
  | Hombro-cabeza-hombro | **−17.6** | 178 | [−24.4, −10.9] | **SÍ** |

  **LEER `res`, NO `n`**: hit-rate y baseline se promedian solo sobre eventos RESUELTOS (los que
  tocaron target o invalidación dentro de las 40 ruedas), y esa muestra es bastante menor que `n`.
  **Ya no es "un único régimen"**: los 10 años cubren la corrección de 2018, el desplome de 2020 y el
  bear market de 2022. Control sobre los últimos 5 años (3.676 eventos): las 5 figuras significativas
  lo son en **ambas** ventanas y con magnitudes estables (Bandera alcista +17.0/+22.1, Triple suelo
  +14.4/+13.9, Doble suelo +9.8/+11.0, Bandera bajista −14.6/−15.1, HCH −17.6/−15.9). Las no
  significativas oscilan alrededor de cero entre ventanas — Cuña desc rota da +0.3 en 10Y y −5.3 en
  5Y: es ruido, no una señal.
  **La política quedó respaldada por evidencia**: las 3 figuras `validada` y las 2 descartadas tienen
  todas edge significativo. Con la muestra chica previa (60 símbolos) ninguna de las positivas lo era
  y Doble suelo había caído a `contexto` por un artefacto de muestra — con el universo completo vuelve
  a `validada` con IC [+6.4, +13.1]. En vivo: de 217 símbolos, 48 tienen figura `validada` con
  objetivo medido y 109 `contexto`.
  Para re-medir: `scratchpad/medir_universo.py` (paralelizado, ~6 min) sobre el pickle del universo.
  **Política `_apply_edge_policy` (patterns.py)**: `edge ≥ +5` ⇒ tier **"validada"** (conserva
  objetivo medido y pesa en score/veredicto); `−12 < edge < +5` ⇒ tier **"contexto"** (se muestra por
  sus niveles de ruptura/anulación, pero `target=None` y prioridad −25, y **no** mueve score ni
  veredicto); `edge ≤ −12` ⇒ **no se emite** (con la medición del universo completo: HCH y Bandera
  bajista). Los triángulos/
  cuñas *en formación* heredan la medición de su propia variante "rota". El umbral de descarte es
  −12 y no −5 a propósito: el límite de 40 ruedas del test castiga más a las figuras de objetivo
  lejano, así que un edge levemente negativo degrada a contexto pero no borra la figura.
  **Gates en vista_web.py**: `_score_stock` y el factor 9e del veredicto exigen
  `pattern["tier"] == "validada"`; el chip de la tesis explica en su tooltip si la figura es
  validada (con su edge) o solo contexto. `_compute_price_levels` se autorregula: las de contexto
  llegan con `target=None` y no compiten como objetivo.
- **Velas japonesas (2026-08, timing de entrada de corto)**: `patterns.detect_candles(opens,highs,
  lows,closes)` — reversion (martillo, envolvente, estrella mañana/tarde, penetrante/nube oscura,
  pinzas, harami), continuacion (3 soldados/cuervos, marubozu) e indecision (doji), sobre las
  ultimas ~10 ruedas DIARIAS con contexto de tendencia previa (drift 5 ruedas vs 0.8·ATR).
  **Confirmacion**: el cierre SIGUIENTE valida ("confirmada"), niega (se descarta) o deja
  "sin confirmacion" (caduca a las 2 ruedas); la vela de hoy queda "por confirmar"; dedup por
  nombre (un 3-velas se re-detecta en ruedas consecutivas); max 3, mas recientes primero. Es capa
  de TIMING: NO pesa en score/veredicto. Payload `candles` en analisis/top3/deep/pulso (attach_
  to_analysis lo adjunta; cloud hereda). UI: markers flecha verde/roja en la vela (`_candleMarkers`,
  "Envolvente ✓" / "? por confirmar"), chip `Vela: <nombre>` verde/rojo (`figChips`), linea
  "Velas (timing corto):" en tesis y "Vela:" en racional. Solo vistas diarias (los markers van
  con fechas diarias; en intradia no matchean y no se dibujan). **Resaltado + encuadre (2026-08)**:
  cada patron lleva `span` (1-3 velas) y `meaning` (explicacion en castellano); `_candleBoxes` +
  primitive `_scBoxOverlay` dibujan un recuadro PUNTEADO translucido verde/rojo sobre las velas que
  FORMAN el patron — SOLO el recuadro: el nombre, el significado y el encuadre vs tesis van en el
  TOOLTIP del hover (texto dibujado sobre el chart resultaba ilegible encima de lineas/markers —
  feedback del usuario 2026-08). El tooltip del hover muestra titulo en negrita + detalle y toma
  el color de la linea/recuadro en su borde (`tipColor`). **GOTCHA LW 4.1 (costo debugging)**:
  `timeScale().logicalToCoordinate()` NO acepta logicos FRACCIONARIOS (devuelve 0, el recuadro
  colapsaba invisible en x=0) y dentro del `draw()` del render pass tampoco es confiable — las
  coordenadas se calculan en `updateAllViews()` con logicos ENTEROS + medio ancho de barra en px,
  y `draw()` solo pinta lo cacheado. Verificado por pixeles del canvas en el dashboard real. El ENCUADRE compara la
  direccion de la vela con `_label_is_bearish(label)`: alineada = "a favor de la tesis: afina el
  timing"; opuesta = "OJO: va CONTRA la tesis; esperar confirmacion antes de ejecutar" — en tesis,
  racional, tooltip del recuadro y chip (⚠). Una vela contra-tesis NO es un bug: es una advertencia
  de timing (ej. vela alcista con tesis bajista = no ejecutar la venta todavia).
- Gotcha: `signals.py` NO se toca — las figuras son contexto/niveles, nunca gatillo de orden.
  Los tests sintéticos exigen techos separados ≥12 ruedas (dobles) y ciclos ~12 ruedas (triples).

## Backtest & Calibración (calidad de la estimación)
- **Ejecución INTRADÍA (2026-09)**: SL y TP se evalúan contra el `high`/`low` de cada barra, no contra el
  cierre, porque el bracket real de `bot.py` es un STP + un LMT vivos toda la rueda. Tres reglas en
  `_simulate_long`/`_simulate_short` (espejo en `bridge/backtester.py`): (1) si la barra **abre** pasada del
  nivel, el fill es en la **apertura** (un STP es market una vez tocado: gap de −5% con stop en −3% sale en
  −5%); (2) si SL y TP se tocan en la **misma barra** se asume el **stop** (con barras diarias no se conoce el
  orden intrabar, y suponer el TP sería contarse una ganancia que pudo no ocurrir); (3) tocado intrabar sin gap,
  el fill es **en el nivel**, no al cierre. Sin `high`/`low` cae al modo por cierres.
  **Impacto medido** (461 trades, 20 large caps, 5Y): 4,1% de los trades cambian de WIN/LOSS, 9,8% salen por
  gap, y el retorno por trade sube en mediana +0,28 pp. Sube porque el modelo viejo **exageraba las pérdidas**:
  dejaba correr al perdedor hasta el cierre (−11%, −12%) en vez de cortarlo en el stop. `avg_loss` pasó de
  −4,82% a −3,43% y `avg_win` de 6,58% a 6,23% (el TP ahora llena en +8% exacto, no en un cierre que se pasó).
- **`backtester.py` — confianza calibrada, no win-rate crudo**: el backtest usa
  **cooldown** (no abre un nuevo trade hasta cerrar el anterior → sin solapes que inflen
  la muestra), resta **coste round-trip** por trade (`BACKTEST_COST_PCT`), y calcula la
  `confidence` como `Φ(t-stat de expectancy>0) · shrinkage(n)` reescalada 0.5→0..100
  (antes era `win_rate·volumen`, que premiaba edges no significativos). Reporta además
  `buy/sell_expectancy`, `avg_win/avg_loss`, `profit_factor`, y stats con-tendencia
  (`*_win_rate_trend`, tag `with_trend` vía SMA200). `bridge/backtester.py` es el duplicado
  standalone — mantener en paridad (numpy disponible en el bridge).
- **`_score_stock` (vista_web.py)** rankea por **edge esperado**: componentes strength (25),
  expectancy (30, con shrinkage por muestra), profit_factor (15), confidence (15), win_rate
  (10), señal activa (5), menos **penalización contra-tendencia** (hasta −15 si el precio va
  contra su SMA200). El fallback relajado en `compute_top3` usa la misma escala. Este 0-100 es
  el "Score" de las tarjetas de recomendación (con tooltip explicativo en el chip).
- **El shrinkage tira al NEUTRAL de cada escala, no a cero (2026-09)**: `sample_w` multiplicaba
  el componente ENTERO, así que un símbolo sin ninguna señal histórica (`n=0`) sacaba 0 de
  expectancy y 0 de win_rate, **20 puntos menos** que uno con 12 trades y edge medido
  exactamente nulo — el docstring prometía "shrunk toward neutral" y el código shrinkeaba hacia
  lo peor. Ahora `s2 = 15 + (raw−15)·w` y `s5 = 5 + (raw−5)·w`; `s3` (profit factor) queda
  multiplicativo **a propósito**: PF 1.0 ya ES 0 puntos, el neutral de esa escala coincide con
  el cero. Consecuencia buscada: "no hay evidencia" ≠ "hay evidencia de que es malo" — un edge
  medido negativo ahora rankea POR DEBAJO de uno desconocido, antes colapsaban juntos cerca de 0.
  Con `sample_w=1` el score no cambia en nada.
- **`win_rate_trend = None` NO es "no hay evidencia" (2026-09)**: si `n_side > 0` significa que
  NINGÚN trade histórico de ese lado fue con-tendencia, o sea que el **100% de la muestra es
  justo el caso que la penalización castiga** — y el código le daba la pena MÁS SUAVE (−10, sin
  el +5 de profundización) al peor escenario. Caso real: CRWD con 15 ventas históricas, las 15
  contra-tendencia. El reverso también es cierto: si la expectancy que ya sumó puntos se midió
  ENTERA en este régimen, cobrar la pena completa es contar dos veces lo mismo. Resuelto con
  `sell/buy_count_trend` (el backtester ya los exportaba, nadie los leía): la pena es un PRIOR
  sobre lo no observado y se descuenta en la proporción en que la muestra ya cubre el régimen
  (`pen *= 1 − 0.6·frac_counter·sample_w`), con **piso del 40%** — que el edge se haya medido
  acá no borra que operar contra la SMA200 es estructuralmente más frágil. Barrido de 60 large
  caps: los 9 elegibles suben (CRWD 48.1 → 54.1; los de muestra 100% contra-tendencia +6).
- **La figura que contradice la tesis se DICE, no solo se penaliza (2026-09)**: `_score_stock`
  ya restaba −3 por una figura confirmada en contra, pero `_generate_thesis`/`_generate_rationale`
  imprimían su texto en seco — "Doble suelo confirmado, objetivo medido $277.31" dentro de una
  VENTA INMINENTE con objetivo $202.44, sin explicar la contradicción. `_fig_frame(direction,
  is_bearish)` (espejo de lo que la capa de VELAS ya hacía) devuelve el sufijo "a favor de la
  tesis" / "OJO: va CONTRA la tesis". Espejo JS en `figChips` (⚠ en el chip + tooltip) —
  mantener paridad.
- **Confianza: "---" ≠ 0.** Con la confianza calibrada, **0 es un resultado real** (hubo señales
  históricas pero su edge no es estadísticamente significativo o es negativo) y se muestra como
  0 en rojo. `fconf(val, nSignals)` reserva "---" SOLO para `nSignals<=0` (ningún setup histórico
  en 5A — nada que backtestear). No volver a colapsar ambos casos en "---": hace parecer que el
  backtest no corrió cuando sí corrió. El resumen de cada recomendación muestra el chip Conf.
- **Pisos y techos horizontales (`_find_sr_levels`)**: pivotes fractales (ventana ±3) del último
  año, agrupados por tolerancia max(0.5·ATR, 0.5%); 2+ toques = nivel fuerte. Se COMBINAN con
  MAs/ATR/swing en `_compute_price_levels`: el objetivo prioriza techos/pisos fuertes dentro de
  la ventana de movimiento esperado (via `_pick_directional_target(sr_levels=...)`), la entrada
  se ancla al piso/techo fuerte más cercano, y el **stop va bajo el piso / sobre el techo fuerte**
  (±0.5·ATR) si está a ≤3·ATR — si no, fórmula ATR/swing con tope de riesgo 3.5·ATR. Se expone
  `stop_basis` y el racional lo muestra ("Stop: $X — bajo piso $Y (n toques)").
- **Transparencia del sistema en tesis y racional**: `_system_status(data, is_bearish)` evalúa
  las 3 condiciones en la dirección del label y devuelve (cumplidas, faltantes) con umbral,
  valor actual y detección de "ACERCANDOSE" (pendiente de las **últimas 5 ruedas** — 1 semana,
  acorde al horizonte swing mensual del usuario; era 3 y leía ruido de un par de días — ej.
  "RSI 36 ACERCANDOSE a <30"). **Ventanas por capa (decisión deliberada, 2026-08)**: las
  condiciones de giro de `signals.py` (hist[-1] vs [-2], marrón vs media) quedan en 1 barra —
  son el GATILLO de entrada que el backtest calibra y suavizarlas cambia el sistema y rompe
  paridad con `bridge/signals.py`; los factores del veredicto de Mi Cartera espejan esas mismas
  condiciones (misma ventana, para no contradecir el label); solo las capas de COMENTARIO
  (ACERCANDOSE) leen 5 ruedas. `_compute_position_trend` se eliminó (dead code sin callers; el
  `trend` del veredicto sale de los scores bull/bear). La **tesis** (`_generate_thesis`) lo usa en la línea 1 para estados
  pre-señal ("ya cumple MACD girando al alza y Koncorde girando desde piso. Para confirmar la
  senal de compra falta: RSI 43 (necesita <30)") y el **racional** (`_generate_rationale`)
  muestra lo mismo en bullets. Importante: COMPRA/VENTA INMINENTE = 2/3 condiciones (el RSI
  puede estar lejos del extremo si es la faltante); la señal ejecutable del bot sigue siendo
  estrictamente 3/3 (`signal` BUY/SELL).
- **Canal de tendencia (`_regression_channel`)**: regresión lineal ±2σ sobre ~120 cierres;
  el techo/piso del canal entra como candidato de objetivo ("Canal superior de tendencia en
  $X") y como ancla de zona de entrada, junto a pisos/techos horizontales y MAs.
- **Objetivo de precio por acción (`_compute_price_levels`)**: NO usa un piso fijo del 10%
  (eso hacía que casi todo mostrara "objetivo 10%"). El **movimiento esperado** se estima por
  acción vía `_estimate_expected_move`: volatilidad (ATR·√días_de_hold) combinada con el
  retorno medio ganador histórico del setup (`buy/sell_avg_win`). `_pick_directional_target`
  elige el primer nivel técnico (MA/swing) dentro de `[0.6, 1.8]·movimiento_esperado`; si no
  hay, usa el movimiento esperado directo. El horizonte usa la relación difusiva `(dist/ATR)²`
  días (antes lineal → daba ~1-2 semanas para todo). Se expone `expected_move_pct`.
- **Filtro de objetivo mínimo**: `config.MIN_OPPORTUNITY_TARGET_PCT` (8.0 acciones) y
  `MIN_OPPORTUNITY_TARGET_PCT_ETF` (7.0 ETFs). `_meets_min_target(data, min_pct)` descarta (no
  recorta) oportunidades cuyo `target_pct` < umbral, en la elegibilidad de `_score_stock` y en el
  fallback relajado de `compute_top3`. El umbral se propaga vía `compute_top3(cache, min_target_pct)`
  → `_score_stock(sym, data, min_target_pct)`; los callers ETF (local y cloud) pasan el 7%.
- **Calibración (`calibration.py` + `/api/calibration`)**: cierra el lazo predicho-vs-real.
  Corre `backtester.run_calibration_trades` sobre 5Y (yfinance) del universo (WATCHLIST +
  escaneados, cap 20, cache 1h) y agrupa por **fuerza de señal** → win-rate/retorno reales,
  más monotonicidad y splits por régimen/dirección. **Solo API** — el panel de UI se quitó
  a pedido del usuario (2026-07); el endpoint local y su espejo cloud siguen vivos.

## Patterns
- IB API wrapper pattern (EWrapper + EClient inheritance)
- Req ID ranges: 1000-1999 historical, 9000-9999 portfolio, 10000+ backtest
- Threading with daemon threads + events for sync
- Spanish variable names (marron, verde, azul for Koncorde)
- ANSI color codes for terminal output
- Fallback stock list (100 liquid stocks) if scanner fails; last successful scan cached in `scanner_cache.json`
- Historical data fallback: `fetch_historical()` falls back to yfinance (circuit breaker: 3 consecutive empty
  IB responses → yfinance for the rest of the cycle). Dashboard boots and analyzes even with TWS down/wedged;
  `EClient.connect()` blocks forever against a wedged TWS, so all connects run in daemon threads with timeouts
- Rate limiting: 0.5s between API calls

## Options Lab (`options_lab.py`)
- Auto-loads top 10 opportunities on tab switch (no manual input needed)
- Pre-screens ALL scanner stocks with quick IV check, then runs full lab on top 10 candidates
- Ranking independent from signal score: considers signal strength + IV regime + HV rank + backtest confidence + liquidity
- **IV y precios REALES de mercado (yfinance)**: `get_option_market(symbol, dtes)` descarga la
  cadena real (bid/ask/IV por strike, cache 10min, `OptionMarket`) y se pasa como `option_market`
  a `generate_options_lab`. Con cadena disponible: la IV ATM alimenta `iv_analysis` (antes muerto —
  `market_iv` nunca se pasaba, `estimated_iv` caía siempre en HV30), y `_apply_market_pricing`
  revalúa cada pata al **mid real** (snapeando strike y DTE a los reales) cobrando medio spread
  bid/ask; recalcula payoff/PoP/EV. Si falta liquidez en alguna pata o no hay cadena → pricing
  teórico Black-Scholes (fallback, comportamiento previo). Flags: `market_priced`, `iv_source`.
- **Scoring por EV**: `_score_strategy` incluye **valor esperado sobre capital** (15pts, EV = media
  del Monte Carlo neto de spread) — corrige el sesgo de premiar PoP alto con EV negativo (vender
  prima barata). Pesos: señal 25, EV 15, PoP 20, R/R 15, régimen IV 15, backtest 10; menos
  penalización por complejidad y por spread ancho. UI muestra "Valor Esp." y badge PRECIO REAL/TEORICO.
- Black-Scholes pricing + Newton-Raphson IV estimation (fallback teórico)
- 15 strategy types: Long Call/Put, Bull/Bear Call/Put Spreads, Iron Condor/Butterfly, Straddle/Strangle (long & short), Calendar Spread, Covered Call, Protective Put, Butterfly, Ratio Put Spread
- Strategy scoring (0-100): signal alignment (30pts), prob of profit (25pts), risk/reward (20pts), IV regime alignment (15pts), backtest support (10pts)
- IV misalignment detection: compares estimated IV vs HV (10d/30d/60d), HV rank percentile, flags when IV/HV ratio > 1.3 (sell premium) or < 0.75 (buy premium)
- HV term structure: HV10 vs HV30 divergence flags calendar spread opportunities
- Historical backtesting: finds similar indicator conditions in 5Y data, measures outcomes at 5/10/20/30/45 day horizons with distribution histograms and percentiles
- Monte Carlo (10K sims, log-normal) for probability of profit per strategy
- Per-strategy detail: payoff diagram (canvas), Greeks table, breakevens, max profit/loss, capital required, net premium, leg details
- **Vencimiento concreto por estrategia y por pata (2026-07)**: `Strategy.expiry`/`expiry_estimated` +
  `OptionLeg.dte/expiry/bid/ask/iv`. Con cadena real, `_apply_market_pricing` resuelve fecha y precio por
  PATA en la cadena de SU vencimiento (antes el calendar valuaba la pata larga en el expiry corto → net ~0)
  y `_mixed_expiry_metrics` recalcula payoff/PoP/EV para vencimientos mixtos. Sin cadena, `_estimate_expiry`
  aproxima al viernes mas cercano a hoy+DTE y marca `expiry_estimated` (UI antepone "≈"). UI: chip
  "Vence <fecha> · Nd" en el header, columna Vencimiento (+ Bid/Ask e IV si hay precios reales) en Patas,
  y bloque "Orden para tu broker" con cada pata en texto operable (COMPRAR/VENDER n× SYM CALL/PUT $strike ·
  vence Vie DD Mes YYYY · límite ≈ mid). No usar `_n(strike,0)` para strikes (redondea 187.5→188): `olabStrike()`.
- **Evolución del precio del paquete (`price_history`, 2026-07)**: `_strategy_price_history` valúa
  las patas COMPLETAS (Σ compras − ventas) con BS día a día sobre los últimos 90 cierres, con el T
  que cada pata tenía ese día; serie anclada (shift paralelo) para que HOY == −net_premium (coincide
  con la prima de la tarjeta). Positivo = débito, negativo = crédito (obligación). UI: canvas
  `stratpx-canvas-<idx>` (`drawStratHistory`, se dibuja junto al payoff al expandir la tarjeta).
- Each stock in scanner has an "OPTIONS LAB" button to jump to deep single-symbol analysis
- API: `/api/options-lab/<symbol>` (single), `/api/options-lab-top` (auto top 10)
- Config: `OPTIONS_RISK_FREE_RATE`, `OPTIONS_DTE_TARGETS`, `OPTIONS_TOP_STRATEGIES`, `OPTIONS_BACKTEST_HORIZONS`
- JS state: `_olabData`, `_olabLoaded`
- CSS classes prefixed `.olab-`

## Trades Históricos (tab)
- **Emparejado por EPISODIO de posición (flat → flat), 2026-09** — `build_trades_history`
  (`vista_web.py`, el cloud la importa tal cual). Cuatro bugs que costaron plata mal reportada:
  1. **El `realized_pnl` de IB viaja en el fill que CIERRA**: en un largo es el SELL, pero en un
     **corto** y en **la pata vendida de todo spread** es el BUY. El código sumaba solo los SELL →
     los shorts salían en $0.00 y cada spread perdía la pata corta entera (COST −310.70 en vez de
     −186.41, ADBE −1001.16 en vez de −693.32, TSLA −1248.67 en vez de −857.97). Ahora
     `_ep_totals` suma el `realized_pnl` de TODOS los fills del episodio, prorrateado por qty
     cuando un fill se parte entre episodios. **Chequeo de regresión**: Σ `pnl` de los trades
     construidos debe dar exactamente Σ `realized_pnl` del `trades_imported.json`.
  2. **Quién decide si un fill ABRE o CIERRA es IB, no el contador de posición.** Reconstruir la
     posición sumando qty y clasificar por el signo NO sirve: la ventana importada casi nunca
     arranca en flat, así que el contador queda corrido para siempre. En ARKK quedaban 649
     acciones fantasma de un largo, y la venta en corto de 700 @86.63 (`realized_pnl=0`, o sea
     **apertura pura** para IB) se usaba para "cerrar" ese largo → solo 51 acciones entraban al
     short, monto invertido **$4.418 en vez de $60.641** y P&L **+$208.76 en vez de +$2.875,61**
     (el resto se perdía por el prorrateo de `_fill_share`). Regla: `realized_pnl != 0` ⇒ cierre;
     `== 0` ⇒ apertura. Si un cierre excede la posición trackeada, el excedente va a un episodio
     `estimated` (apertura previa a la ventana). `pnl_known` cae al criterio de posición solo si
     NINGÚN fill del archivo trae `realized_pnl` (formato viejo).
  3. **Una posición que nunca volvió a flat y no realizó nada NO es un trade cerrado** (un short
     vivo, un spread abierto). Antes salían como filas "+$0.00 / +0.0% / LOSS / 0d". Pero
     descartarlas en silencio hacía parecer que **"faltan trades"**: ahora `build_trades_history`
     devuelve `open_positions` (símbolo/pata, dirección, qty, precio medio, desde cuándo) y el tab
     las lista en un panel aparte bajo el listado (`renderThOpen`). (El bloque que leía
     `portfolio_history.json` para `open_symbols` era **dead code** — nunca se usaba — y se borró:
     el flat→flat lo cubre.)
  4. **Opciones**: se agrupaban por (subyacente, expiry), fusionando spreads distintos abiertos en
     fechas distintas, y el "invertido" se calculaba neteando aperturas CONTRA cierres → retornos
     de −1018% o +266%. Ahora el episodio se arma **por pata** (root, expiry, tipo, strike) y las
     patas se agrupan por **fecha de apertura** (las patas de un spread se abren el mismo día).
     `invested` = |coste neto de apertura| (débito pagado o crédito cobrado, flag `direction`
     DEBITO/CREDITO). Nota: el `realized_pnl` de IB **ya viene neto de comisiones**, así que un
     débito puede perder >100% (AAPL C305/320: $400 de débito, −538.36 con $138 de comisiones).
  5. **`_option_root`**: tras un ajuste/split IB renombra el contrato agregando un dígito al root
     ('SQQQ' → 'SQQQ2'), así que la apertura y el cierre quedaban en símbolos distintos y el
     spread se listaba partido en dos filas (una en $0.00, otra con media pata).
- Shorts de acciones soportados de punta a punta: `direction` LONG/SHORT en el payload, badge
  SHORT, filtro "Shorts", etiquetas "Precio Venta (entrada)" / "Precio Compra (salida)", flechas
  del gráfico invertidas (SELL abre / BUY cierra) y `_generate_trade_thesis(..., direction)` que
  no dice "posible giro al alza" en la entrada de un corto (el endpoint recibe `?dir=`).
- `result` es WIN / LOSS / **BE** (antes `pnl > 0 else LOSS` marcaba perdedor a todo lo que diera
  exactamente 0); el win rate se calcula sobre WIN+LOSS, sin los BE.
- **Retorno del panel = ponderado por capital** (`weighted_return_pct` = Σpnl / Σinvertido). El
  promedio simple de `pnl_pct` mostraba **−40.5% al lado de un P&L de +$184k**: una opción de $300
  que expira vale −100% y pesaba igual que una acción de $80k haciendo +10%. `avg_return_pct`
  sigue en el payload y se muestra en el tooltip de la tarjeta.
- **Signos en el JS**: `pnlSign` salía de `pnl>=0?'+':''` y el valor iba con `Math.abs` → una
  pérdida se mostraba como "$5,428.84" sin el menos. El signo se deriva del valor (`>0` / `<0`),
  y el **`pnl_pct` NO lleva prefijo de signo** (ya trae el suyo: prefijarlo daba "- -40.3%").
- Pairs BUY/SELL fills from `trades_imported.json` into completed round-trip trades
- Supports stocks (STK), options (OPT), and spreads (multiple strikes same expiry grouped)
- Option symbol format: `AAPL  260417C00305000` → ticker, expiry, type (C/P), strike
- Trades without recorded BUY: entry price estimated from `realized_pnl / qty`
- Open positions (cross-referenced with `portfolio_history.json`) excluded
- Chart data lazy-loaded per trade via `/api/trades-history/chart/<trade_id>`
- Uses yfinance for OHLC (60d before entry → 30d after exit) + SPY context
- Stack sincronizado (`scRenderStack`, key `th_<idx>`, período fijo `ALL`): velas (BUY/SELL markers,
  líneas de entrada/salida, SMA20/50) + MACD + RSI (zonas 30/70) + Koncorde, todos con eje y crosshair
  comunes; los fills BUY/SELL se marcan también en los paneles de indicadores (`decorate.events`).
  **Líneas Entrada/Salida SOLO para STK/ETF**: en trades OPT/SPREAD esos precios son PRIMAS del
  contrato, no precios del subyacente — dibujarlas estira la escala de velas hasta lo ilegible.
  Los markers sí quedan, etiquetados "BUY/SELL prima $X".
- Auto-generated thesis in Spanish from indicator values at entry/exit dates
- API: `/api/trades-history` (cached 1hr), `/api/trades-history/chart/<trade_id>`
- CSS classes prefixed `.th-`
- JS state: `_thData`, `_thLoaded`, `_thFilter`, `_thCharts`

## Pulso del Mercado — SPY (`market_pulse.py`, tarjeta en el tab ETFs)
- Tarjeta colapsable arriba del ETF Scanner: análisis técnico conciso del SPY con veredicto
  (score -9..+9 con factores explicables), condiciones del sistema en AMBAS direcciones
  (chips `Compra x/3` / `Venta x/3` con tooltip de qué falta), momentum (MACD/RSI/Koncorde),
  soportes/resistencias por pivotes fractales, figura técnica detectada por reglas y lectura
  de la sesión en curso (gap, rango, RVOL proyectado).
- **Figuras**: desde 2026-08 vienen del motor compartido `patterns.py` (`_detect_patterns` es un
  wrapper sobre `patterns.detect` con `include_cross` + `include_fallback` — el pulso siempre tiene
  algo que decir). El payload suma `fibonacci` (aún sin render en el JS del pulso).
- Patrón `enrichment.py`: server-side puro vía yfinance (5y diario + 15m sesión), cache TTL
  10 min, cero dependencia de TWS/bridge → local y cloud idénticos. Reutiliza
  `indicators.calculate_all` + `signals.check_buy/sell_conditions` (paridad total de lecturas).
- **Dos relojes: sesión cada 10 min, análisis cada hora (2026-08, ajustado 2026-09)**:
  `_build_pulse` separa `df_full` (barra viva, se re-descarga con el TTL de 10 min) de
  `df, _ = _analysis_frame(df_full)`, el frame del ANÁLISIS **congelado por época**: indicadores,
  señal, momentum, condiciones x/3, veredicto, figuras, S/R y chart corren sobre ese frame y no se
  mueven dentro de la misma hora (sin eso parpadeaban cada 10 min con la barra a medio formar).
  Con `SIGNALS_INTRADAY_REFRESH_MINUTES > 0` ese frame incluye la vela de hoy; con 0 la descarta.
  El precio/Δ% del header, `_session_read(df_full, ...)` (gap/RVOL proyectado) y el máximo 52w usan
  la barra viva A PROPÓSITO y sí laten cada 10 min (leen la sesión, no son señal). El payload
  expone `analysis_as_of` + `analysis_live`, mostrados en `.mp-upd` ("analisis de la rueda en
  curso" vs "analisis al cierre del <fecha>").
- **Endpoint `/api/spy-pulse`** (local y espejo cloud con `@login_required`). ¡OJO!:
  `/api/market-pulse` YA EXISTE y es OTRA cosa (quotes + sentimiento miedo/codicia del ticker
  del header/briefing) — no reutilizar ese nombre.
- Gráfico: stack sincronizado estándar (`scRenderStack` key `mp`, períodos ALL→1D) con los
  S/R del análisis como `decorate.priceLines` (labels "Sop/Res Nt", también en intradía) y el
  canal ±2σ vía `decorate.lines` — extensión de `scBuild`: series en eje DIARIO alineadas al
  final del ohlc (right-align/left-pad según ventana), solo se dibujan si `!data.timeVis`.
- JS: `_mpData/_mpLoaded/_mpCollapsed/_mpPeriod`, `updateMarketPulse()` (lazy al entrar al
  tab + REFRESH_MS; expandida refresca solo el texto `#mp-head` si cambió `updated` — no
  re-monta el stack para no romper el zoom del usuario). **Arranca contraída**
  (`_mpCollapsed=true`): barra de título + chips precio/Δ%/veredicto; clic expande y monta
  el gráfico. CSS `.mp-*`.

## Dashboard Tabs
1. **Escáner** — real-time stock scanner with signals
2. **Mi Cartera** — portfolio positions with analysis
3. **Options Lab** — options strategy engine
4. **Trades Históricos** — closed trade analysis with charts

## Cloud / Multi-Tenant Deployment (Railway)
A second, separate deployment lets any user run the dashboard without installing Python locally:
their TWS stays on their machine, a small **bridge** process reads it and streams data over
WebSocket to a shared **cloud server**, which serves the *same* dashboard UI to their browser.

### Architecture
```
User's machine                          Railway (shared)
┌─────────────────┐                     ┌──────────────────────────┐
│ TWS/IB Gateway   │◄── ibapi (local)──►│                          │
│                  │                     │  cloud/server.py         │
│ bridge/main.py   │── WebSocket ───────►│  (Flask + Flask-SocketIO │
│ (pip package)    │   (analysis_batch,  │   + gevent)              │
│                  │    portfolio_data,  │                          │
│                  │    trades_data)     │  Browser ◄── HTTP ───────┤
└──────────────────┘                     └──────────────────────────┘
```
- **`cloud/server.py`** — Flask + Flask-SocketIO server (`async_mode="gevent"`). Per-user in-memory
  store (`user_data[user_id]`) holds live scan results, portfolio, and trades. `bridge_sessions`
  maps a WebSocket `sid` to a `user_id`. **The store is wiped on every container restart/redeploy**
  — two recovery paths keep the dashboard from going blank (ETF Scanner → Total 0, Mi Cartera → $0):
  (1) the bridge re-emits its last COMPLETE stock/ETF/portfolio snapshot on every (re)auth
  (`on_auth` in `bridge/main.py`, cached on `BridgeIB.last_analysis/last_etf_analysis/...`), and
  (2) the server snapshots the store to Postgres (`schedule_persist`, debounced 20s via
  `gevent.spawn_later`, `bars_*` chart cache excluded) and restores it on boot (`_restore_stores`).
  Path (1) is the fast path when the bridge is live; (2) covers restarts while the bridge is offline.
- **`cloud/db.py`** — Postgres: `users` table (email, password, `bridge_token`, `flex_token`,
  `flex_query_id`) + `user_store` table (`user_id` PK, `data` JSONB, `updated_at`) for the store
  snapshot above. The bridge token is what `bridge/main.py --token` authenticates with.
- **`bridge/`** — a **standalone pip package** (`pip install git+https://github.com/.../ib-trading-bot.git`,
  entry point `ib-bridge = bridge.main:main`). Installed into `~/.ib-bridge/venv` on the user's machine
  via `install-bridge.sh` / `/install.sh` (server-generated, so the URL/token are pre-filled).
  **Critical gotcha**: `setup.py` only packages the `bridge/` directory — root-level `indicators.py`,
  `signals.py`, `backtester.py`, `config.py` are NOT included. `bridge/indicators.py`,
  `bridge/signals.py`, `bridge/backtester.py` are therefore deliberate self-contained duplicates
  (same math, no `config.py` import, hardcoded defaults) — not doubled-up dead code.
- The dashboard HTML is `vista_web.py`'s real `DASHBOARD_HTML` (imported as-is for exact parity),
  then `cloud/server.py`'s `_inject_cloud_setup_tab()` splices in a 5th "Conectar TWS" tab + bridge
  status header via targeted string `.replace()` — the local template has no concept of "connect a
  bridge" since the local bot already IS the TWS connection. Never edit `vista_web.py` to add
  cloud-only UI; add it via that injection function instead so local stays untouched.
- **Feedback tab ("Tu Opinion", cloud-only)**: injected right after "Conectar TWS" by the same
  `_inject_cloud_setup_tab()`. Star rating (1-5) + category + comentario → `POST /api/feedback`,
  stored in the `feedback` table (`cloud/db.py`: `save_feedback`/`get_all_feedback`). `GET /api/feedback`
  is **owner-only** (email == `ADMIN_EMAIL`, default lucas.mayorca@gmail.com, env-overridable) and
  powers the "Comentarios recibidos" review panel that stays hidden (403) for everyone else.

### Known gotchas (each cost real debugging time — don't reintroduce)
- **`max_http_buffer_size`**: default is 1MB. A single `analysis_batch` of 10 stocks × 5 years of
  daily OHLC+MACD+RSI+Koncorde series is several MB — without raising this
  (`SocketIO(..., max_http_buffer_size=25*1024*1024)`), the connection dies with "packet is too
  large" the instant the first real batch goes out, and every reconnect repeats the same failure.
- **Reconnection**: rely solely on `socketio.Client(reconnection=True)`'s own background reconnect.
  A manual `sio.connect()`/`sio.disconnect()` fallback on top of it races with the library's own
  thread and produces duplicate connections that fight each other (symptom: auth succeeds then the
  socket closes within milliseconds, repeating every ~20s = `pingTimeout`). The `connect` event
  handler must re-run `bridge_auth` on every (re)connect — the library restores the transport but
  has no idea about that app-level handshake.
- **`safe_emit()`** (`bridge/main.py`) wraps every `sio.emit()` during the scan loop: waits for
  `sio.connected` + the `authenticated` `threading.Event`, retries a few times, and — critically —
  never lets an emit failure crash the whole process (the per-cycle `while True` body also has a
  broad `except Exception` that logs and retries next cycle rather than exiting).
- **Repo must be public** (or the pip-install/curl-install URLs need auth) — `install-bridge.sh` /
  `raw.githubusercontent.com` 404 silently on a private repo.
- **Portfolio holdings vs. scan watchlist**: the bridge's stock list is a fixed ~49 large-cap
  fallback list, not a live top-volume scan like local's `get_top_volume_stocks()`. Mi Cartera's
  position chart reuses the Scanner's cached analysis per symbol — so on every cycle the bridge
  merges current STK holdings (`ib_app.portfolio_positions`) into the scan list, or any held symbol
  outside that list (e.g. IBIT) shows "Sin datos históricos" forever. `_refresh_portfolio()` also
  seeds positions once before the very first cycle so holdings are present from cycle 1.
- **Trades Históricos in the cloud**: `reqExecutions` only ever returns the *current TWS session's*
  fills, not historical trades — there is no live IB API call that backfills months of history.
  Full history requires the user's own **IB Flex Web Service** (`cloud/flex.py`): a one-time Flex
  Query (Account Management → Performance & Reports → Flex Queries, "Trades" section, XML format)
  + a Flex token, both stored per-user (`users.flex_token`/`flex_query_id`) and pasted into
  Conectar TWS → "Ver historial completo de trades". When the trade list is empty, the dashboard
  shows a CTA pointing here instead of a bare "no trades" message (empty ≠ no history — it usually
  just means Flex isn't connected yet).
- **Bridge reinstall**: `run-bridge.sh` only *launches* the already-installed `ib-bridge` CLI — it
  does not pull new code. After any `bridge/` change, the fix requires `rm -rf ~/.ib-bridge &&
  curl -sL .../install-bridge.sh | bash` (a fresh `pip install --upgrade`), not just relaunching.

## Invariantes de la revisión 2026-09 (no reintroducir)
Cada uno costó debugging real. **Los que son verificables viven en `tests/test_invariantes.py`** —
`./venv/bin/python -m pytest tests/ -q`, corre en ~2s. Antes eran prosa acá y por eso nadie los corría:
USO estuvo rankeando #1 con una foto del 2026-09-11 durante días. Si agregás un invariante, agregá el test.
- **El cache de análisis NO se purga solo — Top Recomendaciones recorre el CACHE, no la watchlist.**
  `/api/data` y `/api/etf-data` arman la tabla iterando la lista de símbolos escaneados, pero
  `compute_top3(cache)` itera el dict entero. Un símbolo que sale del universo (un ETF en cartera que
  `split_held` mandó al escáner de ETFs, una tenencia cerrada que dejó de mergearse en la watchlist) o
  cuyo análisis falla — el bridge hace `if result:` y **omite en silencio** los fallidos, el cloud mergea
  por símbolo y nunca desaloja — se queda con su ÚLTIMO snapshot bueno para siempre: seguía saliendo #1
  en Top Recomendaciones, sin aparecer en la tabla, con el mismo precio, la misma señal y el **mismo score
  inmóvil durante días** (caso real: USO congelado en el cierre del 2026-09-11 — $154.90, VENTA FUERTE,
  fuerza 4.3, score 55.6 — parecía hardcodeado y era una foto vieja). Dos defensas:
  (1) `compute_top3` descarta los análisis anteriores a `_stale_as_of_cutoff(cache)` (el cierre más
  reciente del cache menos `config.MAX_ANALYSIS_STALENESS_DAYS`=5; la tolerancia **no puede ser 0**: una
  pasada que cruza las 16:00 ET deja medio universo con el cierre de ayer y medio con el de hoy, y un
  corte estricto vaciaría el Top). `_analysis_as_of` deriva la fecha de `chart.ohlc[-1].time` cuando falta
  `as_of` — **el bridge no manda `as_of`**. (2) El cloud purga `analysis`/`etf_analysis` contra la lista de
  símbolos de cada ciclo (`_prune_analysis`, no purga si la lista viene vacía).
  Corolario: **Mi Cartera del cloud busca en los DOS universos** (`etf_analysis` y `analysis`) — desde
  `split_held` un ETF en cartera vive en el de ETFs, y buscar solo en `analysis` lo dejaba sin chart
  (hasta ahora lo tapaba, justamente, la entrada congelada).
- **Las 4 defensas contra la foto congelada (2026-09, "cómo prevenimos esto a futuro")**. El defecto de
  fondo no era "el cache no se purga" sino que **dos lectores del mismo estado no coincidían sobre la
  fuente de verdad**, y que el sistema **nunca dijo que el número era viejo** — lo cazó el usuario a ojo
  después de días. Cada defensa se mide por si habría mostrado la staleness el día 1:
  1. **`universe_items(cache, universe)` es la fuente de verdad ÚNICA** sobre qué símbolos están
     vigentes: la tabla del escáner y `compute_top3` iteran lo mismo (local pasa `stock_list`/`etf_list`,
     cloud `store["stocks"]`/`["etf_stocks"]`). Mientras coincidan nadie nota nada; en cuanto divergen,
     una de las dos muestra fantasmas. Devuelve `(sym, data, stale_as_of)`: `data=None` si la foto está
     vencida y `stale_as_of` con SU fecha, para poder decirlo en vez de callarlo. Salta los símbolos del
     universo que aún no están en el cache (si no, el header de la tabla aparece sobre el spinner).
  2. **Decir la fecha, no solo filtrarla.** Descartar en silencio tapa el caso de hoy pero deja invisible
     la próxima foto congelada que caiga DENTRO de la tolerancia de 5 días. Cada recomendación viaja con
     `as_of` y el JS (`_asOfBadge`) pinta un badge ámbar cuando difiere de `signals_as_of` del lote (más
     un ⚠ en el chip colapsado); la fila "sin datos" dice "Último análisis: cierre del &lt;fecha&gt;".
     El mapa `stale` viaja aparte de `results` A PROPÓSITO: `results[sym]` debe seguir siendo fila-o-null
     o el `if(!r)` del render cae en la rama equivocada y explota.
  3. **El bridge distingue "falló" de "salió del universo".** El viejo `if result:` las mezclaba y el
     server no podía saber cuál era: en los dos casos se quedaba con el último análisis bueno. Ahora cada
     `analysis_batch`/`etf_analysis_batch` lleva `failed: [syms]`; `_record_failures` los registra en
     `store["failed"]` y los loguea. **No se borra su análisis**: un tropiezo puntual de IB no debe dejar
     la fila en blanco — el filtro por antigüedad lo saca solo cuando su fecha se atrasa. ⚠ Esto es un
     cambio en `bridge/` ⇒ **requiere reinstalar el bridge** (`rm -rf ~/.ib-bridge && curl -sL .../install-bridge.sh | bash`);
     hasta entonces el server sigue funcionando, solo que sin el campo `failed`.
  4. **`audit_universe(cache, universe, label)` evalúa los invariantes en runtime** y loguea `[AUDIT:...]`
     cuando se violan (huérfanos en el cache, análisis anteriores al cierre de referencia). Se llama en
     los 4 endpoints de scanner y se expone en `/api/debug` como `invariant_violations`/`invariants_ok`
     + `failed_last_cycle`. El `/api/debug` viejo mostraba `len(stocks)` y `len(analysis)` por separado,
     que es justo el dato que NO delata un huérfano: los dos números se ven sanos.
- **Unidades de Options Lab**: `payoff_points[].pnl`, `max_profit`, `max_loss`, `capital_required` y
  `expected_value` van TODOS **por posición** (×100). `_compute_payoff` calcula por acción y `_per_position()`
  convierte al exportar — mezclarlas hacía que el gráfico dibujara la curva por acción bajo etiquetas por
  posición ("Max +$305" sobre una curva cuyo máximo es 3.05). Chequeo: `max(payoff_points.pnl) == max_profit`.
- **Covered Call y Protective Put llevan la pata de 100 acciones** (`covered=True`): sin ella la covered call
  mostraba pérdida ilimitada AL ALZA y la protective put (bullish) rendía su máximo si el subyacente colapsaba.
- **Trades Históricos**: Σ`pnl` de los trades construidos == Σ`realized_pnl` de `trades_imported.json` (exacto).
  Un cierre PARCIAL además emite la posición remanente en `open_positions` (si no, "faltan trades").
  `_is_option_symbol` debe coincidir con `_parse_option_symbol`: un fill que pase el primero y falle el segundo
  tumbaba el endpoint entero.
- **reqIds on-demand**: `/api/bars` y el deep analysis de cartera usan `next_ondemand_req_id()` (contador bajo
  lock, 11000-12999) y `portfolio.fetch_chart_data` su propio contador — NO `hash(symbol) % rango`: los rangos
  viejos se solapaban y una colisión servía las barras de otro símbolo sin error visible. La lectura de
  `historical_data[req_id]` va DENTRO del lock.
- **Circuit breakers por consumidor**: `_IB_HIST_FAILS` (loop acciones), `_IB_HIST_FAILS_ETF` (loop ETFs) y
  `_IB_HIST_FAILS_UI` (endpoints on-demand). Compartirlos cortaba un loop a mitad de pasada y mezclaba barras
  de IB y de yfinance dentro del mismo ciclo.
- **`fetch_historical` exige `hist_done`**: con datos parciales (timeout mientras IB streamea) devolvía las
  barras MÁS VIEJAS y publicaba una señal de hace meses como si fuera de hoy.
- **`/api/data` y `/api/etf-data` toman el lock SOLO para el snapshot**; `compute_top3` (que dispara requests
  HTTP de fundamentals) y la serialización van fuera, o el loop de análisis queda frenado decenas de segundos.
- **Inyección del cloud**: los 5 splices de `_inject_cloud_setup_tab` pasan por `_replace_once`, que lanza si el
  ancla no aparece exactamente una vez. Un ancla obsoleta era un `.replace()` no-op SILENCIOSO (ya pasó: el
  commit del Pulso movió una línea de `switchTab` y el tab "Conectar TWS" dejó de inicializarse).
- **`enrichment.py` traduce el símbolo a yfinance** (`_yf_symbol`: "BRK B" → "BRK-B") pero indexa por el de IB.
- **ddof**: las Bollinger del Koncorde usan `std(ddof=0)` (el `stdev()` de Pine es poblacional) y el beta de
  enrichment `np.var(ddof=1)` (para igualar el denominador de `np.cov`).
- **`patterns.detect(apply_policy=False)`** es el modo CRUDO que usa `validate_history`: medir el edge sobre el
  resultado ya filtrado por la política es circular y congela los edges para siempre.

## Reference
- Original Pine Script: `MACD+RSI+KONCORDE YAMIL.txt`
