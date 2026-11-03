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


def test_el_desglose_suma_exactamente_el_score():
    """El desglose que ve el usuario tiene que SER la formula, no un relato.

    Se llena dentro de `_score_stock`, junto al calculo: reconstruirlo afuera
    se desincroniza con la formula en el primer cambio de pesos y la tarjeta
    pasa a explicar un numero que ya no es el que muestra.
    """
    d = mk_analysis("2026-09-18", price=100.0, label="VENTA FUERTE",
                    signal="SELL", strength=4.3)
    d["chart"]["mas"]["sma200_val"] = 130.0     # con-tendencia: sin penalizacion
    d["backtest"].update(sell_count_trend=10, sell_win_rate_trend=0.6)
    parts = []
    score = vw._score_stock("TEST", d, min_target_pct=0, parts=parts)
    assert parts and parts[-1]["lab"] == "TOTAL"
    assert parts[-1]["pts"] == round(score, 1)
    suma = sum(p["pts"] for p in parts[:-1])
    assert abs(suma - score) < 0.1, f"{suma} != {score}"
    # Cada componente acotado dice su techo, y ninguno lo pasa.
    for p in parts[:-1]:
        if p["max"] is not None:
            assert -0.01 <= p["pts"] <= p["max"] + 0.01, p


def test_inelegible_no_deja_un_desglose_a_medias():
    """Si el simbolo no califica, el desglose no puede quedar trunco: la UI
    mostraria media formula sumando a un total que no existe."""
    d = mk_analysis("2026-09-18", label="NEUTRAL", signal="HOLD")
    parts = []
    assert vw._score_stock("TEST", d, min_target_pct=0, parts=parts) is None
    assert not any(p["lab"] == "TOTAL" for p in parts)


# ── 10. El titular lo decide el BALANCE, no una condicion suelta ─────
#
# Caso real (BAC, 2026-10): RSI 32, MACD recien cruzado al alza con la linea
# en -29.9, y el Koncorde pinchando desde arriba de su media. Esa UNICA
# condicion de venta se llevaba el titular entero: "VIRANDO A VENTA" con el
# RSI por el piso y el momentum girando al alza.

def _ind(hist, hist_prev, macd_line, rsi, rsi_prev, marron, marron_prev,
         media, azul=0.0):
    """Indicadores sinteticos con la forma que consume generate_signal."""
    import pandas as pd
    import signals
    # Suficientes barras para leer el giro (-1-LAG) y el giro previo
    # (-1-2*LAG) de signals.py con cualquier ventana configurada.
    n = 2 * signals.TURN_LAG + 4
    serie = lambda last, prev: [prev] * (n - 1) + [last]
    return {
        "macd": pd.DataFrame({"macd": [macd_line] * n,
                              "signal": [macd_line - hist] * n,
                              "hist": serie(hist, hist_prev)}),
        "rsi": pd.DataFrame({"rsi": serie(rsi, rsi_prev)}),
        "koncorde": pd.DataFrame({"marron": serie(marron, marron_prev),
                                  "media": [media] * n, "verde": [0.0] * n,
                                  "azul": [azul] * n}),
    }


# El caso denunciado y su espejo exacto, para que el arreglo no sea de un lado.
CASO_BAC = dict(hist=1.70, hist_prev=0.88, macd_line=-29.87, rsi=32.0,
                rsi_prev=31.0, marron=31.04, marron_prev=33.0, media=18.13,
                azul=21.52)
CASO_ESPEJO = dict(hist=-1.70, hist_prev=-0.88, macd_line=29.87, rsi=68.0,
                   rsi_prev=69.0, marron=-31.04, marron_prev=-33.0,
                   media=-18.13)


def test_una_condicion_suelta_no_impone_el_titular():
    """RSI 32 + MACD cruzando al alza NO es 'VIRANDO A VENTA' porque el
    Koncorde pinche: dos indicadores de tres leen al alza."""
    import signals
    sig = signals.generate_signal(_ind(**CASO_BAC))
    assert sig["signal_label"] == "VIRANDO A COMPRA", sig["signal_label"]
    assert not vw._label_is_bearish(sig["signal_label"])


