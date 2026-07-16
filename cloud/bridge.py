#!/usr/bin/env python3
"""
IB Bridge — Connects your local TWS to the cloud dashboard.

Usage:
    ib-bridge --server https://your-app.railway.app --token YOUR_TOKEN
    ib-bridge --server https://your-app.railway.app --token YOUR_TOKEN --ib-port 7496
"""

import argparse
import json
import math
import os
import sys
import threading
import time
from datetime import datetime

import certifi
import numpy as np
import socketio

os.environ.setdefault("SSL_CERT_FILE", certifi.where())
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract

# ══════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════

SCAN_INTERVAL = 300  # 5 minutes
PORTFOLIO_INTERVAL = 30
CHART_BARS = 252
IB_CLIENT_ID = 50  # unique to avoid conflicts with other IB apps

# MACD / RSI / Koncorde params (same as config.py)
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
RSI_PERIOD = 14
KONCORDE_EMA_LENGTH = 255

# ══════════════════════════════════════════════════════════════
#  ANSI COLORS
# ══════════════════════════════════════════════════════════════

G = "\033[92m"  # green
R = "\033[91m"  # red
Y = "\033[93m"  # yellow
C = "\033[96m"  # cyan
W = "\033[0m"   # reset


def log(msg, color=W):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{color}[{ts}] {msg}{W}")


# ══════════════════════════════════════════════════════════════
#  JSON HELPER
# ══════════════════════════════════════════════════════════════

def clean(obj):
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        v = float(obj)
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, np.ndarray):
        return clean(obj.tolist())
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    return obj


# ══════════════════════════════════════════════════════════════
#  IB CONNECTION
# ══════════════════════════════════════════════════════════════

class BridgeIB(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.connected_event = threading.Event()
        self.historical_data = {}
        self.hist_done = {}
        self.market_data = {}
        self.portfolio_positions = []
        self.portfolio_done = False
        self.account_values = {}
        self.account_done = False
        self.open_orders = []
        self.open_orders_done = False

    def nextValidId(self, orderId):
        self.connected_event.set()

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        ignored = [2104, 2106, 2158, 2119, 2108, 2103, 2105, 2174, 2176]
        if errorCode in ignored:
            return
        if errorCode in [162, 200]:
            self.hist_done[reqId] = True
        if errorCode not in ignored:
            log(f"IB Error {errorCode}: {errorString}", Y)

    def historicalData(self, reqId, bar):
        if reqId not in self.historical_data:
            self.historical_data[reqId] = []
        self.historical_data[reqId].append({
            "date": bar.date, "open": bar.open, "high": bar.high,
            "low": bar.low, "close": bar.close, "volume": float(bar.volume),
        })

    def historicalDataEnd(self, reqId, start, end):
        self.hist_done[reqId] = True

    def updatePortfolio(self, contract, position, marketPrice, marketValue,
                        averageCost, unrealizedPNL, realizedPNL, accountName):
        if position != 0:
            self.portfolio_positions.append({
                "symbol": contract.symbol, "secType": contract.secType,
                "position": float(position), "marketPrice": marketPrice,
                "marketValue": marketValue, "averageCost": averageCost,
                "unrealizedPNL": unrealizedPNL, "realizedPNL": realizedPNL,
            })

    def updateAccountValue(self, key, val, currency, accountName):
        if currency == "USD":
            self.account_values[key] = val

    def accountDownloadEnd(self, accountName):
        self.account_done = True

    def openOrder(self, orderId, contract, order, orderState):
        self.open_orders.append({
            "orderId": orderId, "symbol": contract.symbol,
            "action": order.action, "qty": float(order.totalQuantity),
            "orderType": order.orderType, "lmtPrice": order.lmtPrice,
            "auxPrice": order.auxPrice, "status": orderState.status,
        })

    def openOrderEnd(self):
        self.open_orders_done = True

    def tickPrice(self, reqId, tickType, price, attrib):
        if reqId not in self.market_data:
            self.market_data[reqId] = {}
        types = {1: "bid", 2: "ask", 4: "last",
                 66: "delayed_bid", 67: "delayed_ask", 68: "delayed_last"}
        if tickType in types:
            self.market_data[reqId][types[tickType]] = price


def make_contract(symbol):
    c = Contract()
    c.symbol = symbol
    c.secType = "STK"
    c.exchange = "SMART"
    c.currency = "USD"
    return c


def fetch_historical(app, symbol, req_id, duration="1 Y"):
    app.historical_data[req_id] = []
    app.hist_done[req_id] = False
    app.reqHistoricalData(
        req_id, make_contract(symbol), "",
        duration, "1 day", "TRADES", 1, 1, False, [],
    )
    start = time.time()
    while not app.hist_done.get(req_id, False) and time.time() - start < 30:
        time.sleep(0.2)
    return app.historical_data.get(req_id, [])


# ══════════════════════════════════════════════════════════════
#  STOCK SCANNER (top by volume via yfinance fallback)
# ══════════════════════════════════════════════════════════════

FALLBACK_STOCKS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "BRK.B",
    "JPM", "V", "UNH", "XOM", "MA", "JNJ", "PG", "HD", "AVGO", "CVX",
    "MRK", "ABBV", "LLY", "COST", "PEP", "KO", "ADBE", "CRM", "WMT",
    "MCD", "CSCO", "ACN", "TMO", "ABT", "DHR", "NFLX", "AMD", "INTC",
    "QCOM", "TXN", "NEE", "PM", "ORCL", "IBM", "GE", "CAT", "RTX",
    "BA", "DIS", "AMGN", "PYPL", "SBUX",
]


