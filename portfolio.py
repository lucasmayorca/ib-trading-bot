"""
Portfolio Tracking Module para vista_web.py
============================================
Funciones de tracking de portafolio, analisis de composicion,
historial de snapshots, y deteccion de patrones.

Uso: importar desde vista_web.py e integrar las callbacks en VistaIB,
     registrar el endpoint /api/portfolio en flask_app.
"""

import json
import os
import threading
import time
import math
from datetime import datetime, timedelta
from collections import defaultdict

import numpy as np
import pandas as pd

import config

# ══════════════════════════════════════════════════════════════
#  CONSTANTES
# ══════════════════════════════════════════════════════════════

PORTFOLIO_HISTORY_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "portfolio_history.json"
)

EXECUTIONS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "executions_history.json"
)

COMPLETED_ORDERS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "completed_orders.json"
)

TRADES_IMPORT_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "trades_imported.json"
)

ETF_TICKERS = frozenset([
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "ARKK", "XLF", "XLE", "XLK",
    "XLV", "GLD", "SLV", "TLT", "HYG", "VNQ", "IEFA", "EEM", "VWO", "BND",
    "AGG", "LQD", "SCHD", "VIG", "JEPI", "JEPQ", "VGT", "SMH", "SOXX",
    "XBI", "IBB", "KRE", "XOP", "KWEB", "FXI", "EWZ", "EWJ", "RSP", "SPLG",
    "SPTM", "VEA", "VB", "VO", "VTV", "VUG", "MGK", "COWZ", "DIVO",
])

# Mapeo de ETFs a sector/categoria para breakdown
ETF_SECTOR_MAP = {
    "SPY": "Indice US", "QQQ": "Indice US", "IWM": "Indice US",
    "DIA": "Indice US", "VOO": "Indice US", "VTI": "Indice US",
    "RSP": "Indice US", "SPLG": "Indice US", "SPTM": "Indice US",
    "XLF": "Financiero", "KRE": "Financiero",
    "XLE": "Energia", "XOP": "Energia",
    "XLK": "Tecnologia", "VGT": "Tecnologia", "SMH": "Semiconductores",
    "SOXX": "Semiconductores", "MGK": "Tecnologia",
    "XLV": "Salud", "XBI": "Biotecnologia", "IBB": "Biotecnologia",
    "GLD": "Commodities", "SLV": "Commodities",
    "TLT": "Renta Fija", "HYG": "Renta Fija", "BND": "Renta Fija",
    "AGG": "Renta Fija", "LQD": "Renta Fija",
    "VNQ": "Real Estate",
    "IEFA": "Internacional", "EEM": "Emergentes", "VWO": "Emergentes",
    "KWEB": "China", "FXI": "China", "EWZ": "Brasil", "EWJ": "Japon",
    "VEA": "Internacional",
    "ARKK": "Innovacion", "SCHD": "Dividendos", "VIG": "Dividendos",
    "JEPI": "Income", "JEPQ": "Income", "COWZ": "Value", "DIVO": "Income",
    "VB": "Small Cap", "VO": "Mid Cap", "VTV": "Value", "VUG": "Growth",
}

# Req ID ranges para portfolio (evitar colision con vista_web)
_PORTFOLIO_POS_REQ_BASE = 9000
_PORTFOLIO_MKT_REQ_BASE = 9500
_PORTFOLIO_HIST_REQ_BASE = 10000
_PORTFOLIO_ACCT_REQ_ID = 9900

# Charts de posicion: contador bajo lock en vez de hash(symbol) % 400. Dos
# requests concurrentes de simbolos distintos podian caer en el mismo req_id y
# pisarse el buffer de historical_data, sirviendo velas del otro activo.
_chart_req_lock = threading.Lock()
_chart_req_id = {"n": _PORTFOLIO_HIST_REQ_BASE + 500}


def _next_chart_req_id():
    with _chart_req_lock:
        _chart_req_id["n"] += 1
        if _chart_req_id["n"] > _PORTFOLIO_HIST_REQ_BASE + 899:
            _chart_req_id["n"] = _PORTFOLIO_HIST_REQ_BASE + 500
        return _chart_req_id["n"]


portfolio_lock = threading.Lock()
portfolio_cache = {}        # resultado de analyze_portfolio()
portfolio_cache_ts = 0.0    # timestamp del ultimo analisis
PORTFOLIO_CACHE_TTL = 120   # 2 minutos


# ══════════════════════════════════════════════════════════════
#  IB CALLBACKS (agregar a VistaIB)
# ══════════════════════════════════════════════════════════════

class PortfolioMixin:
    """
    Mixin con las callbacks de IB para portafolio completo.
    Usa reqAccountUpdates que trae todo directo de IBKR:
    - Posiciones con precio de mercado, valor, P&L no realizado y realizado
    - Datos de cuenta (NetLiquidation, Cash, BuyingPower, etc.)

    En __init__ de VistaIB agregar:
        self.portfolio_positions = []
        self.portfolio_positions_done = False
        self.account_values = {}
        self.account_values_done = False
        self.account_summary_data = {}
        self.account_summary_done = False
    """

    def updatePortfolio(self, contract, position, marketPrice, marketValue,
                        averageCost, unrealizedPNL, realizedPNL, accountName):
        """Callback de IB: posicion con datos completos de mercado y P&L."""
        if not hasattr(self, "portfolio_positions"):
            self.portfolio_positions = []
        row = {
            "cuenta": accountName,
            "symbol": contract.symbol,
            "tipo": contract.secType,
            "exchange": contract.exchange,
            "moneda": contract.currency,
            "con_id": contract.conId,
            "cantidad": float(position),
            "costo_promedio": float(averageCost),
            "precio_mercado": float(marketPrice),
            "valor_mercado": float(marketValue),
            "pnl_no_realizado": float(unrealizedPNL),
            "pnl_realizado": float(realizedPNL),
        }
        # IB re-emite updatePortfolio mientras dure la suscripcion (cada tick de
        # precio): appendear duplicaba la posicion y contaba su valor dos veces.
        key = (contract.conId, accountName)
        for i, prev in enumerate(self.portfolio_positions):
            if (prev.get("con_id"), prev.get("cuenta")) == key:
                self.portfolio_positions[i] = row
                return
        self.portfolio_positions.append(row)

    def updateAccountValue(self, key, val, currency, accountName):
        """Callback de IB: dato individual de la cuenta."""
        if not hasattr(self, "account_values"):
            self.account_values = {}
        self.account_values[key] = {
            "value": val,
            "currency": currency,
            "account": accountName,
        }

    def accountDownloadEnd(self, accountName):
        """Callback de IB: fin de la descarga de datos de cuenta."""
        self.account_values_done = True
        self.portfolio_positions_done = True
        n = len(self.portfolio_positions) if hasattr(self, "portfolio_positions") else 0
        print(f"  [Portfolio] Account download completo: {n} posiciones, cuenta {accountName}")

    def position(self, account, contract, pos, avgCost):
        """Callback de IB: posicion basica (fallback)."""
        pass

    def positionEnd(self):
        """Callback de IB: fin de posiciones basicas (fallback)."""
        pass

    def accountSummary(self, reqId, account, tag, value, currency):
        """Callback de IB: dato de resumen de cuenta."""
        if not hasattr(self, "account_summary_data"):
            self.account_summary_data = {}
        self.account_summary_data[tag] = {
            "value": value,
            "currency": currency,
            "account": account,
        }

    def accountSummaryEnd(self, reqId):
        """Callback de IB: fin del resumen de cuenta."""
        self.account_summary_done = True
        print("  [Portfolio] Account summary recibido.")

    # --- Open orders (para SL / TP) ---
    def openOrder(self, orderId, contract, order, orderState):
        """Callback de IB: orden abierta (parent, SL, TP)."""
        if not hasattr(self, "open_orders"):
            self.open_orders = []
        try:
            self.open_orders.append({
                "order_id": orderId,
                "parent_id": int(getattr(order, "parentId", 0) or 0),
                "symbol": contract.symbol,
                "sec_type": contract.secType,
                "action": order.action,
                "order_type": order.orderType,
                "quantity": float(getattr(order, "totalQuantity", 0) or 0),
                "lmt_price": float(getattr(order, "lmtPrice", 0) or 0),
                "aux_price": float(getattr(order, "auxPrice", 0) or 0),
                "status": getattr(orderState, "status", ""),
            })
        except Exception:
            pass

    def orderStatus(self, orderId, status, filled, remaining, avgFillPrice,
                    permId, parentId, lastFillPrice, clientId, whyHeld, mktCapPrice):
        """Callback de IB: estado de orden. Actualiza status si ya la conocemos."""
        if not hasattr(self, "open_orders"):
            return
        for o in self.open_orders:
            if o["order_id"] == orderId:
                o["status"] = status
                try:
                    o["filled"] = float(filled or 0)
                    o["remaining"] = float(remaining or 0)
                except Exception:
                    pass
                break

    def openOrderEnd(self):
        """Callback de IB: fin de la descarga de ordenes abiertas."""
        self.open_orders_done = True

    # --- Executions (historial de fills) ---
    def execDetails(self, reqId, contract, execution):
        """Callback de IB: detalle de una ejecucion (fill)."""
        if not hasattr(self, "executions"):
            self.executions = []
        try:
            self.executions.append({
                "exec_id": execution.execId,
                "time": execution.time,            # ej "20240115  14:30:25 US/Eastern"
                "symbol": contract.symbol,
                "sec_type": contract.secType,
                "currency": contract.currency,
                "side": execution.side,            # "BOT" o "SLD"
                "shares": float(execution.shares),
                "price": float(execution.price),
                "exchange": execution.exchange,
                "order_id": execution.orderId,
                "account": execution.acctNumber,
            })
        except Exception:
            pass

    def execDetailsEnd(self, reqId):
        """Callback de IB: fin de la descarga de ejecuciones."""
        self.executions_done = True

    # --- Completed Orders (historial completo de trades) ---
    def completedOrder(self, contract, order, orderState):
        if not hasattr(self, "completed_orders"):
            self.completed_orders = []
        try:
            self.completed_orders.append({
                "symbol": contract.symbol,
                "sec_type": contract.secType,
                "exchange": contract.primaryExchange or contract.exchange,
                "currency": contract.currency,
                "con_id": contract.conId,
                "action": order.action,
                "order_type": order.orderType,
                "total_qty": float(getattr(order, "totalQuantity", 0) or 0),
                "filled_qty": float(getattr(order, "filledQuantity", 0) or 0),
                "lmt_price": float(getattr(order, "lmtPrice", 0) or 0),
                "aux_price": float(getattr(order, "auxPrice", 0) or 0),
                "avg_fill_price": float(getattr(order, "lmtPrice", 0) or 0),
                "order_id": order.orderId,
                "perm_id": order.permId,
                "parent_id": int(getattr(order, "parentId", 0) or 0),
                "oca_group": getattr(order, "ocaGroup", ""),
                "tif": getattr(order, "tif", ""),
                "account": getattr(order, "account", ""),
                "completed_time": getattr(orderState, "completedTime", ""),
                "completed_status": getattr(orderState, "completedStatus", ""),
                "commission": float(getattr(orderState, "commission", 0) or 0),
                "status": getattr(orderState, "status", ""),
            })
        except Exception:
            pass

    def completedOrdersEnd(self):
        self.completed_orders_done = True


# ══════════════════════════════════════════════════════════════
#  FETCH PORTFOLIO (posiciones + precios)
# ══════════════════════════════════════════════════════════════

