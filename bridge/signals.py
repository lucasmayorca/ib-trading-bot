"""
Signal generation for the bridge (standalone).
BUY/SELL when all 3 indicators align.
"""

import numpy as np


def check_buy_conditions(koncorde_df, macd_df, rsi_df):
    if len(koncorde_df) < 5 or len(macd_df) < 5 or len(rsi_df) < 5:
        return False, {"razon": "Datos insuficientes"}

    hist = macd_df["hist"].iloc[-1]
    hist_prev = macd_df["hist"].iloc[-2]
    hist_prev2 = macd_df["hist"].iloc[-3]

    macd_oversold = hist < 0
    macd_turning = hist > hist_prev
    macd_had_bottom = hist_prev <= hist_prev2
    macd_ok = macd_oversold and macd_turning
    macd_detail = f"hist={hist:.2f} prev={hist_prev:.2f}"
    if macd_ok and macd_had_bottom:
        macd_detail += " PISO CONFIRMADO"
    elif macd_ok:
        macd_detail += " girando"

    rsi_val = rsi_df["rsi"].iloc[-1]
    rsi_prev = rsi_df["rsi"].iloc[-2]
    rsi_ok = rsi_val < 30
    rsi_detail = f"RSI={rsi_val:.1f}"
    if rsi_ok:
        rsi_detail += " SOBREVENTA"
        if rsi_val > rsi_prev:
            rsi_detail += " rebotando"

    marron = koncorde_df["marron"].iloc[-1]
    marron_prev = koncorde_df["marron"].iloc[-2]
    marron_prev2 = koncorde_df["marron"].iloc[-3]
    azul = koncorde_df["azul"].iloc[-1]
    media = koncorde_df["media"].iloc[-1]

    konc_negative = marron < media
    konc_turning = marron > marron_prev
    konc_had_bottom = marron_prev <= marron_prev2
    konc_ok = konc_negative and konc_turning
    konc_detail = f"marron={marron:.1f} media={media:.1f}"
    if konc_ok and konc_had_bottom:
        konc_detail += " PISO CONFIRMADO"
    elif konc_ok:
        konc_detail += " girando"
    if azul > 0:
        konc_detail += f" institucional+(azul={azul:.1f})"

    is_signal = macd_ok and rsi_ok and konc_ok

    strength = 0
    if macd_ok:
        strength += 1
        if macd_had_bottom:
            strength += 0.5
    if rsi_ok:
        strength += 1
        if rsi_val < 20:
            strength += 0.5
        if rsi_val > rsi_prev:
            strength += 0.3
    if konc_ok:
        strength += 1
        if konc_had_bottom:
            strength += 0.5
        if azul > 0:
            strength += 0.3

    return is_signal, {
        "macd_ok": macd_ok, "rsi_ok": rsi_ok, "konc_ok": konc_ok,
        "macd_detail": macd_detail, "rsi_detail": rsi_detail, "konc_detail": konc_detail,
        "macd_rising": bool(hist > hist_prev), "konc_rising": bool(marron > marron_prev),
        "strength": strength, "conditions_met": sum([macd_ok, rsi_ok, konc_ok]),
    }


def check_sell_conditions(koncorde_df, macd_df, rsi_df):
    if len(koncorde_df) < 5 or len(macd_df) < 5 or len(rsi_df) < 5:
        return False, {"razon": "Datos insuficientes"}

    hist = macd_df["hist"].iloc[-1]
    hist_prev = macd_df["hist"].iloc[-2]
    hist_prev2 = macd_df["hist"].iloc[-3]

    macd_overbought = hist > 0
    macd_turning = hist < hist_prev
    macd_had_peak = hist_prev >= hist_prev2
    macd_ok = macd_overbought and macd_turning
    macd_detail = f"hist={hist:.2f} prev={hist_prev:.2f}"
    if macd_ok and macd_had_peak:
        macd_detail += " TECHO CONFIRMADO"
    elif macd_ok:
        macd_detail += " girando"

    rsi_val = rsi_df["rsi"].iloc[-1]
    rsi_prev = rsi_df["rsi"].iloc[-2]
    rsi_ok = rsi_val > 70
    rsi_detail = f"RSI={rsi_val:.1f}"
    if rsi_ok:
        rsi_detail += " SOBRECOMPRA"
        if rsi_val < rsi_prev:
            rsi_detail += " cayendo"

    marron = koncorde_df["marron"].iloc[-1]
    marron_prev = koncorde_df["marron"].iloc[-2]
    marron_prev2 = koncorde_df["marron"].iloc[-3]
    azul = koncorde_df["azul"].iloc[-1]
    media = koncorde_df["media"].iloc[-1]

    konc_positive = marron > media
    konc_turning = marron < marron_prev
    konc_had_peak = marron_prev >= marron_prev2
    konc_ok = konc_positive and konc_turning
    konc_detail = f"marron={marron:.1f} media={media:.1f}"
    if konc_ok and konc_had_peak:
        konc_detail += " TECHO CONFIRMADO"
    elif konc_ok:
        konc_detail += " girando"
    if azul < 0:
        konc_detail += f" institucional-(azul={azul:.1f})"

    is_signal = macd_ok and rsi_ok and konc_ok

    strength = 0
    if macd_ok:
        strength += 1
        if macd_had_peak:
            strength += 0.5
    if rsi_ok:
        strength += 1
        if rsi_val > 80:
            strength += 0.5
        if rsi_val < rsi_prev:
            strength += 0.3
    if konc_ok:
        strength += 1
        if konc_had_peak:
            strength += 0.5
        if azul < 0:
            strength += 0.3

    return is_signal, {
        "macd_ok": macd_ok, "rsi_ok": rsi_ok, "konc_ok": konc_ok,
        "macd_detail": macd_detail, "rsi_detail": rsi_detail, "konc_detail": konc_detail,
        "macd_rising": bool(hist > hist_prev), "konc_rising": bool(marron > marron_prev),
        "strength": strength, "conditions_met": sum([macd_ok, rsi_ok, konc_ok]),
    }