def test_el_balance_es_simetrico():
    """Mismo cuadro dado vuelta: si el de arriba es alcista, este es bajista."""
    import signals
    assert signals.generate_signal(_ind(**CASO_ESPEJO))["signal_label"] == \
        "VIRANDO A VENTA"


def test_el_histograma_no_es_la_zona_del_macd():
    """hist > 0 con la LINEA bien bajo cero es un cruce alcista reciente, no
    sobrecompra. Juzgar la zona por el signo del histograma era lo que daba
    vuelta la lectura justo cuando el cruce se completaba."""
    import signals
    bull, bear = signals._trend_balance(
        rsi=None, macd_hist=1.7, macd_line=-29.9, macd_rising=True,
        marron=None, media=None, marron_rising=None)
    assert bull > bear
    bull2, bear2 = signals._trend_balance(
        rsi=None, macd_hist=1.7, macd_line=29.9, macd_rising=True,
        marron=None, media=None, marron_rising=None)
    assert bull2 < bull  # ya arriba: el mismo giro pesa menos


def test_una_tendencia_sana_no_se_etiqueta_virando():
    """Todo subiendo con RSI 58 es una tendencia, no un giro. Sin un piso de
    evidencia el label se infla y 'VIRANDO A COMPRA' deja de querer decir algo."""
    import signals
    sig = signals.generate_signal(_ind(hist=2.0, hist_prev=1.5, macd_line=5.0,
                                       rsi=58.0, rsi_prev=55.0, marron=30.0,
                                       marron_prev=28.0, media=18.0))
    assert sig["signal_label"] == "NEUTRAL", sig["signal_label"]


def test_sobrevendido_sin_giro_sigue_siendo_zona():
    """RSI 25 pero todo todavia cayendo: zona, no giro."""
    import signals
    sig = signals.generate_signal(_ind(hist=-2.0, hist_prev=-1.5, macd_line=-5.0,
                                       rsi=25.0, rsi_prev=28.0, marron=-30.0,
                                       marron_prev=-28.0, media=-18.0))
    assert sig["signal_label"] == "ZONA DE SOBREVENTA", sig["signal_label"]


def test_inminente_sigue_pidiendo_2_de_3_y_zonas_coherentes():
    """El gate que alimenta Top Recomendaciones no se toca."""
    import signals
    buena = signals.generate_signal(_ind(hist=-2.0, hist_prev=-3.0, macd_line=-5.0,
                                         rsi=28.0, rsi_prev=27.0, marron=-30.0,
                                         marron_prev=-28.0, media=-18.0))
    assert buena["signal_label"] == "COMPRA INMINENTE"
    assert buena["conditions_met"] == 2
    # 2/3 de venta pero con el RSI en 32: no hay coherencia de zona
    floja = signals.generate_signal(_ind(hist=2.0, hist_prev=3.0, macd_line=-20.0,
                                         rsi=32.0, rsi_prev=33.0, marron=30.0,
                                         marron_prev=32.0, media=18.0))
    assert floja["signal_label"] == "VIRANDO A VENTA"


def test_el_detalle_mostrado_sigue_al_titular():
    """Las filas MACD/RSI/Koncorde y el x/3 son del lado del titular: mostrar
    'TECHO CONFIRMADO' del lado vendedor debajo de una lectura alcista era la
    mitad visible del bug."""
    import signals
    sig = signals.generate_signal(_ind(**CASO_BAC))
    assert "TECHO CONFIRMADO" not in sig["konc_detail"], sig["konc_detail"]
    assert sig["conditions_met"] == 0  # ninguna condicion de COMPRA cumplida aun


def test_bridge_clasifica_igual_que_el_local():
    """bridge/signals.py es un duplicado deliberado: si se desincronizan, el
    cloud y el local muestran etiquetas distintas del mismo grafico."""
    import signals
    import bridge.signals as bs
    for caso in (CASO_BAC, CASO_ESPEJO,
                 dict(hist=2.0, hist_prev=3.0, macd_line=5.0, rsi=55.0,
                      rsi_prev=58.0, marron=30.0, marron_prev=35.0, media=18.0),
                 dict(hist=2.0, hist_prev=1.5, macd_line=5.0, rsi=58.0,
                      rsi_prev=55.0, marron=30.0, marron_prev=28.0, media=18.0)):
        a = signals.generate_signal(_ind(**caso))
        b = bs.generate_signal(_ind(**caso))
        assert a["signal_label"] == b["signal_label"], caso
        assert a["signal"] == b["signal"] and a["strength"] == b["strength"]


