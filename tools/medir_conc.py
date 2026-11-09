"""Edge del PERCENTIL DE CONCENTRACION (SPY/RSP) en el momento de entrar.

Es la medicion que decide si la fila CONCENTRACION del Pulso puede pesar en
`_score_stock` o se queda como contexto declarado (ver CLAUDE.md -> Pulso del
Mercado). Resultado: se queda como contexto.

Uso:  PYTHONPATH=. venv/bin/python tools/medir_conc.py [tools/universo_10y.pkl]
      (antes: tools/bajar_universo.py)

Mismo protocolo que tools/medir_ma.py — dos poblaciones (entradas de SEÑAL 3/3
y entradas INCONDICIONALES cada 5 ruedas) con el motor de ejecucion del
backtest (bracket intradia, coste round-trip, cooldown) — con UNA diferencia
metodologica que lo cambia todo:

  La cercania a la SMA200 es PROPIA DE CADA SIMBOLO. El percentil de
  concentracion es UNO SOLO PARA TODO EL MERCADO en cada fecha. Todos los
  trades abiertos el mismo dia comparten ese valor Y comparten el movimiento
  del mercado, asi que no son observaciones independientes: el n efectivo es
  la cantidad de REGIMENES, no de trades.

  Medido: el baseline largo da t=+7.89 (p1a) y t=+9.75 (p3a) sobre 95.568
  trades — evidencia aplastante si uno se la cree. El test de rotacion los deja
  en p=0.30 y p=0.032. Los 95 mil trades viven en ~30 episodios de regimen.

Por eso la inferencia va por ROTACION CIRCULAR de la serie de percentiles
contra el calendario: preserva exactamente la autocorrelacion del regulador y
destruye solo su alineacion con los resultados. Es el null correcto para
"esta variable lenta informa sobre el futuro".

El percentil es POINT-IN-TIME (ventana movil que termina en t, igual que la
tarjeta). Usar la distribucion de la muestra completa seria lookahead: en 2016
nadie conocia el rango de 2026.
"""
import math
import os
import pickle
import sys
import warnings
from concurrent.futures import ProcessPoolExecutor

warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

import config
import indicators
from backtester import _ohlc_arrays, _simulate_long, _simulate_short

PKL = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(__file__), "universo_10y.pkl")

SL, TP = config.STOP_LOSS_PCT, config.TAKE_PROFIT_PCT
HOLD = getattr(config, "BACKTEST_MAX_HOLD_DAYS", 20)
COST = getattr(config, "BACKTEST_COST_PCT", 0.10)
WARM = getattr(config, "BACKTEST_WARMUP_BARS", 260)
LAG = max(1, int(getattr(config, "SIGNAL_TURN_LAG", 3)))
STRIDE = max(1, int(getattr(config, "BACKTEST_BASELINE_STRIDE", 5)))

VENTANAS = [(252, "p1a"), (756, "p3a"), (1260, "p5a")]
NROT = 3000


def serie_percentil():
    """Ratio SPY/RSP con su percentil point-in-time por ventana.

    ALINEA POR FECHA (igual que market_pulse._concentration): son dos descargas
    independientes y un feriado en una sola desplazaria toda la serie.
    """
    import yfinance as yf

    def bajar(sym):
        h = yf.Ticker(sym).history(period="max", interval="1d", auto_adjust=False)
        return pd.DataFrame({
            "date": [d.strftime("%Y-%m-%d") for d in h.index],
            "close": h["Close"].astype(float).values}).dropna()

    m = pd.merge(bajar("SPY"), bajar("RSP"), on="date", how="inner",
                 suffixes=("_a", "_b"))
    m = m[m["close_b"] > 0].reset_index(drop=True)
    r = (m["close_a"] / m["close_b"]).to_numpy(float)
    out = {"date": m["date"].values, "ratio": r}
    for n, col in VENTANAS:
        v = np.full(len(r), np.nan)
        for t in range(n - 1, len(r)):
            seg = r[t - n + 1:t + 1]
            v[t] = (seg < r[t]).mean() * 100
        out[col] = v
    df = pd.DataFrame(out).dropna(subset=["p5a"]).reset_index(drop=True)
    print(f"percentil: {len(df)} fechas {df.date.iloc[0]} .. {df.date.iloc[-1]}",
          flush=True)
    return df


