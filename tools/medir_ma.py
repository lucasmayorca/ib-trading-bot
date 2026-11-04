"""Edge de la CERCANIA a la SMA200/SMA100 en el momento de entrar.

Es la medicion que fija el peso de la "Confluencia con media mayor" en
`vista_web._score_stock` (ver CLAUDE.md -> Backtest & Calibracion).

Uso:  PYTHONPATH=. venv/bin/python tools/medir_ma.py [tools/universo_10y.pkl]
      (antes: tools/bajar_universo.py, que guarda OHLCV completo)

Dos poblaciones, el MISMO motor de ejecucion que el backtest del sistema
(bracket intradia + coste round-trip + cooldown):
  (A) entradas INCONDICIONALES cada 5 ruedas -> mide el efecto de la media SOLA
  (B) entradas de SEÑAL 3/3 (turn_lag de config) -> la poblacion que el score rankea
Si (A) no muestra efecto y (B) si, lo que hay es una interaccion con la señal,
no una propiedad de la media — que es justamente el resultado medido.

Para cada entrada se guarda la distancia CON SIGNO a cada media, en ATR:
  d = (close - ma) / atr      (>0 = precio sobre la media: la media es soporte)
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
from backtester import _ohlc_arrays, _simulate_long, _simulate_short, _sma

PKL = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(__file__), "universo_10y.pkl")

SL, TP = config.STOP_LOSS_PCT, config.TAKE_PROFIT_PCT
HOLD = getattr(config, "BACKTEST_MAX_HOLD_DAYS", 20)
COST = getattr(config, "BACKTEST_COST_PCT", 0.10)
WARM = getattr(config, "BACKTEST_WARMUP_BARS", 260)
LAG = max(1, int(getattr(config, "SIGNAL_TURN_LAG", 3)))
STRIDE = max(1, int(getattr(config, "BACKTEST_BASELINE_STRIDE", 5)))


def _atr_series(highs, lows, closes, period=14):
    n = len(closes)
    tr = np.full(n, np.nan)
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]))
    return pd.Series(tr).rolling(period).mean().values


def one(item):
    """Todas las entradas de un simbolo, con su distancia a cada media."""
    sym, df = item
    try:
        ind = indicators.calculate_all(df)
    except Exception:
        return []
    closes = df["close"].values.astype(float)
    highs, lows, opens = _ohlc_arrays(df)
    hist = ind["macd"]["hist"].values.astype(float)
    rsi = ind["rsi"]["rsi"].values.astype(float)
    marron = ind["koncorde"]["marron"].values.astype(float)
    media = ind["koncorde"]["media"].values.astype(float)
    sma200, sma100 = _sma(closes, 200), _sma(closes, 100)
    atr = _atr_series(highs, lows, closes)
    n = len(df)
    rows = []

    def rec(pop, side, i, trade):
        if trade is None:
            return
        a = atr[i]
        if not a or math.isnan(a) or a <= 0:
            return
        rows.append((sym, pop, side, i, trade["return_pct"],
                     (closes[i] - sma200[i]) / a if not math.isnan(sma200[i]) else np.nan,
                     (closes[i] - sma100[i]) / a if not math.isnan(sma100[i]) else np.nan))

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


def _welch(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    d = a.mean() - b.mean()
    se = math.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))
    return d, d / se, (d - 1.96 * se, d + 1.96 * se)


def reportar(df):
    """Bandas de |distancia| y el contraste cerca-vs-lejos de cada poblacion."""
    import vista_web as vw
    df["a200"], df["a100"] = df["d200"].abs(), df["d100"].abs()
    df["amin"] = df[["a200", "a100"]].min(axis=1)
    bandas = [(0, .5), (.5, 1), (1, 1.5), (1.5, 2), (2, 3), (3, 5), (5, 999)]
    for pop, side, nm in [("sig", "L", "SEÑAL compra 3/3"), ("sig", "S", "SEÑAL venta 3/3"),
                          ("base", "L", "BASE largo"), ("base", "S", "BASE corto")]:
        s = df[(df["pop"] == pop) & (df["side"] == side)]
        print(f"\n=== {nm} — n={len(s)}, exp global {s.pnl.mean():+.3f}% ===")
        print(f"{'|dist| a 200 o 100':>20} {'n':>7} {'exp%':>8} {'win%':>6} {'t':>6}")
        for lo, hi in bandas:
            x = s.pnl[(s.amin >= lo) & (s.amin < hi)].values
            if len(x) < 20:
                print(f"{f'[{lo},{hi})':>20} {len(x):>7}")
                continue
            t = x.mean() / (x.std(ddof=1) / math.sqrt(len(x)))
            print(f"{f'[{lo},{hi})':>20} {len(x):>7} {x.mean():>8.3f} "
                  f"{(x > 0).mean() * 100:>6.1f} {t:>6.2f}")
        cerca = s.pnl[s.amin < vw.MA_NEAR_ZERO_ATR]
        lejos = s.pnl[s.amin >= vw.MA_NEAR_ZERO_ATR]
        if len(cerca) >= 20:
            d, t, ci = _welch(cerca, lejos)
            print(f"  dentro de la rampa (<{vw.MA_NEAR_ZERO_ATR} ATR): n={len(cerca)} "
                  f"({len(cerca) / len(s) * 100:.1f}%) exp {cerca.mean():+.3f} vs "
                  f"{lejos.mean():+.3f} -> {d:+.3f} pp t={t:+.2f} "
                  f"IC95 [{ci[0]:+.2f},{ci[1]:+.2f}]")


if __name__ == "__main__":
    with open(PKL, "rb") as f:
        data = pickle.load(f)
    print(f"{len(data)} simbolos", flush=True)
    rows = []
    with ProcessPoolExecutor(max_workers=8) as ex:
        for k, r in enumerate(ex.map(one, list(data.items()), chunksize=4)):
            rows.extend(r)
            if (k + 1) % 40 == 0:
                print(f"  {k + 1} -> {len(rows)} entradas", flush=True)
    df = pd.DataFrame(rows, columns=["sym", "pop", "side", "i", "pnl", "d200", "d100"])
    out = PKL.replace(".pkl", "_ma.csv")
    df.to_csv(out, index=False)
    print(f"\n{len(df)} entradas -> {out}")
    reportar(df)