def fetch_portfolio(app, timeout=15):
    """
    Solicita posiciones completas via reqAccountUpdates (incluye precios,
    valores de mercado y P&L directo de IBKR) y account summary.

    Args:
        app: instancia VistaIB conectada
        timeout: segundos max de espera

    Returns:
        {
            "positions": [...],   # con precio_mercado, valor_mercado, pnl directo de IB
            "account": {...},     # account_values de updateAccountValue
            "account_summary": {...},  # datos de reqAccountSummary
            "timestamp": "2024-01-15T10:30:00",
        }
        o None si falla
    """
    if not app or not app.isConnected():
        print("  [Portfolio] ERROR: No hay conexion a IB.")
        return None

    # Reset state
    app.portfolio_positions = []
    app.portfolio_positions_done = False
    app.account_values = {}
    app.account_values_done = False
    app.account_summary_data = {}
    app.account_summary_done = False

    # reqAccountUpdates trae todo: posiciones con precios + datos de cuenta
    print("  [Portfolio] Solicitando account updates (posiciones + precios + P&L)...")
    try:
        app.reqAccountUpdates(True, "")  # subscribe=True, acctCode="" = todas las cuentas
    except Exception as e:
        print(f"  [Portfolio] ERROR solicitando account updates: {e}")
        return None

    # Tambien pedir account summary para datos extra
    summary_tags = "NetLiquidation,TotalCashValue,GrossPositionValue,BuyingPower,UnrealizedPnL,RealizedPnL"
    try:
        app.reqAccountSummary(_PORTFOLIO_ACCT_REQ_ID, "All", summary_tags)
    except Exception as e:
        print(f"  [Portfolio] ERROR solicitando account summary: {e}")

    # Esperar a que accountDownloadEnd marque todo como listo
    start = time.time()
    while not app.portfolio_positions_done and time.time() - start < timeout:
        time.sleep(0.3)

    if not app.portfolio_positions_done:
        print("  [Portfolio] TIMEOUT esperando account updates.")

    # Esperar account summary (un poco mas)
    remaining = max(0, timeout - (time.time() - start))
    wait_acct = min(remaining, 5)
    start2 = time.time()
    while not app.account_summary_done and time.time() - start2 < wait_acct:
        time.sleep(0.3)

    # Cancelar suscripciones
    try:
        app.reqAccountUpdates(False, "")  # cancelar suscripcion
    except Exception:
        pass
    try:
        app.cancelAccountSummary(_PORTFOLIO_ACCT_REQ_ID)
    except Exception:
        pass

    positions = list(app.portfolio_positions) if hasattr(app, "portfolio_positions") else []
    account_values = dict(app.account_values) if hasattr(app, "account_values") else {}
    account_summary = dict(app.account_summary_data) if hasattr(app, "account_summary_data") else {}

    if not positions:
        print("  [Portfolio] No se encontraron posiciones abiertas.")

    return {
        "positions": positions,
        "account": account_values,
        "account_summary": account_summary,
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }


# ══════════════════════════════════════════════════════════════
#  OPEN ORDERS (para extraer SL / TP de bracket orders)
# ══════════════════════════════════════════════════════════════

def fetch_open_orders(app, timeout=6):
    """
    Solicita ordenes abiertas via reqAllOpenOrders.
    Retorna una lista de dicts con order_id, symbol, order_type, lmt/aux price, etc.
    """
    if not app or not app.isConnected():
        return []

    app.open_orders = []
    app.open_orders_done = False

    try:
        app.reqAllOpenOrders()
    except Exception as e:
        print(f"  [Portfolio] Error reqAllOpenOrders: {e}")
        return []

    start = time.time()
    while not getattr(app, "open_orders_done", False) and time.time() - start < timeout:
        time.sleep(0.2)

    return list(app.open_orders) if hasattr(app, "open_orders") else []


def extract_sl_tp_by_symbol(open_orders):
    """
    Dado el listado de ordenes abiertas, agrupa SL y TP activos por simbolo.

    Detecta bracket orders:
      - STP / STP LMT  -> stop loss
      - LMT con parentId != 0 y status distinto de cancelado -> take profit

    Returns:
        { "AAPL": { "stop_loss": 180.5, "take_profit": 210.0, "qty": 10 }, ... }
    """
    result = {}
    active_statuses = {"Submitted", "PreSubmitted", "PendingSubmit", "ApiPending"}

    for o in open_orders:
        status = o.get("status", "")
        if status and status not in active_statuses:
            continue

        sym = o.get("symbol", "")
        if not sym:
            continue

        entry = result.setdefault(sym, {
            "stop_loss": None,
            "take_profit": None,
            "qty": 0,
            "entry_limit": None,
        })

        otype = (o.get("order_type") or "").upper()
        parent_id = o.get("parent_id", 0)

        if otype in ("STP", "STP LMT", "TRAIL", "TRAIL LIMIT") and o.get("aux_price", 0) > 0:
            entry["stop_loss"] = o["aux_price"]
            entry["qty"] = max(entry["qty"], o.get("quantity", 0))
        elif otype == "LMT" and parent_id != 0 and o.get("lmt_price", 0) > 0:
            entry["take_profit"] = o["lmt_price"]
            entry["qty"] = max(entry["qty"], o.get("quantity", 0))
        elif otype == "LMT" and parent_id == 0 and o.get("lmt_price", 0) > 0:
            entry["entry_limit"] = o["lmt_price"]

    return result


def extract_pending_entries(open_orders, held_symbols):
    """Ordenes de ENTRADA cargadas en IBKR que AUN NO se ejecutaron: sin
    posicion abierta para ese simbolo. Distinto de extract_sl_tp_by_symbol,
    que solo lee SL/TP de posiciones YA existentes.

    Un bracket (bot.py create_bracket_order) tiene parent con parentId=0 +
    hijos SL/TP con parentId=<id del parent>. Si el parent ya se lleno, el
    simbolo aparece en `held_symbols` y sus hijos se leen via
    extract_sl_tp_by_symbol; si el parent AUN no lleno, no hay posicion y
    esta es la unica funcion que lo detecta (antes se descartaba en
    silencio: se pedia a IB pero nunca se exponia si el simbolo no tenia
    posicion — ver `entry_limit`, que quedaba calculado y sin usar).

    Returns: [{symbol, action, order_type, price, quantity, status, order_id}]
    una entrada por simbolo (la de mayor cantidad si hay varias ordenes)."""
    active_statuses = {"Submitted", "PreSubmitted", "PendingSubmit", "ApiPending"}
    held_upper = {s.upper() for s in held_symbols}
    by_symbol = {}

    for o in open_orders:
        status = o.get("status", "")
        if status and status not in active_statuses:
            continue
        sym = o.get("symbol", "")
        if not sym or sym.upper() in held_upper:
            continue
        if o.get("parent_id", 0):
            continue  # hijo de un bracket (SL/TP) — no es una entrada nueva

        otype = (o.get("order_type") or "").upper()
        if otype == "LMT":
            price = o.get("lmt_price") or 0
        elif otype in ("STP", "STP LMT", "TRAIL", "TRAIL LIMIT"):
            price = o.get("aux_price") or 0
        else:
            price = 0  # MKT u otro tipo sin precio fijo

        qty = o.get("quantity", 0) or 0
        existing = by_symbol.get(sym.upper())
        if existing is None or qty > existing["quantity"]:
            by_symbol[sym.upper()] = {
                "symbol": sym,
                "action": o.get("action", ""),
                "order_type": otype,
                "price": round(price, 2) if price else None,
                "quantity": qty,
                "status": status,
                "order_id": o.get("order_id"),
            }

    return list(by_symbol.values())


def _enrich_pending_orders(pending_entries, build_position_analysis_fn):
    """Adjunta analisis profundo (chart, tesis, niveles, backtest) a cada
    orden pendiente, reutilizando EL MISMO pipeline que las posiciones
    abiertas (build_position_analysis_fn = _build_position_deep_analysis de
    vista_web.py). Se arma un stub de 'position' con cantidad=0 / sin costo
    promedio: el resto del pipeline (veredicto, narrativa) ya tolera esos
    campos en 0/None con gracia (ver pos.get(..., 0) or 0 en vista_web.py)."""
    enriched = []
    for pe in pending_entries:
        sym = pe["symbol"]
        es_etf, sector = _classify_position(sym)
        stub = {
            "symbol": sym, "tipo": "STK", "cuenta": "", "moneda": "USD",
            "cantidad": 0, "costo_promedio": 0, "precio_actual": None,
            "costo_total": 0, "valor_mercado": 0, "pnl": 0, "pnl_pct": 0,
            "pnl_realizado": 0, "es_etf": es_etf, "sector": sector,
            "peso_portafolio": 0, "stop_loss": None, "take_profit": None,
            "is_pending": True, "pending_order": pe,
        }
        if build_position_analysis_fn is not None:
            try:
                deep = build_position_analysis_fn(sym, stub)
                if deep:
                    stub["analysis"] = deep
                    if not stub["precio_actual"]:
                        stub["precio_actual"] = deep.get("price") or None
            except Exception as e:
                print(f"  [Portfolio] Error deep analysis orden pendiente {sym}: {e}")
        enriched.append(stub)
    return enriched


# ══════════════════════════════════════════════════════════════
#  EXECUTIONS (historial persistente de fills)
# ══════════════════════════════════════════════════════════════

_EXEC_REQ_ID = 9950


def _load_executions():
    """Carga ejecuciones persistidas desde JSON."""
    if not os.path.exists(EXECUTIONS_FILE):
        return {"executions": []}
    try:
        with open(EXECUTIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "executions" not in data:
            return {"executions": []}
        return data
    except (json.JSONDecodeError, IOError) as e:
        print(f"  [Portfolio] Error leyendo ejecuciones: {e}")
        return {"executions": []}


def _save_executions(data):
    """Guarda ejecuciones a JSON con escritura atomica."""
    try:
        tmp = EXECUTIONS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, EXECUTIONS_FILE)
    except IOError as e:
        print(f"  [Portfolio] Error guardando ejecuciones: {e}")


def _parse_ib_exec_time(s):
    """
    IB exec time format: 'YYYYMMDD  HH:MM:SS US/Eastern' o 'YYYYMMDD-HH:MM:SS'.
    Retorna dict con {date: 'YYYY-MM-DD', datetime: 'YYYY-MM-DDTHH:MM:SS'}
    """
    if not s:
        return {"date": None, "datetime": None}
    try:
        # Normalizar: sacar TZ si esta al final
        base = s.strip().split(" US/")[0].split(" GMT")[0].strip()
        # Puede venir con '-' o doble espacio como separador
        base = base.replace("-", " ")
        parts = base.split()
        if len(parts) < 2:
            # solo fecha
            d = parts[0]
            if len(d) >= 8:
                return {"date": f"{d[:4]}-{d[4:6]}-{d[6:8]}", "datetime": f"{d[:4]}-{d[4:6]}-{d[6:8]}T00:00:00"}
            return {"date": None, "datetime": None}
        d, t = parts[0], parts[1]
        date = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) >= 8 else None
        return {
            "date": date,
            "datetime": f"{date}T{t}" if date else None,
        }
    except Exception:
        return {"date": None, "datetime": None}


def fetch_executions(app, timeout=6):
    """
    Pide ejecuciones recientes a IB (tipicamente ultimas 24hs) y las mergea
    con las persistidas. Retorna el listado completo acumulado.
    """
    from ibapi.execution import ExecutionFilter

    if not app or not app.isConnected():
        return _load_executions().get("executions", [])

    app.executions = []
    app.executions_done = False

    try:
        flt = ExecutionFilter()
        app.reqExecutions(_EXEC_REQ_ID, flt)
    except Exception as e:
        print(f"  [Portfolio] Error reqExecutions: {e}")
        return _load_executions().get("executions", [])

    start = time.time()
    while not getattr(app, "executions_done", False) and time.time() - start < timeout:
        time.sleep(0.2)

    new_execs = list(app.executions) if hasattr(app, "executions") else []

    # Mergear con las persistidas (dedupe por exec_id)
    stored = _load_executions()
    seen = {e.get("exec_id") for e in stored.get("executions", []) if e.get("exec_id")}

    added = 0
    for e in new_execs:
        if e.get("exec_id") and e["exec_id"] in seen:
            continue
        parsed = _parse_ib_exec_time(e.get("time", ""))
        e["date"] = parsed["date"]
        e["datetime"] = parsed["datetime"]
        stored["executions"].append(e)
        seen.add(e.get("exec_id"))
        added += 1

    if added > 0:
        # Ordenar por fecha
        stored["executions"].sort(key=lambda x: x.get("datetime") or x.get("date") or "")
        _save_executions(stored)
        print(f"  [Portfolio] {added} ejecuciones nuevas persistidas "
              f"(total historico: {len(stored['executions'])}).")

    return stored["executions"]