# ──────────────────────────────────────────────────────────────────────────
# 11. VENTANA DEL GIRO (2026-10) — el gatillo compara contra la barra de hace
#     SIGNAL_TURN_LAG ruedas, no contra la anterior.
#
# Con lag=1 las dos condiciones de giro eran un volado: medido sobre 217
# simbolos x 10 años, el marron del Koncorde cambiaba de "subiendo" a "bajando"
# el 43% de los dias y el histograma el 28%. Consecuencias: el Top de
# recomendaciones se renovaba entero cada dia (1.15 de 5 simbolos en comun con
# el dia anterior, el 70% de las rachas de elegibilidad duraba 1 solo dia) y el
# gatillo de COMPRA tenia expectancy NEGATIVA (-0.172%/trade, PF 0.92). Con
# lag=3: +0.621%/trade, PF 1.32, t=3.80, positivo en las dos mitades de la
# decada. El riesgo ahora es que una de las CUATRO copias se quede atras.
# ──────────────────────────────────────────────────────────────────────────

def test_las_cuatro_copias_usan_la_misma_ventana_de_giro():
    """config, signals, el backtest y los dos duplicados del bridge tienen que
    medir el MISMO gatillo. Si el backtest usa otra ventana, sus estadisticas
    describen una estrategia que nadie opera; si el bridge usa otra, el cloud
    muestra una señal distinta del mismo grafico."""
    import config
    import signals
    import bridge.signals as bs
    import bridge.backtester as bbt
    assert signals.TURN_LAG == config.SIGNAL_TURN_LAG
    assert bs.TURN_LAG == signals.TURN_LAG
    assert bbt.TURN_LAG == signals.TURN_LAG


def test_el_backtest_dispara_en_las_mismas_barras_que_signals():
    """El gatillo del backtest esta reimplementado con numpy (no llama a
    signals.py). Esta es la unica atadura que impide que se separen: sobre una
    serie sintetica, los dias en que el backtest abriria una COMPRA tienen que
    ser exactamente aquellos en que generate_signal devuelve BUY."""
    import numpy as np
    import pandas as pd
    import backtester
    import indicators
    import signals

    rng = np.random.default_rng(7)
    n = 700
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
    df = pd.DataFrame({
        "date": pd.bdate_range("2021-01-01", periods=n).astype(str),
        "open": close, "high": close * 1.01, "low": close * 0.99,
        "close": close, "volume": np.full(n, 1e6),
    })
    ind = indicators.calculate_all(df)

    # Dias en los que el SISTEMA (signals.py) ve una compra completa.
    del_sistema = set()
    for i in range(600, n):
        sub = {k: v.iloc[:i + 1] for k, v in ind.items()}
        ok, _ = signals.check_buy_conditions(sub["koncorde"], sub["macd"], sub["rsi"])
        if ok:
            del_sistema.add(i)

    # Dias en los que el BACKTEST abriria (misma condicion, sin cooldown para
    # que no se pierda ninguno por tener una posicion abierta).
    lag = signals.TURN_LAG
    h = ind["macd"]["hist"].values
    r = ind["rsi"]["rsi"].values
    m = ind["koncorde"]["marron"].values
    med = ind["koncorde"]["media"].values
    del_backtest = {
        i for i in range(600, n)
        if h[i] < 0 and h[i] > h[i - lag] and r[i] < 30
        and m[i] < med[i] and m[i] > m[i - lag]
    }
    assert del_sistema == del_backtest, (
        sorted(del_sistema ^ del_backtest))
    # El backtest real lee la misma constante (si alguien la cambia en un solo
    # lado, el conjunto de arriba deja de coincidir).
    assert backtester.config.SIGNAL_TURN_LAG == lag


