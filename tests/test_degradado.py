"""El camino DEGRADADO, barrido a fuerza bruta.

Por que existe: el bug que vacio Top Recomendaciones fue un `float(None)` en un
insumo del desglose. No se cazo escribiendo codigo con cuidado ni mirando la
pantalla — se cazo alimentando las funciones con datos incompletos. Un simbolo
sin trades historicos no es un borde raro: es el caso mas comun.

Dos invariantes, sobre cada combinacion de datos faltantes:
  1. NINGUNA funcion que produce texto o numeros para el usuario puede LANZAR.
     El caller del cloud envuelve compute_top3 en un try que devuelve [], asi
     que una excepcion se convierte en una seccion en blanco sin aviso.
  2. NINGUNA puede filtrar un centinela ("None", "nan", "undefined"). Un
     "Backtest 5Y: None señales" es una mentira con cara de dato.

Si agregas un componente al score, una linea al racional o un factor al
veredicto, este test lo cubre solo. Corre en ~2s.
"""
import itertools
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import option_position as op
import option_strategy as os_
import vista_web as vw

CENTINELAS = re.compile(r"\bNone\b|\bnan\b|\bNaN\b|undefined|\bInfinity\b")

_BT = {"confidence": 61, "buy_win_rate": .67, "buy_expectancy": 4.17,
       "buy_profit_factor": 4.79, "buy_count": 14, "buy_count_trend": 14,
       "buy_win_rate_trend": .7, "buy_avg_win": 7.9, "buy_avg_loss": -3.3,
       "buy_baseline_expectancy": -0.17, "buy_baseline_profit_factor": .93,
       "buy_baseline_win_rate": .36, "baseline_count": 199,
       "sell_win_rate": .4, "sell_expectancy": -1.0, "sell_profit_factor": .8,
       "sell_count": 5, "sell_count_trend": 0, "sell_win_rate_trend": None,
       "sell_avg_win": 3.0, "sell_avg_loss": -4.0,
       "sell_baseline_expectancy": -0.5, "sell_baseline_profit_factor": .9,
       "sell_baseline_win_rate": .34}

BACKTESTS = {
    "lleno": _BT,
    "ausente": None,
    "vacio": {},
    "solo_confianza": {"confidence": 0},
    "todo_None": {k: None for k in _BT},          # el caso que rompio
    "sin_perdedores": {**_BT, "buy_profit_factor": 999.0,
                       "buy_baseline_profit_factor": None},
    "sin_baseline": {k: v for k, v in _BT.items() if "baseline" not in k},
    "sin_muestra": {**_BT, "buy_count": 0, "sell_count": 0},
}


def _ohlc(n=300, price=100.0):
    return [{"time": f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}", "open": price,
             "high": price * 1.02, "low": price * .98, "close": price}
            for i in range(n)]


CHARTS = {
    "lleno": {"ohlc": _ohlc(), "mas": {"sma200_val": 90.0, "sma100_val": 97.0,
                                       "sma50_val": 99.0, "sma20_val": 100.0}},
    "ausente": None,
    "vacio": {},
    "sin_ohlc": {"mas": {"sma200_val": 90.0}},
    "ohlc_corto": {"ohlc": _ohlc(5), "mas": {}},
    "sin_medias": {"ohlc": _ohlc()},
    "medias_None": {"ohlc": _ohlc(), "mas": {"sma200_val": None, "sma100_val": None}},
}

LABELS = ["COMPRA FUERTE", "VENTA FUERTE", "COMPRA INMINENTE", "VENTA INMINENTE",
          "VIRANDO A COMPRA", "VIRANDO A VENTA", "ZONA DE SOBREVENTA",
          "ZONA DE SOBRECOMPRA", "NEUTRAL"]

FIGURAS = {
    "ninguna": None,
    "validada": {"name": "Doble suelo", "direction": "alcista", "status": "confirmada",
                 "tier": "validada", "target": 120.0, "invalidation": 90.0,
                 "priority": 70, "text": "Doble suelo confirmado"},
    "campos_None": {"name": None, "direction": "alcista", "status": None,
                    "tier": "validada", "target": None, "invalidation": None,
                    "priority": 0, "text": None},
}

POSICIONES = {
    "larga": {"symbol": "T", "tipo": "STK", "cantidad": 100, "costo_promedio": 95.0,
              "precio_actual": 100.0, "pnl": 500.0, "pnl_pct": 5.26,
              "valor_mercado": 10000.0, "peso_portafolio": .2,
              "stop_loss": 92.0, "take_profit": 110.0},
    "corta": {"symbol": "T", "tipo": "STK", "cantidad": -100, "costo_promedio": 95.0,
              "precio_actual": 100.0, "pnl": -500.0, "pnl_pct": -5.26},
    "costo_cero": {"symbol": "T", "tipo": "STK", "cantidad": 0, "costo_promedio": 0,
                   "precio_actual": None, "pnl": 0, "pnl_pct": 0},
    "pendiente": {"symbol": "T", "tipo": "STK", "cantidad": 0, "costo_promedio": 0,
                  "precio_actual": None, "pnl": 0, "pnl_pct": 0, "is_pending": True,
                  "pending_order": {"action": "BUY", "price": 90.0, "quantity": 100,
                                    "order_type": "LMT", "status": "Submitted"}},
    "vacia": {},
}


