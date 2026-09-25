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


# ── 4. Refresco intradia por EPOCA ───────────────────────────────────
# El analisis corria sobre el cierre de AYER toda la jornada (no captaba ruedas
# volatiles); re-evaluarlo en cada ciclo de 5 min lo hacia parpadear. La epoca
# es el punto medio: barra viva, congelada hasta el proximo salto de hora.

from datetime import datetime  # noqa: E402


def test_epoca_avanza_una_vez_por_hora_durante_la_rueda():
    """Dentro de la misma hora la epoca no cambia (no se recalcula nada);
    al cruzar la hora en punto, si."""
    e1 = vw.signal_epoch(datetime(2026, 9, 23, 10, 5), minutes=60)
    e2 = vw.signal_epoch(datetime(2026, 9, 23, 10, 55), minutes=60)
    e3 = vw.signal_epoch(datetime(2026, 9, 23, 11, 0), minutes=60)
    assert e1 == e2 and e1 != e3


def test_fuera_del_horario_una_sola_epoca():
    """Post-cierre, pre-market del dia siguiente y fin de semana comparten la
    epoca de la ultima rueda cerrada: sin datos nuevos no se re-analiza."""
    post = vw.signal_epoch(datetime(2026, 9, 25, 16, 1), minutes=60)   # viernes
    noche = vw.signal_epoch(datetime(2026, 9, 25, 22, 0), minutes=60)
    sabado = vw.signal_epoch(datetime(2026, 9, 26, 12, 0), minutes=60)
    domingo = vw.signal_epoch(datetime(2026, 9, 27, 12, 0), minutes=60)
    lunes_pre = vw.signal_epoch(datetime(2026, 9, 28, 8, 0), minutes=60)
    assert post == noche == sabado == domingo == lunes_pre == "2026-09-25#cierre"


def test_el_cierre_dispara_un_ultimo_analisis():
    """La epoca post-cierre difiere de la ultima intradia: hay exactamente UNA
    pasada mas despues de las 16:00 para tomar el cierre definitivo."""
    ultima_intra = vw.signal_epoch(datetime(2026, 9, 23, 15, 59), minutes=60)
    post = vw.signal_epoch(datetime(2026, 9, 23, 16, 1), minutes=60)
    assert ultima_intra != post


def test_sin_refresco_intradia_no_hay_gate():
    """Con el refresco en 0 la epoca es vacia y NADA se considera fresco: el
    loop vuelve al comportamiento viejo (recalcula en cada pasada)."""
    assert vw.signal_epoch(datetime(2026, 9, 23, 10, 5), minutes=0) == ""
    assert not vw._analysis_is_fresh({"epoch": ""}, "")


def test_analisis_fallido_se_reintenta_enseguida():
    """Un None (analisis que fallo) nunca es fresco: se reintenta en la pasada
    siguiente en vez de quedar congelado una hora."""
    assert not vw._analysis_is_fresh(None, "2026-09-23#010")
    assert vw._analysis_is_fresh({"epoch": "2026-09-23#010"}, "2026-09-23#010")
    assert not vw._analysis_is_fresh({"epoch": "2026-09-23#009"}, "2026-09-23#010")


def test_barra_viva_entra_al_analisis_y_nan_nunca():
    """Con refresco intradia la vela de hoy SE MANTIENE (sin ella el analisis
    miraba el cierre de ayer). Una ultima barra con cierre NaN se descarta
    siempre: yfinance la devuelve antes de la apertura y envenena todo."""
    import pandas as pd
    hoy = datetime(2026, 9, 23, 11, 0)
    df = pd.DataFrame({"date": ["2026-09-22", "2026-09-23"],
                       "open": [10.0, 11.0], "high": [11.0, 12.0],
                       "low": [9.0, 10.0], "close": [10.5, 11.5],
                       "volume": [1e6, 1e6]})
    assert len(vw._drop_partial_bar(df, now_et=hoy)) == 2      # intradia activo
    df_nan = df.copy()
    df_nan.loc[1, "close"] = float("nan")
    assert len(vw._drop_partial_bar(df_nan, now_et=hoy)) == 1


def test_bridge_en_paridad_con_el_local():
    """El bridge es self-contained y duplica signal_epoch: si las dos copias se
    desincronizan, el cloud re-analiza en momentos distintos que el local."""
    import bridge.main as bm
    assert bm.SIGNALS_INTRADAY_REFRESH_MINUTES == vw._intraday_minutes()
    for t in (datetime(2026, 9, 23, 9, 35), datetime(2026, 9, 23, 10, 55),
              datetime(2026, 9, 23, 16, 1), datetime(2026, 9, 26, 12, 0)):
        assert vw.signal_epoch(t) == bm.signal_epoch(t), t


