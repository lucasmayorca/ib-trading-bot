from ibapi.client import EClient
from ibapi.wrapper import EWrapper
import threading
import time

class IBApi(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.positions = []
        self.done = False

    def nextValidId(self, orderId):
        super().nextValidId(orderId)
        print("Conectado! Solicitando posiciones...\n")
        self.reqPositions()

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        if errorCode in [2104, 2106, 2158, 2119, 2108]:
            return
        if errorCode == 502:
            print("ERROR: No se puede conectar a TWS. Asegurate de que este abierto.")
        else:
            print(f"Error {errorCode}: {errorString}")

    def position(self, account, contract, position, avgCost):
        self.positions.append({
            "cuenta": account,
            "symbol": contract.symbol,
            "tipo": contract.secType,
            "exchange": contract.exchange,
            "moneda": contract.currency,
            "cantidad": position,
            "costo_promedio": avgCost,
        })

    def positionEnd(self):
        self.done = True
        print("=" * 70)
        print(f"{'SIMBOLO':<10} {'TIPO':<6} {'CANTIDAD':>10} {'COSTO PROM':>12} {'VALOR TOTAL':>14} {'MONEDA':<6}")
        print("=" * 70)
        total_usd = 0.0
        for p in self.positions:
            valor_total = p["cantidad"] * p["costo_promedio"]
            total_usd += valor_total
            print(f"{p['symbol']:<10} {p['tipo']:<6} {p['cantidad']:>10.2f} {p['costo_promedio']:>12.2f} {valor_total:>14.2f} {p['moneda']:<6}")
        print("=" * 70)
        print(f"{'TOTAL':>38} {total_usd:>14.2f} USD")
        print(f"\nPosiciones encontradas: {len(self.positions)}")
        print(f"Cuenta: {self.positions[0]['cuenta'] if self.positions else 'N/A'}")

def run_loop(app):
    app.run()

def main():
    print("TENENCIA - Interactive Brokers")
    print("Conectando a TWS...\n")

    app = IBApi()
    app.connect("127.0.0.1", 7497, 2)

    api_thread = threading.Thread(target=run_loop, args=(app,), daemon=True)
    api_thread.start()

    timeout = 10
    start = time.time()
    while not app.done and time.time() - start < timeout:
        time.sleep(0.5)

    if not app.done:
        if not app.isConnected():
            print("ERROR: No se pudo conectar a TWS.")
            print("Verifica que TWS/IB Gateway este abierto y la API habilitada en puerto 7497.")
        else:
            print("Timeout esperando posiciones. Puede que no haya posiciones abiertas.")

    app.disconnect()

if __name__ == "__main__":
    main()
