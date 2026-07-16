## Overview
Automated trading bot connected to Interactive Brokers TWS. Scans top ~75 NYSE/NASDAQ stocks by volume, calculates MACD + RSI + Koncorde indicators, and executes bracket orders when all three align.

## Tech Stack
- **Language**: Python 3.14.0
- **Broker API**: ibapi 9.81.1.post1 (Interactive Brokers)
- **Web Dashboard**: Flask 3.1.3
- **Data**: pandas, numpy, yfinance, scipy
- **Virtual env**: `/venv/`

## Entry Points

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
| `vista_web.py` | Flask dashboard (4400+ lines) |
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

## Configuration (config.py)
- `SCAN_COUNT = 75` stocks
- `SCAN_INTERVAL_SECONDS = 300` (5 min)
- `MAX_PER_TRADE = 5000` USD
- `STOP_LOSS_PCT = 3.0`, `TAKE_PROFIT_PCT = 8.0`
- `MAX_OPEN_POSITIONS = 10`
- MACD: 12/26/9, RSI: 14/21, Koncorde EMA: 255

## Patterns
- IB API wrapper pattern (EWrapper + EClient inheritance)
- Req ID ranges: 1000-1999 historical, 9000-9999 portfolio, 10000+ backtest
- Threading with daemon threads + events for sync
- Spanish variable names (marron, verde, azul for Koncorde)
- ANSI color codes for terminal output
- Fallback stock list (100 liquid stocks) if scanner fails
- Rate limiting: 0.5s between API calls

## Options Lab
- Black-Scholes pricing + Newton-Raphson IV estimation
- 15 strategy types: Long Call/Put, Bull/Bear Spreads, Iron Condor/Butterfly, Straddle/Strangle, Calendar Spread, Covered Call, Protective Put, Butterfly, Ratio Spread
- Strategy scoring (0-100) based on: signal alignment, prob of profit, risk/reward, IV regime, backtest support
- IV misalignment detection: compares estimated IV vs HV (10d/30d/60d), HV rank percentile
- Historical backtesting: finds similar indicator conditions in 5Y data, measures outcomes at 5/10/20/30/45 day horizons
- Monte Carlo (10K sims) for probability of profit per strategy
- API: `/api/options-lab/<symbol>`, `/api/options-lab-top`
- Config: `OPTIONS_RISK_FREE_RATE`, `OPTIONS_DTE_TARGETS`, `OPTIONS_BACKTEST_HORIZONS`

## Reference
- Original Pine Script: `MACD+RSI+KONCORDE YAMIL.txt`