def _zones_coherent_buy(rsi, macd_hist, marron, media):
    """Coherencia de ZONA para setup de compra (espejo de signals.py raiz):
    hist MACD <= 0, RSI < 45 y Koncorde bajo su media."""
    checks = []
    if macd_hist is not None:
        checks.append(macd_hist <= 0)
    if rsi is not None:
        checks.append(rsi < 45)
    if marron is not None and media is not None:
        checks.append(marron < media)
    return bool(checks) and all(checks)


def _zones_coherent_sell(rsi, macd_hist, marron, media):
    """Coherencia de ZONA para setup de venta: hist >= 0, RSI > 55, marron > media."""
    checks = []
    if macd_hist is not None:
        checks.append(macd_hist >= 0)
    if rsi is not None:
        checks.append(rsi > 55)
    if marron is not None and media is not None:
        checks.append(marron > media)
    return bool(checks) and all(checks)


# Umbrales del balance de evidencia (ver _trend_balance).
TREND_MIN_LEAN = 2.5    # evidencia minima del lado que gana para etiquetar VIRANDO
TREND_MIN_MARGIN = 1.0  # ventaja minima sobre el otro lado


def _trend_balance(rsi, macd_hist, macd_line, macd_rising,
                   marron, media, marron_rising):
    """Pondera la evidencia alcista y bajista de los 3 indicadores.

    Cada indicador vota por ZONA (en que mitad del rango estamos: que setup
    del sistema esta en juego) y por GIRO (si nos acercamos a ese setup o nos
    alejamos). Devuelve (bull, bear).

    Existe porque contar las condiciones de UN solo lado dejaba que la primera
    condicion que apareciera etiquetara el cuadro entero: un Koncorde que
    pincha desde arriba de su media bastaba para decir "VIRANDO A VENTA" con
    el RSI en 32 y el MACD recien cruzado al alza. El titular lo decide el
    peso de la evidencia, nunca una condicion suelta.
    """
    bull = 0.0
    bear = 0.0

    # --- RSI: zona. Es el ancla, dice de que lado del rango estamos ---
    if rsi is not None:
        if rsi < 30:
            bull += 2.0
        elif rsi < 35:
            bull += 1.5
        elif rsi < 45:
            bull += 1.0
        elif rsi > 70:
            bear += 2.0
        elif rsi > 65:
            bear += 1.5
        elif rsi > 55:
            bear += 1.0

    # --- MACD: giro + donde esta la LINEA respecto de cero ---
    # El histograma solo dice MACD vs senal. Con la linea bien bajo cero un
    # histograma positivo no es sobrecompra: es un cruce alcista recien hecho
    # (el caso que rompia la etiqueta). La zona la da la linea, no el hist.
    if macd_hist is not None and macd_rising is not None:
        if macd_rising:
            if macd_hist < 0:
                bull += 1.5   # condicion de compra del sistema: giro bajo cero
            elif macd_line is not None and macd_line < 0:
                bull += 1.2   # cruce alcista reciente, la linea aun bajo cero
            else:
                bull += 0.5   # momentum a favor pero ya en zona alta
        else:
            if macd_hist > 0:
                bear += 1.5
            elif macd_line is not None and macd_line > 0:
                bear += 1.2
            else:
                bear += 0.5

    # --- Koncorde: marron vs su media (zona) + giro ---
    if marron is not None and media is not None and marron_rising is not None:
        if marron_rising:
            bull += 1.5 if marron < media else 0.5
        else:
            bear += 1.5 if marron > media else 0.5

    return bull, bear


