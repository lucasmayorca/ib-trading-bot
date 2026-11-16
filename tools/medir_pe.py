"""Edge del P/E (valuacion) en el momento de entrar.

Es la medicion que decide si el panel de P/E (`valuation.py`) puede pesar en
`_score_stock` o se queda como contexto declarado, igual que se hizo con la
concentracion (`tools/medir_conc.py`) y con las figuras.

Uso:  PYTHONPATH=. venv/bin/python tools/medir_pe.py [tools/universo_10y.pkl]
      (requiere `valuation_cache.json` poblado — lo llena el warmer del server)

DOS MEDIDAS, que contestan preguntas DISTINTAS:

  (1) PROPIO ("own"): percentil del P/E de la accion contra SU PROPIA historia,
      ventana expansiva point-in-time. Es EXACTAMENTE lo que muestra el panel:
      "esta accion esta cara o barata PARA SI MISMA".
  (2) TRANSVERSAL ("xs"): percentil del P/E de la accion contra el de TODAS las
      demas ESE MISMO DIA. Es el factor value clasico: "esta accion esta cara o
      barata contra el resto del universo".

La (1) es la que se pidio medir; la (2) va igual porque tiene MUCHA mas muestra
(no necesita calentamiento) y porque si la (1) no da nada conviene saber si es
por falta de senal o por falta de datos.

LIMITE DE POTENCIA, declarado de entrada: el `income_stmt` de yfinance da 4-5
ejercicios, asi que la serie de P/E arranca ~2023 y no cubre los 10 años del
universo. Para el percentil PROPIO hay que descontar ademas el calentamiento.
Con esa ventana la cantidad de señales 3/3 es chica: un resultado nulo aca NO
prueba que no haya edge, prueba que con estos datos no se puede ver. El reporte
imprime la muestra y la MDE (minimum detectable effect) para que eso quede a la
vista y no se lea un nulo como una refutacion.

INFERENCIA: el P/E es POR SIMBOLO, asi que no cae en "la trampa del n" de la
concentracion (una variable macro identica para todos los simbolos del dia). Lo
que si queda es que los trades del mismo dia comparten el movimiento del
mercado, asi que ademas del t ingenuo se corre una PERMUTACION DENTRO DE CADA
FECHA: se barajan los percentiles ENTRE SIMBOLOS el mismo dia. Eso preserva la
estructura temporal y la distribucion marginal, y destruye solo el vinculo
simbolo-valuacion — el null correcto para "la valuacion PROPIA de la accion
informa, mas alla de que dia es".
"""
import math
import os
import pickle
import sys
import warnings
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor

warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

import config
import indicators
import valuation as V
from backtester import _ohlc_arrays, _simulate_long, _simulate_short

PKL = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(__file__), "universo_10y.pkl")
CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     V.CACHE_FILE)

SL, TP = config.STOP_LOSS_PCT, config.TAKE_PROFIT_PCT
HOLD = getattr(config, "BACKTEST_MAX_HOLD_DAYS", 20)
COST = getattr(config, "BACKTEST_COST_PCT", 0.10)
WARM = getattr(config, "BACKTEST_WARMUP_BARS", 260)
LAG = max(1, int(getattr(config, "SIGNAL_TURN_LAG", 3)))
STRIDE = max(1, int(getattr(config, "BACKTEST_BASELINE_STRIDE", 5)))
MIN_PIT = 120            # calentamiento del percentil propio (~6 meses)
NPERM = 3000


def _pit_pct(vals):
    """Percentil EXPANSIVO point-in-time: en cada t, contra lo visto hasta t.

    Usar la distribucion completa seria lookahead — el percentil que el panel
    muestra HOY no es el que se podia calcular en 2024.
    """
    out = [None] * len(vals)
    hist = []
    for i, v in enumerate(vals):
        if v is None:
            continue
        if len(hist) >= MIN_PIT:
            out[i] = sum(1 for x in hist if x < v) / len(hist) * 100.0
        hist.append(v)
    return out


def one(item):
    sym, df, pts = item
    try:
        ind = indicators.calculate_all(df)
    except Exception:
        return []
    closes = df["close"].values.astype(float)
    highs, lows, opens = _ohlc_arrays(df)
    fechas = df["date"].astype(str).tolist()

    pe = V.pe_series(fechas, [float(c) for c in closes], pts)
    if pe is None:
        return []
    pit = _pit_pct(pe)

    hist = ind["macd"]["hist"].values.astype(float)
    rsi = ind["rsi"]["rsi"].values.astype(float)
    marron = ind["koncorde"]["marron"].values.astype(float)
    media = ind["koncorde"]["media"].values.astype(float)
    n = len(df)
    rows = []

    def rec(pop, side, i, t):
        if t is None or pe[i] is None:
            return
        rows.append((sym, pop, side, fechas[i], t["return_pct"], pe[i], pit[i]))

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