def get_executions_for_symbol(symbol, all_executions=None):
    """
    Devuelve ejecuciones para un simbolo en formato listo para chart markers:
    [{time: 'YYYY-MM-DD', price: float, qty: float, side: 'BOT'|'SLD'}]
    """
    execs = all_executions if all_executions is not None else _load_executions().get("executions", [])
    symbol_u = symbol.upper()
    result = []
    for e in execs:
        if (e.get("symbol") or "").upper() != symbol_u:
            continue
        date = e.get("date") or (_parse_ib_exec_time(e.get("time", "")).get("date"))
        if not date:
            continue
        result.append({
            "time": date,
            "price": float(e.get("price", 0) or 0),
            "qty": float(e.get("shares", 0) or 0),
            "side": (e.get("side") or "").upper(),
        })
    result.sort(key=lambda x: x["time"])
    return result


# ══════════════════════════════════════════════════════════════
#  CHART DATA PARA UN SIMBOLO (periodos 1M, 3M, 6M, 1Y, 5Y)
# ══════════════════════════════════════════════════════════════

# Mapa de periodo -> (duration_str, bar_size)
CHART_PERIOD_MAP = {
    "1M":  ("1 M",  "1 day"),
    "3M":  ("3 M",  "1 day"),
    "6M":  ("6 M",  "1 day"),
    "1Y":  ("1 Y",  "1 day"),
    "2Y":  ("2 Y",  "1 day"),
    "5Y":  ("5 Y",  "1 week"),
}


def fetch_chart_data(app, symbol, period="6M", fetch_historical_fn=None):
    """
    Obtiene OHLC para el simbolo/periodo pedido.
    Usa la funcion fetch_historical_fn de vista_web pero con un duration custom.

    Returns:
        { "symbol": "...", "period": "...", "candles": [{time, open, high, low, close}, ...] }
    """
    if period not in CHART_PERIOD_MAP:
        period = "6M"
    duration, _bar_size = CHART_PERIOD_MAP[period]

    if fetch_historical_fn is None:
        return None

    req_id = _next_chart_req_id()
    try:
        df = fetch_historical_fn(app, symbol, req_id, duration=duration)
    except TypeError:
        # fallback sin duration kwarg
        df = fetch_historical_fn(app, symbol, req_id)
    except Exception as e:
        print(f"  [Portfolio] Error fetch_chart_data {symbol}/{period}: {e}")
        return None

    if df is None or len(df) == 0:
        return None

    candles = []
    for _, row in df.iterrows():
        d = str(row["date"]).replace(" ", "").replace("-", "")
        if len(d) >= 8:
            ts = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        else:
            ts = str(row["date"])
        try:
            candles.append({
                "time": ts,
                "open": round(float(row["open"]), 2),
                "high": round(float(row["high"]), 2),
                "low": round(float(row["low"]), 2),
                "close": round(float(row["close"]), 2),
            })
        except (ValueError, TypeError):
            continue

    return {
        "symbol": symbol,
        "period": period,
        "candles": candles,
    }


# ══════════════════════════════════════════════════════════════
#  PORTFOLIO HISTORY (snapshots diarios)
# ══════════════════════════════════════════════════════════════

