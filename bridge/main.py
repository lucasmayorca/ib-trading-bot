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
import sys
import threading
import time
from datetime import datetime

import certifi
import numpy as np
import pandas as pd
import requests
import socketio
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.execution import ExecutionFilter

from bridge.indicators import calculate_macd, calculate_rsi, calculate_koncorde, sma, ema
from bridge.signals import generate_signal
from bridge.backtester import run_backtest

MA_PERIODS = [200, 100, 50, 20]
EMA_PERIOD = 9

# ══════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════

SCAN_INTERVAL = 300
PORTFOLIO_INTERVAL = 30
IB_CLIENT_ID = 50

# ══════════════════════════════════════════════════════════════
#  ANSI COLORS
# ══════════════════════════════════════════════════════════════

G = "\033[92m"
R = "\033[91m"
Y = "\033[93m"
C = "\033[96m"
W = "\033[0m"


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
        self.pending_execs = {}       # exec_id -> partial fill dict (awaiting commission report)
        self.new_fills = []           # completed fills (exec + commission), ready to send
        self.executions_done = False
        self.sent_order_ids = set()   # order_ids already emitted to the server this run

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
            "quantity": float(order.totalQuantity),
            "orderType": order.orderType, "order_type": order.orderType,
            "lmtPrice": order.lmtPrice, "lmt_price": order.lmtPrice,
            "auxPrice": order.auxPrice, "aux_price": order.auxPrice,
            "parent_id": int(getattr(order, "parentId", 0) or 0),
            "status": orderState.status,
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

    # --- Executions (fills, for Trades Historicos) ---
    def execDetails(self, reqId, contract, execution):
        try:
            raw_time = str(execution.time or "").strip()
            date_part = raw_time.split()[0] if raw_time else ""
            date_str = _format_bar_date(date_part) if date_part else ""
            try:
                weekday = datetime.strptime(date_str, "%Y-%m-%d").strftime("%A") if date_str else ""
            except ValueError:
                weekday = ""

            is_option = contract.secType == "OPT"
            symbol = contract.localSymbol if is_option and contract.localSymbol else contract.symbol

            self.pending_execs[execution.execId] = {
                "symbol": symbol,
                "action": "BUY" if execution.side == "BOT" else "SELL",
                "filled_qty": float(execution.shares),
                "avg_fill_price": float(execution.price),
                "lmt_price": float(execution.price),
                "order_type": "",
                "exchange": execution.exchange,
                "parent_id": 0,
                "order_id": execution.orderId,
                "perm_id": str(execution.permId),
                "date": date_str,
                "datetime": date_str,
                "weekday": weekday,
                "hour": None,
                "sec_type": contract.secType,
                "currency": contract.currency,
                "realized_pnl": 0.0,
                "commission": 0.0,
            }
        except Exception as e:
            log(f"execDetails error: {e}", Y)

    def execDetailsEnd(self, reqId):
        self.executions_done = True

    def commissionReport(self, commissionReport):
        pending = self.pending_execs.get(commissionReport.execId)
        if pending is None:
            return
        pending["commission"] = float(commissionReport.commission or 0)
        rpnl = commissionReport.realizedPNL
        if rpnl is not None and rpnl < 1e15:  # IB sends a sentinel huge value when N/A
            pending["realized_pnl"] = float(rpnl)
        self.new_fills.append(pending)
        del self.pending_execs[commissionReport.execId]


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
    timeout = 25 if duration == "5 Y" else 15
    while not app.hist_done.get(req_id, False) and time.time() - start < timeout:
        time.sleep(0.1)  # More responsive polling
    bars = app.historical_data.get(req_id, [])
    if not bars:
        log(f"  No bars for {symbol} (timeout after {timeout}s)", Y)
    elif len(bars) < 50:
        log(f"  {symbol}: only {len(bars)} bars (need 50)", Y)
    return bars


def fetch_new_fills(app, req_id):
    """Pull executions IB reports since the bridge connected (reqExecutions
    typically only surfaces recent/current-session fills), pair each with
    its commission report, and return the ones not yet sent."""
    app.executions_done = False
    app.reqExecutions(req_id, ExecutionFilter())

    start = time.time()
    while not app.executions_done and time.time() - start < 10:
        time.sleep(0.1)
    time.sleep(1.5)  # commissionReport callbacks trail execDetails slightly

    fills = app.new_fills
    app.new_fills = []

    new_fills = [f for f in fills if f["order_id"] not in app.sent_order_ids]
    for f in new_fills:
        app.sent_order_ids.add(f["order_id"])
    return new_fills


