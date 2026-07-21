# === CONEXION IB ===
IB_HOST = "127.0.0.1"
IB_PORT = 7497          # 7497 = paper trading, 7496 = live
IB_CLIENT_ID = 3

# === SCANNER ===
SCAN_COUNT = 75          # Top N acciones por volumen
SCAN_EXCHANGE = "NYSE"
SCAN_INSTRUMENT = "STK"

# === INDICADORES (mismos parametros que el Pine Script) ===

# MACD
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# RSI
RSI_PERIOD = 14
RSI_MA_PERIOD = 21       # WMA sobre RSI

# Estocastico
STOCH_PERIOD = 14
STOCH_SMOOTH_K = 1
STOCH_SMOOTH_D = 3

# Koncorde
KONCORDE_EMA_LENGTH = 255
KONCORDE_PVI_NVI_PERIOD = 15
KONCORDE_PVI_NVI_RANGE = 90
KONCORDE_MFI_PERIOD = 14
KONCORDE_BB_PERIOD = 25
KONCORDE_BB_MULT = 2.0
KONCORDE_RSI_PERIOD = 14
KONCORDE_STOCH_PERIOD = 21
KONCORDE_STOCH_SMOOTH = 3
KONCORDE_MEDIA_PERIOD = 21

# === SENALES ===
# Score minimo para generar senal (cada indicador aporta 0-1, total max 3)
SIGNAL_MIN_SCORE_BUY = 2.0
SIGNAL_MIN_SCORE_SELL = 2.0

# === RISK MANAGEMENT ===
MAX_PER_TRADE = 5000     # USD maximo por operacion
STOP_LOSS_PCT = 3.0      # Stop loss %
TAKE_PROFIT_PCT = 8.0    # Take profit %
MAX_OPEN_POSITIONS = 10  # Maximo de posiciones abiertas simultaneas

# === BACKTESTING ===
BACKTEST_DURATION = "5 Y"      # 5 anos de datos para backtesting
BACKTEST_WARMUP_BARS = 260     # Barras iniciales a saltar (EMA255 + margen)
BACKTEST_MAX_HOLD_DAYS = 20    # Max dias por trade simulado
BACKTEST_COST_PCT = 0.10       # Coste round-trip por trade (comision + slippage), en %
BACKTEST_ROBUST_TRADES = 12    # Nº de trades no-solapados para peso de confianza pleno
BACKTEST_COOLDOWN = True       # No abrir un nuevo trade hasta cerrar el anterior (evita solapes)
BACKTEST_TREND_SMA = 200       # SMA para clasificar regimen (con/contra tendencia)

# === DATOS HISTORICOS ===
HIST_DURATION = "1 Y"    # 1 año de datos
HIST_BAR_SIZE = "1 day"  # Barras diarias
HIST_WHAT_TO_SHOW = "TRADES"

# === LOOP ===
SCAN_INTERVAL_SECONDS = 300  # Cada 5 minutos

# === VISTA ANALISIS (watchlist estilo Classic Lucas) ===
WATCHLIST = [
    "SPY",    # S&P 500
    "QQQ",    # Nasdaq 100
    "AAPL",   # Apple
    "TSLA",   # Tesla
    "AMZN",   # Amazon
    "GOOGL",  # Google
    "MSFT",   # Microsoft
    "NVDA",   # Nvidia
    "META",   # Meta
    "AMD",    # AMD
]
VISTA_CLIENT_ID = 4          # Client ID exclusivo para la vista
VISTA_REFRESH_SECONDS = 300  # Refrescar indicadores cada 5 minutos

# === PORTFOLIO AVANZADO ===

# Benchmark
BENCHMARK_SYMBOL = "SPY"

# Allocation targets (pct, deben sumar 1.0)
ALLOCATION_TARGETS = {
    "stocks": 0.70,
    "etfs": 0.30,
}
ALLOCATION_DRIFT_THRESHOLD = 0.10  # Alertar si drift > 10%

# VaR
VAR_CONFIDENCE_95 = 0.95
VAR_CONFIDENCE_99 = 0.99
VAR_LOOKBACK_DAYS = 252  # 1 year

# === OPTIONS LAB ===
OPTIONS_RISK_FREE_RATE = 0.05   # Tasa libre de riesgo (5% approx)
OPTIONS_DTE_TARGETS = [21, 30, 45]  # Vencimientos a evaluar (dias)
OPTIONS_TOP_STRATEGIES = 10     # Cuantas estrategias mostrar
OPTIONS_BACKTEST_HORIZONS = [5, 10, 20, 30, 45]  # Horizontes de backtest (dias)

# Telegram alerts
TELEGRAM_BOT_TOKEN = ""      # Obtener de @BotFather
TELEGRAM_CHAT_ID = ""        # Tu chat ID personal
TELEGRAM_ENABLED = False     # Activar manualmente
