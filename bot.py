"""
Bot de Trading - MACD + RSI + Koncorde
Conecta a IB TWS, escanea top 50 acciones por volumen,
calcula indicadores y genera senales con confirmacion por terminal.
"""

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order
import threading
import time
from datetime import datetime

import pandas as pd
import numpy as np

import config
import indicators
import signals
from scanner import get_top_volume_stocks, make_contract


class TradingBot(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.next_order_id = None
        self.connected_event = threading.Event()
        self.historical_data = {}
        self.hist_done = {}
        self.positions = {}
        self.positions_done = False

    # === CONNECTION ===
    def nextValidId(self, orderId):
        self.next_order_id = orderId
        self.connected_event.set()

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        if errorCode in [2104, 2106, 2158, 2119, 2108, 2103, 2105]:
            return
        if errorCode == 162:  # No data
            self.hist_done[reqId] = True
            return
        if errorCode == 200:  # No security definition
            self.hist_done[reqId] = True
            return
        if errorCode not in [2174, 2176]:
            print(f"  Error {errorCode} (req {reqId}): {errorString}")

    # === HISTORICAL DATA ===
    def historicalData(self, reqId, bar):
        if reqId not in self.historical_data:
            self.historical_data[reqId] = []
        self.historical_data[reqId].append({
            "date": bar.date,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        })

    def historicalDataEnd(self, reqId, start, end):
        self.hist_done[reqId] = True

    # === POSITIONS ===
    def position(self, account, contract, pos, avgCost):
        if pos != 0:
            self.positions[contract.symbol] = {
                "pos": pos, "avgCost": avgCost, "secType": contract.secType
            }

    def positionEnd(self):
        self.positions_done = True

    # === ORDERS ===
    def orderStatus(self, orderId, status, filled, remaining, avgFillPrice,
                    permId, parentId, lastFillPrice, clientId, whyHeld, mktCapPrice):
        if status == "Filled":
            print(f"  ORDEN {orderId} EJECUTADA: {filled} @ ${avgFillPrice:.2f}")
        elif status in ["Cancelled", "ApiCancelled"]:
            print(f"  ORDEN {orderId} CANCELADA")

    def openOrder(self, orderId, contract, order, orderState):
        if orderState.status == "Filled":
            print(f"  Orden {orderId}: {order.action} {order.totalQuantity} {contract.symbol} - FILLED")


def connect_bot():
    """Conecta el bot a TWS y retorna la instancia."""
    bot = TradingBot()
    bot.connect(config.IB_HOST, config.IB_PORT, config.IB_CLIENT_ID)
    thread = threading.Thread(target=bot.run, daemon=True)
    thread.start()

    if not bot.connected_event.wait(timeout=10):
        print("ERROR: No se pudo conectar a TWS")
        return None

    return bot


def get_historical_data(bot, symbol, req_id):
    """Solicita datos historicos para un simbolo."""
    contract = make_contract(symbol)
    bot.historical_data[req_id] = []
    bot.hist_done[req_id] = False

    bot.reqHistoricalData(
        req_id, contract,
        "",  # endDateTime (vacio = ahora)
        config.HIST_DURATION,
        config.HIST_BAR_SIZE,
        config.HIST_WHAT_TO_SHOW,
        1,   # useRTH (solo horario regular)
        1,   # formatDate
        False,  # keepUpToDate
        []
    )

    timeout = 30
    start = time.time()
    while not bot.hist_done.get(req_id, False) and time.time() - start < timeout:
        time.sleep(0.3)

    data = bot.historical_data.get(req_id, [])
    if not data:
        return None

    df = pd.DataFrame(data)
    df["volume"] = df["volume"].astype(float)
    return df


def get_current_positions(bot):
    """Obtiene posiciones actuales de la cuenta."""
    bot.positions = {}
    bot.positions_done = False
    bot.reqPositions()

    timeout = 10
    start = time.time()
    while not bot.positions_done and time.time() - start < timeout:
        time.sleep(0.3)

    return bot.positions


def calculate_quantity(price, max_amount=None):
    """Calcula cantidad de acciones a comprar segun presupuesto."""
    if max_amount is None:
        max_amount = config.MAX_PER_TRADE
    if price <= 0:
        return 0
    qty = int(max_amount / price)
    return max(qty, 1)


def create_bracket_order(bot, action, quantity, price):
    """Crea orden con stop-loss y take-profit."""
    parent_id = bot.next_order_id
    bot.next_order_id += 1
    sl_id = bot.next_order_id
    bot.next_order_id += 1
    tp_id = bot.next_order_id
    bot.next_order_id += 1

    # Orden principal (LIMIT al precio actual)
    parent = Order()
    parent.orderId = parent_id
    parent.action = action
    parent.orderType = "LMT"
    parent.totalQuantity = quantity
    parent.lmtPrice = round(price, 2)
    parent.transmit = False

    if action == "BUY":
        sl_price = round(price * (1 - config.STOP_LOSS_PCT / 100), 2)
        tp_price = round(price * (1 + config.TAKE_PROFIT_PCT / 100), 2)
        sl_action = "SELL"
    else:
        sl_price = round(price * (1 + config.STOP_LOSS_PCT / 100), 2)
        tp_price = round(price * (1 - config.TAKE_PROFIT_PCT / 100), 2)
        sl_action = "BUY"

    # Take profit
    take_profit = Order()
    take_profit.orderId = tp_id
    take_profit.action = sl_action
    take_profit.orderType = "LMT"
    take_profit.totalQuantity = quantity
    take_profit.lmtPrice = tp_price
    take_profit.parentId = parent_id
    take_profit.transmit = False

    # Stop loss
    stop_loss = Order()
    stop_loss.orderId = sl_id
    stop_loss.action = sl_action
    stop_loss.orderType = "STP"
    stop_loss.totalQuantity = quantity
    stop_loss.auxPrice = sl_price
    stop_loss.parentId = parent_id
    stop_loss.transmit = True  # Ultimo en transmitir

    return parent, take_profit, stop_loss


def display_signal(symbol, sig, price, position_info=None):
    """Muestra la senal en terminal."""
    signal_colors = {"BUY": "\033[92m", "SELL": "\033[91m", "HOLD": "\033[93m"}
    reset = "\033[0m"
    check = "\033[92m+\033[0m"
    cross = "\033[91m-\033[0m"
    color = signal_colors.get(sig["signal"], "")

    print(f"\n{'='*60}")
    print(f"  {color}{sig['signal']}{reset}  {symbol}  @  ${price:.2f}  (fuerza: {sig['strength']:.1f})")
    print(f"{'='*60}")
    print(f"  Condiciones: {sig['conditions_met']}/3")
    print(f"    [{check if sig['macd_ok'] else cross}] MACD:     {sig['macd_detail']}")
    print(f"    [{check if sig['rsi_ok'] else cross}] RSI:      {sig['rsi_detail']}")
    print(f"    [{check if sig['konc_ok'] else cross}] Koncorde: {sig['konc_detail']}")

    if "rsi" in sig["values"]:
        print(f"  RSI actual: {sig['values']['rsi']:.1f}")
    if "macd" in sig["values"]:
        m = sig["values"]["macd"]
        print(f"  MACD: {m['macd']:.2f} | Signal: {m['signal']:.2f} | Hist: {m['hist']:.2f}")
    if "koncorde" in sig["values"]:
        k = sig["values"]["koncorde"]
        print(f"  Koncorde: V={k['verde']:.1f} M={k['marron']:.1f} A={k['azul']:.1f} Med={k['media']:.1f}")

    if position_info:
        print(f"  Posicion actual: {position_info['pos']} acciones @ ${position_info['avgCost']:.2f}")

    if sig["signal"] == "BUY":
        qty = calculate_quantity(price)
        total = qty * price
        sl = price * (1 - config.STOP_LOSS_PCT / 100)
        tp = price * (1 + config.TAKE_PROFIT_PCT / 100)
        print(f"\n  Operacion sugerida:")
        print(f"    COMPRAR {qty} acciones x ${price:.2f} = ${total:.2f}")
        print(f"    Stop-Loss: ${sl:.2f} (-{config.STOP_LOSS_PCT}%)")
        print(f"    Take-Profit: ${tp:.2f} (+{config.TAKE_PROFIT_PCT}%)")
    elif sig["signal"] == "SELL" and position_info:
        qty = int(abs(position_info["pos"]))
        print(f"\n  Operacion sugerida:")
        print(f"    VENDER {qty} acciones @ ${price:.2f}")

    print()


def run_scan_cycle(bot):
    """Ejecuta un ciclo completo de escaneo y analisis."""
    now = datetime.now().strftime("%H:%M:%S")
    print(f"\n{'#'*60}")
    print(f"  ESCANEO - {now}")
    print(f"{'#'*60}")

    # Obtener posiciones actuales
    current_positions = get_current_positions(bot)
    open_pos_count = len([p for p in current_positions.values() if p["secType"] == "STK"])

    print(f"\nPosiciones abiertas: {open_pos_count}/{config.MAX_OPEN_POSITIONS}")

    # Obtener top acciones por volumen
    print(f"\nEscaneando top {config.SCAN_COUNT} acciones por volumen...")
    stocks = get_top_volume_stocks()
    if not stocks:
        print("No se obtuvieron acciones del scanner.")
        return

    print(f"Obtenidas {len(stocks)} acciones. Analizando...\n")

    actionable_signals = []

    for i, stock in enumerate(stocks):
        symbol = stock["symbol"]
        req_id = 1000 + i

        # Obtener datos historicos
        df = get_historical_data(bot, symbol, req_id)
        if df is None or len(df) < 50:
            continue

        # Calcular indicadores
        try:
            ind = indicators.calculate_all(df)
        except Exception as e:
            continue

        # Generar senal
        sig = signals.generate_signal(ind)
        price = df["close"].iloc[-1]

        pos_info = current_positions.get(symbol)

        if sig["signal"] == "BUY":
            if open_pos_count < config.MAX_OPEN_POSITIONS and not pos_info:
                actionable_signals.append((symbol, sig, price, pos_info))
        elif sig["signal"] == "SELL" and pos_info and pos_info["pos"] > 0:
            actionable_signals.append((symbol, sig, price, pos_info))

        # Progreso
        if (i + 1) % 10 == 0:
            print(f"  Analizadas {i + 1}/{len(stocks)}...")

        time.sleep(0.5)  # Rate limiting IB API

    # Mostrar resultados
    buy_signals = [s for s in actionable_signals if s[1]["signal"] == "BUY"]
    sell_signals = [s for s in actionable_signals if s[1]["signal"] == "SELL"]

    print(f"\n{'='*60}")
    print(f"  RESULTADOS: {len(buy_signals)} BUY | {len(sell_signals)} SELL")
    print(f"{'='*60}")

    if not actionable_signals:
        print("  No hay senales accionables en este momento.")
        return

    # Ordenar por score (mas fuerte primero)
    actionable_signals.sort(key=lambda x: abs(x[1]["total_score"]), reverse=True)

    # Mostrar cada senal y pedir confirmacion
    for symbol, sig, price, pos_info in actionable_signals:
        display_signal(symbol, sig, price, pos_info)

        try:
            resp = input(f"  Ejecutar {sig['signal']} {symbol}? (si/no/salir): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nSaliendo del ciclo...")
            return

        if resp == "salir":
            print("Ciclo interrumpido.")
            return
        elif resp == "si":
            execute_order(bot, symbol, sig, price, pos_info)
            if sig["signal"] == "BUY":
                open_pos_count += 1
        else:
            print(f"  {symbol} omitido.")


def execute_order(bot, symbol, sig, price, pos_info):
    """Ejecuta una orden en IB."""
    contract = make_contract(symbol)

    if sig["signal"] == "BUY":
        qty = calculate_quantity(price)
        parent, tp, sl = create_bracket_order(bot, "BUY", qty, price)
        print(f"  Enviando orden BUY {qty} {symbol} @ ${price:.2f}...")
        bot.placeOrder(parent.orderId, contract, parent)
        bot.placeOrder(tp.orderId, contract, tp)
        bot.placeOrder(sl.orderId, contract, sl)
        print(f"  Orden enviada (ID: {parent.orderId}) con SL=${sl.auxPrice:.2f} TP=${tp.lmtPrice:.2f}")

    elif sig["signal"] == "SELL" and pos_info:
        qty = int(abs(pos_info["pos"]))
        order = Order()
        order.orderId = bot.next_order_id
        bot.next_order_id += 1
        order.action = "SELL"
        order.orderType = "LMT"
        order.totalQuantity = qty
        order.lmtPrice = round(price, 2)
        order.transmit = True
        print(f"  Enviando orden SELL {qty} {symbol} @ ${price:.2f}...")
        bot.placeOrder(order.orderId, contract, order)
        print(f"  Orden enviada (ID: {order.orderId})")


def main():
    print("=" * 60)
    print("  BOT DE TRADING - MACD + RSI + KONCORDE")
    print(f"  Max por operacion: ${config.MAX_PER_TRADE:,}")
    print(f"  Stop-Loss: {config.STOP_LOSS_PCT}% | Take-Profit: {config.TAKE_PROFIT_PCT}%")
    print(f"  Puerto: {config.IB_PORT} ({'PAPER' if config.IB_PORT == 7497 else 'LIVE'})")
    print("=" * 60)

    bot = connect_bot()
    if not bot:
        return

    print("\nConectado a TWS!\n")

    try:
        while True:
            run_scan_cycle(bot)
            print(f"\nProximo escaneo en {config.SCAN_INTERVAL_SECONDS // 60} minutos...")
            print("Presiona Ctrl+C para detener.\n")
            time.sleep(config.SCAN_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        print("\n\nDeteniendo bot...")
        bot.disconnect()
        print("Bot desconectado. Hasta luego!")


if __name__ == "__main__":
    main()