# ══════════════════════════════════════════════════════════════
#  STOCK LIST
# ══════════════════════════════════════════════════════════════

FALLBACK_STOCKS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
    "JPM", "V", "UNH", "XOM", "MA", "JNJ", "PG", "HD", "AVGO", "CVX",
    "MRK", "ABBV", "LLY", "COST", "PEP", "KO", "ADBE", "CRM", "WMT",
    "MCD", "CSCO", "ACN", "TMO", "ABT", "DHR", "NFLX", "AMD", "INTC",
    "QCOM", "TXN", "NEE", "PM", "ORCL", "IBM", "GE", "CAT", "RTX",
    "BA", "DIS", "AMGN", "PYPL", "SBUX",
]


FALLBACK_ETFS = [
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "EFA", "EEM",
    "VWO", "VEA", "IEMG", "AGG", "BND", "TLT", "IEF", "SHY",
    "LQD", "HYG", "TIP", "VCIT", "VNQ", "XLRE", "IYR", "VNQI",
    "XLF", "XLK", "XLV", "XLE", "XLI", "XLY", "XLP", "XLU",
    "XLB", "XLC", "ARKK", "ARKW", "ARKG", "ARKF",
    "GLD", "SLV", "GDX", "IAU", "USO", "UNG",
    "SMH", "SOXX", "XBI", "IBB", "ITB", "XHB",
    "KRE", "XME", "XRT", "HACK", "BOTZ", "ROBO",
    "VIG", "SCHD", "DVY", "HDV", "VYM", "DGRO",
    "MTUM", "VLUE", "QUAL", "SIZE", "USMV",
    "RSP", "SPHD", "SPLV", "MOAT", "COWZ",
    "TQQQ", "SQQQ", "SPXL", "SPXS", "UVXY",
]


def get_stock_list():
    return FALLBACK_STOCKS[:50]


def get_etf_list():
    return FALLBACK_ETFS[:75]


# ══════════════════════════════════════════════════════════════
#  ANALYSIS
# ══════════════════════════════════════════════════════════════

def _format_bar_date(raw_date):
    """Convert IB bar date to YYYY-MM-DD format."""
    s = str(raw_date).strip()
    # IB formats: "20240315" (8 digits) or "2024-03-15" or "20240315  00:00:00" etc.
    s = s.split()[0]  # drop any time component
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s  # already YYYY-MM-DD or similar


def analyze_stock(ib_app, symbol, req_id):
    bars = fetch_historical(ib_app, symbol, req_id, duration="5 Y")
    if not bars:
        log(f"  {symbol}: No bars returned", Y)
        return None
    if len(bars) < 50:
        log(f"  {symbol}: Insufficient bars ({len(bars)} < 50), skipping", Y)
        return None

    try:
        df = pd.DataFrame(bars)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        macd_df = calculate_macd(df)
        rsi_df = calculate_rsi(df)
        koncorde_df = calculate_koncorde(df)

        indicators = {"macd": macd_df, "rsi": rsi_df, "koncorde": koncorde_df}
        signal_result = generate_signal(indicators)
        backtest_result = run_backtest(df, indicators_dict=indicators)

        price = float(df["close"].iloc[-1])
        close = df["close"]

        avg_vol = df["volume"].iloc[-20:].mean()
        dv = float(price * avg_vol)
        dollar_vol = dv if not (math.isnan(dv) or math.isinf(dv)) else 0.0

        ohlc = []
        dates = []
        for _, row in df.iterrows():
            d = _format_bar_date(row.get("date", ""))
            dates.append(d)
            ohlc.append({
                "time": d,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            })

        mas = {}
        for p in MA_PERIODS:
            if len(close) >= p:
                ma_series = sma(close, p)
                mas[f"sma{p}"] = [round(float(x), 2) if not pd.isna(x) else None for x in ma_series]
                mas[f"sma{p}_val"] = round(float(ma_series.iloc[-1]), 2)
            else:
                mas[f"sma{p}"] = []
                mas[f"sma{p}_val"] = None

        if len(close) >= EMA_PERIOD:
            ema_series = ema(close, EMA_PERIOD)
            mas["ema9"] = [round(float(x), 2) if not pd.isna(x) else None for x in ema_series]
            mas["ema9_val"] = round(float(ema_series.iloc[-1]), 2)
        else:
            mas["ema9"] = []
            mas["ema9_val"] = None

        chart_data = {
            "ohlc": ohlc,
            "dates": dates,
            "mas": mas,
            "macd": {
                "hist": [float(v) if not pd.isna(v) else None for v in macd_df["hist"]],
                "macd": [float(v) if not pd.isna(v) else None for v in macd_df["macd"]],
                "signal": [float(v) if not pd.isna(v) else None for v in macd_df["signal"]],
            },
            "rsi": [float(v) if not pd.isna(v) else None for v in rsi_df["rsi"]],
            "koncorde": {
                "verde": [float(v) if not pd.isna(v) else None for v in koncorde_df["verde"]],
                "marron": [float(v) if not pd.isna(v) else None for v in koncorde_df["marron"]],
                "azul": [float(v) if not pd.isna(v) else None for v in koncorde_df["azul"]],
                "media": [float(v) if not pd.isna(v) else None for v in koncorde_df["media"]],
            },
        }

        return clean({
            "symbol": symbol,
            "price": price,
            "dollar_vol": dollar_vol,
            "signal": signal_result.get("signal", "NEUTRAL"),
            "signal_label": signal_result.get("signal_label", "NEUTRAL"),
            "strength": signal_result.get("strength", 0),
            "conditions_met": signal_result.get("conditions_met", 0),
            "macd_ok": signal_result.get("macd_ok", False),
            "rsi_ok": signal_result.get("rsi_ok", False),
            "konc_ok": signal_result.get("konc_ok", False),
            "macd_detail": signal_result.get("macd_detail", ""),
            "rsi_detail": signal_result.get("rsi_detail", ""),
            "konc_detail": signal_result.get("konc_detail", ""),
            "values": signal_result.get("values", {}),
            "backtest": backtest_result,
            "chart": chart_data,
        })
    except Exception as e:
        log(f"Analysis error for {symbol}: {e}", R)
        return None


