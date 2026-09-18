"""Mide el edge de las figuras (patterns._EDGE) sobre TODO el universo, en paralelo.

Uso:  PYTHONPATH=. venv/bin/python tools/medir_edge.py <bars|all> <salida.json> [universo.pkl]
  bars = barras finales por simbolo (1250 ~ 5Y) o "all" para la historia completa.

Requiere el pickle que genera tools/bajar_universo.py. Imprime edge, eventos
RESUELTOS, IC95 y significancia por figura: eso es lo que hay que volcar a _EDGE.
Tarda ~6 min sobre 217 simbolos x 10 años con 6 workers.
"""
import json, math, os, pickle, sys, warnings
warnings.filterwarnings("ignore")
from concurrent.futures import ProcessPoolExecutor

import patterns

PKL = sys.argv[3] if len(sys.argv) > 3 else os.path.join(os.path.dirname(__file__), "universo_10y.pkl")
BARS = sys.argv[1] if len(sys.argv) > 1 else "1250"
OUT = sys.argv[2]
NBARS = None if BARS == "all" else int(BARS)


def eventos(item):
    sym, df = item
    try:
        if NBARS:
            df = df.iloc[-NBARS:]
        if len(df) < 400:
            return sym, []
        return sym, patterns.validate_history(df["high"].tolist(),
                                              df["low"].tolist(),
                                              df["close"].tolist())
    except Exception:
        return sym, []


def main():
    with open(PKL, "rb") as f:
        data = pickle.load(f)
    items = sorted(data.items())
    print(f"midiendo {len(items)} simbolos, ventana={BARS}", flush=True)

    todos, hechos = [], 0
    with ProcessPoolExecutor(max_workers=6) as ex:
        for sym, evs in ex.map(eventos, items, chunksize=2):
            todos.extend(evs)
            hechos += 1
            if hechos % 25 == 0:
                print(f"  {hechos}/{len(items)} -> {len(todos)} eventos", flush=True)

    by = {}
    for e in todos:
        t = by.setdefault(e["type"], {"n": 0, "hits": 0, "unres": 0,
                                      "base_sum": 0.0, "base_n": 0, "same_bar": 0})
        t["n"] += 1
        t["hits"] += 1 if e["hit"] else 0
        t["same_bar"] += e.get("same_bar", 0)
        if e["resolved"]:
            t["base_sum"] += e.get("baseline", 0.5)
            t["base_n"] += 1
        else:
            t["unres"] += 1

    out = {}
    for t, v in by.items():
        res = v["base_n"]
        if res < 1:
            continue
        hr = v["hits"] / res * 100
        base = v["base_sum"] / res * 100
        edge = hr - base
        se = math.sqrt(max(hr / 100 * (1 - hr / 100), 1e-9) / res) * 100
        out[t] = {"n": v["n"], "res": res, "hit_rate": round(hr, 1),
                  "baseline": round(base, 1), "edge": round(edge, 1),
                  "se": round(se, 1), "z": round(edge / se, 2) if se else 0.0,
                  "ic_lo": round(edge - 1.96 * se, 1), "ic_hi": round(edge + 1.96 * se, 1),
                  "sig": abs(edge) >= 1.96 * se, "same_bar": v["same_bar"]}

    with open(OUT, "w") as f:
        json.dump(out, f, indent=1)

    print(f"\n{'FIGURA':28s} {'n':>5s} {'res':>5s} {'hit%':>6s} {'base%':>6s} "
          f"{'EDGE':>7s} {'z':>6s} {'IC95':>17s} sig")
    print("-" * 96)
    for t, v in sorted(out.items(), key=lambda kv: -kv[1]["edge"]):
        print(f"{t:28s} {v['n']:>5d} {v['res']:>5d} {v['hit_rate']:>6.1f} {v['baseline']:>6.1f} "
              f"{v['edge']:>+7.1f} {v['z']:>+6.2f}  [{v['ic_lo']:>+6.1f},{v['ic_hi']:>+6.1f}]  "
              f"{'SI' if v['sig'] else 'no'}")
    print("-" * 96)
    print(f"eventos: {sum(v['n'] for v in out.values())} | "
          f"resueltos: {sum(v['res'] for v in out.values())} | "
          f"colisiones: {sum(v['same_bar'] for v in out.values())}")


if __name__ == "__main__":
    main()