def _classify_trend(signal, buy_details, sell_details, vals):
    """Etiqueta descriptiva de tendencia. Devuelve (label, direccion).

    `signal` (BUY/SELL/HOLD) es el gatillo de ordenes y no se toca aca; esta
    etiqueta es display-only y mas granular. La direccion ("bull"/"bear"/
    "neutral") viaja al lado del texto para no tener que re-parsear el label
    con substrings (SOBREVENTA contiene VENTA).
    """
    if signal == "BUY":
        return ("COMPRA FUERTE" if buy_details.get("strength", 0) >= 4
                else "COMPRA"), "bull"

    if signal == "SELL":
        return ("VENTA FUERTE" if sell_details.get("strength", 0) >= 4
                else "VENTA"), "bear"

    # --- HOLD: analizar tendencia con los indicadores ---
    buy_met = buy_details.get("conditions_met", 0)
    sell_met = sell_details.get("conditions_met", 0)

    rsi = vals.get("rsi")
    macd_vals = vals.get("macd") or {}
    macd_hist = macd_vals.get("hist")
    macd_line = macd_vals.get("macd")
    konc = vals.get("koncorde") or {}
    marron = konc.get("marron")
    media = konc.get("media")

    macd_rising = buy_details.get("macd_rising", sell_details.get("macd_rising"))
    konc_rising = buy_details.get("konc_rising", sell_details.get("konc_rising"))

    # 2 de 3 condiciones + zonas coherentes => INMINENTE (gatea el Top).
    if buy_met == 2 and _zones_coherent_buy(rsi, macd_hist, marron, media):
        return "COMPRA INMINENTE", "bull"
    if sell_met == 2 and _zones_coherent_sell(rsi, macd_hist, marron, media):
        return "VENTA INMINENTE", "bear"

    # Debajo de eso manda el BALANCE de evidencia: 2/3 sin coherencia de zona
    # sigue cayendo en VIRANDO de su lado (el balance siempre lo acompana),
    # pero una condicion suelta ya no puede imponer el titular.
    bull, bear = _trend_balance(rsi, macd_hist, macd_line, macd_rising,
                                marron, media, konc_rising)
    if bull >= TREND_MIN_LEAN and bull - bear >= TREND_MIN_MARGIN:
        return "VIRANDO A COMPRA", "bull"
    if bear >= TREND_MIN_LEAN and bear - bull >= TREND_MIN_MARGIN:
        return "VIRANDO A VENTA", "bear"

    if rsi is not None:
        if rsi < 35:
            return "ZONA DE SOBREVENTA", "bull"
        if rsi > 65:
            return "ZONA DE SOBRECOMPRA", "bear"

    return "NEUTRAL", "neutral"


def generate_signal(indicators):
    koncorde = indicators["koncorde"]
    macd = indicators["macd"]
    rsi_data = indicators["rsi"]

    is_buy, buy_details = check_buy_conditions(koncorde, macd, rsi_data)
    is_sell, sell_details = check_sell_conditions(koncorde, macd, rsi_data)

    if is_buy:
        signal, details = "BUY", buy_details
    elif is_sell:
        signal, details = "SELL", sell_details
    else:
        buy_met = buy_details.get("conditions_met", 0)
        sell_met = sell_details.get("conditions_met", 0)
        details = buy_details if buy_met >= sell_met else sell_details
        signal = "HOLD"

    last_vals = {}
    if len(koncorde) > 0:
        k = koncorde.iloc[-1]
        last_vals["koncorde"] = {"verde": k["verde"], "marron": k["marron"], "azul": k["azul"], "media": k["media"]}
    if len(macd) > 0:
        m = macd.iloc[-1]
        last_vals["macd"] = {"macd": m["macd"], "signal": m["signal"], "hist": m["hist"]}
    if len(rsi_data) > 0:
        last_vals["rsi"] = rsi_data.iloc[-1]["rsi"]

    signal_label, label_dir = _classify_trend(
        signal, buy_details, sell_details, last_vals
    )

    # Las filas que se muestran (x/3, fuerza, detalle por indicador) siguen la
    # DIRECCION del titular. Elegirlas por "el lado con mas condiciones" era lo
    # que colaba un "TECHO CONFIRMADO" de Koncorde debajo de una lectura
    # alcista: una sola condicion del lado opuesto se llevaba toda la narrativa.
    if signal == "HOLD":
        if label_dir == "bull":
            details = buy_details
        elif label_dir == "bear":
            details = sell_details

    return {
        "signal": signal,
        "signal_label": signal_label,
        "strength": details.get("strength", 0),
        "conditions_met": details.get("conditions_met", 0),
        "macd_ok": details.get("macd_ok", False),
        "rsi_ok": details.get("rsi_ok", False),
        "konc_ok": details.get("konc_ok", False),
        "macd_detail": details.get("macd_detail", ""),
        "rsi_detail": details.get("rsi_detail", ""),
        "konc_detail": details.get("konc_detail", ""),
        "values": last_vals,
    }
