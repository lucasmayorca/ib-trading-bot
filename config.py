# === CONEXION IB ===
IB_HOST = "127.0.0.1"
IB_PORT = 7497          # 7497 = paper trading, 7496 = live
IB_CLIENT_ID = 3

# === SCANNER ===
# Universo objetivo (acciones y ETFs). El scanner de IB devuelve como maximo ~50
# filas por suscripcion, asi que scanner.py fusiona el top-volumen en vivo con la
# lista curada de respaldo hasta completar SCAN_COUNT simbolos unicos.
SCAN_COUNT = 100         # Top N acciones / ETFs por volumen
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
# (No hay umbral de score configurable: signals.py exige 3/3 condiciones para
#  BUY/SELL. SIGNAL_MIN_SCORE_BUY/SELL existieron aca pero ningun modulo los
#  leia — se quitaron en 2026-09 para que config.py no prometa una perilla
#  que no mueve nada.)

# Objetivo minimo (% de movimiento al target) para MOSTRAR una oportunidad en las
# recomendaciones. No fuerza el objetivo (eso lo estima _compute_price_levels por
# volatilidad+historico); solo filtra: si el movimiento esperado al target es menor,
# la oportunidad no se lista. Umbral distinto para acciones y ETFs (los ETFs se
# mueven menos, asi que su piso es mas bajo).
MIN_OPPORTUNITY_TARGET_PCT = 8.0        # acciones
MIN_OPPORTUNITY_TARGET_PCT_ETF = 7.0    # ETFs

# Cuantas recomendaciones "Top" mostrar en cada scanner (acciones y ETFs).
TOP_RECOMMENDATIONS = 5

# Cada cuantos MINUTOS se re-evaluan señales y recomendaciones durante la rueda.
# 0 = modo viejo: solo cierres diarios confirmados (la barra del dia en curso se
# descarta hasta las 16:00 ET).
#
# El problema que resuelve el valor 60: el analisis corria sobre el cierre de
# AYER durante toda la jornada, asi que una rueda muy volatil no movia nada
# hasta el dia siguiente. Pero re-evaluarlo en cada ciclo de 5 min tampoco sirve
# — las condiciones de giro del sistema son comparaciones de ultima barra
# (hist[-1] vs hist[-2], marron vs media, RSI) y con la barra a medio formar
# parpadeaban: las recomendaciones entraban y salian del Top varias veces por
# hora. El punto medio es una barra viva pero "congelada por hora": el analisis
# ve el precio de hoy y se refresca en el reloj (10:00, 11:00, ... ET), sin
# reaccionar a cada tick. Coherente con el horizonte swing del usuario (mediana
# ~31 dias por trade, medido de trades_imported.json): el cortisimo plazo no
# deberia cambiar la tesis, pero el analisis tiene que estar al dia.
#
# Contrapartida asumida: el backtest solo ve barras CERRADAS, asi que durante la
# rueda la señal en vivo se calcula sobre una barra que sus estadisticas nunca
# vieron (la ultima vela puede revertir antes del cierre). `live_bar=True` en
# cada analisis marca exactamente cuando pasa eso.
SIGNALS_INTRADAY_REFRESH_MINUTES = 60

# Antiguedad maxima (dias corridos) de un analisis para que compita en Top
# Recomendaciones. El cache de analisis esta indexado POR SIMBOLO y no se purga
# solo: un simbolo que sale del universo escaneado (un ETF que dejo de
# analizarse en el loop de acciones, una tenencia que se cerro) o que falla su
# analisis se queda con su ULTIMO snapshot bueno para siempre. Como el ranking
# recorre el cache entero -- no la watchlist que dibuja la tabla -- esa foto
# congelada seguia compitiendo (y ganando) con un score inmovil durante dias.
# La tolerancia no es 0 porque una pasada que cruza las 16:00 ET deja parte del
# universo con el cierre de ayer y parte con el de hoy; 5 dias absorbe eso mas
# un fin de semana largo y sigue cortando cualquier congelamiento real.
MAX_ANALYSIS_STALENESS_DAYS = 5

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

# === FLEX WEB SERVICE (trades historicos) ===
import os as _os
FLEX_TOKEN = _os.environ.get("IB_FLEX_TOKEN", "643600840119916776246936")
FLEX_QUERY_ID = _os.environ.get("IB_FLEX_QUERY_ID", "1542204")

# Telegram alerts
TELEGRAM_BOT_TOKEN = ""      # Obtener de @BotFather
TELEGRAM_CHAT_ID = ""        # Tu chat ID personal
TELEGRAM_ENABLED = False     # Activar manualmente