def _data(label, bt, chart, fig):
    d = {"signal": ("BUY" if label.startswith("COMPRA") else
                    "SELL" if label.startswith("VENTA") else "HOLD"),
         "signal_label": label, "strength": 4.3, "conditions_met": 3, "price": 100.0,
         "rsi_ok": True, "macd_ok": True, "konc_ok": True,
         "macd_detail": "hist=-1.0", "rsi_detail": "RSI=28.3",
         "konc_detail": "marron=14",
         "values": {"rsi": 28.3, "macd": {"macd": -1.0, "signal": -.5, "hist": -.5},
                    "koncorde": {"verde": 10.0, "marron": 14.0, "azul": 3.0,
                                 "media": 47.0}},
         "pattern": FIGURAS[fig], "fib": None, "candles": []}
    if BACKTESTS[bt] is not None:
        d["backtest"] = BACKTESTS[bt]
    if CHARTS[chart] is not None:
        d["chart"] = CHARTS[chart]
    return d


def _sin_centinelas(nombre, valor):
    txt = valor if isinstance(valor, str) else json.dumps(valor, default=str,
                                                          ensure_ascii=False)
    m = CENTINELAS.search(txt or "")
    assert not m, f"{nombre} filtro '{m.group(0)}': ...{txt[max(0, m.start()-80):m.end()+80]}..."


CASOS = list(itertools.product(LABELS, BACKTESTS, CHARTS, FIGURAS))


@pytest.mark.parametrize("label,bt,chart,fig", CASOS)
def test_el_analisis_degradado_ni_lanza_ni_miente(label, bt, chart, fig):
    d = _data(label, bt, chart, fig)
    tag = f"{label}|bt={bt}|chart={chart}|fig={fig}"
    partes = []
    vw._score_stock("T", d, parts=partes)          # puede devolver None: es valido
    _sin_centinelas(f"score_parts [{tag}]", partes)
    lv = vw._compute_price_levels(d)
    _sin_centinelas(f"levels [{tag}]", lv)
    _sin_centinelas(f"thesis [{tag}]", vw._generate_thesis("T", d, lv or {}, {}))
    _sin_centinelas(f"rationale [{tag}]", vw._generate_rationale("T", d, lv or {}))


@pytest.mark.parametrize("label,pos_k", list(itertools.product(LABELS, POSICIONES)))
def test_la_ficha_de_posicion_degradada_ni_lanza_ni_miente(label, pos_k):
    for bt in ("lleno", "todo_None", "vacio"):
        d = _data(label, bt, "lleno", "validada")
        pos = dict(POSICIONES[pos_k])
        lv = vw._compute_price_levels(d)
        vd = vw._compute_position_verdict(d, pos, lv)
        _sin_centinelas(f"verdict [{label}|{pos_k}|{bt}]", vd)
        txt = vw._generate_position_recommendation("T", d, pos, lv or {}, [], vd, None)
        _sin_centinelas(f"narrativa [{label}|{pos_k}|{bt}]", txt)


def _pata(right, qty, strike, prima=5.0, expiry="20270115", **extra):
    # `costo_promedio` POR CONTRATO, como lo manda IB (ver describe)
    p = {"symbol": "T", "tipo": "OPT", "cantidad": qty, "costo_promedio": prima * 100,
         "precio_actual": prima, "right": right, "strike": strike,
         "expiry": expiry, "multiplier": "100", "pnl": 0.0, "pnl_pct": 0.0,
         "valor_mercado": abs(qty) * prima * 100, **extra}
    p["option"] = op.describe(p)
    return p


PATAS = ([_pata(r, q, k) for r in ("C", "P") for q in (1, -1, 7, -7) for k in (90.0, 110.0)]
         + [{"symbol": "T", "tipo": "OPT", "cantidad": 1, "costo_promedio": 500.0},
            {"symbol": "T", "tipo": "OPT", "cantidad": 1, "costo_promedio": 0,
             "precio_actual": None, "right": "C", "strike": None, "expiry": None,
             "multiplier": None}])


@pytest.mark.parametrize("i", range(len(PATAS)))
def test_una_pata_degradada_ni_lanza_ni_miente(i):
    p = dict(PATAS[i])
    _sin_centinelas(f"describe [{i}]", op.describe(p))
    _sin_centinelas(f"enrich [{i}]", op.enrich(dict(p), underlying_price=100.0, atr=2.0))


GRUPOS = {
    "bull_call_spread": [_pata("C", 3, 750.0, 20.0), _pata("C", -3, 850.0, 8.0)],
    "collar": [_pata("P", 7, 116.0, 3.1), _pata("C", -7, 127.0, 2.8)],
    "straddle": [_pata("C", 1, 100.0), _pata("P", 1, 100.0)],
    "call_desnudo": [_pata("C", -5, 110.0)],
    "put_comprado": [_pata("P", 2, 105.0)],
    "calendar": [_pata("C", 1, 100.0, expiry="20261120"),
                 _pata("C", -1, 100.0, expiry="20270115")],
    "sin_prima": [_pata("C", 1, 100.0, prima=0)],
}


@pytest.mark.parametrize("nombre", list(GRUPOS))
def test_una_estrategia_degradada_ni_lanza_ni_miente(nombre):
    patas = [dict(p) for p in GRUPOS[nombre]]
    _sin_centinelas(f"payoff [{nombre}]",
                    os_.analyze_payoff([p["option"] for p in patas]))
    fus = os_.collapse(patas)
    _sin_centinelas(f"collapse [{nombre}]", fus)
    for x in fus:
        # el ratio de DECISION, si existe, tiene que estar en las mismas
        # unidades que el P&L (dolares) — no 100x por el multiplicador
        r = x.get("pnl_vs_risk_pct")
        if r is not None:
            assert abs(r) < 1000, f"{nombre}: pnl_vs_risk_pct={r} huele a unidades mezcladas"
