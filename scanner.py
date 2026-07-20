"""
Scanner de mercado: obtiene las top N acciones por volumen via IB API.
"""

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.scanner import ScannerSubscription
from ibapi.contract import Contract
from ibapi.tag_value import TagValue
import threading
import time
import json
import os
from datetime import datetime
import config

# Cache del ultimo scan exitoso: fuera de horario el scanner de IB devuelve
# vacio (error 165), y sin esto caeriamos siempre a la lista fallback estatica.
SCAN_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scanner_cache.json")


def _save_scan_cache(key, symbols):
    try:
        cache = {}
        if os.path.exists(SCAN_CACHE_FILE):
            with open(SCAN_CACHE_FILE) as f:
                cache = json.load(f)
        cache[key] = {"saved_at": datetime.now().isoformat(timespec="seconds"), "symbols": symbols}
        with open(SCAN_CACHE_FILE, "w") as f:
            json.dump(cache, f)
    except Exception as e:
        print(f"  No se pudo guardar cache del scanner: {e}")


def _load_scan_cache(key):
    try:
        with open(SCAN_CACHE_FILE) as f:
            entry = json.load(f).get(key)
        if entry and entry.get("symbols"):
            print(f"  Usando ultimo scan real cacheado ({entry['saved_at']}, {len(entry['symbols'])} simbolos).")
            return entry["symbols"]
    except Exception:
        pass
    return None