def test_el_giro_se_mide_contra_la_barra_de_hace_lag_ruedas():
    """Un histograma que sube respecto a AYER pero sigue por debajo del de hace
    LAG ruedas NO es un giro: ese ruido de una barra era el que renovaba el Top
    entero todos los dias."""
    import pandas as pd
    import signals
    lag = signals.TURN_LAG
    if lag < 2:
        return  # con lag=1 no hay nada que distinguir
    n = 2 * lag + 4
    # hist: venia de -1.0, cayo a -3.0 y hoy rebota a -2.0. Sube vs. ayer,
    # pero sigue muy por debajo de la barra de hace `lag` ruedas.
    hist = [-1.0] * (n - lag) + [-3.0] * (lag - 1) + [-2.0]
    ind = {
        "macd": pd.DataFrame({"macd": [-5.0] * n, "signal": [-3.0] * n,
                              "hist": hist}),
        "rsi": pd.DataFrame({"rsi": [25.0] * n}),
        "koncorde": pd.DataFrame({"marron": [-30.0] * n, "media": [-18.0] * n,
                                  "verde": [0.0] * n, "azul": [0.0] * n}),
    }
    ok, det = signals.check_buy_conditions(ind["koncorde"], ind["macd"], ind["rsi"])
    assert hist[-1] > hist[-2]          # sube respecto a ayer...
    assert not det["macd_ok"]           # ...pero el sistema no lo llama giro
    assert not ok


# ──────────────────────────────────────────────────────────────────────────
# 12. EDGE SOBRE BASELINE, NO EXPECTANCY CRUDA (2026-10)
#
# La expectancy cruda mide la señal MAS la deriva del activo. Sobre 217 simbolos
# x 10 años: entrar en corto al azar da -0.518%/trade y en largo al azar +0.347%
# — 0.87 pp que no son merito ni culpa del sistema. Puntuar la expectancy cruda
# premiaba comprar lo que sube solo y castigaba toda lectura bajista aunque la
# señal informara (la de VENTA tiene edge de +0.225 pp sobre el azar, p=0.0116).
# El baseline NO es constante entre simbolos (rango intercuartil 0.68 pp), asi
# que se mide por simbolo en el backtest y no se hardcodea.
# ──────────────────────────────────────────────────────────────────────────

def _bt(**kw):
    """Backtest sintetico con los campos que lee _score_stock."""
    base = {
        "confidence": 50.0, "buy_count": 20, "sell_count": 20,
        "buy_win_rate": 0.5, "sell_win_rate": 0.5,
        "buy_expectancy": 0.0, "sell_expectancy": 0.0,
        "buy_profit_factor": 1.0, "sell_profit_factor": 1.0,
        "buy_win_rate_trend": None, "sell_win_rate_trend": None,
        "buy_count_trend": 20, "sell_count_trend": 20,
        "buy_baseline_expectancy": None, "sell_baseline_expectancy": None,
        "buy_baseline_profit_factor": None, "sell_baseline_profit_factor": None,
        "buy_baseline_win_rate": None, "sell_baseline_win_rate": None,
        "baseline_count": 0,
    }
    base.update(kw)
    return base


def _analysis(label, signal, bt, price=100.0):
    """Analisis minimo que pasa la elegibilidad de _score_stock."""
    n = 300
    ohlc = [{"time": f"2026-01-{(i % 28) + 1:02d}", "open": price, "high": price * 1.01,
             "low": price * 0.99, "close": price} for i in range(n)]
    return {
        "signal": signal, "signal_label": label, "strength": 3.0,
        "conditions_met": 2 if signal == "HOLD" else 3,
        "backtest": bt, "price": price,
        "values": {"rsi": 72.0 if "VENTA" in label else 28.0,
                   "macd": {"hist": 1.0, "macd": 1.0},
                   "koncorde": {"marron": 10.0, "media": 5.0, "azul": 0.0}},
        "rsi_ok": True, "macd_ok": True, "konc_ok": False,
        "chart": {"ohlc": ohlc, "mas": {"sma200_val": None}},
    }


