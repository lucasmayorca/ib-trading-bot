"""
Vista de Analisis - Dashboard con MACD + RSI + KONCORDE
Replica la logica del script de TradingView dentro del ecosistema IB.
Conecta a TWS, obtiene datos historicos y market data en tiempo real,
calcula todos los indicadores y muestra senales para cada simbolo.

Uso: python vista_analisis.py
"""

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
import threading
import time
import sys
from datetime import datetime

import pandas as pd

import config
import indicators
import signals


# ══════════════════════════════════════════════════════════════
#  COLORES ANSI
# ══════════════════════════════════════════════════════════════

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
CYAN = "\033[96m"
MAGENTA = "\033[95m"
WHITE = "\033[97m"
BG_GREEN = "\033[42m"
BG_RED = "\033[41m"
BG_YELLOW = "\033[43m"

CHECK = f"{GREEN}+{RESET}"
CROSS = f"{RED}-{RESET}"


# ══════════════════════════════════════════════════════════════
#  CONEXION IB
# ══════════════════════════════════════════════════════════════

class VistaIB(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.connected_event = threading.Event()
        self.historical_data = {}
        self.hist_done = {}
        self.market_data = {}

    def nextValidId(self, orderId):
        self.connected_event.set()

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        ignored = [2104, 2106, 2158, 2119, 2108, 2103, 2105, 2174, 2176]
        if errorCode in ignored:
            return
        if errorCode in [162, 200]:
            self.hist_done[reqId] = True
            return

    def historicalData(self, reqId, bar):
        if reqId not in self.historical_data:
            self.historical_data[reqId] = []
        self.historical_data[reqId].append({
            "date": bar.date,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": float(bar.volume),
        })

    def historicalDataEnd(self, reqId, start, end):
        self.hist_done[reqId] = True

    def tickPrice(self, reqId, tickType, price, attrib):
        if reqId not in self.market_data:
            self.market_data[reqId] = {}
        types = {
            1: "bid", 2: "ask", 4: "last",
            66: "delayed_bid", 67: "delayed_ask", 68: "delayed_last",
        }
        if tickType in types:
            self.market_data[reqId][types[tickType]] = price

    def tickSize(self, reqId, tickType, size):
        if reqId not in self.market_data:
            self.market_data[reqId] = {}
        types = {8: "volume", 72: "delayed_volume"}
        if tickType in types:
            self.market_data[reqId][types[tickType]] = size


def make_contract(symbol):
    c = Contract()
    c.symbol = symbol
    c.secType = "STK"
    c.exchange = "SMART"
    c.currency = "USD"
    return c


# ══════════════════════════════════════════════════════════════
#  DATOS E INDICADORES
# ══════════════════════════════════════════════════════════════

def fetch_historical(app, symbol, req_id):
    """Obtiene datos historicos de IB para un simbolo."""
    contract = make_contract(symbol)
    app.historical_data[req_id] = []
    app.hist_done[req_id] = False

    app.reqHistoricalData(
        req_id, contract, "",
        config.HIST_DURATION, config.HIST_BAR_SIZE,
        config.HIST_WHAT_TO_SHOW, 1, 1, False, []
    )

    start = time.time()
    while not app.hist_done.get(req_id, False) and time.time() - start < 30:
        time.sleep(0.2)

    data = app.historical_data.get(req_id, [])
    if not data:
        return None
    return pd.DataFrame(data)


def analyze_symbol(df):
    """Calcula indicadores y genera senal para un DataFrame OHLCV."""
    if df is None or len(df) < 50:
        return None
    try:
        ind = indicators.calculate_all(df)
        sig = signals.generate_signal(ind)
        sig["price"] = df["close"].iloc[-1]
        return sig
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════
#  DISPLAY
# ══════════════════════════════════════════════════════════════

def _signal_badge(signal):
    if signal == "BUY":
        return f"{BG_GREEN}{WHITE}{BOLD} COMPRA {RESET}"
    elif signal == "SELL":
        return f"{BG_RED}{WHITE}{BOLD} VENTA  {RESET}"
    return f"{BG_YELLOW}{WHITE}{BOLD}  HOLD  {RESET}"


def _check_icon(ok):
    return CHECK if ok else CROSS


def _rsi_label(val):
    if val < 30:
        return f"{GREEN}SOBREVENTA{RESET}"
    elif val < 40:
        return f"{GREEN}bajo{RESET}"
    elif val > 70:
        return f"{RED}SOBRECOMPRA{RESET}"
    elif val > 60:
        return f"{RED}alto{RESET}"
    return f"{DIM}neutral{RESET}"


def _koncorde_bar(verde, marron):
    """Mini barra visual: verde vs marron."""
    if verde > marron:
        diff = min(int(abs(verde - marron) / 2), 10)
        return f"{GREEN}{'█' * max(diff, 1)}{RESET}"
    else:
        diff = min(int(abs(marron - verde) / 2), 10)
        return f"{RED}{'█' * max(diff, 1)}{RESET}"


def _get_rt_price(app, symbol, fallback_price):
    """Obtiene precio en tiempo real si esta disponible."""
    if symbol not in config.WATCHLIST:
        return fallback_price, {}
    mkt_idx = config.WATCHLIST.index(symbol)
    mkt_req = 5000 + mkt_idx
    mkt = app.market_data.get(mkt_req, {})
    rt = mkt.get("delayed_last") or mkt.get("last")
    price = rt if rt and rt > 0 else fallback_price
    return price, mkt


def display_dashboard(results, app, last_update, next_update):
    """Dibuja el dashboard completo en terminal."""
    print("\033[H\033[J", end="")

    now = datetime.now()
    w = 78

    # ── Encabezado ──
    print(f"{BOLD}{CYAN}{'═' * w}{RESET}")
    print(f"{BOLD}{CYAN}  VISTA ANALISIS{RESET}  -  MACD + RSI + KONCORDE")
    print(f"  {now.strftime('%d-%b-%Y %H:%M:%S')}  |  Puerto: {config.IB_PORT}"
          f" ({'PAPER' if config.IB_PORT == 7497 else 'LIVE'})")
    print(f"{BOLD}{CYAN}{'═' * w}{RESET}")

    # ── Contadores ──
    buy_c = sum(1 for r in results.values() if r and r["signal"] == "BUY")
    sell_c = sum(1 for r in results.values() if r and r["signal"] == "SELL")
    hold_c = sum(1 for r in results.values() if r and r["signal"] == "HOLD")
    no_data = sum(1 for r in results.values() if r is None)

    print(f"\n  {GREEN}■ COMPRA:{RESET} {buy_c}   "
          f"{RED}■ VENTA:{RESET} {sell_c}   "
          f"{YELLOW}■ HOLD:{RESET} {hold_c}   "
          f"{DIM}Sin datos: {no_data}{RESET}\n")

    # ── Tabla resumen ──
    header = (f"  {BOLD}{'SIMBOLO':<8} {'PRECIO':>9}  {'SEÑAL':>7} "
              f"{'FUERZA':>7}  {'MACD':>4} {'RSI':>4} {'KONC':>4}  COND{RESET}")
    print(header)
    print(f"  {'─' * 64}")

    # Ordenar: BUY primero (mayor fuerza), luego SELL, luego HOLD
    order = {"BUY": 0, "SELL": 1, "HOLD": 2}
    sorted_symbols = sorted(
        results.items(),
        key=lambda x: (
            order.get(x[1]["signal"], 3) if x[1] else 4,
            -(x[1]["strength"] if x[1] else 0),
        ),
    )

    for symbol, sig in sorted_symbols:
        if sig is None:
            print(f"  {DIM}{symbol:<8} {'---':>9}  {'N/A':>7}{RESET}")
            continue

        price, _ = _get_rt_price(app, symbol, sig["price"])

        sig_label = sig["signal"]
        if sig_label == "BUY":
            sig_label = f"{GREEN}{BOLD}COMPRA{RESET}"
        elif sig_label == "SELL":
            sig_label = f"{RED}{BOLD}VENTA {RESET}"
        else:
            sig_label = f"{YELLOW}HOLD  {RESET}"

        cond = sig["conditions_met"]
        cond_color = GREEN if cond == 3 else (YELLOW if cond >= 2 else RED)

        print(f"  {BOLD}{symbol:<8}{RESET} ${price:>8.2f}  {sig_label} "
              f"{sig['strength']:>7.1f}  "
              f"  {_check_icon(sig['macd_ok'])}    {_check_icon(sig['rsi_ok'])}    {_check_icon(sig['konc_ok'])}  "
              f"{cond_color}{cond}/3{RESET}")

    # ── Detalle por simbolo ──
    print(f"\n{CYAN}{'─' * w}{RESET}")
    print(f"{BOLD}  DETALLE POR SIMBOLO{RESET}")
    print(f"{CYAN}{'─' * w}{RESET}")

    for symbol, sig in sorted_symbols:
        if sig is None:
            continue

        price, mkt = _get_rt_price(app, symbol, sig["price"])

        # Market data extras
        extras = ""
        bid = mkt.get("delayed_bid") or mkt.get("bid")
        ask = mkt.get("delayed_ask") or mkt.get("ask")
        vol = mkt.get("delayed_volume") or mkt.get("volume")
        if bid:
            extras += f"  Bid:{bid:.2f}"
        if ask:
            extras += f"  Ask:{ask:.2f}"
        if vol:
            extras += f"  Vol:{vol / 1_000_000:.1f}M" if vol >= 1_000_000 else f"  Vol:{vol:,.0f}"

        badge = _signal_badge(sig["signal"])
        cond = sig["conditions_met"]
        cond_color = GREEN if cond == 3 else (YELLOW if cond >= 2 else RED)

        print(f"\n  {BOLD}{symbol}{RESET}  ${price:.2f}{extras}"
              f"  {badge}  {cond_color}{cond}/3{RESET}"
              f"  fuerza: {sig['strength']:.1f}")

        # Condiciones con check/cross (mismo estilo que bot.py)
        print(f"    [{_check_icon(sig['macd_ok'])}] {BLUE}MACD{RESET}:     {sig['macd_detail']}")
        print(f"    [{_check_icon(sig['rsi_ok'])}] {MAGENTA}RSI{RESET}:      {sig['rsi_detail']}")
        print(f"    [{_check_icon(sig['konc_ok'])}] {YELLOW}Koncorde{RESET}: {sig['konc_detail']}")

        # Valores actuales
        vals = []
        if "macd" in sig["values"]:
            m = sig["values"]["macd"]
            hist_c = GREEN if m["hist"] > 0 else RED
            vals.append(f"MACD:{m['macd']:+.2f} Sig:{m['signal']:+.2f} "
                        f"Hist:{hist_c}{m['hist']:+.2f}{RESET}")
        if "rsi" in sig["values"]:
            r = sig["values"]["rsi"]
            vals.append(f"RSI:{r:.1f}({_rsi_label(r)})")
        if "koncorde" in sig["values"]:
            k = sig["values"]["koncorde"]
            vc = GREEN if k["verde"] > k["marron"] else RED
            ac = GREEN if k["azul"] > 0 else RED
            bar = _koncorde_bar(k["verde"], k["marron"])
            vals.append(f"K: {vc}V={k['verde']:+.1f}{RESET} "
                        f"{YELLOW}M={k['marron']:+.1f}{RESET} "
                        f"{ac}A={k['azul']:+.1f}{RESET} "
                        f"Med={k['media']:+.1f} {bar}")

        if vals:
            print(f"    {DIM}{'  |  '.join(vals)}{RESET}")

    # ── Pie ──
    print(f"\n{CYAN}{'═' * w}{RESET}")
    print(f"  Actualizado: {last_update}  |  Proximo: {next_update}")
    print(f"  Ctrl+C para detener")
    print(f"{CYAN}{'═' * w}{RESET}")


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

def main():
    watchlist = config.WATCHLIST
    if not watchlist:
        print("ERROR: WATCHLIST esta vacia en config.py")
        return

    print(f"{BOLD}VISTA ANALISIS - MACD + RSI + KONCORDE{RESET}")
    print(f"Simbolos: {', '.join(watchlist)}")
    print(f"Conectando a TWS ({config.IB_HOST}:{config.IB_PORT})...\n")

    app = VistaIB()
    app.connect(config.IB_HOST, config.IB_PORT, config.VISTA_CLIENT_ID)

    thread = threading.Thread(target=app.run, daemon=True)
    thread.start()

    if not app.connected_event.wait(timeout=10):
        print("ERROR: No se pudo conectar a TWS.")
        print("Verifica que TWS o IB Gateway estan corriendo.")
        return

    print("Conectado!\n")

    # Datos diferidos (gratis)
    app.reqMarketDataType(4)  # delayed-frozen: sirve el cierre fuera de horario
    time.sleep(0.5)

    # Suscribir market data para cada simbolo
    for i, symbol in enumerate(watchlist):
        app.reqMktData(5000 + i, make_contract(symbol), "", False, False, [])
        time.sleep(0.3)

    try:
        while True:
            results = {}
            for i, symbol in enumerate(watchlist):
                req_id = 2000 + i
                sys.stdout.write(
                    f"\r  Analizando {symbol}... ({i + 1}/{len(watchlist)})    "
                )
                sys.stdout.flush()

                df = fetch_historical(app, symbol, req_id)
                results[symbol] = analyze_symbol(df)
                time.sleep(1)  # IB pacing

            last_update = datetime.now().strftime("%H:%M:%S")
            next_ts = time.time() + config.VISTA_REFRESH_SECONDS
            next_update = datetime.fromtimestamp(next_ts).strftime("%H:%M:%S")

            display_dashboard(results, app, last_update, next_update)

            time.sleep(config.VISTA_REFRESH_SECONDS)

    except KeyboardInterrupt:
        print(f"\n\n{DIM}Deteniendo...{RESET}")
        for i in range(len(watchlist)):
            app.cancelMktData(5000 + i)
        app.disconnect()
        print("Desconectado.")


if __name__ == "__main__":
    main()