class ScannerApp(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.symbols = []
        self.scan_done = False
        self.connected = False

    def nextValidId(self, orderId):
        self.connected = True

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        if errorCode in [2104, 2106, 2158, 2119, 2108]:
            return
        if errorCode == 162:  # Scanner query returned no results
            self.scan_done = True
            return
        if errorCode not in [2103, 2105]:
            print(f"  Scanner error {errorCode}: {errorString}")

    def scannerData(self, reqId, rank, contractDetails, distance, benchmark, projection, legsStr):
        contract = contractDetails.contract
        self.symbols.append({
            "rank": rank + 1,
            "symbol": contract.symbol,
            "secType": contract.secType,
            "exchange": contract.primaryExchange or contract.exchange,
            "currency": contract.currency,
            "conId": contract.conId,
        })

    def scannerDataEnd(self, reqId):
        self.scan_done = True
        self.cancelScannerSubscription(reqId)


def get_top_volume_stocks(count=None):
    """
    Obtiene las top N acciones por volumen del dia en NYSE/NASDAQ via IB Scanner.
    Retorna lista de dicts con info de cada accion.
    """
    if count is None:
        count = config.SCAN_COUNT

    app = ScannerApp()

    # connect() bloqueante dentro del thread: con TWS colgada no responde nunca
    # y colgaria todo el arranque — aca esperamos app.connected con timeout.
    def _connect_and_run():
        try:
            app.connect(config.IB_HOST, config.IB_PORT, config.IB_CLIENT_ID + 10)
            app.run()
        except Exception:
            pass

    thread = threading.Thread(target=_connect_and_run, daemon=True)
    thread.start()
    t0 = time.time()
    while not app.connected and time.time() - t0 < 8:
        time.sleep(0.25)

    if not app.connected:
        print("ERROR: Scanner no pudo conectar a TWS")
        app.disconnect()
        return _load_scan_cache("stocks") or []

    sub = ScannerSubscription()
    sub.instrument = "STK"
    sub.locationCode = "STK.US.MAJOR"
    sub.scanCode = "MOST_ACTIVE"
    sub.numberOfRows = count
    sub.abovePrice = 5.0        # Filtrar penny stocks
    sub.marketCapAbove = 1e9    # Min $1B market cap

    app.reqScannerSubscription(1, sub, [], [])

    timeout = 15
    start = time.time()
    while not app.scan_done and time.time() - start < timeout:
        time.sleep(0.5)

    app.disconnect()
    time.sleep(0.5)

    if not app.symbols:
        cached = _load_scan_cache("stocks")
        if cached:
            return cached[:count]
        print(f"  Scanner sin resultados. Usando lista fallback de {config.SCAN_COUNT} acciones.")
        return get_fallback_stocks()[:config.SCAN_COUNT]

    _save_scan_cache("stocks", app.symbols)
    return app.symbols


# Fallback: top 100 acciones mas liquidas de USA (por si el scanner no funciona)
FALLBACK_STOCKS = [
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "BRK B",
    "JPM", "V", "UNH", "XOM", "JNJ", "MA", "PG", "AVGO", "HD", "CVX",
    "MRK", "ABBV", "LLY", "KO", "PEP", "BAC", "COST", "TMO", "MCD",
    "WMT", "CSCO", "CRM", "ACN", "ABT", "ADBE", "AMD", "NFLX", "DHR",
    "ORCL", "TXN", "INTC", "QCOM", "UBER", "MS", "GS", "SCHW", "MELI",
    "DIS", "NKE", "PYPL", "SQ", "COIN",
    # 51-100
    "NOW", "ISRG", "BKNG", "AMGN", "AMAT", "AXP", "IBM", "GE", "CAT",
    "LRCX", "MDLZ", "ADI", "GILD", "REGN", "VRTX", "PANW", "SYK", "SNPS",
    "CDNS", "KLAC", "BSX", "MMC", "CME", "CB", "PGR", "ABNB", "SHOP",
    "ICE", "MCO", "APH", "CRWD", "WDAY", "MAR", "HLT", "FTNT", "MRVL",
    "DASH", "ROP", "MNST", "MSCI", "DXCM", "CPRT", "IDXX", "TTD", "ON",
    "MCHP", "TEAM", "GEN", "FSLR", "ENPH",
]


def get_fallback_stocks():
    """Retorna lista de acciones fallback como dicts."""
    return [
        {"rank": i + 1, "symbol": s, "secType": "STK", "exchange": "SMART", "currency": "USD", "conId": 0}
        for i, s in enumerate(FALLBACK_STOCKS)
    ]


def make_contract(symbol, exchange="SMART", currency="USD"):
    """Crea un Contract object para una accion."""
    contract = Contract()
    contract.symbol = symbol
    contract.secType = "STK"
    contract.exchange = exchange
    contract.currency = currency
    return contract


# ══════════════════════════════════════════════════════════════
#  ETF SCANNER
# ══════════════════════════════════════════════════════════════

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


def get_fallback_etfs():
    """Retorna lista de ETFs fallback como dicts."""
    return [
        {"rank": i + 1, "symbol": s, "secType": "STK", "exchange": "SMART", "currency": "USD", "conId": 0}
        for i, s in enumerate(FALLBACK_ETFS)
    ]


def get_top_volume_etfs(count=None):
    """
    Obtiene los top N ETFs por volumen del dia via IB Scanner.
    Retorna lista de dicts con info de cada ETF.
    """
    if count is None:
        count = config.SCAN_COUNT

    app = ScannerApp()

    # connect() bloqueante dentro del thread: con TWS colgada no responde nunca
    # y colgaria todo el arranque — aca esperamos app.connected con timeout.
    def _connect_and_run():
        try:
            app.connect(config.IB_HOST, config.IB_PORT, config.IB_CLIENT_ID + 11)
            app.run()
        except Exception:
            pass

    thread = threading.Thread(target=_connect_and_run, daemon=True)
    thread.start()
    t0 = time.time()
    while not app.connected and time.time() - t0 < 8:
        time.sleep(0.25)

    if not app.connected:
        print("ERROR: ETF Scanner no pudo conectar a TWS")
        app.disconnect()
        return _load_scan_cache("etfs") or get_fallback_etfs()[:count]

    sub = ScannerSubscription()
    sub.instrument = "STK.ETF"
    sub.locationCode = "STK.US.MAJOR"
    sub.scanCode = "MOST_ACTIVE"
    sub.numberOfRows = count

    app.reqScannerSubscription(2, sub, [], [])

    timeout = 15
    start = time.time()
    while not app.scan_done and time.time() - start < timeout:
        time.sleep(0.5)

    app.disconnect()
    time.sleep(0.5)

    if not app.symbols:
        cached = _load_scan_cache("etfs")
        if cached:
            return cached[:count]
        print(f"  ETF Scanner sin resultados. Usando lista fallback de {count} ETFs.")
        return get_fallback_etfs()[:count]

    _save_scan_cache("etfs", app.symbols)
    return app.symbols


if __name__ == "__main__":
    print(f"Buscando top {config.SCAN_COUNT} acciones por volumen...\n")
    stocks = get_top_volume_stocks()
    if stocks:
        print(f"{'#':<4} {'SIMBOLO':<8} {'EXCHANGE':<12}")
        print("-" * 28)
        for s in stocks:
            print(f"{s['rank']:<4} {s['symbol']:<8} {s['exchange']:<12}")
        print(f"\nTotal: {len(stocks)} acciones")
    else:
        print("No se obtuvieron resultados del scanner.")
