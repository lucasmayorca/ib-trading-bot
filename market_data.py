from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
import threading
import time

class IBApi(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.market_data = {}
        self.symbols = {}
    
    def nextValidId(self, orderId):
        super().nextValidId(orderId)
        print("CONECTADO!\n")
        self.start_market_data()
    
    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        if errorCode in [2104, 2106, 2158, 2119, 2108]:
            return
        print(f"Error {errorCode}: {errorString}")
    
    def tickPrice(self, reqId, tickType, price, attrib):
        if reqId not in self.market_data:
            self.market_data[reqId] = {}
        tick_types = {1: "bid", 2: "ask", 4: "last", 66: "delayed_bid", 67: "delayed_ask", 68: "delayed_last"}
        if tickType in tick_types:
            self.market_data[reqId][tick_types[tickType]] = price
            self.display_data()
    
    def tickSize(self, reqId, tickType, size):
        if reqId not in self.market_data:
            self.market_data[reqId] = {}
        tick_types = {0: "bid_size", 3: "ask_size", 5: "last_size", 8: "volume", 72: "delayed_volume"}
        if tickType in tick_types:
            self.market_data[reqId][tick_types[tickType]] = size
            self.display_data()
    
    def display_data(self):
        print("\033[H\033[J")
        print("=" * 60)
        print("MERCADO - DIFERIDOS")
        print("=" * 60)
        for reqId, symbol_name in self.symbols.items():
            if reqId not in self.market_data:
                continue
            data = self.market_data[reqId]
            print(f"\n{symbol_name}")
            print("-" * 60)
            last = data.get("delayed_last") or data.get("last")
            bid = data.get("delayed_bid") or data.get("bid")
            ask = data.get("delayed_ask") or data.get("ask")
            vol = data.get("delayed_volume") or data.get("volume")
            if last:
                print(f"ULTIMO: ${last:.2f}")
            if bid and ask:
                print(f"BID: ${bid:.2f} | ASK: ${ask:.2f}")
            if vol:
                print(f"VOLUMEN: {vol:,}")
        print("\nCtrl+C para detener")
    
    def start_market_data(self):
        print("Configurando datos diferidos...")
        self.reqMarketDataType(3)
        time.sleep(1)
        symbols = [
            {"symbol": "SPY", "name": "SPY - S&P 500"},
            {"symbol": "AAPL", "name": "AAPL - Apple"},
            {"symbol": "TSLA", "name": "TSLA - Tesla"}
        ]
        print(f"Solicitando {len(symbols)} simbolos...\n")
        for idx, info in enumerate(symbols, start=1):
            contract = Contract()
            contract.symbol = info["symbol"]
            contract.secType = "STK"
            contract.exchange = "SMART"
            contract.currency = "USD"
            self.symbols[idx] = info["name"]
            print(f"  -> {info['name']}")
            self.reqMktData(idx, contract, "", False, False, [])
            time.sleep(0.5)

def run_loop(app):
    app.run()

def main():
    print("MONITOR DE MERCADO")
    print("Conectando...\n")
    app = IBApi()
    app.connect("127.0.0.1", 7497, 1)
    api_thread = threading.Thread(target=run_loop, args=(app,), daemon=True)
    api_thread.start()
    time.sleep(3)
    if not app.isConnected():
        print("ERROR: No conectado")
        return
    try:
        while app.isConnected():
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nDeteniendo...")
        for reqId in app.symbols.keys():
            app.cancelMktData(reqId)
        app.disconnect()
        print("Desconectado")

if __name__ == "__main__":
    main()