# ══════════════════════════════════════════════════════════════
#  MAIN BRIDGE LOOP
# ══════════════════════════════════════════════════════════════

def _refresh_portfolio(ib_app, log, timeout=15):
    """Pull current positions/account values/open orders from TWS.
    Leaves reqAccountUpdates subscribed on return -- the caller unsubscribes
    (reqAccountUpdates(False, "")) after it has sent the data, matching the
    original single-use-per-cycle flow."""
    ib_app.portfolio_positions = []
    ib_app.account_values = {}
    ib_app.account_done = False
    ib_app.open_orders = []
    ib_app.open_orders_done = False
    ib_app.reqAccountUpdates(True, "")
    ib_app.reqAllOpenOrders()

    # A fixed sleep here is fragile — right after a big stock scan TWS can
    # be slow to flush the account download, and a short wait would ship
    # an empty portfolio. Poll accountDownloadEnd instead.
    wait_start = time.time()
    while not ib_app.account_done and time.time() - wait_start < timeout:
        time.sleep(0.2)
    if not ib_app.account_done:
        log(f"  Timeout esperando datos de cartera de TWS ({timeout}s), usando lo que haya", Y)


def safe_emit(sio, event, data, server_url=None, authenticated=None, retries=8, retry_delay=4):
    """Emit that survives a mid-scan WebSocket drop instead of crashing the
    whole bridge. Just waits for python-socketio's own background
    reconnection (reconnection=True) to restore the transport and for the
    'connect' handler's re-auth handshake to finish, then sends.

    Important: this must NOT also trigger a manual sio.connect()/disconnect()
    here. Doing that raced with the library's own auto-reconnect thread and
    produced two simultaneous connections fighting each other — the server
    logs showed a fresh connection authenticate successfully and then get
    closed within milliseconds, on a loop, every ~20s (exactly pingTimeout).
    Let the library own reconnection; we only wait for it."""
    for attempt in range(retries):
        try:
            if not sio.connected:
                log(f"  Conexion caida, esperando reconexion... (intento {attempt + 1}/{retries})", Y)
                for _ in range(retry_delay * 10):
                    if sio.connected:
                        break
                    time.sleep(0.1)
            if sio.connected and authenticated is not None and not authenticated.is_set():
                authenticated.wait(timeout=10)
            if not sio.connected:
                raise RuntimeError("aun desconectado tras esperar")
            sio.emit(event, data)
            return True
        except Exception as e:
            log(f"  No se pudo enviar '{event}' (intento {attempt + 1}/{retries}): {e}", Y)
            time.sleep(retry_delay)
    log(f"  Se descarto el envio de '{event}' tras {retries} intentos", R)
    return False