def get_stock_list(ib_app):
    """Try IB scanner first, fall back to hardcoded list."""
    try:
        from scanner import get_top_volume_stocks
        stocks = get_top_volume_stocks(count=75)
        if stocks and len(stocks) > 10:
            return stocks
    except Exception as e:
        log(f"Scanner failed: {e}", Y)
    return FALLBACK_STOCKS[:50]


# ══════════════════════════════════════════════════════════════
#  ANALYSIS
# ══════════════════════════════════════════════════════════════

def analyze_stock(ib_app, symbol, req_id):
    """Fetch data + calculate indicators for one stock."""
    bars = fetch_historical(ib_app, symbol, req_id)
    if not bars or len(bars) < 50:
        return None

    try:
        import pandas as pd
        import indicators as ind
        import signals as sig

        df = pd.DataFrame(bars)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        macd_df = ind.calculate_macd(df)
        rsi_df = ind.calculate_rsi(df)
        koncorde_df = ind.calculate_koncorde(df)

        indicators = {"macd": macd_df, "rsi": rsi_df, "koncorde": koncorde_df}
        signal_result = sig.generate_signal(indicators)

        price = float(df["close"].iloc[-1])
        return clean({
            "symbol": symbol,
            "price": price,
            "signal": signal_result.get("signal", "NEUTRAL"),
            "score": signal_result.get("score", 0),
            "rsi": float(rsi_df["rsi"].iloc[-1]) if len(rsi_df) > 0 else 0,
            "macd_status": signal_result.get("macd_detail", "—"),
            "koncorde_status": signal_result.get("koncorde_detail", "—"),
            "macd_histogram": float(macd_df["hist"].iloc[-1]) if len(macd_df) > 0 else 0,
        })
    except Exception as e:
        log(f"Analysis error for {symbol}: {e}", R)
        return None


# ══════════════════════════════════════════════════════════════
#  MAIN BRIDGE LOOP
# ══════════════════════════════════════════════════════════════