def _comp(parts, *names):
    for p in parts:
        if p["lab"] in names:
            return p["pts"]
    return None


def test_el_score_mide_edge_no_expectancy_cruda():
    """Dos setups con la MISMA expectancy cruda pero distinto baseline tienen que
    puntuar distinto: el que la saca en un activo que cae solo no tiene edge."""
    import vista_web as vw
    # expectancy -0.3% con baseline -0.6% => edge +0.3 pp (la señal aporta)
    buena = []
    vw._score_stock("A", _analysis("VENTA INMINENTE", "HOLD",
                    _bt(sell_expectancy=-0.3, sell_baseline_expectancy=-0.6)),
                    min_target_pct=0, parts=buena)
    # misma expectancy -0.3% pero baseline 0.0 => edge -0.3 pp (la señal resta)
    mala = []
    vw._score_stock("B", _analysis("VENTA INMINENTE", "HOLD",
                    _bt(sell_expectancy=-0.3, sell_baseline_expectancy=0.0)),
                    min_target_pct=0, parts=mala)
    e_buena = _comp(buena, "Edge sobre el baseline")
    e_mala = _comp(mala, "Edge sobre el baseline")
    assert e_buena is not None and e_mala is not None
    assert e_buena > e_mala, (e_buena, e_mala)


def test_edge_cero_puntua_el_neutral_de_la_escala():
    """Una señal que rinde exactamente lo que rinde entrar al azar no aporta
    evidencia: tiene que caer en el neutral (15 de 30), no en 0 ni en el maximo."""
    import vista_web as vw
    parts = []
    vw._score_stock("A", _analysis("VENTA INMINENTE", "HOLD",
                    _bt(sell_expectancy=-0.5, sell_baseline_expectancy=-0.5)),
                    min_target_pct=0, parts=parts)
    assert abs(_comp(parts, "Edge sobre el baseline") - 15.0) < 0.01


def test_sin_baseline_el_score_no_se_rompe():
    """El bridge sin reinstalar manda backtests sin los campos de baseline: el
    ranking tiene que caer a la expectancy cruda, no explotar ni dar 0."""
    import vista_web as vw
    parts = []
    s = vw._score_stock("A", _analysis("COMPRA INMINENTE", "HOLD",
                        _bt(buy_expectancy=2.0)), min_target_pct=0, parts=parts)
    assert s is not None and s > 0
    assert _comp(parts, "Expectancy historica") is not None   # etiqueta del fallback
    assert _comp(parts, "Edge sobre el baseline") is None


def test_el_backtest_exporta_el_baseline_y_el_bridge_tambien():
    """Los campos tienen que existir en las DOS copias (el cloud lee los del
    bridge). Si una no los manda, su ranking usa otra formula que el local."""
    import backtester
    import bridge.backtester as bbt
    campos = ("buy_baseline_expectancy", "sell_baseline_expectancy",
              "buy_baseline_profit_factor", "sell_baseline_profit_factor",
              "buy_baseline_win_rate", "sell_baseline_win_rate", "baseline_count")
    for mod in (backtester, bbt):
        vacio = mod._empty_result()
        for c in campos:
            assert c in vacio, (mod.__name__, c)
    assert bbt.BASELINE_STRIDE == backtester.config.BACKTEST_BASELINE_STRIDE


def test_el_baseline_se_mide_de_verdad_sobre_una_serie():
    """Extremo a extremo: sobre una serie con deriva alcista, el baseline LARGO
    tiene que salir mejor que el CORTO. Si salen iguales, no se esta midiendo."""
    import numpy as np
    import pandas as pd
    import backtester
    rng = np.random.default_rng(11)
    n = 700
    # deriva alcista clara + ruido
    close = 100 * np.exp(np.cumsum(rng.normal(0.0008, 0.015, n)))
    df = pd.DataFrame({
        "date": pd.bdate_range("2021-01-01", periods=n).astype(str),
        "open": close, "high": close * 1.01, "low": close * 0.99,
        "close": close, "volume": np.full(n, 1e6),
    })
    r = backtester.run_backtest(df)
    assert r["baseline_count"] > 50, r["baseline_count"]
    assert r["buy_baseline_expectancy"] > r["sell_baseline_expectancy"], (
        r["buy_baseline_expectancy"], r["sell_baseline_expectancy"])


