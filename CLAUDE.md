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

## Configuration (config.py)
- `SCAN_COUNT = 75` stocks
- `SCAN_INTERVAL_SECONDS = 300` (5 min)
- `MAX_PER_TRADE = 5000` USD
- `STOP_LOSS_PCT = 3.0`, `TAKE_PROFIT_PCT = 8.0`
- `MAX_OPEN_POSITIONS = 10`
- MACD: 12/26/9, RSI: 14/21, Koncorde EMA: 255

## Web Dashboard (vista_web.py)
- Default chart period: 1Y (scanner, top recommendations, portfolio)
- Counters bar breaks down by signal_label: Compra, Venta, Compra Inminente, Venta Inminente, Virando a Compra/Venta, Zona Extrema, Neutral (only shown if count > 0)
- Thesis includes: signal label + direction, indicator status (MACD hist, RSI level, Koncorde vs media), moving averages (SMA200/50/20 + golden/death cross), institutional flow (Koncorde azul), target with consistent direction, fundamentals
- Portfolio "Composicion por Tipo" and "Distribucion por Sector" sections removed

## Patterns
- IB API wrapper pattern (EWrapper + EClient inheritance)
- Req ID ranges: 1000-1999 historical, 9000-9999 portfolio, 10000+ backtest
- Threading with daemon threads + events for sync
- Spanish variable names (marron, verde, azul for Koncorde)
- ANSI color codes for terminal output
- Fallback stock list (100 liquid stocks) if scanner fails
- Rate limiting: 0.5s between API calls

## Options Lab (`options_lab.py`)
- Auto-loads top 10 opportunities on tab switch (no manual input needed)
- Pre-screens ALL scanner stocks with quick IV check, then runs full lab on top 10 candidates
- Ranking independent from signal score: considers signal strength + IV regime + HV rank + backtest confidence + liquidity
- Black-Scholes pricing + Newton-Raphson IV estimation
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

## Reference
- Original Pine Script: `MACD+RSI+KONCORDE YAMIL.txt`