def add_xs(df):
    """Percentil TRANSVERSAL: contra las demas acciones del MISMO dia."""
    df = df.copy()
    df["xs"] = df.groupby("date")["pe"].rank(pct=True) * 100.0
    # con menos de 10 simbolos ese dia el rank no significa nada
    cuenta = df.groupby("date")["pe"].transform("count")
    df.loc[cuenta < 10, "xs"] = np.nan
    return df


def _hi_lo(pnl, p, lo_t=30, hi_t=70):
    hi, lo = pnl[p >= hi_t], pnl[p <= lo_t]
    if len(hi) < 25 or len(lo) < 25:
        return None, len(hi), len(lo)
    return lo.mean() - hi.mean(), len(hi), len(lo)   # BARATO menos CARO


def _mde(pnl, nh, nl):
    """MDE INGENUA: supone observaciones independientes. Se imprime solo al
    lado de la de permutacion, para que se vea cuanto cuesta la dependencia."""
    s = pnl.std(ddof=1)
    if not nh or not nl or not s:
        return float("nan")
    return 2.80 * s * math.sqrt(1 / nh + 1 / nl)


def _mde_perm(nulos, poder=0.80):
    """MDE sobre la MISMA distribucion nula que decide el p-valor.

    Es la honesta: hereda la estructura de dependencia del test (acá, que los
    trades del mismo dia comparten el movimiento del mercado). Con la ingenua,
    un nulo puede parecer "bien potenciado" cuando en realidad no se podia ver.
    """
    from scipy.stats import norm as _norm
    v = np.asarray([x for x in nulos if x is not None], dtype=float)
    if len(v) < 50:
        return float("nan")
    # CENTRAR el null antes de medir su dispersion. Si la permutacion tiene
    # poca libertad (p.ej. pocas fechas con mas de un simbolo), el estadistico
    # permutado queda PEGADO al observado en vez de centrado en cero, y una MDE
    # que asume centro cero sale absurdamente chica — justo el error que se
    # quiere evitar. El p-valor sigue siendo el de siempre (|null| vs |obs|):
    # esto solo corrige la lectura de POTENCIA.
    c0 = float(v.mean())
    crit = float(np.quantile(np.abs(v - c0), 0.95))
    return (crit + float(_norm.ppf(poder)) * float(v.std(ddof=1)),
            c0, float(v.std(ddof=1)))