def one(item):
    """Todas las entradas de un simbolo, con la FECHA (para cruzar el regimen)."""
    sym, df = item
    try:
        ind = indicators.calculate_all(df)
    except Exception:
        return []
    closes = df["close"].values.astype(float)
    highs, lows, opens = _ohlc_arrays(df)
    fechas = df["date"].astype(str).values
    hist = ind["macd"]["hist"].values.astype(float)
    rsi = ind["rsi"]["rsi"].values.astype(float)
    marron = ind["koncorde"]["marron"].values.astype(float)
    media = ind["koncorde"]["media"].values.astype(float)
    n = len(df)
    rows = []

    def rec(pop, side, i, t):
        if t is not None:
            rows.append((sym, pop, side, fechas[i], t["return_pct"]))

    for i in range(WARM, n, STRIDE):
        rec("base", "L", i, _simulate_long(closes, i, SL, TP, HOLD, COST,
                                           highs=highs, lows=lows, opens=opens))
        rec("base", "S", i, _simulate_short(closes, i, SL, TP, HOLD, COST,
                                            highs=highs, lows=lows, opens=opens))

    for side in ("L", "S"):
        blocked = -1
        for i in range(max(WARM, LAG), n):
            if i <= blocked:
                continue
            h, h1, r = hist[i], hist[i - LAG], rsi[i]
            m, m1, med = marron[i], marron[i - LAG], media[i]
            if any(math.isnan(x) for x in (h, h1, r, m, m1, med)):
                continue
            if side == "L":
                ok = h < 0 and h > h1 and r < 30 and m < med and m > m1
                sim = _simulate_long
            else:
                ok = h > 0 and h < h1 and r > 70 and m > med and m < m1
                sim = _simulate_short
            if not ok:
                continue
            t = sim(closes, i, SL, TP, HOLD, COST, highs=highs, lows=lows, opens=opens)
            if t is not None:
                rec("sig", side, i, t)
                blocked = t["exit_idx"]
    return rows


def _hi_lo(pnl, p, lo_t=20, hi_t=80):
    hi, lo = pnl[p >= hi_t], pnl[p <= lo_t]
    if len(hi) < 30 or len(lo) < 30:
        return None, len(hi), len(lo)
    return hi.mean() - lo.mean(), len(hi), len(lo)


