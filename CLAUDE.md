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
- **COMPRA INMINENTE**: 2/3 buy conditions met
- **VENTA INMINENTE**: 2/3 sell conditions met
- **VIRANDO A COMPRA**: 1/3 buy or multiple bullish hints (RSI < 40, MACD/Koncorde turning)
- **VIRANDO A VENTA**: 1/3 sell or multiple bearish hints (RSI > 60, MACD/Koncorde turning)
- **ZONA DE SOBREVENTA**: RSI < 35, no conditions met
- **ZONA DE SOBRECOMPRA**: RSI > 65, no conditions met
- **NEUTRAL**: no clear direction

`signal_label` is display-only; `signal` (BUY/SELL/HOLD) drives order execution in `bot.py`.
The thesis (`_generate_thesis`) and rationale (`_generate_rationale`) use `signal_label` for direction consistency.
`_label_is_bearish(label)` (vista_web.py) is the shared bearish/bullish check used by `_compute_price_levels`
(entry/target/stop), `_score_stock` (win_rate/avg_return component), and the recommendation/portfolio deep-analysis
win_rate/avg_return fields — always branch on `signal_label` here, not raw `signal`, since INMINENTE/VIRANDO/ZONA
labels can be directional while `signal` is still HOLD.

## Configuration (config.py)
- `SCAN_COUNT = 100` acciones y ETFs. El scanner de IB devuelve máx ~50 filas por
  suscripción, así que `scanner._merge_to_count()` fusiona el top-volumen en vivo (≤50)
  con la lista curada de respaldo hasta completar 100 únicos (con/sin TWS). Fallbacks:
  `FALLBACK_STOCKS`=100, `FALLBACK_ETFS`=113. El bridge (`bridge/main.py`) tiene sus
  propias copias (self-contained) también a 100 — `get_stock_list()[:100]`, `get_etf_list()[:100]`
- `SCAN_INTERVAL_SECONDS = 300` (5 min)
- `MAX_PER_TRADE = 5000` USD
- `STOP_LOSS_PCT = 3.0`, `TAKE_PROFIT_PCT = 8.0`
- `MAX_OPEN_POSITIONS = 10`
- MACD: 12/26/9, RSI: 14/21, Koncorde EMA: 255
- Backtest: `BACKTEST_COST_PCT=0.10` (round-trip), `BACKTEST_COOLDOWN=True`,
  `BACKTEST_ROBUST_TRADES=12` (muestra para confianza plena), `BACKTEST_TREND_SMA=200`

## Web Dashboard (vista_web.py)
- Default chart period: 1Y (scanner, top recommendations, portfolio)
- `compute_top3()` muestra `config.TOP_RECOMMENDATIONS` (5) recomendaciones en cada scanner
  (acciones y ETFs, local y cloud). El nombre `top3`/`renderTop3` se conserva por historia;
  el render itera sobre la longitud del array, no asume 3.
- Counters bar breaks down by signal_label: Compra, Venta, Compra Inminente, Venta Inminente, Virando a Compra/Venta, Zona Extrema, Neutral (only shown if count > 0)
- Thesis includes: signal label + direction, indicator status (MACD hist, RSI level, Koncorde vs media), moving averages (SMA200/50/20 + golden/death cross), institutional flow (Koncorde azul), target with consistent direction, fundamentals
- Portfolio "Composicion por Tipo" and "Distribucion por Sector" sections removed
- **Theme: "Cobalto Suizo" (light)** — white surfaces on warm-grey bg (#f4f4f1), cobalt accent (#2456e6),
  buy green #0b7a4b, sell red #c22436, hold amber #b45309. Tokens live in the `:root{...}` block of
  `DASHBOARD_HTML`; chart colors (Lightweight Charts / Chart.js / canvas payoff) are passed via JS literals,
  NOT CSS — when changing palette, sweep both. Dark-theme colors must not be reintroduced (user explicitly
  chose light background for readability).

## Backtest & Calibración (calidad de la estimación)
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
  contra su SMA200). El fallback relajado en `compute_top3` usa la misma escala.
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
  más monotonicidad y splits por régimen/dirección. UI: panel colapsable arriba del tab
  Escáner (`.calib-*`, `renderCalibration`). El cloud tiene su propio endpoint espejo.

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
- Each stock in scanner has an "OPTIONS LAB" button to jump to deep single-symbol analysis
- API: `/api/options-lab/<symbol>` (single), `/api/options-lab-top` (auto top 10)
- Config: `OPTIONS_RISK_FREE_RATE`, `OPTIONS_DTE_TARGETS`, `OPTIONS_TOP_STRATEGIES`, `OPTIONS_BACKTEST_HORIZONS`
- JS state: `_olabData`, `_olabLoaded`
- CSS classes prefixed `.olab-`

## Trades Históricos (tab)
- Pairs BUY/SELL fills from `trades_imported.json` into completed round-trip trades
- Supports stocks (STK), options (OPT), and spreads (multiple strikes same expiry grouped)
- Option symbol format: `AAPL  260417C00305000` → ticker, expiry, type (C/P), strike
- Trades without recorded BUY: entry price estimated from `realized_pnl / qty`
- Open positions (cross-referenced with `portfolio_history.json`) excluded
- Chart data lazy-loaded per trade via `/api/trades-history/chart/<trade_id>`
- Uses yfinance for OHLC (60d before entry → 30d after exit) + SPY context
- Candlestick chart (Lightweight Charts): BUY/SELL markers, entry/exit price lines, SMA20/50
- Indicator charts (Chart.js): MACD, RSI (with 30/70 zones), Koncorde — all with vertical BUY/SELL marker lines
- Auto-generated thesis in Spanish from indicator values at entry/exit dates
- API: `/api/trades-history` (cached 1hr), `/api/trades-history/chart/<trade_id>`
- CSS classes prefixed `.th-`
- JS state: `_thData`, `_thLoaded`, `_thFilter`, `_thCharts`

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

## Reference
- Original Pine Script: `MACD+RSI+KONCORDE YAMIL.txt`
