"""
Script de prueba de conexión a Interactive Brokers TWS API
"""

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
import threading
import time

class IBApi(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.nextorderId = None
        
    def nextValidId(self, orderId: int):
        super().nextValidId(orderId)
        print(f"✅ Conexión establecida!")
        print(f"📝 Próximo Order ID: {orderId}")
        self.nextorderId = orderId
        
    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        if errorCode in [2104, 2106, 2158, 2119]:
            print(f"ℹ️  {errorString}")
        elif errorCode == 502:
            print(f"❌ ERROR: No se puede conectar a TWS")
        else:
            print(f"⚠️  Error {errorCode}: {errorString}")
    
    def currentTime(self, time_val):
        from datetime import datetime
        dt = datetime.fromtimestamp(time_val)
        print(f"🕐 Hora del servidor: {dt}")

def run_loop(app):
    app.run()

def main():
    print("🚀 Conectando a TWS...")
    
    app = IBApi()
    app.connect("127.0.0.1", 7497, 1)
    
    api_thread = threading.Thread(target=run_loop, args=(app,), daemon=True)
    api_thread.start()
    
    time.sleep(3)
    
    if app.isConnected():
        print("✅ CONECTADO!")
        app.reqCurrentTime()
        time.sleep(2)
        print("\nPresiona Ctrl+C para salir")
        
        try:
            while app.isConnected():
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n🛑 Desconectando...")
            app.disconnect()
    else:
        print("❌ NO CONECTADO - Verifica TWS")

if __name__ == "__main__":
    main()