# ──────────────────────────────────────────────────────────────────────────
# 13. EXECUTE_SELL_SIGNALS (2026-10) — la rama SELL del bot CIERRA LARGOS
#
# `bot.py` nunca abrio cortos: su rama SELL exige `pos_info["pos"] > 0`. El flag
# apaga esa EJECUCION, no la señal. Medido sin el tope temporal del backtest
# (como opera en vivo, donde el bracket no caduca): la señal SELL llega antes
# que el bracket en 21 de 971 trades (2.2%) y en esos sale PEOR (+4.32% vs
# +5.31%, -0.99 pp); el efecto global es -0.021 pp/trade (p=0.39).
# ──────────────────────────────────────────────────────────────────────────

def test_el_bot_nunca_abre_cortos():
    """La rama SELL exige una posicion LARGA previa. Si alguien la afloja, el
    bot empieza a shortear — y shortear con este setup pierde plata en los 10
    años medidos (-0.29%/trade, PF 0.85)."""
    import re
    src = open(os.path.join(os.path.dirname(__file__), "..", "bot.py")).read()
    # OJO: hay dos ramas SELL en bot.py — la de impresion (position_info) y la
    # de ejecucion dentro de run_scan_cycle (pos_info). Esta es la segunda.
    m = re.search(r'elif sig\["signal"\] == "SELL" and pos_info[^\n]*', src)
    assert m, "no se encontro la rama SELL de run_scan_cycle"
    rama = m.group(0)
    assert 'pos_info["pos"] > 0' in rama, rama


def test_el_flag_de_sell_existe_y_esta_documentado():
    import config
    assert hasattr(config, "EXECUTE_SELL_SIGNALS")
    assert isinstance(config.EXECUTE_SELL_SIGNALS, bool)


def test_con_el_flag_apagado_la_señal_sell_no_genera_orden():
    """La señal se sigue calculando y mostrando; lo que no ocurre es la orden.
    Se verifica sobre el codigo porque run_scan_cycle necesita TWS."""
    import re
    src = open(os.path.join(os.path.dirname(__file__), "..", "bot.py")).read()
    i = src.index('elif sig["signal"] == "SELL" and pos_info')
    bloque = src[i:i + 700]
    # el append a actionable_signals tiene que estar detras del flag
    assert "EXECUTE_SELL_SIGNALS" in bloque, bloque[:300]
    pos_flag = bloque.index("EXECUTE_SELL_SIGNALS")
    pos_append = bloque.index("actionable_signals.append")
    assert pos_flag < pos_append, "el append no esta gobernado por el flag"
    # y lo salteado se reporta: una salida ignorada en silencio es invisible
    assert "skipped_sells" in bloque
    assert "skipped_sells" in src[src.index("RESULTADOS:"):src.index("RESULTADOS:") + 600]


# ── 14. Mi Cartera: la tesis de entrada ──────────────────────────────
# El motor reconstruye el pasado con el MISMO codigo que evalua el presente
# (indicators -> signals -> patterns, sobre la serie cortada). Si esa identidad
# se rompe, la ficha narra una entrada que el sistema nunca habria disparado.

def _serie_sintetica(n=400, seed=7):
    """OHLCV con la forma que consume indicators.calculate_all."""
    import numpy as np
    import pandas as pd
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0.05, 1.4, n))
    close = np.maximum(close, 5.0)
    high = close * (1 + abs(rng.normal(0, .008, n)))
    low = close * (1 - abs(rng.normal(0, .008, n)))
    dates = pd.date_range("2024-01-01", periods=n, freq="B").strftime("%Y%m%d")
    return pd.DataFrame({"date": dates, "open": close, "high": high,
                         "low": low, "close": close,
                         "volume": rng.integers(1e6, 5e6, n).astype(float)})