def test_pie_de_pagina_distingue_rueda_en_curso_de_cierre():
    """El pie decia siempre "cierre del X". Con la barra viva hay que decir que
    la señal corre sobre una vela en formacion, y a que hora se refresco."""
    vivo = {"A": dict(mk_analysis("2026-09-23"), live_bar=True,
                      epoch="2026-09-23#011")}
    cerrado = {"A": dict(mk_analysis("2026-09-22"), live_bar=False)}
    assert vw.signals_label(vivo) == "rueda en curso, 11:00 ET"
    assert vw.signals_label(cerrado) == "cierre del 2026-09-22"


# ── 9. El score: shrinkage, contra-tendencia y figuras contradictorias ──

def _score_of(**over):
    """Analisis sintetico alcista con backtest manipulable, y su score."""
    d = mk_analysis("2026-09-18", price=100.0, label="COMPRA FUERTE",
                    signal="BUY", strength=5.1)
    d["chart"]["mas"]["sma200_val"] = 70.0          # con-tendencia por defecto
    d["backtest"] = {"confidence": 0, "buy_win_rate": 0.0,
                     "buy_expectancy": 0.0, "buy_profit_factor": 1.0,
                     "buy_count": 0, "buy_count_trend": 0,
                     "buy_win_rate_trend": None}
    d["backtest"].update(over.pop("backtest", {}))
    d.update(over)
    return vw._score_stock("TEST", d, min_target_pct=0)


def test_sin_muestra_el_shrinkage_va_al_neutral_no_a_cero():
    """Sin historia: expectancy y win rate valen su NEUTRAL (15 + 5), no 0.

    Multiplicar el componente entero por sample_w castigaba al simbolo sin
    historia 20 puntos mas que a uno con edge medido exactamente nulo. "Sin
    evidencia" es la media, no lo peor.
    """
    sin_muestra = _score_of()                       # n=0 -> sample_w=0
    # 25 (conviccion) + 0 (confianza) + 5 (señal activa) + 15 + 5 de neutral
    assert sin_muestra == 50.0
    # Un edge medido EXACTAMENTE nulo con muestra plena da lo mismo...
    nulo = _score_of(backtest={"buy_count": 12, "buy_count_trend": 12,
                               "buy_win_rate": 0.5, "buy_expectancy": 0.0,
                               "buy_profit_factor": 1.0,
                               "buy_win_rate_trend": 0.5})
    assert nulo == sin_muestra
    # ...y un edge medido NEGATIVO tiene que quedar por DEBAJO del desconocido.
    malo = _score_of(backtest={"buy_count": 12, "buy_count_trend": 12,
                               "buy_win_rate": 0.2, "buy_expectancy": -4.0,
                               "buy_profit_factor": 1.0,
                               "buy_win_rate_trend": 0.2})
    assert malo < sin_muestra


def test_muestra_toda_contra_tendencia_no_cobra_la_pena_entera():
    """`win_rate_trend=None` con muestra NO es "no hay evidencia".

    Caso CRWD: 15 ventas historicas, las 15 contra-tendencia. El codigo viejo
    leia el None como el caso benigno y le daba la pena MAS SUAVE (-10) al peor
    escenario. La expectancy que ya sumo puntos se midio EXACTAMENTE en este
    regimen, asi que la pena (un prior sobre lo no observado) se descuenta.
    """
    bt = {"buy_count": 15, "buy_win_rate": 0.47, "buy_expectancy": 2.17,
          "buy_profit_factor": 2.2}
    def _sc(count_trend, wr_trend):
        d = mk_analysis("2026-09-18", price=100.0, label="COMPRA FUERTE",
                        signal="BUY", strength=5.1)
        d["chart"]["mas"]["sma200_val"] = 130.0     # comprar BAJO la SMA200
        d["backtest"] = dict(bt, confidence=0, buy_count_trend=count_trend,
                             buy_win_rate_trend=wr_trend)
        return vw._score_stock("TEST", d, min_target_pct=0)

    toda_contra = _sc(0, None)                      # 15/15 contra-tendencia
    toda_a_favor = _sc(15, 0.6)                     # el edge NO cubre este caso
    assert toda_contra > toda_a_favor
    # Piso del 40%: que el edge se haya medido aca no borra la pena.
    assert toda_contra - toda_a_favor == 6.0        # pena de 4 vs pena de 10


def test_figura_contra_la_tesis_se_dice_ademas_de_penalizarse():
    """El score ya la penalizaba; la narrativa la imprimia en seco."""
    assert "CONTRA la tesis" in vw._fig_frame("alcista", is_bearish=True)
    assert "CONTRA la tesis" in vw._fig_frame("bajista", is_bearish=False)
    assert "a favor" in vw._fig_frame("bajista", is_bearish=True)
    assert "a favor" in vw._fig_frame("alcista", is_bearish=False)
    assert vw._fig_frame(None, is_bearish=True) == ""
