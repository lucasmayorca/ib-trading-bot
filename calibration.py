"""
Calibracion del modelo — cierra el lazo entre lo que el sistema predice y lo
que realmente ocurre.

Recorre el historial (5Y) de un universo de simbolos, replica cada senal del
sistema con `backtester.run_calibration_trades`, y agrupa los trades por FUERZA
de senal (y por regimen con/contra tendencia). Para cada bucket reporta el
win-rate y el retorno realizados. Si el modelo esta bien calibrado, a mayor
fuerza deberia observarse mayor win-rate: un "reliability diagram".

Funcion pura: recibe DataFrames OHLCV, no depende de IB.
"""

import backtester

# Buckets de fuerza. Toda senal que dispara cumple 3/3 condiciones (base 3.0);
# los bonus (piso/techo confirmado, RSI extremo, rebote) llevan la fuerza hasta
# ~4.6. Agrupamos ese rango para ver si los bonus predicen mejores resultados.
_STRENGTH_BUCKETS = [
    ("3.0 – 3.3", 3.0, 3.3),
    ("3.3 – 3.6", 3.3, 3.6),
    ("3.6 – 4.0", 3.6, 4.0),
    ("4.0+ (fuerte)", 4.0, 99.0),
]


def _agg(trades):
    """Agrega una lista de trades en {n, win_rate, avg_return}."""
    n = len(trades)
    if n == 0:
        return {"n": 0, "win_rate": None, "avg_return": None}
    wins = sum(1 for t in trades if t["win"])
    avg = sum(float(t["return_pct"]) for t in trades) / n
    return {
        "n": n,
        "win_rate": round(wins / n * 100, 1),
        "avg_return": round(float(avg), 2),
    }


def build_calibration(trades):
    """Construye el reporte de calibracion a partir de la lista combinada de
    trades del universo (cada uno con side/strength/with_trend/return_pct/win)."""
    overall = _agg(trades)

    # Por fuerza de senal
    by_strength = []
    for label, lo, hi in _STRENGTH_BUCKETS:
        bucket = [t for t in trades if lo <= t["strength"] < hi]
        row = _agg(bucket)
        row["label"] = label
        by_strength.append(row)

    # Monotonicidad: el win-rate deberia crecer con la fuerza (calibracion sana)
    wrs = [r["win_rate"] for r in by_strength if r["win_rate"] is not None and r["n"] >= 10]
    monotonic = all(wrs[i] <= wrs[i + 1] for i in range(len(wrs) - 1)) if len(wrs) >= 2 else None

    # Por regimen (con/contra tendencia)
    by_trend = {
        "with_trend": _agg([t for t in trades if t["with_trend"]]),
        "counter_trend": _agg([t for t in trades if not t["with_trend"]]),
    }

    # Por direccion
    by_side = {
        "buy": _agg([t for t in trades if t["side"] == "buy"]),
        "sell": _agg([t for t in trades if t["side"] == "sell"]),
    }

    return {
        "overall": overall,
        "by_strength": by_strength,
        "by_trend": by_trend,
        "by_side": by_side,
        "monotonic": monotonic,
        "symbols_used": None,   # lo rellena el llamador
    }


def collect_universe_trades(ohlc_by_symbol):
    """Dado {symbol: df_OHLCV}, ejecuta el backtest de calibracion en cada uno
    y devuelve (trades_combinados, n_simbolos_con_datos)."""
    all_trades = []
    used = 0
    for sym, df in ohlc_by_symbol.items():
        try:
            trades = backtester.run_calibration_trades(df)
        except Exception:
            trades = []
        if trades:
            used += 1
            for t in trades:
                t["symbol"] = sym
            all_trades.extend(trades)
    return all_trades, used


def calibrate_universe(ohlc_by_symbol):
    """Atajo: colecta trades del universo y construye el reporte de calibracion."""
    trades, used = collect_universe_trades(ohlc_by_symbol)
    report = build_calibration(trades)
    report["symbols_used"] = used
    return report
