"""Potencia e incertidumbre del edge de cada FIGURA tecnica.

`patterns.validate_universe` mide `edge = hit_rate − baseline` y la nota de
`patterns._EDGE` decide la politica con eso: `edge >= +5` ⇒ la figura pesa en
el score, `<= -12` ⇒ no se emite. O sea que **aca la medicion no es un dato de
color: define que figura mueve plata**. Vale entonces preguntarle lo mismo que
a la concentracion y al P/E: ¿cuanta incertidumbre tiene ese numero, y que
tamaño de efecto era detectable?

EL PROBLEMA DE LA INFERENCIA ACTUAL: el "sig" de `patterns.py` sale de
`|edge| >= 1.96 * EE_binomial(hit_rate)`, que supone que **los eventos son
independientes**. No lo son, por dos vias:
  (1) muchos eventos son del MISMO SIMBOLO (una cuña ascendente se re-detecta
      cada pocas ruedas mientras la figura vive);
  (2) muchos caen en la MISMA FECHA en simbolos distintos — una figura que se
      resuelve en el desplome de marzo 2020 en 50 papeles no son 50
      observaciones, es una.
Las dos inflan el n efectivo y angostan el intervalo.

LO QUE HACE ESTA HERRAMIENTA: bootstrap de CLUSTERS — remuestrea SIMBOLOS
enteros (y, aparte, TRIMESTRES calendario enteros) y recalcula el edge. El
desvio de esa distribucion es el error estandar honesto, y de ahi sale:
    MDE(80%) = (z_0.975 + z_0.80) * EE = 2.80 * EE
Se imprime al lado del EE binomial para que se vea el precio de la dependencia.

Uso:  PYTHONPATH=. venv/bin/python tools/medir_figuras.py [tools/universo_10y.pkl]
"""
import math
import os
import pickle
import sys
import warnings
from concurrent.futures import ProcessPoolExecutor

warnings.filterwarnings("ignore")
import numpy as np

import patterns

PKL = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(__file__), "universo_10y.pkl")
NBOOT = 2000
Z = 1.959964 + 0.8416212          # 95% a dos colas + 80% de potencia


def eventos_de(item):
    sym, df = item
    try:
        ev = patterns.validate_history(df["high"].tolist(), df["low"].tolist(),
                                       df["close"].tolist())
    except Exception:
        return []
    fechas = df["date"].astype(str).tolist()
    out = []
    for e in ev:
        if not e.get("resolved"):
            continue              # hit-rate y baseline se promedian solo sobre resueltos
        i = e.get("i", 0)
        out.append((sym, fechas[i][:7] if i < len(fechas) else "?",
                    e["type"], bool(e["hit"]), float(e.get("baseline", 0.5))))
    return out


def _edge(sub):
    if not sub:
        return None
    hits = sum(1 for r in sub if r[3])
    base = sum(r[4] for r in sub) / len(sub)
    return (hits / len(sub) - base) * 100


def _trimestre(mes):
    try:
        a, m = mes.split("-")
        return f"{a}Q{(int(m) - 1) // 3 + 1}"
    except Exception:
        return mes


def boot(sub, clave, rng, nboot=NBOOT):
    """Bootstrap de clusters: remuestrea grupos ENTEROS, no eventos sueltos."""
    grupos = {}
    for r in sub:
        grupos.setdefault(clave(r), []).append(r)
    keys = list(grupos)
    if len(keys) < 8:
        return None
    vals = []
    for _ in range(nboot):
        pick = rng.integers(0, len(keys), len(keys))
        muestra = [x for k in pick for x in grupos[keys[k]]]
        e = _edge(muestra)
        if e is not None:
            vals.append(e)
    return np.array(vals) if len(vals) > 50 else None


if __name__ == "__main__":
    with open(PKL, "rb") as f:
        data = pickle.load(f)
    print(f"{len(data)} simbolos", flush=True)
    filas = []
    with ProcessPoolExecutor(max_workers=8) as ex:
        for k, r in enumerate(ex.map(eventos_de, list(data.items()), chunksize=4)):
            filas.extend(r)
            if (k + 1) % 50 == 0:
                print(f"  {k + 1} -> {len(filas)} eventos resueltos", flush=True)
    print(f"\n{len(filas)} eventos RESUELTOS")

    rng = np.random.default_rng(17)
    por_fig = {}
    for r in filas:
        por_fig.setdefault(r[2], []).append(r)

    print(f"\n{'figura':<28} {'res':>5} {'edge':>7} {'EEbin':>6} {'EEsim':>6} "
          f"{'EEtri':>6} {'IC95 (clusters)':>18} {'MDE':>6}  politica")
    print("-" * 110)
    orden = sorted(por_fig.items(), key=lambda kv: -(_edge(kv[1]) or 0))
    for fig, sub in orden:
        n = len(sub)
        e = _edge(sub)
        if e is None or n < 30:
            continue
        hr = sum(1 for r in sub if r[3]) / n
        ee_bin = math.sqrt(hr * (1 - hr) / n) * 100
        bs = boot(sub, lambda r: r[0], rng)                 # por SIMBOLO
        bt = boot(sub, lambda r: _trimestre(r[1]), rng)     # por TRIMESTRE
        ee_s = bs.std(ddof=1) if bs is not None else float("nan")
        ee_t = bt.std(ddof=1) if bt is not None else float("nan")
        ee = max([x for x in (ee_s, ee_t) if x == x] or [ee_bin])
        lo, hi = e - 1.96 * ee, e + 1.96 * ee
        mde = Z * ee
        pol = ("validada" if e >= patterns._EDGE_MIN_TARGET
               else ("descartada" if e <= patterns._EDGE_MIN_SHOW else "contexto"))
        # ¿el intervalo cruza el umbral que decide la politica?
        umbral = (patterns._EDGE_MIN_TARGET if pol != "descartada"
                  else patterns._EDGE_MIN_SHOW)
        frag = "  ⚠ el IC cruza el umbral" if lo < umbral < hi else ""
        print(f"{fig:<28} {n:>5} {e:>+7.1f} {ee_bin:>6.1f} {ee_s:>6.1f} "
              f"{ee_t:>6.1f} {f'[{lo:+.1f},{hi:+.1f}]':>18} {mde:>6.1f}  {pol}{frag}")

    print("\nEEbin = error estandar binomial (el que usa patterns.py hoy)")
    print("EEsim = bootstrap remuestreando SIMBOLOS enteros")
    print("EEtri = bootstrap remuestreando TRIMESTRES enteros")
    print("El IC y la MDE usan el MAYOR de los dos de clusters — el mas conservador.")
