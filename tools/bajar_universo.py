"""Descarga 10 años de OHLC para TODO el universo (acciones + ETFs).

Uso:  PYTHONPATH=. venv/bin/python tools/bajar_universo.py [salida.pkl]
Default: tools/universo_10y.pkl (ignorado por git, ~30 MB).
Paso 1 de la re-medicion del edge de figuras; despues correr tools/medir_edge.py.
"""
import os, pickle, sys, time, warnings
warnings.filterwarnings("ignore")
import pandas as pd, yfinance as yf
import scanner
from enrichment import _yf_symbol

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "universo_10y.pkl")

syms = sorted(set(scanner.FALLBACK_STOCKS) | set(scanner.FALLBACK_ETFS))
yf_map = {s: _yf_symbol(s) for s in syms}
print(f"universo: {len(syms)} simbolos", flush=True)

data = {}
CH = 40
for k in range(0, len(syms), CH):
    chunk = syms[k:k + CH]
    tickers = sorted({yf_map[s] for s in chunk})
    for intento in range(3):
        try:
            raw = yf.download(tickers, period="10y", interval="1d", group_by="ticker",
                              progress=False, auto_adjust=False, threads=False)
            break
        except Exception as e:
            print(f"  reintento {intento+1}: {e}", flush=True)
            time.sleep(5)
    else:
        continue
    for s in chunk:
        try:
            d = raw[yf_map[s]].dropna()
        except Exception:
            continue
        if len(d) < 400:
            continue
        data[s] = pd.DataFrame({
            "high": d["High"].values.astype(float),
            "low": d["Low"].values.astype(float),
            "close": d["Close"].values.astype(float),
        })
    print(f"  {k+len(chunk)}/{len(syms)} -> acumulados {len(data)}", flush=True)
    time.sleep(1)

with open(OUT, "wb") as f:
    pickle.dump(data, f)

largos = sorted(len(v) for v in data.values())
print(f"\nOK: {len(data)} simbolos con datos")
print(f"barras: min {largos[0]}, mediana {largos[len(largos)//2]}, max {largos[-1]}")
print(f"con >=1250 barras (5Y completos): {sum(1 for l in largos if l>=1250)}")
print(f"con >=2400 barras (10Y completos): {sum(1 for l in largos if l>=2400)}")
faltan = [s for s in syms if s not in data]
print(f"sin datos ({len(faltan)}): {', '.join(faltan)}")
