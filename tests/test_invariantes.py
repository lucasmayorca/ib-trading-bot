"""Los invariantes que ya costaron debugging, ejecutables.

Vivian como prosa en CLAUDE.md ("chequeos que conviene correr"), asi que nadie
los corria: USO estuvo rankeando #1 con una foto del 2026-09-11 durante dias.
Correr con:  ./venv/bin/python -m pytest tests/ -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import vista_web as vw


# ── helpers ──────────────────────────────────────────────────────────

def mk_analysis(as_of, price=100.0, label="VENTA FUERTE", signal="SELL",
                strength=4.3, n=300):
    """Analisis sintetico con la forma que produce analyze_symbol."""
    ohlc = [{"time": f"2026-01-{(i % 28) + 1:02d}", "open": price,
             "high": price * 1.01, "low": price * 0.99, "close": price}
            for i in range(n)]
    ohlc[-1]["time"] = as_of
    return {
        "signal": signal, "signal_label": label, "strength": strength,
        "conditions_met": 3, "price": price, "as_of": as_of,
        "backtest": {"confidence": 61, "sell_win_rate": .44,
                     "sell_expectancy": 2.5, "sell_profit_factor": 1.8,
                     "sell_count": 10, "sell_avg_win": 8.0, "sell_avg_loss": -3.0},
        "chart": {"ohlc": ohlc, "mas": {"sma200_val": 70.0}},
    }


# ── 1. Snapshots congelados fuera del ranking ────────────────────────

def test_foto_vieja_no_rankea():
    """Caso USO: analisis del 11-09 compitiendo contra los del 18-09."""
    cache = {"USO": mk_analysis("2026-09-11"),
             "SPY": mk_analysis("2026-09-18"),
             "QQQ": mk_analysis("2026-09-18")}
    cutoff = vw._stale_as_of_cutoff(cache)
    assert vw._is_stale_analysis(cache["USO"], cutoff)
    assert not vw._is_stale_analysis(cache["SPY"], cutoff)


def test_tolerancia_absorbe_la_pasada_que_cruza_el_cierre():
    """Una pasada que cruza las 16:00 ET deja medio universo con el cierre de
    ayer: un corte estricto vaciaria el Top. La tolerancia NO puede ser 0."""
    cache = {"A": mk_analysis("2026-09-18"), "B": mk_analysis("2026-09-17"),
             "C": mk_analysis("2026-09-16")}   # viernes previo
    cutoff = vw._stale_as_of_cutoff(cache)
    assert not any(vw._is_stale_analysis(cache[s], cutoff) for s in ("B", "C"))


def test_sin_fechas_no_filtra_nada():
    """Un cache sin fechas (formato viejo) no debe quedar vacio."""
    d = mk_analysis("2026-09-11")
    d.pop("as_of")
    d["chart"]["ohlc"] = []
    cache = {"A": d}
    assert vw._stale_as_of_cutoff(cache) == ""
    assert not vw._is_stale_analysis(d, "")


# ── 2. Tabla y ranking sobre el MISMO universo ───────────────────────

def test_universe_items_descarta_huerfanos():
    """Un simbolo en el cache pero fuera del universo no se sirve."""
    cache = {"SPY": mk_analysis("2026-09-18"), "USO": mk_analysis("2026-09-18")}
    vistos = [s for s, d, _ in vw.universe_items(cache, ["SPY"]) if d]
    assert vistos == ["SPY"]


def test_universe_items_reporta_la_fecha_de_la_foto_vieja():
    """No basta con filtrar: hay que poder DECIR de cuando es el dato."""
    cache = {"SPY": mk_analysis("2026-09-18"), "USO": mk_analysis("2026-09-01")}
    stale = {s: st for s, d, st in vw.universe_items(cache, ["SPY", "USO"]) if st}
    assert stale == {"USO": "2026-09-01"}


def test_universe_items_ignora_los_aun_no_analizados():
    """Un simbolo del universo sin entrada en el cache no ocupa fila (si no,
    el header de la tabla aparece sobre el spinner al arrancar)."""
    cache = {"SPY": mk_analysis("2026-09-18")}
    assert [s for s, _, _ in vw.universe_items(cache, ["SPY", "NVDA"])] == ["SPY"]


def test_audit_detecta_huerfano_y_foto_vieja():
    cache = {"SPY": mk_analysis("2026-09-18"), "USO": mk_analysis("2026-09-11")}
    problemas = vw.audit_universe(cache, ["SPY"])
    assert len(problemas) == 2                       # huerfano + fecha vencida
    assert "USO" in problemas[0] and "USO" in problemas[1]
    assert vw.audit_universe({"SPY": mk_analysis("2026-09-18")}, ["SPY"]) == []


def test_audit_no_grita_por_la_pasada_que_cruza_el_cierre():
    """Una alarma que suena todos los dias a las 16:00 ET es una alarma que
    nadie mira: el corte de la auditoria es el mismo que el del ranking."""
    cache = {"A": mk_analysis("2026-09-18"), "B": mk_analysis("2026-09-17")}
    assert vw.audit_universe(cache, ["A", "B"]) == []


# ── 3. Direccion del label (trampa de subcadenas) ────────────────────

def test_sobreventa_no_es_bajista():
    """'SOBREVENTA' contiene 'VENTA' y 'SOBRECOMPRA' contiene 'COMPRA'."""
    assert not vw._label_is_bearish("ZONA DE SOBREVENTA")
    assert vw._label_is_bearish("ZONA DE SOBRECOMPRA")
    assert vw._label_is_bearish("VENTA FUERTE")
    assert not vw._label_is_bearish("COMPRA INMINENTE")


# ── 4. Trades Historicos: la plata reportada cuadra con IB ───────────

def test_suma_de_pnl_igual_a_realized_pnl():
    """Sum(pnl de los trades construidos) == Sum(realized_pnl del archivo)."""
    import json
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "trades_imported.json")
    if not os.path.exists(path):
        import pytest
        pytest.skip("trades_imported.json no disponible")
    with open(path) as f:
        raw = json.load(f)
    fills = raw.get("trades", raw) if isinstance(raw, dict) else raw
    esperado = sum(float(t.get("realized_pnl") or 0) for t in fills)
    data = vw.build_trades_history()
    obtenido = sum(float(t.get("pnl") or 0) for t in data.get("trades", []))
    assert abs(obtenido - esperado) < 0.01, (
        f"P&L construido {obtenido:.2f} != realized_pnl de IB {esperado:.2f}")


# ── 5. Options Lab: unidades por posicion ────────────────────────────

def test_payoff_en_las_mismas_unidades_que_max_profit():
    """max(payoff_points.pnl) == max_profit — ambos POR POSICION (x100).

    Mezclarlas dibujaba la curva por accion bajo etiquetas por posicion
    ("Max +$305" sobre una curva cuyo maximo es 3.05).
    """
    import options_lab as ol
    S, T, r, sigma, dte = 100.0, 30 / 365, 0.05, 0.30, 30
    for nombre, fn in (("long_call", ol.long_call),
                       ("bull_call_spread", ol.bull_call_spread),
                       ("iron_condor", ol.iron_condor)):
        st = fn(S, T, r, sigma, dte)
        d = st.to_dict() if hasattr(st, "to_dict") else st
        pts = [p["pnl"] for p in d["payoff_points"]]
        assert abs(max(pts) - d["max_profit"]) < 1.0, (
            f"{nombre}: payoff max {max(pts):.2f} vs max_profit {d['max_profit']:.2f}")
        assert abs(min(pts) - d["max_loss"]) < 1.0, (
            f"{nombre}: payoff min {min(pts):.2f} vs max_loss {d['max_loss']:.2f}")


def test_covered_call_lleva_la_pata_de_acciones():
    """Sin las 100 acciones, la covered call muestra perdida ilimitada AL ALZA
    y la protective put rinde su maximo si el subyacente colapsa."""
    import options_lab as ol
    S, T, r, sigma, dte = 100.0, 30 / 365, 0.05, 0.30, 30
    for fn in (ol.covered_call, ol.protective_put):
        st = fn(S, T, r, sigma, dte)
        d = st.to_dict() if hasattr(st, "to_dict") else st
        tiene_acciones = any((l.get("type") or l.get("right") or "").upper()
                             in ("STOCK", "STK", "ACCIONES")
                             for l in d.get("legs", []))
        assert tiene_acciones or d.get("covered"), (
            f"{d.get('name')}: falta la pata de 100 acciones")