def reportar(T, C):
    """Buckets, contraste alto-vs-bajo y la INTERACCION con la señal.

    La interaccion (diferencia-en-diferencias señal − baseline) es la unica que
    justificaria tocar el score: si el efecto esta tambien en el baseline, es
    del MERCADO, no de la señal, y un score por simbolo no es donde va.
    """
    idx = {d: i for i, d in enumerate(C["date"].to_numpy())}
    T = T[T["date"].isin(idx)].reset_index(drop=True)
    T["pos"] = T["date"].map(idx)
    print(f"\n{len(T)} trades  {T.date.min()} .. {T.date.max()}  "
          f"({T.date.nunique()} fechas, {T.sym.nunique()} simbolos)")
    rng = np.random.default_rng(11)
    pops = [("sig", "L", "SEÑAL compra 3/3"), ("sig", "S", "SEÑAL venta 3/3"),
            ("base", "L", "BASE largo"), ("base", "S", "BASE corto")]

    for _, col in VENTANAS:
        arr = C[col].to_numpy(float)
        print(f"\n########## PERCENTIL {col.upper()} ##########")
        for pop, side, nm in pops:
            s = T[(T["pop"] == pop) & (T["side"] == side)]
            pnl, pos = s["pnl"].to_numpy(float), s["pos"].to_numpy(int)
            p = arr[pos]
            print(f"\n--- {nm}: n={len(s)} en {s.date.nunique()} fechas, "
                  f"exp global {pnl.mean():+.3f}%")
            for lo, hi in [(0, 20), (20, 40), (40, 60), (60, 80), (80, 101)]:
                x = pnl[(p >= lo) & (p < hi)]
                if len(x) < 20:
                    print(f"{f'   p[{lo},{hi})':>16} {len(x):>7}")
                    continue
                print(f"{f'   p[{lo},{hi})':>16} {len(x):>7} {x.mean():>8.3f}% "
                      f"win {(x > 0).mean() * 100:>5.1f}%")
            d, nh, nl = _hi_lo(pnl, p)
            if d is None:
                print(f"   alto vs bajo: muestra insuficiente (hi={nh} lo={nl})")
                continue
            hv, lv = pnl[p >= 80], pnl[p <= 20]
            se = math.sqrt(hv.var(ddof=1) / len(hv) + lv.var(ddof=1) / len(lv))
            cnt = 0
            for _ in range(NROT):
                k = int(rng.integers(252, len(arr) - 252))
                d2, _a, _b = _hi_lo(pnl, np.roll(arr, k)[pos])
                if d2 is not None and abs(d2) >= abs(d):
                    cnt += 1
            print(f"   alto(n={nh}) vs bajo(n={nl}): {d:+.3f} pp   "
                  f"t INGENUO {d / se:+.2f}   p(rotacion) {(cnt + 1) / (NROT + 1):.3f}")

        print()
        for side, nm in (("L", "COMPRA"), ("S", "VENTA")):
            sg = T[(T["pop"] == "sig") & (T["side"] == side)]
            bs = T[(T["pop"] == "base") & (T["side"] == side)]
            ps_i, pb_i = sg["pos"].to_numpy(int), bs["pos"].to_numpy(int)
            vs, vb = sg["pnl"].to_numpy(float), bs["pnl"].to_numpy(float)

            def dd(a):
                ps, pb = a[ps_i], a[pb_i]
                hs, ls = vs[ps >= 80], vs[ps <= 20]
                hb, lb = vb[pb >= 80], vb[pb <= 20]
                if min(len(hs), len(ls), len(hb), len(lb)) < 30:
                    return None
                return (hs.mean() - hb.mean()) - (ls.mean() - lb.mean())

            obs = dd(arr)
            if obs is None:
                print(f"   interaccion {nm}: muestra insuficiente")
                continue
            cnt = 0
            for _ in range(NROT):
                k = int(rng.integers(252, len(arr) - 252))
                v = dd(np.roll(arr, k))
                if v is not None and abs(v) >= abs(obs):
                    cnt += 1
            print(f"   INTERACCION {nm} (edge sobre baseline, alto − bajo): "
                  f"{obs:+.3f} pp   p(rotacion) {(cnt + 1) / (NROT + 1):.3f}")


if __name__ == "__main__":
    C = serie_percentil()
    with open(PKL, "rb") as f:
        data = pickle.load(f)
    print(f"{len(data)} simbolos", flush=True)
    rows = []
    with ProcessPoolExecutor(max_workers=8) as ex:
        for k, r in enumerate(ex.map(one, list(data.items()), chunksize=4)):
            rows.extend(r)
            if (k + 1) % 50 == 0:
                print(f"  {k + 1} -> {len(rows)} entradas", flush=True)
    T = pd.DataFrame(rows, columns=["sym", "pop", "side", "date", "pnl"])
    out = PKL.replace(".pkl", "_conc.csv")
    pd.merge(T, C, on="date", how="inner").to_csv(out, index=False)
    print(f"\n{len(T)} entradas -> {out}")
    reportar(T, C)