def run_bridge(server_url, bridge_token, ib_host="127.0.0.1", ib_port=7497):
    log(f"Conectando a TWS en {ib_host}:{ib_port}...", C)
    ib_app = BridgeIB()
    ib_app.connect(ib_host, ib_port, IB_CLIENT_ID)
    ib_thread = threading.Thread(target=ib_app.run, daemon=True)
    ib_thread.start()

    if not ib_app.connected_event.wait(timeout=15):
        log("No se pudo conectar a TWS. Asegurate de tener TWS/IB Gateway abierto con la API habilitada.", R)
        sys.exit(1)

    log("Conectado a TWS", G)

    # engineio's polling transport uses `requests`, which bundles its own
    # trust store and just works. But the WebSocket upgrade goes through
    # websocket-client instead, which falls back to the *platform* default
    # trust store unless explicitly told otherwise — and on plenty of
    # machines (this one included) that store doesn't have the right chain,
    # so every WS upgrade fails with a swallowed SSLCertVerificationError,
    # falls back to long-polling, and that then dies to its own too-short
    # read timeout in a loop. Passing a Session with .verify pointed at
    # certifi's bundle makes engineio thread the same CA path into the
    # WebSocket handshake too.
    http_session = requests.Session()
    http_session.verify = certifi.where()

    sio = socketio.Client(
        reconnection=True, reconnection_delay=5, reconnection_delay_max=15,
        http_session=http_session,
    )
    authenticated = threading.Event()

    @sio.on("auth_result")
    def on_auth(data):
        if data.get("ok"):
            log("Autenticado con el servidor cloud", G)
            authenticated.set()
        else:
            log(f"Auth failed: {data.get('error')}", R)
            sys.exit(1)

    @sio.event
    def connect():
        # Fires on the *first* connect and on every automatic/manual
        # reconnect. python-socketio only restores the transport — it has
        # no idea about our app-level "bridge_auth" handshake, so we have
        # to redo it here every time or the server will just ignore us
        # (bridge_sessions won't have this new socket id mapped to a user).
        log("Conectado al servidor (socket)", G)
        authenticated.clear()
        sio.emit("bridge_auth", {"bridge_token": bridge_token})

    @sio.event
    def disconnect():
        log("Desconectado del servidor cloud", Y)

    @sio.on("request_bars")
    def on_request_bars(data):
        symbol = data.get("symbol")
        period = data.get("period", "1Y")
        duration_map = {"1M": "1 M", "3M": "3 M", "6M": "6 M", "1Y": "1 Y", "2Y": "2 Y", "5Y": "5 Y"}
        dur = duration_map.get(period, "1 Y")
        log(f"Fetching bars for {symbol} ({period})", C)
        bars = fetch_historical(ib_app, symbol, 8000, duration=dur)
        safe_emit(sio, "bars_data", clean({"symbol": symbol, "period": period, "bars": bars}), server_url, authenticated)

    log(f"Conectando al servidor: {server_url}", C)
    try:
        sio.connect(server_url)
    except Exception as e:
        log(f"No se pudo conectar al servidor: {e}", R)
        log("Verifica que la URL del servidor sea correcta", Y)
        ib_app.disconnect()
        sys.exit(1)

    if not authenticated.wait(timeout=10):
        log("Timeout esperando autenticacion", R)
        ib_app.disconnect()
        sys.exit(1)

    log("Bridge activo — escaneando mercado cada 5 minutos", G)
    log("Presiona Ctrl+C para detener\n", W)

    initial_fills = fetch_new_fills(ib_app, 6999)
    if initial_fills:
        log(f"  {len(initial_fills)} fills recientes encontrados, enviando al servidor...", C)
        safe_emit(sio, "trades_data", clean({"fills": initial_fills}), server_url, authenticated)

    # Seed ib_app.portfolio_positions before the very first scan cycle so
    # held stocks get merged into the watchlist from cycle 1 (otherwise
    # they'd only show up starting cycle 2, once a portfolio_data refresh
    # has actually run once).
    _refresh_portfolio(ib_app, log)
    ib_app.reqAccountUpdates(False, "")

    try:
        while True:
            try:
                stocks = get_stock_list()
                # The scan watchlist is a fixed list of well-known large
                # caps -- it has no idea what the user actually holds. Mi
                # Cartera's chart reuses the Scanner's cached analysis for
                # each symbol, so a held stock that isn't on that fixed
                # list (e.g. IBIT) would never get analyzed and its chart
                # would show "Sin datos historicos disponibles" forever.
                # Merge current holdings in so every position gets a chart.
                held_stocks = {p["symbol"] for p in ib_app.portfolio_positions if p.get("secType") == "STK"}
                for sym in held_stocks:
                    if sym not in stocks:
                        stocks.append(sym)

                safe_emit(sio, "stock_list", {"symbols": stocks}, server_url, authenticated)
                log(f"Escaneando {len(stocks)} acciones...", C)

                results = {}
                success_count = 0
                for i, symbol in enumerate(stocks):
                    req_id = 1000 + i
                    result = analyze_stock(ib_app, symbol, req_id)
                    if result:
                        results[symbol] = result
                        success_count += 1
                        log(f"  ✓ {symbol}: {result.get('signal', 'NEUTRAL')} (strength={result.get('strength', 0):.1f})", G)
                    time.sleep(0.5)

                    if (i + 1) % 10 == 0:
                        if results:
                            log(f"  Enviando {len(results)} análisis al servidor...", C)
                            safe_emit(sio, "analysis_batch", clean({"results": results}), server_url, authenticated)
                        results = {}

                if results:
                    log(f"  Enviando {len(results)} análisis finales al servidor...", C)
                    safe_emit(sio, "analysis_batch", clean({"results": results}), server_url, authenticated)

                log(f"Escaneo completado: {success_count}/{len(stocks)} acciones analizadas", G if success_count > 0 else Y)

                # --- ETF scan ---
                etfs = get_etf_list()
                safe_emit(sio, "etf_stock_list", {"symbols": etfs}, server_url, authenticated)
                log(f"Escaneando {len(etfs)} ETFs...", C)

                etf_results = {}
                etf_success = 0
                for i, symbol in enumerate(etfs):
                    req_id = 3000 + i
                    result = analyze_stock(ib_app, symbol, req_id)
                    if result:
                        etf_results[symbol] = result
                        etf_success += 1
                    time.sleep(0.5)

                    if (i + 1) % 10 == 0:
                        if etf_results:
                            log(f"  Enviando {len(etf_results)} análisis ETF al servidor...", C)
                            safe_emit(sio, "etf_analysis_batch", clean({"results": etf_results}), server_url, authenticated)
                        etf_results = {}

                if etf_results:
                    log(f"  Enviando {len(etf_results)} análisis ETF finales al servidor...", C)
                    safe_emit(sio, "etf_analysis_batch", clean({"results": etf_results}), server_url, authenticated)

                log(f"ETF scan completado: {etf_success}/{len(etfs)} ETFs analizados", G if etf_success > 0 else Y)

                _refresh_portfolio(ib_app, log)

                log(f"  Enviando cartera: {len(ib_app.portfolio_positions)} posiciones, {len(ib_app.open_orders)} ordenes abiertas...", C)
                safe_emit(sio, "portfolio_data", clean({
                    "positions": ib_app.portfolio_positions,
                    "account_values": ib_app.account_values,
                    "open_orders": ib_app.open_orders,
                    "executions": [],
                }), server_url, authenticated)
                ib_app.reqAccountUpdates(False, "")

                new_fills = fetch_new_fills(ib_app, 7000)
                if new_fills:
                    log(f"  {len(new_fills)} fills nuevos detectados, enviando al servidor...", C)
                    safe_emit(sio, "trades_data", clean({"fills": new_fills}), server_url, authenticated)

                buy_count = sum(1 for r in results.values() if r.get("signal") == "BUY") if results else 0
                sell_count = sum(1 for r in results.values() if r.get("signal") == "SELL") if results else 0
                log(f"Scan completo — {buy_count} BUY, {sell_count} SELL senales", G)
                log(f"Proximo scan en {SCAN_INTERVAL // 60} minutos...", W)
                time.sleep(SCAN_INTERVAL)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as e:
                log(f"Error durante el ciclo de escaneo: {e}", R)
                log(f"Reintentando en {SCAN_INTERVAL // 60} minutos...", Y)
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

    print("""
+==========================================+
|       IB Trading Bridge v1.0.0           |
|  Conecta tu TWS al dashboard cloud       |
+==========================================+
""")
    run_bridge(args.server, args.token, args.ib_host, args.ib_port)


if __name__ == "__main__":
    main()