def test_la_direccion_del_label_espeja_a_label_is_bearish():
    """TRAMPA DE SUBCADENAS: 'SOBREVENTA' contiene 'VENTA'. position_thesis no
    puede importar vista_web (ciclo), asi que duplica la regla — y la copia
    tiene que coincidir exactamente o la ficha pinta de bajista una lectura
    alcista."""
    import position_thesis as pt
    labels = ["COMPRA FUERTE", "COMPRA", "VENTA FUERTE", "VENTA",
              "COMPRA INMINENTE", "VENTA INMINENTE", "VIRANDO A COMPRA",
              "VIRANDO A VENTA", "ZONA DE SOBREVENTA", "ZONA DE SOBRECOMPRA",
              "NEUTRAL"]
    for lab in labels:
        assert (pt.label_direction(lab) == "bear") == vw._label_is_bearish(lab), lab
    # y el caso que origino el bug, explicito en las dos direcciones
    assert pt.label_direction("ZONA DE SOBREVENTA") == "bull"
    assert pt.label_direction("ZONA DE SOBRECOMPRA") == "bear"


def test_cortar_las_series_calculadas_es_igual_que_recalcular():
    """Los indicadores son causales: el valor en la barra i no puede depender
    de barras futuras. Es lo que permite calcular una sola vez y cortar — si
    dejara de valer, los hitos entrada->hoy serian ficcion."""
    import indicators
    import position_thesis as pt
    df = _serie_sintetica()
    i = 300
    entero = pt._slice_ind(indicators.calculate_all(df), i)
    recortado = indicators.calculate_all(df.iloc[:i + 1].reset_index(drop=True))
    for k in ("macd", "rsi", "koncorde"):
        a, b = entero[k].iloc[-1], recortado[k].iloc[-1]
        for col in a.index:
            va, vb = float(a[col]), float(b[col])
            if va != va and vb != vb:
                continue          # NaN en los dos lados: calentamiento
            assert abs(va - vb) < 1e-6, f"{k}.{col}: {va} vs {vb}"


def test_la_senal_reconstruida_en_la_ultima_barra_es_la_de_hoy():
    """El snapshot de 'hoy' que arma position_thesis tiene que coincidir con
    generate_signal sobre la serie completa: si divergen, la columna HOY de la
    ficha contradice al label de su propia tarjeta."""
    import indicators
    import signals
    import position_thesis as pt
    df = _serie_sintetica()
    hoy = signals.generate_signal(indicators.calculate_all(df))
    r = pt.build("TEST", None, df)
    assert r["now"]["signal"] == hoy["signal"]
    assert r["now"]["signal_label"] == hoy["signal_label"]
    assert r["now"]["conditions_met"] == hoy["conditions_met"]


def test_sin_fecha_de_entrada_se_dice_no_se_inventa():
    import position_thesis as pt
    r = pt.build("TEST", None, _serie_sintetica())
    assert r["status"] == "sin_historial"
    assert r["entry"] is None
    assert "no se puede reconstruir" in r["summary"]
    # pero el presente SI se informa: no hay motivo para ocultarlo
    assert r["now"]["signal_label"]


def test_la_tesis_distingue_intacta_de_dada_vuelta():
    import position_thesis as pt
    df = _serie_sintetica()
    fecha = str(df["date"].iloc[150])
    r = pt.build("TEST", fecha, df, avg_cost=float(df["close"].iloc[150]))
    assert r["status"] in ("intacta", "debilitada", "invalidada",
                           "dada_vuelta", "sin_senal")
    assert r["bars_held"] == len(df) - 1 - 150
    assert r["entry"]["as_of"] == pt._norm_date(fecha)
    assert isinstance(r["flips"], int) and r["flips"] >= 0
    # los hitos nunca son anteriores a la entrada
    for m in r["milestones"]:
        assert m["date"] >= r["entry_date"], m


# ── 14b. La narrativa no puede decir "no se" ─────────────────────────