def reportar(T, col, nombre):
    print(f"\n########## {nombre} ##########")
    T = T[T[col].notna()]
    rng = np.random.default_rng(13)
    pops = [("sig", "L", "SEÑAL compra 3/3"), ("sig", "S", "SEÑAL venta 3/3"),
            ("base", "L", "BASE largo"), ("base", "S", "BASE corto")]
    for pop, side, nm in pops:
        s = T[(T["pop"] == pop) & (T["side"] == side)]
        if len(s) < 60:
            print(f"\n--- {nm}: n={len(s)} — muestra insuficiente")
            continue
        pnl = s["pnl"].to_numpy(float)
        p = s[col].to_numpy(float)
        print(f"\n--- {nm}: n={len(s)} en {s.date.nunique()} fechas / "
              f"{s.sym.nunique()} simbolos, exp global {pnl.mean():+.3f}%")
        print(f"{'bucket':>16} {'n':>6} {'exp%':>8} {'win%':>6} {'P/E med':>8}")
        for lo, hi in [(0, 20), (20, 40), (40, 60), (60, 80), (80, 101)]:
            m = (p >= lo) & (p < hi)
            x = pnl[m]
            if len(x) < 15:
                print(f"{f'p[{lo},{hi})':>16} {len(x):>6}")
                continue
            print(f"{f'p[{lo},{hi})':>16} {len(x):>6} {x.mean():>8.3f} "
                  f"{(x > 0).mean() * 100:>6.1f} "
                  f"{np.median(s['pe'].to_numpy(float)[m]):>8.1f}")
        d, nh, nl = _hi_lo(pnl, p)
        if d is None:
            print(f"   barato vs caro: muestra insuficiente (caro={nh} barato={nl})")
            continue
        hv, lv = pnl[p >= 70], pnl[p <= 30]
        se = math.sqrt(hv.var(ddof=1) / len(hv) + lv.var(ddof=1) / len(lv))
        # permutacion DENTRO de cada fecha: baraja el percentil ENTRE simbolos
        por_fecha = defaultdict(list)
        for k, f in enumerate(s["date"].tolist()):
            por_fecha[f].append(k)
        idx = list(por_fecha.values())
        cnt, nulos = 0, []
        for _ in range(NPERM):
            pp = p.copy()
            for grupo in idx:
                if len(grupo) > 1:
                    vals = pp[grupo]
                    rng.shuffle(vals)
                    pp[grupo] = vals
            d2, a, b = _hi_lo(pnl, pp)
            nulos.append(d2)
            if d2 is not None and abs(d2) >= abs(d):
                cnt += 1
        multi = sum(1 for g in idx if len(g) > 1)
        mp, c0, sd = _mde_perm(nulos)
        mi = _mde(pnl, nh, nl)
        print(f"   BARATO(p<=30,n={nl}) − CARO(p>=70,n={nh}): {d:+.3f} pp   "
              f"t {d / se:+.2f}   p(perm) {(cnt + 1) / (NPERM + 1):.3f}")
        # EL CENTRO DEL NULL NO ES UN DEFECTO: ES LA DESCOMPOSICION.
        # Barajar DENTRO de cada fecha mantiene fijo, a proposito, de que DIAS
        # salen los trades baratos y caros — en un desplome casi todo el
        # universo queda barato para si mismo, y eso es un efecto de FECHA, no
        # de la valuacion del simbolo. Por eso el null se centra en la parte de
        # fecha y no en cero (verificado: barajando GLOBAL el null si centra en
        # +0.007). Lo que el test aisla es el residuo:
        #     observado = parte de FECHA (centro del null) + parte PROPIA
        # y la MDE (sobre el null centrado) es la potencia para ese residuo.
        print(f"   MDE 80%: {mp:.3f} pp (permutacion)  vs {mi:.3f} pp (ingenua)"
              f"   | fechas permutables: {multi} de {len(idx)}")
        print(f"      descomposicion: fecha {c0:+.3f} pp  +  PROPIA "
              f"{d - c0:+.3f} pp   (null sd {sd:.3f})")

    # interaccion: lo unico que justificaria tocar el score
    print()
    for side, nm in (("L", "COMPRA"), ("S", "VENTA")):
        sg = T[(T["pop"] == "sig") & (T["side"] == side)]
        bs = T[(T["pop"] == "base") & (T["side"] == side)]
        if len(sg) < 60 or len(bs) < 200:
            print(f"   interaccion {nm}: muestra insuficiente")
            continue
        def dd(ps, pb):
            vs, vb = sg["pnl"].to_numpy(float), bs["pnl"].to_numpy(float)
            hs, ls = vs[ps >= 70], vs[ps <= 30]
            hb, lb = vb[pb >= 70], vb[pb <= 30]
            if min(len(hs), len(ls), len(hb), len(lb)) < 25:
                return None
            return (ls.mean() - hs.mean()) - (lb.mean() - hb.mean())
        obs = dd(sg[col].to_numpy(float), bs[col].to_numpy(float))
        if obs is None:
            print(f"   interaccion {nm}: muestra insuficiente")
            continue
        print(f"   INTERACCION {nm} (barato−caro en la SEÑAL menos lo mismo en "
              f"el BASELINE): {obs:+.3f} pp")


if __name__ == "__main__":
    import json
    with open(PKL, "rb") as f:
        data = pickle.load(f)
    cache = json.load(open(CACHE))
    items = [(s, data[s], [(p[0], p[1]) for p in cache[s]["pts"]])
             for s in sorted(cache)
             if s in data and cache[s].get("pts")]
    print(f"{len(items)} acciones con precios + EPS", flush=True)

    rows = []
    with ProcessPoolExecutor(max_workers=8) as ex:
        for k, r in enumerate(ex.map(one, items, chunksize=4)):
            rows.extend(r)
            if (k + 1) % 25 == 0:
                print(f"  {k + 1} -> {len(rows)} entradas", flush=True)
    T = pd.DataFrame(rows, columns=["sym", "pop", "side", "date", "pnl", "pe", "own"])
    T = add_xs(T)
    out = PKL.replace(".pkl", "_pe.csv")
    T.to_csv(out, index=False)
    print(f"\n{len(T)} entradas con P/E -> {out}")
    print(f"  ventana: {T.date.min()} .. {T.date.max()}")
    print(f"  con percentil PROPIO (tras calentamiento): {T['own'].notna().sum()}")
    reportar(T, "own", "PERCENTIL PROPIO (lo que muestra el panel)")
    reportar(T, "xs", "PERCENTIL TRANSVERSAL (barato vs caro del universo)")