def run_bridge(server_url, bridge_token, ib_host="127.0.0.1", ib_port=7497):
    # --- Connect to IB TWS first ---
    log(f"Conectando a TWS en {ib_host}:{ib_port}...", C)
    ib_app = BridgeIB()
    ib_app.connect(ib_host, ib_port, IB_CLIENT_ID)
    ib_thread = threading.Thread(target=ib_app.run, daemon=True)
    ib_thread.start()

    if not ib_app.connected_event.wait(timeout=15):
        log("No se pudo conectar a TWS. ¿Está abierta con la API habilitada?", R)
        sys.exit(1)

    log("Conectado a TWS ✓", G)

    # --- Connect to cloud server ---
    sio = socketio.Client(reconnection=True, reconnection_delay=5)
    authenticated = threading.Event()

    @sio.on("auth_result")
    def on_auth(data):
        if data.get("ok"):
            log("Autenticado con el servidor cloud", G)
            authenticated.set()
        else:
            log(f"Auth failed: {data.get('error')}", R)
            sys.exit(1)

    @sio.on("request_bars")
    def on_request_bars(data):
        symbol = data.get("symbol")
        period = data.get("period", "1Y")
        duration_map = {"1M": "1 M", "3M": "3 M", "6M": "6 M", "1Y": "1 Y", "2Y": "2 Y", "5Y": "5 Y"}
        dur = duration_map.get(period, "1 Y")
        log(f"Fetching bars for {symbol} ({period})", C)
        bars = fetch_historical(ib_app, symbol, 8000, duration=dur)
        sio.emit("bars_data", clean({"symbol": symbol, "period": period, "bars": bars}))

    log(f"Conectando al servidor: {server_url}", C)
    try:
        sio.connect(server_url, transports=["websocket"])
    except Exception as e:
        log(f"No se pudo conectar al servidor: {e}", R)
        log("Verifica que la URL del servidor sea correcta", Y)
        ib_app.disconnect()
        sys.exit(1)

    sio.emit("bridge_auth", {"bridge_token": bridge_token})
    if not authenticated.wait(timeout=10):
        log("Timeout esperando autenticación", R)
        ib_app.disconnect()
        sys.exit(1)

    log("Bridge activo — escaneando mercado cada 5 minutos", G)
    log("Presiona Ctrl+C para detener\n", W)

    # --- Main loop ---
    try:
        while True:
            # 1) Get stock list
            stocks = get_stock_list(ib_app)
            sio.emit("stock_list", {"symbols": stocks})
            log(f"Escaneando {len(stocks)} acciones...", C)

            # 2) Analyze each stock
            results = {}
            for i, symbol in enumerate(stocks):
                req_id = 1000 + i
                result = analyze_stock(ib_app, symbol, req_id)
                if result:
                    results[symbol] = result
                time.sleep(0.5)  # IB rate limit

                # Send batch every 10 stocks
                if (i + 1) % 10 == 0:
                    sio.emit("analysis_batch", clean({"results": results}))
                    results = {}

            # Send remaining
            if results:
                sio.emit("analysis_batch", clean({"results": results}))

            # 3) Portfolio data
            ib_app.portfolio_positions = []
            ib_app.account_values = {}
            ib_app.account_done = False
            ib_app.open_orders = []
            ib_app.open_orders_done = False
            ib_app.reqAccountUpdates(True, "")
            ib_app.reqAllOpenOrders()

            time.sleep(3)  # wait for portfolio data
            sio.emit("portfolio_data", clean({
                "positions": ib_app.portfolio_positions,
                "account_values": ib_app.account_values,
                "open_orders": ib_app.open_orders,
                "executions": [],
            }))
            ib_app.reqAccountUpdates(False, "")

            buy_count = sum(1 for r in results.values() if r.get("signal") == "BUY") if results else 0
            sell_count = sum(1 for r in results.values() if r.get("signal") == "SELL") if results else 0
            log(f"Scan completo — {buy_count} BUY, {sell_count} SELL señales", G)
            log(f"Próximo scan en {SCAN_INTERVAL // 60} minutos...", W)
            time.sleep(SCAN_INTERVAL)

    except KeyboardInterrupt:
        log("\nDeteniendo bridge...", Y)
    finally:
        ib_app.disconnect()
        sio.disconnect()
        log("Bridge desconectado", W)


# ══════════════════════════════════════════════════════════════
#  CLI ENTRY POINT
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="IB Bridge — Conecta tu TWS al dashboard cloud",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  ib-bridge --server https://my-app.railway.app --token abc123
  ib-bridge --server https://my-app.railway.app --token abc123 --ib-port 7496
        """,
    )
    parser.add_argument("--server", required=True, help="URL del servidor cloud")
    parser.add_argument("--token", required=True, help="Tu bridge token (ver dashboard)")
    parser.add_argument("--ib-host", default="127.0.0.1", help="TWS host (default: 127.0.0.1)")
    parser.add_argument("--ib-port", type=int, default=7497, help="TWS port (default: 7497 paper, 7496 live)")
    args = parser.parse_args()

    print(f"""
╔══════════════════════════════════════════╗
║       IB Trading Bridge v1.0.0          ║
║  Conecta tu TWS al dashboard cloud      ║
╚══════════════════════════════════════════╝
""")
    run_bridge(args.server, args.token, args.ib_host, args.ib_port)


if __name__ == "__main__":
    main()