def _load_history():
    """Carga historial de snapshots desde archivo JSON."""
    if not os.path.exists(PORTFOLIO_HISTORY_FILE):
        return {"snapshots": []}
    try:
        with open(PORTFOLIO_HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "snapshots" not in data:
            return {"snapshots": []}
        return data
    except (json.JSONDecodeError, IOError) as e:
        print(f"  [Portfolio] Error leyendo historial: {e}")
        return {"snapshots": []}


def _save_history(history):
    """Guarda historial de snapshots a archivo JSON."""
    try:
        tmp_file = PORTFOLIO_HISTORY_FILE + ".tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
        os.replace(tmp_file, PORTFOLIO_HISTORY_FILE)
    except IOError as e:
        print(f"  [Portfolio] Error guardando historial: {e}")


def save_daily_snapshot(positions_enriched, total_value, composition):
    """
    Guarda un snapshot diario del portafolio.
    Si ya existe un snapshot para hoy, lo reemplaza.

    Args:
        positions_enriched: lista de posiciones con precios actuales
        total_value: valor total del portafolio
        composition: dict con pct de stocks, etfs, etc.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    history = _load_history()

    # Preparar datos del snapshot (solo lo esencial, no guardar charts)
    snapshot_positions = []
    for p in positions_enriched:
        snapshot_positions.append({
            "symbol": p.get("symbol", ""),
            "tipo": p.get("tipo", "STK"),
            "cantidad": p.get("cantidad", 0),
            "costo_promedio": p.get("costo_promedio", 0),
            "precio_actual": p.get("precio_actual"),
            "valor_mercado": p.get("valor_mercado"),
            "pnl": p.get("pnl"),
            "pnl_pct": p.get("pnl_pct"),
            "es_etf": p.get("es_etf", False),
            "sector": p.get("sector", ""),
        })

    snapshot = {
        "date": today,
        "total_value": round(total_value, 2) if total_value else 0,
        "positions": snapshot_positions,
        "composition": composition,
        "num_positions": len(snapshot_positions),
    }

    # Reemplazar si ya existe hoy, sino agregar
    existing_idx = None
    for i, s in enumerate(history["snapshots"]):
        if s.get("date") == today:
            existing_idx = i
            break

    if existing_idx is not None:
        history["snapshots"][existing_idx] = snapshot
    else:
        history["snapshots"].append(snapshot)

    # Mantener maximo 2 anos de historial (~730 snapshots)
    max_snapshots = 730
    if len(history["snapshots"]) > max_snapshots:
        history["snapshots"] = history["snapshots"][-max_snapshots:]

    # Ordenar por fecha
    history["snapshots"].sort(key=lambda s: s.get("date", ""))

    _save_history(history)
    print(f"  [Portfolio] Snapshot guardado para {today} (total: ${total_value:,.2f})")


# ══════════════════════════════════════════════════════════════
#  CLASIFICACION Y COMPOSICION
# ══════════════════════════════════════════════════════════════

def _classify_position(symbol, sec_type="STK"):
    """
    Clasifica una posicion como stock o ETF y asigna sector.

    Returns:
        (es_etf: bool, sector: str)
    """
    symbol_upper = symbol.upper()
    if symbol_upper in ETF_TICKERS:
        sector = ETF_SECTOR_MAP.get(symbol_upper, "ETF Otro")
        return True, sector
    return False, "Accion"


def _compute_composition(positions_enriched):
    """
    Calcula la composicion del portafolio.

    Returns:
        {
            "stocks_pct": 0.7,
            "etfs_pct": 0.3,
            "cash_pct": 0.0,  (solo si viene de account data)
            "by_type": {"stocks": {value, pct, count}, "etfs": {...}},
            "by_sector": {"Tecnologia": {value, pct, symbols}, ...},
            "by_symbol": {"AAPL": {value, pct}, ...},
            "concentration": {top1_pct, top3_pct, top5_pct, hhi},
        }
    """
    total_value = 0.0
    stock_value = 0.0
    etf_value = 0.0
    stock_count = 0
    etf_count = 0
    sector_values = defaultdict(lambda: {"value": 0.0, "symbols": []})
    symbol_values = {}

    for p in positions_enriched:
        val = p.get("valor_mercado")
        if val is None or val <= 0:
            continue
        sym = p.get("symbol", "")
        total_value += val
        symbol_values[sym] = val

        if p.get("es_etf"):
            etf_value += val
            etf_count += 1
            sector = p.get("sector", "ETF Otro")
        else:
            stock_value += val
            stock_count += 1
            sector = p.get("sector", "Accion")

        sector_values[sector]["value"] += val
        sector_values[sector]["symbols"].append(sym)

    if total_value <= 0:
        return {
            "stocks_pct": 0, "etfs_pct": 0,
            "by_type": {}, "by_sector": {}, "by_symbol": {},
            "concentration": {},
        }

    # Porcentajes por tipo
    stocks_pct = round(stock_value / total_value, 4)
    etfs_pct = round(etf_value / total_value, 4)

    # Sector breakdown
    by_sector = {}
    for sector, data in sector_values.items():
        by_sector[sector] = {
            "value": round(data["value"], 2),
            "pct": round(data["value"] / total_value, 4),
            "symbols": data["symbols"],
        }
    # Ordenar por valor descendente
    by_sector = dict(sorted(by_sector.items(), key=lambda x: x[1]["value"], reverse=True))

    # Symbol breakdown
    by_symbol = {}
    for sym, val in symbol_values.items():
        by_symbol[sym] = {
            "value": round(val, 2),
            "pct": round(val / total_value, 4),
        }
    by_symbol = dict(sorted(by_symbol.items(), key=lambda x: x[1]["value"], reverse=True))

    # Concentracion
    sorted_pcts = sorted(
        [v["pct"] for v in by_symbol.values()], reverse=True
    )
    top1 = sorted_pcts[0] if len(sorted_pcts) >= 1 else 0
    top3 = sum(sorted_pcts[:3]) if len(sorted_pcts) >= 3 else sum(sorted_pcts)
    top5 = sum(sorted_pcts[:5]) if len(sorted_pcts) >= 5 else sum(sorted_pcts)
    # HHI (Herfindahl-Hirschman Index) - medida de concentracion
    hhi = sum(p ** 2 for p in sorted_pcts) if sorted_pcts else 0

    return {
        "stocks_pct": stocks_pct,
        "etfs_pct": etfs_pct,
        "by_type": {
            "stocks": {"value": round(stock_value, 2), "pct": stocks_pct, "count": stock_count},
            "etfs": {"value": round(etf_value, 2), "pct": etfs_pct, "count": etf_count},
        },
        "by_sector": by_sector,
        "by_symbol": by_symbol,
        "concentration": {
            "top1_pct": round(top1, 4),
            "top3_pct": round(top3, 4),
            "top5_pct": round(top5, 4),
            "hhi": round(hhi, 4),
        },
    }


# ══════════════════════════════════════════════════════════════
#  ANALISIS DE HISTORIAL Y PATRONES
# ══════════════════════════════════════════════════════════════

def _analyze_history_patterns(history):
    """
    Analiza el historial de snapshots para detectar patrones.

    Returns dict con:
        - performance_by_composition: rendimiento segun % stocks vs ETFs
        - correlation: correlacion entre stock% y retornos
        - drawdowns: periodos de drawdown con composicion
        - best_worst_periods: mejores y peores periodos
        - summary: resumen textual de patrones encontrados
    """
    snapshots = history.get("snapshots", [])
    if len(snapshots) < 2:
        return {
            "performance_by_composition": {},
            "correlation": None,
            "drawdowns": [],
            "best_worst_periods": {"best": [], "worst": []},
            "summary": "Historial insuficiente (se necesitan al menos 2 snapshots).",
            "total_snapshots": len(snapshots),
        }

    # Construir series temporales
    dates = []
    values = []
    stock_pcts = []
    etf_pcts = []

    for s in snapshots:
        tv = s.get("total_value", 0)
        if tv <= 0:
            continue
        dates.append(s["date"])
        values.append(tv)
        comp = s.get("composition", {})
        stock_pcts.append(comp.get("stocks_pct", 0))
        etf_pcts.append(comp.get("etfs_pct", 0))

    if len(values) < 2:
        return {
            "performance_by_composition": {},
            "correlation": None,
            "drawdowns": [],
            "best_worst_periods": {"best": [], "worst": []},
            "summary": "Datos de valor insuficientes para analisis.",
            "total_snapshots": len(snapshots),
        }

    values_arr = np.array(values, dtype=float)
    stock_pcts_arr = np.array(stock_pcts, dtype=float)

    # --- Retornos diarios ---
    returns = np.diff(values_arr) / values_arr[:-1]
    returns_pct = returns * 100

    # --- 1. Rendimiento por composicion ---
    # Clasificar periodos en "alto stock" (>70%) vs "bajo stock" (<30%) vs "mixto"
    perf_by_comp = {}
    high_stock_returns = []
    low_stock_returns = []
    mixed_returns = []

    for i, ret in enumerate(returns):
        sp = stock_pcts_arr[i]  # composicion al inicio del periodo
        if sp >= 0.7:
            high_stock_returns.append(ret)
        elif sp <= 0.3:
            low_stock_returns.append(ret)
        else:
            mixed_returns.append(ret)

    if high_stock_returns:
        hr = np.array(high_stock_returns)
        perf_by_comp["alto_stocks_70pct_plus"] = {
            "avg_return_pct": round(float(np.mean(hr) * 100), 3),
            "volatility_pct": round(float(np.std(hr) * 100), 3),
            "sharpe_approx": round(float(np.mean(hr) / np.std(hr)) if np.std(hr) > 0 else 0, 3),
            "n_days": len(high_stock_returns),
            "total_return_pct": round(float(np.sum(hr) * 100), 2),
        }

    if low_stock_returns:
        lr = np.array(low_stock_returns)
        perf_by_comp["bajo_stocks_30pct_menos"] = {
            "avg_return_pct": round(float(np.mean(lr) * 100), 3),
            "volatility_pct": round(float(np.std(lr) * 100), 3),
            "sharpe_approx": round(float(np.mean(lr) / np.std(lr)) if np.std(lr) > 0 else 0, 3),
            "n_days": len(low_stock_returns),
            "total_return_pct": round(float(np.sum(lr) * 100), 2),
        }

    if mixed_returns:
        mr = np.array(mixed_returns)
        perf_by_comp["mixto_30_70pct"] = {
            "avg_return_pct": round(float(np.mean(mr) * 100), 3),
            "volatility_pct": round(float(np.std(mr) * 100), 3),
            "sharpe_approx": round(float(np.mean(mr) / np.std(mr)) if np.std(mr) > 0 else 0, 3),
            "n_days": len(mixed_returns),
            "total_return_pct": round(float(np.sum(mr) * 100), 2),
        }

    # --- 2. Correlacion stock% vs retornos ---
    correlation = None
    if len(returns) >= 5 and len(stock_pcts_arr[:-1]) == len(returns):
        sp_for_corr = stock_pcts_arr[:-1]
        if np.std(sp_for_corr) > 0 and np.std(returns) > 0:
            corr_matrix = np.corrcoef(sp_for_corr, returns)
            if corr_matrix.shape == (2, 2):
                corr_val = float(corr_matrix[0, 1])
                if not (math.isnan(corr_val) or math.isinf(corr_val)):
                    correlation = {
                        "value": round(corr_val, 4),
                        "interpretation": (
                            "Fuerte correlacion positiva - mas stocks = mas retorno"
                            if corr_val > 0.5 else
                            "Correlacion positiva moderada"
                            if corr_val > 0.2 else
                            "Correlacion negativa - mas stocks = menor retorno"
                            if corr_val < -0.2 else
                            "Sin correlacion significativa"
                        ),
                    }

    # --- 3. Drawdowns ---
    drawdowns = []
    peak = values_arr[0]
    dd_start = None
    dd_start_date = None

    for i in range(len(values_arr)):
        if values_arr[i] > peak:
            # Nuevo maximo: si estabamos en drawdown, cerrar
            if dd_start is not None:
                dd_depth = (peak - min(values_arr[dd_start:i + 1])) / peak
                if dd_depth > 0.02:  # solo drawdowns > 2%
                    trough_idx = dd_start + np.argmin(values_arr[dd_start:i + 1])
                    avg_stock_pct = float(np.mean(stock_pcts_arr[dd_start:i + 1]))
                    drawdowns.append({
                        "start_date": dd_start_date,
                        "end_date": dates[i],
                        "trough_date": dates[trough_idx],
                        "peak_value": round(float(peak), 2),
                        "trough_value": round(float(values_arr[trough_idx]), 2),
                        "depth_pct": round(float(dd_depth * 100), 2),
                        "duration_days": i - dd_start,
                        "avg_stock_pct": round(avg_stock_pct, 3),
                        "composition_at_trough": {
                            "stocks_pct": round(float(stock_pcts_arr[trough_idx]), 3),
                            "etfs_pct": round(float(etf_pcts[trough_idx]), 3),
                        },
                    })
                dd_start = None
            peak = values_arr[i]
        elif values_arr[i] < peak and dd_start is None:
            dd_start = i
            dd_start_date = dates[i]

    # Drawdown activo (no cerrado)
    if dd_start is not None and len(values_arr) > dd_start:
        dd_depth = (peak - min(values_arr[dd_start:])) / peak
        if dd_depth > 0.02:
            trough_idx = dd_start + np.argmin(values_arr[dd_start:])
            avg_stock_pct = float(np.mean(stock_pcts_arr[dd_start:]))
            drawdowns.append({
                "start_date": dd_start_date,
                "end_date": None,  # activo
                "trough_date": dates[trough_idx],
                "peak_value": round(float(peak), 2),
                "trough_value": round(float(values_arr[trough_idx]), 2),
                "depth_pct": round(float(dd_depth * 100), 2),
                "duration_days": len(values_arr) - dd_start,
                "avg_stock_pct": round(avg_stock_pct, 3),
                "active": True,
                "composition_at_trough": {
                    "stocks_pct": round(float(stock_pcts_arr[trough_idx]), 3),
                    "etfs_pct": round(float(etf_pcts[trough_idx]), 3),
                },
            })

    # Ordenar drawdowns por profundidad
    drawdowns.sort(key=lambda d: d["depth_pct"], reverse=True)

    # --- 4. Mejores y peores periodos (ventanas de 5 dias) ---
    best_periods = []
    worst_periods = []
    window = min(5, len(returns))

    if window >= 2:
        rolling_returns = []
        for i in range(len(returns) - window + 1):
            period_return = float(np.prod(1 + returns[i:i + window]) - 1)
            avg_stock = float(np.mean(stock_pcts_arr[i:i + window]))
            rolling_returns.append({
                "start_date": dates[i],
                "end_date": dates[i + window],
                "return_pct": round(period_return * 100, 2),
                "avg_stock_pct": round(avg_stock, 3),
                "num_positions": snapshots[i].get("num_positions", 0) if i < len(snapshots) else 0,
            })

        # Top 5 mejores
        sorted_best = sorted(rolling_returns, key=lambda x: x["return_pct"], reverse=True)
        best_periods = sorted_best[:5]

        # Top 5 peores
        sorted_worst = sorted(rolling_returns, key=lambda x: x["return_pct"])
        worst_periods = sorted_worst[:5]

    # --- 5. Resumen textual ---
    summary_parts = []
    total_return = (values_arr[-1] - values_arr[0]) / values_arr[0] * 100
    summary_parts.append(
        f"Retorno total del portafolio: {total_return:+.2f}% "
        f"desde {dates[0]} hasta {dates[-1]} ({len(dates)} snapshots)."
    )

    avg_stock_overall = float(np.mean(stock_pcts_arr))
    summary_parts.append(
        f"Composicion promedio: {avg_stock_overall * 100:.0f}% acciones, "
        f"{(1 - avg_stock_overall) * 100:.0f}% ETFs."
    )

    if correlation:
        summary_parts.append(f"Correlacion stock% vs retornos: {correlation['value']:+.3f} - {correlation['interpretation']}.")

    if drawdowns:
        max_dd = drawdowns[0]
        summary_parts.append(
            f"Maximo drawdown: -{max_dd['depth_pct']:.1f}% "
            f"(del {max_dd['start_date']} al {max_dd.get('end_date', 'presente')}), "
            f"composicion promedio {max_dd['avg_stock_pct'] * 100:.0f}% acciones."
        )

    if best_periods:
        bp = best_periods[0]
        summary_parts.append(
            f"Mejor periodo de {window} dias: +{bp['return_pct']:.2f}% "
            f"({bp['start_date']} a {bp['end_date']}), "
            f"{bp['avg_stock_pct'] * 100:.0f}% acciones."
        )

    if worst_periods:
        wp = worst_periods[0]
        summary_parts.append(
            f"Peor periodo de {window} dias: {wp['return_pct']:.2f}% "
            f"({wp['start_date']} a {wp['end_date']}), "
            f"{wp['avg_stock_pct'] * 100:.0f}% acciones."
        )

    return {
        "performance_by_composition": perf_by_comp,
        "correlation": correlation,
        "drawdowns": drawdowns[:10],  # top 10
        "best_worst_periods": {
            "best": best_periods,
            "worst": worst_periods,
        },
        "total_return_pct": round(float(total_return), 2),
        "total_snapshots": len(snapshots),
        "history_range": {
            "from": dates[0],
            "to": dates[-1],
        },
        "summary": " ".join(summary_parts),
    }


def _get_history_chart_data(history, max_points=365):
    """
    Extrae datos para graficar la evolucion del portafolio.

    Returns:
        {
            "dates": [...],
            "values": [...],
            "stocks_pct": [...],
            "etfs_pct": [...],
        }
    """
    snapshots = history.get("snapshots", [])
    if not snapshots:
        return {"dates": [], "values": [], "stocks_pct": [], "etfs_pct": []}

    # Tomar los ultimos max_points
    recent = snapshots[-max_points:]

    dates = []
    values = []
    stocks_pcts = []
    etfs_pcts = []

    for s in recent:
        tv = s.get("total_value", 0)
        if tv <= 0:
            continue
        dates.append(s["date"])
        values.append(round(tv, 2))
        comp = s.get("composition", {})
        stocks_pcts.append(round(comp.get("stocks_pct", 0), 4))
        etfs_pcts.append(round(comp.get("etfs_pct", 0), 4))

    return {
        "dates": dates,
        "values": values,
        "stocks_pct": stocks_pcts,
        "etfs_pct": etfs_pcts,
    }


# ══════════════════════════════════════════════════════════════
#  PORTFOLIO ANALYSIS ENGINE
# ══════════════════════════════════════════════════════════════

def analyze_portfolio(app, analyze_symbol_fn=None, fetch_historical_fn=None,
                      build_position_analysis_fn=None):
    """
    Motor principal de analisis de portafolio.

    1. Obtiene posiciones actuales con precios live/delayed
    2. Calcula P&L por posicion (no realizado)
    3. Obtiene indicadores para cada simbolo en tenencia
    4. Computa composicion (% stocks vs ETFs, sector breakdown)
    5. Carga historial y detecta patrones

    Args:
        app: instancia VistaIB conectada
        analyze_symbol_fn: funcion analyze_symbol(df) de vista_web
        fetch_historical_fn: funcion fetch_historical(app, symbol, req_id) de vista_web

    Returns:
        dict completo con toda la info del portafolio o None si falla
    """
    global portfolio_cache, portfolio_cache_ts

    # Verificar cache
    now = time.time()
    if portfolio_cache and (now - portfolio_cache_ts) < PORTFOLIO_CACHE_TTL:
        print("  [Portfolio] Usando cache (< 2 min).")
        return portfolio_cache

    print("  [Portfolio] Iniciando analisis completo...")

    # 1. Obtener posiciones
    portfolio_data = fetch_portfolio(app)
    if portfolio_data is None:
        print("  [Portfolio] No se pudieron obtener posiciones.")
        return None

    raw_positions = portfolio_data["positions"]
    account_values = portfolio_data.get("account", {})
    account_summary = portfolio_data.get("account_summary", {})
    # Merge: account_values tiene datos de updateAccountValue, account_summary de reqAccountSummary
    merged_account = {**account_values, **account_summary}

    # Ordenes abiertas: se piden SIEMPRE aca (incluso sin posiciones todavia)
    # para no perder ordenes de ENTRADA que IB ya tiene cargadas pero aun no
    # se llenaron — antes solo se pedian mas abajo, dentro del branch con
    # posiciones activas, asi que una cartera vacia con una orden pendiente
    # nunca las mostraba.
    try:
        open_orders = fetch_open_orders(app)
    except Exception as e:
        print(f"  [Portfolio] Error obteniendo open orders: {e}")
        open_orders = []
    held_symbols = {p["symbol"] for p in raw_positions if p.get("cantidad")}
    pending_entries = extract_pending_entries(open_orders, held_symbols)
    if pending_entries:
        print(f"  [Portfolio] {len(pending_entries)} orden(es) de entrada cargada(s) en IB sin ejecutar aun.")
    pending_orders_enriched = _enrich_pending_orders(pending_entries, build_position_analysis_fn)

    if not raw_positions:
        result = {
            "positions": [],
            "pending_orders": pending_orders_enriched,
            "total_value": 0,
            "total_cost": 0,
            "total_pnl": 0,
            "total_pnl_pct": 0,
            "total_pnl_realizado": 0,
            "composition": {},
            "account": _format_account_data(merged_account),
            "indicators": {},
            "history_analysis": _analyze_history_patterns(_load_history()),
            "history_chart": _get_history_chart_data(_load_history()),
            "timestamp": portfolio_data["timestamp"],
            "warnings": ["No se encontraron posiciones abiertas."],
        }
        with portfolio_lock:
            portfolio_cache = result
            portfolio_cache_ts = now
        return result

    # Filtrar posiciones de acciones USD con cantidad > 0
    active_positions = [
        p for p in raw_positions
        if p["cantidad"] != 0 and p["tipo"] == "STK"
    ]

    if not active_positions:
        print("  [Portfolio] No hay posiciones activas de tipo STK.")
        active_positions = [p for p in raw_positions if p["cantidad"] != 0]

    # 2. Enriquecer posiciones (precios y P&L ya vienen de IB via updatePortfolio)
    symbols = list(set(p["symbol"] for p in active_positions))

    # 2b. SL / TP por simbolo (open_orders ya se pidio arriba, antes del
    # filtro de posiciones activas, justamente para no perder ordenes de
    # entrada sin posicion todavia — ver pending_orders_enriched)
    try:
        sl_tp_map = extract_sl_tp_by_symbol(open_orders)
        print(f"  [Portfolio] Ordenes abiertas: {len(open_orders)} "
              f"({sum(1 for v in sl_tp_map.values() if v.get('stop_loss') or v.get('take_profit'))} con SL/TP)")
    except Exception as e:
        print(f"  [Portfolio] Error extrayendo SL/TP: {e}")
        sl_tp_map = {}

    # 2c. Acumular ejecuciones (para marcadores en charts)
    try:
        fetch_executions(app)
    except Exception as e:
        print(f"  [Portfolio] Error obteniendo ejecuciones: {e}")

    positions_enriched = []
    total_value = 0.0
    total_cost = 0.0
    total_pnl_realizado = 0.0
    warnings = []

    for p in active_positions:
        sym = p["symbol"]
        cantidad = p["cantidad"]
        costo_prom = p["costo_promedio"]
        es_etf, sector = _classify_position(sym, p.get("tipo", "STK"))

        # Datos directos de IB (updatePortfolio)
        precio_actual = p.get("precio_mercado", 0)
        valor_mercado = abs(p.get("valor_mercado", 0))
        pnl = p.get("pnl_no_realizado", 0)
        pnl_realizado = p.get("pnl_realizado", 0)

        # Fallback si IB no mando precio
        if not precio_actual or precio_actual <= 0:
            warnings.append(f"{sym}: sin precio de mercado de IB, usando costo promedio.")
            precio_actual = costo_prom
            valor_mercado = abs(cantidad) * precio_actual

        costo_total = abs(cantidad) * costo_prom
        pnl_pct = (pnl / costo_total * 100) if costo_total > 0 else 0.0

        total_value += valor_mercado
        total_cost += costo_total
        total_pnl_realizado += pnl_realizado

        # Ordenes activas asociadas (SL / TP)
        order_info = sl_tp_map.get(sym, {})
        sl_price = order_info.get("stop_loss")
        tp_price = order_info.get("take_profit")

        enriched = {
            "symbol": sym,
            "tipo": p.get("tipo", "STK"),
            "cuenta": p.get("cuenta", ""),
            "moneda": p.get("moneda", "USD"),
            "cantidad": cantidad,
            "costo_promedio": round(costo_prom, 4),
            "precio_actual": round(precio_actual, 2) if precio_actual else None,
            "costo_total": round(costo_total, 2),
            "valor_mercado": round(valor_mercado, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "pnl_realizado": round(pnl_realizado, 2),
            "es_etf": es_etf,
            "sector": sector,
            "peso_portafolio": 0,  # se calcula despues
            "stop_loss": round(sl_price, 2) if sl_price else None,
            "take_profit": round(tp_price, 2) if tp_price else None,
        }
        positions_enriched.append(enriched)

    # Calcular peso de cada posicion
    for p in positions_enriched:
        if total_value > 0:
            p["peso_portafolio"] = round(p["valor_mercado"] / total_value, 4)

    # Ordenar por valor descendente
    positions_enriched.sort(key=lambda x: x["valor_mercado"], reverse=True)

    # Total P&L
    total_pnl = sum(p["pnl"] for p in positions_enriched)
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0.0

    # 4. Composicion
    composition = _compute_composition(positions_enriched)

    # 5. Indicadores tecnicos para cada posicion (reusa analyze_symbol)
    indicators_data = {}
    if analyze_symbol_fn and fetch_historical_fn:
        print(f"  [Portfolio] Calculando indicadores para {len(symbols)} posiciones...")
        for i, sym in enumerate(symbols):
            try:
                req_id = _PORTFOLIO_HIST_REQ_BASE + i
                df = fetch_historical_fn(app, sym, req_id)
                if df is not None and len(df) >= 50:
                    analysis = analyze_symbol_fn(df)
                    if analysis:
                        # Solo guardar indicadores esenciales, no charts completos
                        indicators_data[sym] = {
                            "signal": analysis.get("signal", "HOLD"),
                            "signal_label": analysis.get("signal_label", analysis.get("signal", "HOLD")),
                            "strength": analysis.get("strength", 0),
                            "conditions_met": analysis.get("conditions_met", 0),
                            "macd_ok": analysis.get("macd_ok", False),
                            "rsi_ok": analysis.get("rsi_ok", False),
                            "konc_ok": analysis.get("konc_ok", False),
                            "macd_detail": analysis.get("macd_detail", ""),
                            "rsi_detail": analysis.get("rsi_detail", ""),
                            "konc_detail": analysis.get("konc_detail", ""),
                            "price": analysis.get("price", 0),
                            "values": analysis.get("values", {}),
                        }
                time.sleep(0.8)  # rate limit IB
            except Exception as e:
                print(f"  [Portfolio] Error analizando indicadores de {sym}: {e}")
    else:
        # Si no se pasan las funciones, intentar importar del cache
        try:
            from vista_web import analysis_cache
            for sym in symbols:
                if sym in analysis_cache and analysis_cache[sym]:
                    cached = analysis_cache[sym]
                    indicators_data[sym] = {
                        "signal": cached.get("signal", "HOLD"),
                        "signal_label": cached.get("signal_label", cached.get("signal", "HOLD")),
                        "strength": cached.get("strength", 0),
                        "conditions_met": cached.get("conditions_met", 0),
                        "macd_ok": cached.get("macd_ok", False),
                        "rsi_ok": cached.get("rsi_ok", False),
                        "konc_ok": cached.get("konc_ok", False),
                        "macd_detail": cached.get("macd_detail", ""),
                        "rsi_detail": cached.get("rsi_detail", ""),
                        "konc_detail": cached.get("konc_detail", ""),
                        "price": cached.get("price", 0),
                        "values": cached.get("values", {}),
                    }
        except ImportError:
            pass

    # Agregar indicadores a cada posicion enriquecida
    for p in positions_enriched:
        sym = p["symbol"]
        if sym in indicators_data:
            p["indicadores"] = indicators_data[sym]

    # 5b. Analisis profundo estilo escaner por posicion (charts, tesis, targets, veredicto)
    if build_position_analysis_fn is not None:
        print(f"  [Portfolio] Construyendo analisis profundo para {len(positions_enriched)} posiciones...")
        for p in positions_enriched:
            try:
                deep = build_position_analysis_fn(p["symbol"], p)
                if deep:
                    p["analysis"] = deep
                    # Refrescar indicadores basicos con lo profundo (mantiene compat)
                    ind = p.get("indicadores") or {}
                    ind.setdefault("signal", deep.get("signal", "HOLD"))
                    ind.setdefault("signal_label", deep.get("signal_label", deep.get("signal", "HOLD")))
                    ind.setdefault("strength", deep.get("strength", 0))
                    ind.setdefault("conditions_met", deep.get("conditions_met", 0))
                    p["indicadores"] = ind
            except Exception as e:
                print(f"  [Portfolio] Error deep analysis {p['symbol']}: {e}")

    # 6. Historial y patrones
    history = _load_history()
    history_analysis = _analyze_history_patterns(history)
    history_chart = _get_history_chart_data(history)

    # 7. Guardar snapshot diario
    try:
        save_daily_snapshot(positions_enriched, total_value, composition)
    except Exception as e:
        print(f"  [Portfolio] Error guardando snapshot: {e}")
        warnings.append(f"Error guardando snapshot diario: {e}")

    # 7b. Metricas agregadas de cartera (beta, yield, P/E, distribucion de senales)
    metrics = _compute_portfolio_metrics(positions_enriched, total_value)

    # 8. Alertas del portafolio
    alerts = _generate_portfolio_alerts(positions_enriched, composition, indicators_data)

    # 9. Telegram alerts (async, no bloquea)
    try:
        check_and_send_alerts(positions_enriched, total_value, total_pnl_pct)
    except Exception as e:
        print(f"  [Portfolio] Error enviando alertas Telegram: {e}")

    # Resultado final
    result = {
        "positions": positions_enriched,
        "pending_orders": pending_orders_enriched,
        "total_value": round(total_value, 2),
        "total_cost": round(total_cost, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl_pct, 2),
        "total_pnl_realizado": round(total_pnl_realizado, 2),
        "num_positions": len(positions_enriched),
        "composition": composition,
        "account": _format_account_data(merged_account),
        "indicators": indicators_data,
        "history_analysis": history_analysis,
        "history_chart": history_chart,
        "alerts": alerts,
        "metrics": metrics,
        "timestamp": portfolio_data["timestamp"],
        "warnings": warnings,
    }

    with portfolio_lock:
        portfolio_cache = result
        portfolio_cache_ts = now

    print(f"  [Portfolio] Analisis completo. {len(positions_enriched)} posiciones, "
          f"valor total: ${total_value:,.2f}, P&L: ${total_pnl:+,.2f} ({total_pnl_pct:+.2f}%)")

    return result


def _format_account_data(account_data):
    """Formatea los datos de cuenta en algo mas usable."""
    formatted = {}
    for tag, info in account_data.items():
        try:
            val = float(info.get("value", 0))
        except (ValueError, TypeError):
            val = info.get("value", "")
        formatted[tag] = {
            "value": val,
            "currency": info.get("currency", "USD"),
        }
    return formatted


def _compute_portfolio_metrics(positions, total_value):
    """
    Metricas agregadas y simples de la cartera para el header informativo.

    Devuelve:
      beta               — beta ponderado por peso (vs S&P 500)
      dividend_yield_pct — yield promedio ponderado (%)
      pe                 — P/E promedio ponderado
      strength           — fuerza tecnica promedio ponderada (0-5.1)
      score              — score tecnico promedio ponderado (0-100)
      risk_reward        — R/R promedio ponderado (solo posiciones con senal)
      signal_counts      — {buy, sell, hold, actionable}
      next_earnings      — {symbol, date, days_until} o None
      coverage           — dict con % del portfolio cubierto por cada metrica

    Todas las metricas ponderan por peso_portafolio y omiten posiciones sin dato.
    """
    if not positions or total_value <= 0:
        return {}

    def _wavg(field_extractor):
        """Weighted average sobre posiciones con dato valido."""
        num = 0.0
        wt = 0.0
        for p in positions:
            w = p.get("peso_portafolio", 0) or 0
            if w <= 0:
                continue
            v = field_extractor(p)
            if v is None:
                continue
            try:
                fv = float(v)
                if fv != fv or fv in (float("inf"), float("-inf")):
                    continue
            except (TypeError, ValueError):
                continue
            num += fv * w
            wt += w
        if wt <= 0:
            return None, 0.0
        return num / wt, wt

    def _fund(p, key):
        deep = p.get("analysis") or {}
        fund = deep.get("fundamentals") or {}
        return fund.get(key)

    beta, cov_beta = _wavg(lambda p: _fund(p, "beta"))
    # yfinance devuelve dividendYield YA en porcentaje (KO -> 2.41 = 2.41%),
    # igual que lo consumen la tesis y el JS: multiplicarlo daba "241%"
    dy, cov_dy = _wavg(lambda p: _fund(p, "dividend_yield"))
    pe, cov_pe = _wavg(lambda p: _fund(p, "trailing_pe"))

    strength, _ = _wavg(lambda p: (p.get("analysis") or {}).get("strength"))
    score, _ = _wavg(lambda p: (p.get("analysis") or {}).get("score"))

    # R/R solo cuenta posiciones con senal tecnica (BUY/SELL), no HOLD
    def _rr(p):
        d = p.get("analysis") or {}
        sig = d.get("signal", "HOLD")
        if sig not in ("BUY", "SELL"):
            return None
        return d.get("risk_reward")
    rr, _ = _wavg(_rr)

    # Distribucion de senales (por conteo, no por peso)
    counts = {"buy": 0, "sell": 0, "hold": 0}
    for p in positions:
        sig = ((p.get("analysis") or {}).get("signal")
               or (p.get("indicadores") or {}).get("signal")
               or "HOLD").upper()
        if sig == "BUY":
            counts["buy"] += 1
        elif sig == "SELL":
            counts["sell"] += 1
        else:
            counts["hold"] += 1
    counts["actionable"] = counts["buy"] + counts["sell"]

    # Proximo earnings (mas cercano en el tiempo, solo futuros)
    next_earn = None
    for p in positions:
        earn = _fund(p, "earnings")
        if not earn:
            continue
        d = earn.get("days_until")
        if d is None or d < 0:
            continue
        if next_earn is None or d < next_earn["days_until"]:
            next_earn = {
                "symbol": p["symbol"],
                "date": earn.get("next_date"),
                "days_until": d,
            }

    return {
        "beta": round(beta, 2) if beta is not None else None,
        "dividend_yield_pct": round(dy, 2) if dy is not None else None,
        "pe": round(pe, 1) if pe is not None else None,
        "strength": round(strength, 2) if strength is not None else None,
        "score": round(score, 1) if score is not None else None,
        "risk_reward": round(rr, 2) if rr is not None else None,
        "signal_counts": counts,
        "next_earnings": next_earn,
        "coverage": {
            "beta_pct": round(cov_beta * 100, 0),
            "dy_pct": round(cov_dy * 100, 0),
            "pe_pct": round(cov_pe * 100, 0),
        },
    }


def _generate_portfolio_alerts(positions, composition, indicators):
    """
    Genera alertas accionables (calls-to-action) para las posiciones abiertas.

    Solo emite alertas relevantes para tomar decision AHORA:
      - VENDER: senal de VENTA activa (3/3) sobre posicion abierta
      - SUMAR: senal de COMPRA activa (3/3) sobre posicion abierta
      - REVISAR: perdida grande sin senal de compra que la respalde
      - STOP-LOSS PROXIMO: precio cerca del stop configurado

    Returns:
        lista de dicts con estructura enriquecida para CTA:
          {level, action, symbol, price, headline, reason, strength}
    """
    alerts = []

    for p in positions:
        sym = p["symbol"]
        deep = p.get("analysis") or {}
        ind = indicators.get(sym, {}) or {}
        signal = deep.get("signal") or ind.get("signal", "HOLD")
        strength = deep.get("strength") or ind.get("strength", 0)
        conds = deep.get("conditions_met") or ind.get("conditions_met", 0)
        price = p.get("precio_actual") or deep.get("price") or 0
        pnl_pct = p.get("pnl_pct", 0) or 0
        cantidad = p.get("cantidad", 0)
        verdict = deep.get("verdict") or ""
        headline = deep.get("headline") or ""
        reason = deep.get("verdict_summary") or deep.get("verdict_reason") or ""
        target = deep.get("target") or 0
        stop = deep.get("stop_loss") or 0

        # 1) VENDER — senal SELL activa sobre posicion larga
        if signal == "SELL" and cantidad > 0:
            alerts.append({
                "level": "danger",
                "action": "SELL",
                "symbol": sym,
                "price": price,
                "headline": f"VENDER {sym}",
                "reason": (reason or f"Senal de VENTA activa (3/3 indicadores, fuerza {strength:.1f})."),
                "strength": round(strength, 1),
                "target": target,
                "pnl_pct": round(pnl_pct, 2),
            })
            continue

        # 2) SUMAR — senal BUY activa sobre posicion abierta
        if signal == "BUY" and cantidad > 0:
            alerts.append({
                "level": "success",
                "action": "ADD",
                "symbol": sym,
                "price": price,
                "headline": f"SUMAR {sym}",
                "reason": (reason or f"Senal de COMPRA activa (3/3 indicadores, fuerza {strength:.1f}). Oportunidad para promediar."),
                "strength": round(strength, 1),
                "target": target,
                "pnl_pct": round(pnl_pct, 2),
            })
            continue

        # 3) REDUCIR — verdict REDUCE (2/3 indicadores girando a venta o perdida grande)
        if verdict == "REDUCE":
            alerts.append({
                "level": "warning",
                "action": "REDUCE",
                "symbol": sym,
                "price": price,
                "headline": f"REVISAR {sym}",
                "reason": reason or f"{conds}/3 indicadores girando bajistas. Momentum negativo.",
                "strength": round(strength, 1),
                "pnl_pct": round(pnl_pct, 2),
            })
            continue

        # 4) STOP-LOSS proximo — precio a menos de ~2% del SL configurado en IB
        sl_ib = p.get("stop_loss")
        if sl_ib and price and cantidad > 0:
            try:
                dist = (price - sl_ib) / price
                if 0 < dist < 0.02:
                    alerts.append({
                        "level": "warning",
                        "action": "WATCH",
                        "symbol": sym,
                        "price": price,
                        "headline": f"{sym} cerca del stop",
                        "reason": f"Precio ${price:.2f} a {dist*100:.1f}% del stop-loss ${sl_ib:.2f}.",
                        "pnl_pct": round(pnl_pct, 2),
                    })
            except Exception:
                pass

    # Ordenar por urgencia: SELL primero, luego ADD, REDUCE, WATCH
    priority = {"SELL": 0, "ADD": 1, "REDUCE": 2, "WATCH": 3}
    alerts.sort(key=lambda a: priority.get(a.get("action", ""), 9))
    return alerts


# ══════════════════════════════════════════════════════════════
#  API ENDPOINT HELPER
# ══════════════════════════════════════════════════════════════

def register_portfolio_endpoint(flask_app, ib_app_ref, analyze_symbol_fn=None,
                                 fetch_historical_fn=None, to_json_fn=None,
                                 build_position_analysis_fn=None):
    """
    Registra el endpoint /api/portfolio en la Flask app.

    Args:
        flask_app: instancia Flask
        ib_app_ref: funcion que retorna la instancia VistaIB actual
                    (lambda: ib_app) para evitar referencia circular
        analyze_symbol_fn: funcion analyze_symbol de vista_web
        fetch_historical_fn: funcion fetch_historical de vista_web
        to_json_fn: funcion to_json de vista_web
        build_position_analysis_fn: callback opcional que recibe (symbol, position)
            y devuelve el analisis profundo estilo escaner (levels, thesis,
            charts MACD/RSI/Koncorde, veredicto). Se adjunta como p['analysis'].
    """
    from flask import Response

    @flask_app.route("/api/portfolio")
    def api_portfolio():
        app = ib_app_ref() if callable(ib_app_ref) else ib_app_ref
        if app is None or not app.isConnected():
            payload = json.dumps({"error": "No hay conexion a IB."})
            return Response(payload, status=503, mimetype="application/json")

        try:
            result = analyze_portfolio(
                app,
                analyze_symbol_fn=analyze_symbol_fn,
                fetch_historical_fn=fetch_historical_fn,
                build_position_analysis_fn=build_position_analysis_fn,
            )
        except Exception as e:
            print(f"  [Portfolio] Error en analyze_portfolio: {e}")
            import traceback
            traceback.print_exc()
            payload = json.dumps({"error": f"Error analizando portafolio: {str(e)}"})
            return Response(payload, status=500, mimetype="application/json")

        if result is None:
            payload = json.dumps({"error": "No se pudieron obtener datos del portafolio."})
            return Response(payload, status=500, mimetype="application/json")

        if to_json_fn:
            return Response(to_json_fn(result), mimetype="application/json")
        else:
            return Response(json.dumps(result, default=str), mimetype="application/json")

    @flask_app.route("/api/portfolio/history")
    def api_portfolio_history():
        """Endpoint para obtener solo el historial de snapshots."""
        history = _load_history()
        chart_data = _get_history_chart_data(history)
        analysis = _analyze_history_patterns(history)

        result = {
            "chart": chart_data,
            "analysis": analysis,
            "total_snapshots": len(history.get("snapshots", [])),
        }

        if to_json_fn:
            return Response(to_json_fn(result), mimetype="application/json")
        else:
            return Response(json.dumps(result, default=str), mimetype="application/json")

    @flask_app.route("/api/portfolio/snapshot")
    def api_portfolio_snapshot():
        """Endpoint para forzar un snapshot sin analisis completo."""
        app = ib_app_ref() if callable(ib_app_ref) else ib_app_ref
        if app is None or not app.isConnected():
            payload = json.dumps({"error": "No hay conexion a IB."})
            return Response(payload, status=503, mimetype="application/json")

        # Forzar refresh de cache
        global portfolio_cache_ts
        portfolio_cache_ts = 0

        result = analyze_portfolio(
            app,
            analyze_symbol_fn=analyze_symbol_fn,
            fetch_historical_fn=fetch_historical_fn,
            build_position_analysis_fn=build_position_analysis_fn,
        )

        if result:
            payload = json.dumps({"ok": True, "timestamp": result.get("timestamp", "")})
        else:
            payload = json.dumps({"ok": False, "error": "No se pudieron obtener datos."})

        return Response(payload, mimetype="application/json")

    # Cache simple para charts por simbolo+periodo (5 min)
    chart_cache = {}
    CHART_CACHE_TTL = 300

    @flask_app.route("/api/portfolio/chart/<symbol>")
    def api_portfolio_chart(symbol):
        """Devuelve OHLC para un simbolo en el periodo pedido."""
        from flask import request
        app = ib_app_ref() if callable(ib_app_ref) else ib_app_ref
        if app is None or not app.isConnected():
            payload = json.dumps({"error": "No hay conexion a IB."})
            return Response(payload, status=503, mimetype="application/json")

        period = (request.args.get("period", "6M") or "6M").upper()
        if period not in CHART_PERIOD_MAP:
            period = "6M"

        cache_key = f"{symbol.upper()}:{period}"
        now_ts = time.time()
        cached = chart_cache.get(cache_key)
        if cached and (now_ts - cached["ts"]) < CHART_CACHE_TTL:
            return Response(
                to_json_fn(cached["data"]) if to_json_fn else json.dumps(cached["data"], default=str),
                mimetype="application/json",
            )

        try:
            data = fetch_chart_data(
                app, symbol.upper(), period=period,
                fetch_historical_fn=fetch_historical_fn,
            )
        except Exception as e:
            print(f"  [Portfolio] Error chart {symbol}/{period}: {e}")
            payload = json.dumps({"error": f"Error obteniendo chart: {str(e)}"})
            return Response(payload, status=500, mimetype="application/json")

        if data is None:
            payload = json.dumps({"error": f"Sin datos para {symbol} / {period}"})
            return Response(payload, status=404, mimetype="application/json")

        # Enriquecer con datos de la posicion (entry, SL, TP) si los tenemos en cache
        try:
            pos_cache = portfolio_cache or {}
            for p in pos_cache.get("positions", []) or []:
                if p.get("symbol", "").upper() == symbol.upper():
                    data["avg_cost"] = p.get("costo_promedio")
                    data["current_price"] = p.get("precio_actual")
                    data["stop_loss"] = p.get("stop_loss")
                    data["take_profit"] = p.get("take_profit")
                    data["quantity"] = p.get("cantidad")
                    data["pnl_pct"] = p.get("pnl_pct")
                    break
        except Exception:
            pass

        # Intentar refrescar ejecuciones (si hay fills nuevos hoy)
        try:
            fetch_executions(app)
        except Exception:
            pass

        # Ejecuciones para este simbolo (marcadores en chart)
        try:
            execs = get_executions_for_symbol(symbol.upper())
            # Limitar a las que caen dentro del rango de candles
            if data.get("candles"):
                first_t = data["candles"][0]["time"]
                last_t = data["candles"][-1]["time"]
                execs = [e for e in execs if first_t <= e["time"] <= last_t]
            data["executions"] = execs
        except Exception as e:
            print(f"  [Portfolio] Error adjuntando execs a chart {symbol}: {e}")
            data["executions"] = []

        chart_cache[cache_key] = {"data": data, "ts": now_ts}

        return Response(
            to_json_fn(data) if to_json_fn else json.dumps(data, default=str),
            mimetype="application/json",
        )

    # Endpoints eliminados en el redesign de la pestana de cartera:
    #   /api/portfolio/benchmark, /news, /returns, /correlation, /rebalancing,
    #   /trades-analysis, /trades-import/*, /trades-deep-analysis, /journal, /var,
    #   /telegram/* — la vista actual solo usa /api/portfolio y /api/portfolio/chart/<sym>.

    print("  [Portfolio] Endpoints registrados: /api/portfolio, "
          "/api/portfolio/history, /api/portfolio/snapshot, /api/portfolio/chart/<symbol>")


# ══════════════════════════════════════════════════════════════
#  1. BENCHMARK vs S&P 500
# ══════════════════════════════════════════════════════════════

_benchmark_cache = {}
_benchmark_cache_ts = 0.0


# ══════════════════════════════════════════════════════════════
#  2. NEWS FEED
# ══════════════════════════════════════════════════════════════

_news_cache = {}
_news_cache_ts = 0.0


# ══════════════════════════════════════════════════════════════
#  3. RETURNS TABLE & HEATMAP
# ══════════════════════════════════════════════════════════════

def _calc_streak(returns):
    if not returns:
        return 0
    streak = 0
    direction = 1 if returns[-1] >= 0 else -1
    for r in reversed(returns):
        if (r >= 0 and direction > 0) or (r < 0 and direction < 0):
            streak += 1
        else:
            break
    return streak * direction


# ══════════════════════════════════════════════════════════════
#  4. CORRELATION MATRIX
# ══════════════════════════════════════════════════════════════

_corr_cache = {}
_corr_cache_ts = 0.0


# ══════════════════════════════════════════════════════════════
#  5. ALLOCATION TARGETS & REBALANCING
# ══════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════
#  6. TRADE JOURNAL
# ══════════════════════════════════════════════════════════════

JOURNAL_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "trade_journal.json"
)


def _load_journal():
    if not os.path.exists(JOURNAL_FILE):
        return {"notes": {}}
    try:
        with open(JOURNAL_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"notes": {}}


def _save_journal(data):
    try:
        tmp = JOURNAL_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, JOURNAL_FILE)
    except IOError as e:
        print(f"  [Portfolio] Error guardando journal: {e}")


def save_journal_note(exec_id, note):
    journal = _load_journal()
    journal["notes"][exec_id] = {
        "note": note,
        "updated": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }
    _save_journal(journal)


# ══════════════════════════════════════════════════════════════
#  7. VALUE AT RISK & STRESS TESTS
# ══════════════════════════════════════════════════════════════

_var_cache = {}
_var_cache_ts = 0.0


# ══════════════════════════════════════════════════════════════
#  8. TELEGRAM ALERTS
# ══════════════════════════════════════════════════════════════

def send_telegram_alert(message):
    import urllib.request
    import urllib.parse

    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return False, "Telegram no configurado. Setea TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID en config.py"

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    params = urllib.parse.urlencode({
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }).encode("utf-8")

    try:
        req = urllib.request.Request(url, data=params, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                return True, "Mensaje enviado."
            return False, f"Error Telegram: {result.get('description', 'Unknown')}"
    except Exception as e:
        return False, f"Error enviando a Telegram: {e}"


def check_and_send_alerts(positions, total_value, total_pnl_pct):
    if not config.TELEGRAM_ENABLED:
        return

    alerts_to_send = []

    for p in positions:
        pnl_pct = p.get("pnl_pct", 0)
        if pnl_pct < -5:
            alerts_to_send.append(
                f"⚠️ <b>{p['symbol']}</b> cae {pnl_pct:.1f}% (${p.get('pnl', 0):+,.0f})"
            )

        ind = p.get("indicadores", {})
        if ind.get("signal") == "SELL" and p.get("cantidad", 0) > 0:
            alerts_to_send.append(
                f"🔴 <b>{p['symbol']}</b> tiene senal de VENTA (fuerza {ind.get('strength', 0):.1f})"
            )

    if alerts_to_send:
        header = f"📊 <b>IB Trading Bot</b> | Cartera ${total_value:,.0f} ({total_pnl_pct:+.1f}%)\n\n"
        message = header + "\n".join(alerts_to_send)
        send_telegram_alert(message)


# ══════════════════════════════════════════════════════════════
#  COMPLETED ORDERS — HISTORIAL COMPLETO DE TRADES
# ══════════════════════════════════════════════════════════════

_completed_orders_cache = {"data": None, "ts": 0}
_COMPLETED_REQ_ID = 9500


def _load_completed_orders():
    if not os.path.exists(COMPLETED_ORDERS_FILE):
        return {"orders": []}
    try:
        with open(COMPLETED_ORDERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict) or "orders" not in data:
                return {"orders": []}
            return data
    except (json.JSONDecodeError, IOError):
        return {"orders": []}


def _save_completed_orders(data):
    try:
        tmp = COMPLETED_ORDERS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, COMPLETED_ORDERS_FILE)
    except IOError as e:
        print(f"  [Portfolio] Error guardando completed orders: {e}")


def _parse_completed_time(time_str):
    """Parse IB completedTime format: '20240115 14:30:25 US/Eastern' or similar."""
    if not time_str:
        return {"date": None, "datetime": None, "weekday": None, "hour": None}
    try:
        parts = time_str.strip().split()
        d = parts[0].replace("-", "")
        if len(d) >= 8:
            date_str = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        else:
            date_str = None

        time_part = parts[1] if len(parts) > 1 else "00:00:00"
        dt_str = f"{date_str}T{time_part}" if date_str else None

        weekday = None
        hour = None
        if date_str:
            try:
                dt_obj = datetime.strptime(date_str, "%Y-%m-%d")
                weekday = dt_obj.strftime("%A")
                hour = int(time_part.split(":")[0]) if time_part else None
            except Exception:
                pass

        return {"date": date_str, "datetime": dt_str, "weekday": weekday, "hour": hour}
    except Exception:
        return {"date": None, "datetime": None, "weekday": None, "hour": None}


def fetch_completed_orders(app, timeout=10):
    """
    Pide ordenes completadas a IB y las persiste.
    IB devuelve todo el historial disponible de la cuenta.
    """
    if not app or not app.isConnected():
        return _load_completed_orders().get("orders", [])

    app.completed_orders = []
    app.completed_orders_done = False

    try:
        app.reqCompletedOrders(False)
    except Exception as e:
        print(f"  [Portfolio] Error reqCompletedOrders: {e}")
        return _load_completed_orders().get("orders", [])

    start = time.time()
    while not getattr(app, "completed_orders_done", False) and time.time() - start < timeout:
        time.sleep(0.2)

    new_orders = list(app.completed_orders) if hasattr(app, "completed_orders") else []

    stored = _load_completed_orders()
    seen = set()
    for o in stored.get("orders", []):
        key = f"{o.get('perm_id', '')}_{o.get('completed_time', '')}"
        seen.add(key)

    added = 0
    for o in new_orders:
        key = f"{o.get('perm_id', '')}_{o.get('completed_time', '')}"
        if key in seen:
            continue
        parsed = _parse_completed_time(o.get("completed_time", ""))
        o["date"] = parsed["date"]
        o["datetime"] = parsed["datetime"]
        o["weekday"] = parsed["weekday"]
        o["hour"] = parsed["hour"]
        stored["orders"].append(o)
        seen.add(key)
        added += 1

    if added > 0:
        stored["orders"].sort(key=lambda x: x.get("datetime") or x.get("date") or "")
        _save_completed_orders(stored)
        print(f"  [Portfolio] {added} ordenes completadas nuevas "
              f"(total historico: {len(stored['orders'])}).")

    return stored["orders"]


def _load_imported_trades():
    if not os.path.exists(TRADES_IMPORT_FILE):
        return {"trades": []}
    try:
        with open(TRADES_IMPORT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict) or "trades" not in data:
                return {"trades": []}
            return data
    except (json.JSONDecodeError, IOError):
        return {"trades": []}


def _save_imported_trades(data):
    try:
        tmp = TRADES_IMPORT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, TRADES_IMPORT_FILE)
    except IOError as e:
        print(f"  [Portfolio] Error guardando trades importados: {e}")


def fetch_flex_report(token, query_id):
    """
    Descarga un Flex Report de IB.
    1. Solicita el reporte via FlexStatementService
    2. Espera a que IB lo genere
    3. Descarga el XML/CSV y parsea las trades
    """
    import urllib.request
    import xml.etree.ElementTree as ET

    base_url = "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService"
    request_url = f"{base_url}.SendRequest?t={token}&q={query_id}&v=3"

    try:
        req = urllib.request.Request(request_url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            xml_text = resp.read().decode("utf-8")

        root = ET.fromstring(xml_text)
        status = root.findtext("Status", "")
        if status != "Success":
            error_msg = root.findtext("ErrorMessage", "Unknown error")
            return {"ok": False, "error": f"IB Flex error: {error_msg}"}

        reference_code = root.findtext("ReferenceCode", "")
        base_url2 = root.findtext("Url", base_url)

        time.sleep(3)

        for attempt in range(10):
            get_url = f"{base_url2}.GetStatement?t={token}&q={reference_code}&v=3"
            req2 = urllib.request.Request(get_url)
            with urllib.request.urlopen(req2, timeout=30) as resp2:
                data_text = resp2.read().decode("utf-8")

            if "<FlexQueryResponse" in data_text or "<FlexStatementResponse" in data_text:
                break
            if "Statement generation in progress" in data_text:
                time.sleep(5)
                continue
            break

        trades = _parse_flex_xml(data_text)
        if trades:
            stored = _load_imported_trades()
            seen = {f"{t['symbol']}_{t['datetime']}_{t['action']}" for t in stored["trades"]}
            added = 0
            for t in trades:
                key = f"{t['symbol']}_{t['datetime']}_{t['action']}"
                if key not in seen:
                    stored["trades"].append(t)
                    seen.add(key)
                    added += 1
            stored["trades"].sort(key=lambda x: x.get("datetime") or "")
            _save_imported_trades(stored)
            _completed_orders_cache["data"] = None
            return {"ok": True, "imported": added, "total": len(stored["trades"])}
        return {"ok": False, "error": "No se encontraron trades en el Flex Report"}

    except Exception as e:
        return {"ok": False, "error": str(e)}


def _parse_flex_xml(xml_text):
    import xml.etree.ElementTree as ET
    trades = []
    try:
        root = ET.fromstring(xml_text)
        for stmt in root.iter("FlexStatement"):
            for trade_el in stmt.iter("Trade"):
                a = trade_el.attrib
                symbol = a.get("symbol", "")
                if not symbol:
                    continue
                action = "BUY" if a.get("buySell", "").upper() in ("BUY", "BOT") else "SELL"
                qty = abs(float(a.get("quantity", 0)))
                price = float(a.get("tradePrice", 0) or a.get("price", 0))
                commission = abs(float(a.get("ibCommission", 0) or a.get("commission", 0)))
                trade_date = a.get("tradeDate", "")
                trade_time = a.get("tradeTime", "")
                if len(trade_date) == 8:
                    trade_date = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}"
                dt_str = f"{trade_date}T{trade_time}" if trade_time else trade_date

                weekday = None
                hour = None
                try:
                    dt_obj = datetime.strptime(trade_date, "%Y-%m-%d")
                    weekday = dt_obj.strftime("%A")
                    if trade_time:
                        hour = int(trade_time.split(":")[0])
                except Exception:
                    pass

                trades.append({
                    "symbol": symbol,
                    "action": action,
                    "filled_qty": qty,
                    "avg_fill_price": price,
                    "lmt_price": price,
                    "commission": commission,
                    "order_type": a.get("orderType", "LMT"),
                    "exchange": a.get("exchange", ""),
                    "parent_id": 0,
                    "order_id": int(a.get("ibOrderID", 0) or 0),
                    "perm_id": a.get("ibExecID", ""),
                    "date": trade_date,
                    "datetime": dt_str,
                    "weekday": weekday,
                    "hour": hour,
                    "sec_type": a.get("assetCategory", "STK"),
                    "currency": a.get("currency", "USD"),
                    "realized_pnl": float(a.get("fifoPnlRealized", 0) or 0),
                })
    except Exception as e:
        print(f"  [Portfolio] Error parseando Flex XML: {e}")
    return trades


def import_trades_csv(csv_content):
    """
    Importa trades desde CSV de IB Activity Statement.
    Soporta formatos: IB Flex CSV, generic CSV con columnas:
    Symbol, Date/Time, Action/Side, Quantity, Price, Commission
    """
    import csv
    import io

    trades = []
    reader = csv.DictReader(io.StringIO(csv_content))
    field_names = [f.strip() for f in (reader.fieldnames or [])]

    col_map = {}
    for f in field_names:
        fl = f.lower().strip()
        if fl in ("symbol", "ticker", "sym"):
            col_map["symbol"] = f
        elif fl in ("date/time", "datetime", "tradetime", "trade date/time", "date", "tradedate"):
            col_map["datetime"] = f
        elif fl in ("buy/sell", "side", "action", "type", "buysell", "buy sell"):
            col_map["action"] = f
        elif fl in ("quantity", "qty", "shares", "filled qty", "amount"):
            col_map["qty"] = f
        elif fl in ("t. price", "price", "tradeprice", "trade price", "exec price", "fill price"):
            col_map["price"] = f
        elif fl in ("comm/fee", "commission", "comm", "ibcommission", "ib commission"):
            col_map["commission"] = f
        elif fl in ("realized p/l", "realized pnl", "fifopnlrealized", "pnl"):
            col_map["realized_pnl"] = f
        elif fl in ("exchange", "listing exchange"):
            col_map["exchange"] = f
        elif fl in ("asset category", "assetcategory", "sec type"):
            col_map["sec_type"] = f

    if "symbol" not in col_map:
        return {"ok": False, "error": f"No se encontro columna 'Symbol' en el CSV. Columnas: {', '.join(field_names)}"}

    for row in reader:
        symbol = row.get(col_map.get("symbol", ""), "").strip()
        if not symbol:
            continue

        raw_action = row.get(col_map.get("action", ""), "").strip().upper()
        if raw_action in ("BUY", "BOT", "B"):
            action = "BUY"
        elif raw_action in ("SELL", "SLD", "S"):
            action = "SELL"
        else:
            continue

        try:
            qty = abs(float(row.get(col_map.get("qty", ""), "0").replace(",", "")))
        except ValueError:
            continue
        if qty == 0:
            continue

        try:
            price = abs(float(row.get(col_map.get("price", ""), "0").replace(",", "")))
        except ValueError:
            price = 0

        try:
            commission = abs(float(row.get(col_map.get("commission", ""), "0").replace(",", "")))
        except ValueError:
            commission = 0

        try:
            realized_pnl = float(row.get(col_map.get("realized_pnl", ""), "0").replace(",", ""))
        except ValueError:
            realized_pnl = 0

        raw_dt = row.get(col_map.get("datetime", ""), "").strip()
        date_str, dt_str, weekday, hour = _parse_csv_datetime(raw_dt)

        exchange = row.get(col_map.get("exchange", ""), "").strip()
        sec_type = row.get(col_map.get("sec_type", ""), "STK").strip() or "STK"

        trades.append({
            "symbol": symbol,
            "action": action,
            "filled_qty": qty,
            "avg_fill_price": price,
            "lmt_price": price,
            "commission": commission,
            "order_type": "LMT",
            "exchange": exchange,
            "parent_id": 0,
            "order_id": 0,
            "perm_id": "",
            "date": date_str,
            "datetime": dt_str,
            "weekday": weekday,
            "hour": hour,
            "sec_type": sec_type,
            "currency": "USD",
            "realized_pnl": realized_pnl,
        })

    if not trades:
        return {"ok": False, "error": "No se encontraron trades validos en el CSV"}

    stored = _load_imported_trades()
    seen = {f"{t['symbol']}_{t['datetime']}_{t['action']}" for t in stored["trades"]}
    added = 0
    for t in trades:
        key = f"{t['symbol']}_{t['datetime']}_{t['action']}"
        if key not in seen:
            stored["trades"].append(t)
            seen.add(key)
            added += 1

    stored["trades"].sort(key=lambda x: x.get("datetime") or "")
    _save_imported_trades(stored)
    _completed_orders_cache["data"] = None
    return {"ok": True, "imported": added, "total": len(stored["trades"]),
            "sample": trades[:3]}


def _parse_csv_datetime(raw):
    """Parse various datetime formats from IB CSVs."""
    if not raw:
        return None, None, None, None
    raw = raw.strip().replace('"', '')
    for fmt in ["%Y-%m-%d, %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y%m%d;%H%M%S",
                "%Y%m%d %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%Y-%m-%d", "%Y%m%d",
                "%m/%d/%Y"]:
        try:
            dt_obj = datetime.strptime(raw, fmt)
            date_str = dt_obj.strftime("%Y-%m-%d")
            dt_str = dt_obj.strftime("%Y-%m-%dT%H:%M:%S")
            weekday = dt_obj.strftime("%A")
            hour = dt_obj.hour
            return date_str, dt_str, weekday, hour
        except ValueError:
            continue
    return None, None, None, None


def _classify_exit(order):
    """Clasifica la salida: stop_loss, take_profit, o manual."""
    otype = (order.get("order_type") or "").upper()
    parent_id = int(order.get("parent_id", 0) or 0)
    if parent_id > 0:
        if otype == "STP":
            return "stop_loss"
        elif otype in ("LMT", "TRAIL"):
            return "take_profit"
    return "manual"


_deep_cache = {"data": None, "ts": 0}


def _parse_option_symbol(sym):
    """Parse IB option symbol like 'AAPL  260417C00305000' into components."""
    parts = sym.strip().split()
    if len(parts) < 2:
        return None
    underlying = parts[0]
    opt_code = parts[1] if len(parts) == 2 else parts[-1]
    if len(opt_code) < 9:
        return None
    try:
        date_str = opt_code[:6]
        put_call = opt_code[6]
        strike_raw = opt_code[7:]
        strike = int(strike_raw) / 1000.0
        exp_date = f"20{date_str[:2]}-{date_str[2:4]}-{date_str[4:6]}"
        return {
            "underlying": underlying,
            "expiry": exp_date,
            "type": "CALL" if put_call == "C" else "PUT",
            "strike": strike,
        }
    except (ValueError, IndexError):
        return None


def _identify_option_strategies(trades):
    """Group option trades into strategies based on same underlying + expiry."""
    from collections import defaultdict
    opt_trades = []
    for t in trades:
        sym = t.get("symbol", "")
        parsed = _parse_option_symbol(sym)
        if parsed:
            opt_trades.append({**t, **parsed})

    groups = defaultdict(list)
    for t in opt_trades:
        key = f"{t['underlying']}_{t['expiry']}"
        groups[key].append(t)

    strategies = []
    for key, legs in groups.items():
        underlying = legs[0]["underlying"]
        expiry = legs[0]["expiry"]
        types = set(l["type"] for l in legs)
        strikes = sorted(set(l["strike"] for l in legs))
        total_pnl = sum(float(l.get("net_pnl", 0) or l.get("gross_pnl", 0) or 0) for l in legs)

        if len(legs) == 1:
            strat_name = f"Naked {legs[0]['type']}"
        elif len(legs) == 2:
            if types == {"CALL"} and len(strikes) == 2:
                strat_name = "Bull Call Spread" if legs[0].get("action") == "BUY" else "Bear Call Spread"
            elif types == {"PUT"} and len(strikes) == 2:
                strat_name = "Bear Put Spread" if legs[0].get("action") == "BUY" else "Bull Put Spread"
            elif types == {"CALL", "PUT"} and len(strikes) == 2:
                strat_name = "Strangle"
            elif types == {"CALL", "PUT"} and len(strikes) == 1:
                strat_name = "Straddle"
            else:
                strat_name = "Combo"
        elif len(legs) == 3:
            if len(strikes) == 3 and len(types) == 1:
                strat_name = "Butterfly"
            else:
                strat_name = "Combo 3-leg"
        elif len(legs) == 4:
            if len(types) == 1 and len(strikes) == 4:
                strat_name = "Iron Condor" if types == {"PUT"} or types == {"CALL"} else "Condor"
            elif types == {"CALL", "PUT"} and len(strikes) >= 2:
                strat_name = "Iron Condor"
            else:
                strat_name = "Combo 4-leg"
        else:
            strat_name = f"Combo {len(legs)}-leg"

        strategies.append({
            "underlying": underlying,
            "expiry": expiry,
            "strategy": strat_name,
            "legs": len(legs),
            "strikes": strikes,
            "types": list(types),
            "total_pnl": round(total_pnl, 2),
            "result": "WIN" if total_pnl > 0 else "LOSS" if total_pnl < 0 else "NEUTRAL",
        })

    return strategies