def test_ninguna_rama_difiere_la_respuesta_al_proximo_ciclo():
    """'Sin condiciones cerca de activarse; revisar en el proximo ciclo' era el
    verdict_summary — o sea, justo lo que se veia colapsado en la tarjeta."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "vista_web.py")).read()
    i = src.index("def _generate_position_recommendation")
    cuerpo = src[i:src.index("\ndef ", i + 10)]
    # solo puede aparecer en el comentario que explica por que se saco
    for linea in cuerpo.splitlines():
        if "revisar en el proximo ciclo" in linea:
            assert linea.strip().startswith("#"), linea


def test_sin_costo_conocido_no_se_inventa_un_breakeven():
    """costo_promedio=0 (toda orden pendiente) emitia 'breakeven ($0.00)'."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "vista_web.py")).read()
    i = src.index("def _generate_position_recommendation")
    cuerpo = src[i:src.index("\ndef ", i + 10)]
    assert "be_ref" in cuerpo
    # avg_cost solo puede aparecer detras de "breakeven" en UN lugar: la
    # definicion de be_ref, que esta guardada por `if avg_cost > 0`.
    ocurrencias = [l for l in cuerpo.splitlines() if "breakeven ${avg_cost" in l]
    assert len(ocurrencias) == 1, ocurrencias
    assert "if avg_cost > 0 else" in ocurrencias[0], ocurrencias[0]
    assert "breakeven (${avg_cost" not in cuerpo


def test_una_orden_pendiente_no_dice_que_ya_entraste():
    src = open(os.path.join(os.path.dirname(__file__), "..", "vista_web.py")).read()
    i = src.index("def _generate_position_recommendation")
    cuerpo = src[i:src.index("\ndef ", i + 10)]
    assert 'Todavia no entraste en {sym}' in cuerpo
    j = cuerpo.index('if pos.get("is_pending"):')
    k = cuerpo.index('p1 = f"Entraste en {sym}"')
    assert j < k, "la rama de pendiente tiene que ganarle a la de entrada"


# ── 14c. Lo que el dashboard muestra ─────────────────────────────────

def test_los_parrafos_del_veredicto_se_separan_de_verdad():
    """DASHBOARD_HTML es un raw string: '\\\\n\\\\n' llegaba al JS como
    backslash+n literal, el split nunca partia y los 4 parrafos salian pegados
    en un solo <div> (ademas, el resaltado de 'Recomendacion:' nunca corria)."""
    h = vw.DASHBOARD_HTML
    assert "rec.verdict_reason.split('\\n\\n')" in h
    assert "rec.verdict_reason.split('\\\\n\\\\n')" not in h


def test_el_porque_del_veredicto_viaja_al_frontend():
    """_compute_position_verdict pondera ~19 factores y el payload exportaba
    solo la palabra final: la ficha mostraba una accion que no podia explicar."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "vista_web.py")).read()
    i = src.index("def _build_position_deep_analysis")
    cuerpo = src[i:src.index("\ndef ", i + 10)]
    for campo in ("verdict_factors", "bull_score", "bear_score", "entry_thesis"):
        assert f'"{campo}"' in cuerpo, campo


def test_la_linea_de_costo_entra_en_el_autoescalado():
    """Las priceLines de Lightweight Charts NO escalan el eje: con el precio
    bastante bajo el costo, la linea quedaba fuera de la ventana visible."""
    h = vw.DASHBOARD_HTML
    i = h.index("function _portAnalDecorate")
    cuerpo = h[i:h.index("\nfunction ", i + 10)]
    assert "scale:true" in cuerpo, "la linea de costo no pide entrar en escala"
    assert "autoscaleInfoProvider" in h
    # y el flag no puede filtrarse a createPriceLine (LW lo rechaza)
    assert "delete opts.scale;" in h


def test_el_cloud_exporta_los_mismos_campos_nuevos():
    """Paridad: el usuario mira el cloud. Si el espejo no manda estos campos,
    la ficha se degrada en silencio justo donde el usuario la usa."""
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "cloud", "server.py")).read()
    i = src.index("def _build_cloud_position_analysis")
    cuerpo = src[i:src.index("\ndef ", i + 10)]
    for campo in ("entry_thesis", "verdict_factors", "bull_score", "bear_score"):
        assert f'"{campo}"' in cuerpo, campo
    # y el lado del backtest lo decide el LABEL, no el signal crudo
    assert "_label_is_bearish(data.get(\"signal_label\", sig))" in cuerpo
    assert 'if sig == "SELL"' not in cuerpo
