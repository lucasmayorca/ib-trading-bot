"""
patterns.py — Motor COMPARTIDO de figuras tecnicas (chartista) por reglas.

Generaliza el detector que nacio en market_pulse.py (solo SPY) para que todos
los analisis lo usen: escaner de acciones, escaner ETF, Top Recomendaciones,
Mi Cartera y el propio Pulso del Mercado. Corre server-side sobre las series
del chart que ya viajan en cada analisis (patron enrichment.py: cero cambios
en el bridge — el cloud lo computa al recibir cada batch).

Detectores: ruptura de nivel, doble/triple techo-suelo, hombro-cabeza-hombro
(y su inverso), triangulos/cuñas (incluida "rota"), banderas de continuacion,
cruce dorado/muerte (opcional), divergencia RSI/precio, canal/estructura como
fallback (opcional) — mas el contexto de retrocesos de FIBONACCI del ultimo
impulso relevante.

Schema de cada figura (superset del que ya usaba el pulso — su JS lo ignora):
  {
    "name":         "Doble techo", ...
    "direction":    "alcista" | "bajista" | "neutral",
    "status":       "en formacion" | "por confirmar" | "confirmada" | "vigente",
    "key_level":    nivel de referencia (neckline / nivel roto / linea),
    "breakout":     nivel cuyo cierre CONFIRMA la figura (puede ser None),
    "invalidation": nivel cuyo cierre ANULA la figura (puede ser None),
    "target":       objetivo MEDIDO de la figura (altura proyectada; puede ser None),
    "priority":     ranking interno para elegir la figura dominante,
    "text":         una frase en español lista para tesis/racional/narrativa,
    "secondary":    texto de una segunda figura relevante (solo en la principal),
    "draw":         geometria DIBUJABLE de la figura (opcional): {
                      "segments": [{"x0","y0","x1","y1","dash","w"}...],
                      "points":   [{"x","y","label","pos"}...]
                    } con x como OFFSET desde la ultima barra (0 = hoy) — el
                    frontend lo convierte en series superpuestas del chart
                    (rectas del triangulo, neckline, palo/canal de la bandera).
  }

API:
  detect(highs, lows, closes, rsi=None, sma50=None, sma200=None,
         include_cross=False, include_fallback=False)
      -> {"pattern": principal|None, "candidates": [...], "fibonacci": fib|None,
          "structure": (dir -1|0|1, texto)}
  attach_to_analysis(sig)  -> muta sig: sig["pattern"], sig["fib"] (para el
      payload del escaner; excluye cruce de MAs — la tesis ya lo cuenta — y el
      fallback de canal — los niveles ya lo muestran).
  validate_universe({sym: df}) -> hit-rate historico por tipo de figura
      (para /api/calibration: ¿el objetivo medido se alcanza antes que la
      invalidacion? — evidencia para calibrar los pesos, no fe).

Todas las funciones son puras (numpy, sin IO). Las series deben venir de
cierres CONFIRMADOS (los analisis ya descartan la barra parcial).
"""

import math

import numpy as np

# ── prioridades (figura dominante = la de mayor prioridad) ──
_PRIO = {
    "ruptura_conf": 90, "ruptura_por_conf": 80,
    "triple_conf": 88, "triple_form": 72,
    "hch_conf": 87, "hch_form": 71,
    "doble_conf": 85, "doble_form": 70,
    "bandera_conf": 82, "bandera_form": 65,
    "triangulo_roto": 75, "triangulo_form": 60,
    "cruce": 50, "divergencia": 45,
    "canal": 30, "estructura": 20,
}


def _r(x, nd=2):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, nd)


def _usd(x):
    return f"${x:,.2f}" if x is not None else "$--"


def _sanitize(vals):
    """Lista de floats con None/NaN reemplazados por el valor anterior."""
    out = []
    prev = None
    for v in vals:
        try:
            f = float(v)
            if math.isnan(f) or math.isinf(f):
                f = None
        except (TypeError, ValueError):
            f = None
        if f is None:
            f = prev
        if f is not None:
            out.append(f)
            prev = f
        elif out:
            out.append(out[-1])
        else:
            out.append(0.0)
    return out


def _atr_arr(highs, lows, closes, period=14):
    n = len(closes)
    if n < period + 1:
        return None
    trs = []
    for i in range(max(1, n - period * 3), n):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    if len(trs) < period:
        return None
    return float(np.mean(trs[-period:]))


def _pivots_arr(highs, lows, lookback=252, window=3):
    """Pivotes fractales (idx absoluto, precio) sobre las ultimas `lookback`."""
    n = len(highs)
    start = max(window, n - lookback)
    piv_h, piv_l = [], []
    for i in range(start, n - window):
        seg_h = highs[i - window:i + window + 1]
        seg_l = lows[i - window:i + window + 1]
        if highs[i] == max(seg_h):
            piv_h.append((i, float(highs[i])))
        if lows[i] == min(seg_l):
            piv_l.append((i, float(lows[i])))
    return piv_h, piv_l


def _cluster_levels(pivots, tol):
    out = []
    for idx, price in sorted(pivots, key=lambda p: p[1]):
        if out and abs(price - out[-1]["level"]) <= tol:
            c = out[-1]
            c["level"] = (c["level"] * c["touches"] + price) / (c["touches"] + 1)
            c["touches"] += 1
            c["last_idx"] = max(c["last_idx"], idx)
        else:
            out.append({"level": price, "touches": 1, "last_idx": idx})
    return out


# ══════════════════════════════════════════════════════════════
#  DETECTORES
# ══════════════════════════════════════════════════════════════

def _pat_breakout(closes, all_levels, atr):
    """Cierre cruzando un nivel de 2+ toques en las ultimas 5 ruedas."""
    if len(closes) < 10 or not atr:
        return None
    c_now, c_ref = closes[-1], closes[-6]
    min_move = 0.25 * atr
    best = None
    for lvl in all_levels:
        if lvl["touches"] < 2:
            continue
        L = lvl["level"]
        if c_ref < L and c_now > L + min_move:
            cand = ("alcista", lvl)
        elif c_ref > L and c_now < L - min_move:
            cand = ("bajista", lvl)
        else:
            continue
        if best is None or lvl["touches"] > best[1]["touches"]:
            best = cand
    if not best:
        return None
    d, lvl = best
    L = lvl["level"]
    confirmed = ((closes[-2] > L and c_now > L + 0.4 * atr) if d == "alcista"
                 else (closes[-2] < L and c_now < L - 0.4 * atr))
    st = "confirmada" if confirmed else "por confirmar"
    rol = "soporte" if d == "alcista" else "resistencia"
    return {
        "name": "Ruptura " + ("alcista" if d == "alcista" else "bajista"),
        "direction": d, "status": st, "key_level": _r(L),
        "breakout": _r(L), "invalidation": _r(L), "target": None,
        "priority": _PRIO["ruptura_conf"] if confirmed else _PRIO["ruptura_por_conf"],
        "text": (f"{'Ruptura' if d == 'alcista' else 'Perdida'} del nivel "
                 f"{_usd(_r(L))} ({lvl['touches']} toques), {st} — "
                 f"ese nivel ahora actua de {rol}"),
    }


def _pat_double_triple(highs, lows, closes, piv_h, piv_l, atr):
    """Doble/TRIPLE techo o suelo con los ultimos pivotes del lado."""
    if not atr:
        return None
    n = len(closes)

    def _check(pivs, is_top):
        if len(pivs) < 2:
            return None
        triple = False
        if len(pivs) >= 3:
            (i1, p1), (i2, p2), (i3, p3) = pivs[-3:]
            if (max(p1, p2, p3) - min(p1, p2, p3) <= 0.75 * atr
                    and i3 - i1 >= 20 and i2 - i1 >= 5 and i3 - i2 >= 5
                    and i3 >= n - 45):
                triple = True
        if triple:
            idx_a, idx_b = i1, i3
            level = (p1 + p2 + p3) / 3
            touch_pts = [(i1, p1), (i2, p2), (i3, p3)]
        else:
            (i1, p1), (i2, p2) = pivs[-2:]
            if abs(p1 - p2) > 0.6 * atr or (i2 - i1) < 12 or i2 < n - 45:
                return None
            idx_a, idx_b = i1, i2
            level = (p1 + p2) / 2
            touch_pts = [(i1, p1), (i2, p2)]
        seg = lows[idx_a:idx_b + 1] if is_top else highs[idx_a:idx_b + 1]
        neck = float(min(seg)) if is_top else float(max(seg))
        depth = (level - neck) if is_top else (neck - level)
        if depth < 1.5 * atr:
            return None
        c = closes[-1]
        base = "Triple" if triple else "Doble"
        if is_top:
            if c < neck - 3 * atr:          # figura vieja, ya jugo
                return None
            if c > level + 0.5 * atr:       # rompio el techo: figura anulada
                return None
            confirmed = c < neck
            d, nm = "bajista", f"{base} techo"
            target = neck - depth
            invalid = level + 0.5 * atr
        else:
            if c > neck + 3 * atr:
                return None
            if c < level - 0.5 * atr:
                return None
            confirmed = c > neck
            d, nm = "alcista", f"{base} suelo"
            target = neck + depth
            invalid = level - 0.5 * atr
        st = "confirmada" if confirmed else "en formacion"
        key = ("triple_conf" if triple else "doble_conf") if confirmed \
            else ("triple_form" if triple else "doble_form")
        if confirmed:
            txt = (f"{nm} en {_usd(_r(level))}, confirmado con cierre "
                   f"{'bajo' if is_top else 'sobre'} el neckline {_usd(_r(neck))} "
                   f"— objetivo medido {_usd(_r(target))}")
        else:
            txt = (f"{nm} en {_usd(_r(level))}, se confirma "
                   f"{'bajo' if is_top else 'sobre'} {_usd(_r(neck))} "
                   f"(proyectaria {_usd(_r(target))}); se anula "
                   f"{'sobre' if is_top else 'bajo'} {_usd(_r(invalid))}")
        off_a = int(n - 1 - idx_a)
        draw = {
            "segments": [
                # nivel de los techos/pisos tocados, a lo largo de la formacion
                {"x0": off_a, "y0": _r(level), "x1": 0, "y1": _r(level), "dash": True},
                # neckline (el nivel que confirma)
                {"x0": off_a, "y0": _r(neck), "x1": 0, "y1": _r(neck), "dash": False},
            ],
            "points": [{"x": int(n - 1 - i), "y": _r(pv), "label": "T",
                        "pos": "above" if is_top else "below"}
                       for i, pv in touch_pts],
        }
        return {"name": nm, "direction": d, "status": st,
                "key_level": _r(neck), "breakout": _r(neck),
                "invalidation": _r(invalid), "target": _r(target),
                "priority": _PRIO[key], "text": txt, "draw": draw}

    top = _check(piv_h, True)
    bot = _check(piv_l, False)
    if top and bot:
        return top if top["priority"] >= bot["priority"] else bot
    return top or bot


def _pat_hch(highs, lows, closes, piv_h, piv_l, atr):
    """Hombro-cabeza-hombro (y su inverso) con los 3 ultimos pivotes."""
    if not atr:
        return None
    n = len(closes)

    def _check(inverted):
        pivs = piv_l if inverted else piv_h
        rec = [(i, p) for i, p in pivs if i >= n - 140]
        if len(rec) < 3:
            return None
        (i1, p1), (i2, p2), (i3, p3) = rec[-3:]
        if i3 < n - 45 or i2 - i1 < 5 or i3 - i2 < 5:
            return None
        if not inverted:
            if not (p2 > p1 + 0.8 * atr and p2 > p3 + 0.8 * atr):
                return None
            if abs(p1 - p3) > 1.6 * atr:        # hombros muy desparejos
                return None
            v1 = float(min(lows[i1:i2 + 1]))
            v2 = float(min(lows[i2:i3 + 1]))
            neck = (v1 + v2) / 2
            depth = p2 - neck
            if depth < 2 * atr:
                return None
            c = closes[-1]
            if c < neck - 3 * atr:              # ya jugo
                return None
            if c > p3 + 0.5 * atr:              # supero el hombro derecho
                return None
            confirmed = c < neck
            d, nm = "bajista", "Hombro-cabeza-hombro"
            target = neck - depth
            invalid = p3 + 0.5 * atr
            head_txt = _usd(_r(p2))
        else:
            if not (p2 < p1 - 0.8 * atr and p2 < p3 - 0.8 * atr):
                return None
            if abs(p1 - p3) > 1.6 * atr:
                return None
            v1 = float(max(highs[i1:i2 + 1]))
            v2 = float(max(highs[i2:i3 + 1]))
            neck = (v1 + v2) / 2
            depth = neck - p2
            if depth < 2 * atr:
                return None
            c = closes[-1]
            if c > neck + 3 * atr:
                return None
            if c < p3 - 0.5 * atr:
                return None
            confirmed = c > neck
            d, nm = "alcista", "HCH invertido"
            target = neck + depth
            invalid = p3 - 0.5 * atr
            head_txt = _usd(_r(p2))
        st = "confirmada" if confirmed else "en formacion"
        if confirmed:
            txt = (f"{nm} (cabeza en {head_txt}) confirmado con cierre "
                   f"{'bajo' if not inverted else 'sobre'} el neckline "
                   f"{_usd(_r(neck))} — objetivo medido {_usd(_r(target))}")
        else:
            txt = (f"{nm} en formacion (cabeza en {head_txt}): se confirma "
                   f"{'bajo' if not inverted else 'sobre'} {_usd(_r(neck))} "
                   f"(proyectaria {_usd(_r(target))}); se anula "
                   f"{'sobre' if not inverted else 'bajo'} {_usd(_r(invalid))}")
        off1 = int(n - 1 - i1)
        draw = {
            "segments": [
                # neckline desde el hombro izquierdo hasta hoy
                {"x0": off1, "y0": _r(neck), "x1": 0, "y1": _r(neck), "dash": False},
            ],
            "points": [
                {"x": int(n - 1 - i1), "y": _r(p1), "label": "H",
                 "pos": "below" if inverted else "above"},
                {"x": int(n - 1 - i2), "y": _r(p2), "label": "C",
                 "pos": "below" if inverted else "above"},
                {"x": int(n - 1 - i3), "y": _r(p3), "label": "H",
                 "pos": "below" if inverted else "above"},
            ],
        }
        return {"name": nm, "direction": d, "status": st,
                "key_level": _r(neck), "breakout": _r(neck),
                "invalidation": _r(invalid), "target": _r(target),
                "priority": _PRIO["hch_conf" if confirmed else "hch_form"],
                "text": txt, "draw": draw}

    return _check(False) or _check(True)


def _pat_triangle(highs, lows, closes, piv_h, piv_l, atr):
    """Triangulo o cuña: rectas sobre pivotes H y L de ~90 ruedas convergiendo."""
    if not atr:
        return None
    n = len(closes)
    price = closes[-1]
    ph = [(i, p) for i, p in piv_h if i >= n - 90]
    pl = [(i, p) for i, p in piv_l if i >= n - 90]
    if len(ph) < 3 or len(pl) < 3:
        return None
    xh, yh = np.array([p[0] for p in ph], float), np.array([p[1] for p in ph])
    xl, yl = np.array([p[0] for p in pl], float), np.array([p[1] for p in pl])
    sh, ih = np.polyfit(xh, yh, 1)
    sl_, il = np.polyfit(xl, yl, 1)
    x0 = float(min(xh.min(), xl.min()))
    x1 = float(n - 1)
    spread0 = (ih + sh * x0) - (il + sl_ * x0)
    spread1 = (ih + sh * x1) - (il + sl_ * x1)
    if spread0 <= 0 or spread1 <= 0.3 * atr or spread1 > 0.72 * spread0:
        return None
    shp = sh / price * 100
    slp = sl_ / price * 100
    FLAT = 0.025
    res_now, sup_now = _r(ih + sh * x1), _r(il + sl_ * x1)
    if shp < -FLAT and slp > FLAT:
        nm, d = "Triangulo simetrico", "neutral"
        txt = (f"Triangulo simetrico: compresion entre {_usd(sup_now)} y "
               f"{_usd(res_now)} — la ruptura define la direccion")
    elif abs(shp) <= FLAT and slp > FLAT:
        nm, d = "Triangulo ascendente", "alcista"
        txt = (f"Triangulo ascendente: pisos crecientes contra resistencia "
               f"{_usd(res_now)} (sesgo de ruptura alcista)")
    elif shp < -FLAT and abs(slp) <= FLAT:
        nm, d = "Triangulo descendente", "bajista"
        txt = (f"Triangulo descendente: techos decrecientes sobre soporte "
               f"{_usd(sup_now)} (sesgo de quiebre bajista)")
    elif shp > FLAT and slp > FLAT:
        nm, d = "Cuña ascendente", "bajista"
        txt = (f"Cuña ascendente: sube en compresion — figura de agotamiento, "
               f"se activa bajo {_usd(sup_now)}")
    elif shp < -FLAT and slp < -FLAT:
        nm, d = "Cuña descendente", "alcista"
        txt = (f"Cuña descendente: cae en compresion — suele resolver al alza, "
               f"se activa sobre {_usd(res_now)}")
    else:
        return None

    margin = 0.25 * atr
    line_h_now, line_l_now = ih + sh * x1, il + sl_ * x1
    # geometria: cada recta arranca en SU primer pivote (extrapolar la recta de
    # un lado hasta el pivote mas viejo del otro la dibuja lejos de las velas)
    xh0, xl0 = float(xh.min()), float(xl.min())
    tri_draw = {"segments": [
        {"x0": int(n - 1 - xh0), "y0": _r(ih + sh * xh0), "x1": 0, "y1": _r(line_h_now), "dash": False},
        {"x0": int(n - 1 - xl0), "y0": _r(il + sl_ * xl0), "x1": 0, "y1": _r(line_l_now), "dash": False},
    ]}
    above = price > line_h_now + margin
    below = price < line_l_now - margin
    if above or below:
        # figura ROTA: reciente = señal direccional; vieja = ya jugo
        was_inside = False
        for back in range(2, 9):
            xb = n - back
            cb = closes[xb]
            if (il + sl_ * xb) - margin <= cb <= (ih + sh * xb) + margin:
                was_inside = True
                break
        if not was_inside:
            return None
        d2 = "alcista" if above else "bajista"
        lvl = float(line_h_now if above else line_l_now)
        target = lvl + spread0 if above else lvl - spread0
        return {"name": nm + " rota", "direction": d2, "status": "confirmada",
                "key_level": _r(lvl), "breakout": _r(lvl),
                "invalidation": _r(lvl), "target": _r(target),
                "priority": _PRIO["triangulo_roto"],
                "text": (f"{nm} rota {'al alza' if above else 'a la baja'}: "
                         f"el precio salio de la figura "
                         f"{'sobre' if above else 'bajo'} {_usd(_r(lvl))} "
                         f"— objetivo medido {_usd(_r(target))}"),
                "draw": tri_draw}

    if d == "alcista":
        breakout, invalid = res_now, sup_now
        target = _r(float(res_now) + spread0) if res_now else None
    elif d == "bajista":
        breakout, invalid = sup_now, res_now
        target = _r(float(sup_now) - spread0) if sup_now else None
    else:
        breakout, invalid, target = res_now, sup_now, None
    return {"name": nm, "direction": d, "status": "en formacion",
            "key_level": breakout, "breakout": breakout,
            "invalidation": invalid, "target": target,
            "priority": _PRIO["triangulo_form"], "text": txt, "draw": tri_draw}


def _pat_flag(highs, lows, closes, atr):
    """Bandera de continuacion: impulso fuerte (palo) + consolidacion corta."""
    n = len(closes)
    if n < 40 or not atr:
        return None
    best = None
    for end in range(max(12, n - 16), n - 3):
        for span in (5, 8, 12):
            s = end - span
            if s < 0:
                continue
            move = closes[end] - closes[s]
            if abs(move) >= 3.5 * atr and (best is None or abs(move) > abs(best[2])):
                best = (s, end, move)
    if best is None:
        return None
    s, e, move = best
    cons_len = n - 1 - e
    if cons_len < 3 or e + 1 >= n - 1:
        return None
    cons_h = float(max(highs[e + 1:n - 1]))
    cons_l = float(min(lows[e + 1:n - 1]))
    cons_range = cons_h - cons_l
    if cons_range <= 0 or cons_range > 0.55 * abs(move):
        return None
    c = closes[-1]
    up = move > 0
    if up:
        breakout, invalid = cons_h, cons_l - 0.5 * atr
        target = breakout + abs(move)
        if c < invalid:
            return None
        confirmed = c > breakout + 0.15 * atr
        nm, d = "Bandera alcista", "alcista"
    else:
        breakout, invalid = cons_l, cons_h + 0.5 * atr
        target = breakout - abs(move)
        if c > invalid:
            return None
        confirmed = c < breakout - 0.15 * atr
        nm, d = "Bandera bajista", "bajista"
    move_pct = abs(move) / closes[s] * 100 if closes[s] else 0
    st = "confirmada" if confirmed else "en formacion"
    if confirmed:
        txt = (f"{nm} rota {'al alza' if up else 'a la baja'} tras impulso de "
               f"{move_pct:.0f}% en {e - s} ruedas — objetivo medido {_usd(_r(target))}")
    else:
        txt = (f"{nm}: impulso de {move_pct:.0f}% en {e - s} ruedas y "
               f"consolidacion de {cons_len} ruedas; ruptura "
               f"{'sobre' if up else 'bajo'} {_usd(_r(breakout))} proyectaria "
               f"{_usd(_r(target))} (se anula {'bajo' if up else 'sobre'} "
               f"{_usd(_r(invalid))})")
    off_s, off_e = int(n - 1 - s), int(n - 1 - e)
    draw = {"segments": [
        # palo del impulso
        {"x0": off_s, "y0": _r(closes[s]), "x1": off_e, "y1": _r(closes[e]),
         "dash": False, "w": 2},
        # canal de consolidacion (la bandera)
        {"x0": off_e, "y0": _r(cons_h), "x1": 0, "y1": _r(cons_h), "dash": True},
        {"x0": off_e, "y0": _r(cons_l), "x1": 0, "y1": _r(cons_l), "dash": True},
    ]}
    return {"name": nm, "direction": d, "status": st,
            "key_level": _r(breakout), "breakout": _r(breakout),
            "invalidation": _r(invalid), "target": _r(target),
            "priority": _PRIO["bandera_conf" if confirmed else "bandera_form"],
            "text": txt, "draw": draw}


def _pat_ma_cross(sma50, sma200):
    """Cruce dorado / de la muerte en las ultimas 20 ruedas."""
    if sma50 is None or sma200 is None:
        return None
    a, b = np.asarray(sma50, float), np.asarray(sma200, float)
    if len(a) < 25 or len(b) < 25 or np.isnan(a[-25:]).any() or np.isnan(b[-25:]).any():
        return None
    diff = a - b
    for back in range(1, 21):
        if diff[-back] == 0:
            continue
        if np.sign(diff[-back]) != np.sign(diff[-1]):
            golden = diff[-1] > 0
            nm = "Cruce dorado" if golden else "Cruce de la muerte"
            return {
                "name": nm, "direction": "alcista" if golden else "bajista",
                "status": "confirmada", "key_level": None,
                "breakout": None, "invalidation": None, "target": None,
                "priority": _PRIO["cruce"],
                "text": (f"{nm} hace {back} ruedas (SMA50 "
                         f"{'sobre' if golden else 'bajo'} SMA200) — señal de "
                         f"tendencia de fondo {'alcista' if golden else 'bajista'}"),
            }
    return None


def _pat_divergence(piv_h, piv_l, rsi_series, n):
    """Divergencia RSI/precio entre los dos ultimos pivotes del lado activo."""
    if rsi_series is None:
        return None
    rsi = np.asarray(_sanitize(rsi_series), float)

    def _check(pivs, bearish):
        recent = [(i, p) for i, p in pivs if i >= n - 60]
        if len(recent) < 2:
            return None
        (i1, p1), (i2, p2) = recent[-2], recent[-1]
        if i2 < n - 25 or i1 >= len(rsi) or i2 >= len(rsi):
            return None
        r1, r2 = rsi[i1], rsi[i2]
        if np.isnan(r1) or np.isnan(r2):
            return None
        dv_draw = {"segments": [
            # recta uniendo los dos pivotes de precio que divergen del RSI
            {"x0": int(n - 1 - i1), "y0": _r(p1),
             "x1": int(n - 1 - i2), "y1": _r(p2), "dash": True},
        ]}
        if bearish and p2 > p1 and r2 < r1 - 2:
            return {"name": "Divergencia bajista", "direction": "bajista",
                    "status": "vigente", "key_level": None,
                    "breakout": None, "invalidation": None, "target": None,
                    "priority": _PRIO["divergencia"],
                    "text": ("Divergencia bajista: el precio hizo un maximo mas alto "
                             f"pero el RSI no acompaño ({r1:.0f} → {r2:.0f}) — "
                             "el impulso pierde fuerza"),
                    "draw": dv_draw}
        if not bearish and p2 < p1 and r2 > r1 + 2:
            return {"name": "Divergencia alcista", "direction": "alcista",
                    "status": "vigente", "key_level": None,
                    "breakout": None, "invalidation": None, "target": None,
                    "priority": _PRIO["divergencia"],
                    "text": ("Divergencia alcista: el precio hizo un minimo mas bajo "
                             f"pero el RSI subio ({r1:.0f} → {r2:.0f}) — "
                             "la caida pierde fuerza"),
                    "draw": dv_draw}
        return None

    return _check(piv_h, True) or _check(piv_l, False)


def structure(piv_h, piv_l):
    """Estructura de tendencia por pivotes: HH/HL, LH/LL o mixta."""
    def _dir(pivs):
        if len(pivs) < 2:
            return 0
        vals = [p for _, p in pivs[-3:]]
        ups = sum(1 for i in range(1, len(vals)) if vals[i] > vals[i - 1])
        downs = len(vals) - 1 - ups
        return 1 if ups > downs else (-1 if downs > ups else 0)

    dh, dl = _dir(piv_h), _dir(piv_l)
    if dh > 0 and dl >= 0:
        return 1, "maximos y minimos crecientes"
    if dh <= 0 and dl < 0:
        return -1, "maximos y minimos decrecientes"
    return 0, "estructura mixta (sin tendencia clara de pivotes)"


def _pat_channel_fallback(closes, struct_txt, lookback=120):
    """Canal de regresion / estructura como figura por defecto."""
    arr = np.asarray(closes[-lookback:], float)
    n = len(arr)
    if n < 40:
        return {"name": "Estructura", "direction": "neutral", "status": "vigente",
                "key_level": None, "breakout": None, "invalidation": None,
                "target": None, "priority": _PRIO["estructura"],
                "text": struct_txt.capitalize()}
    xs = np.arange(n, dtype=float)
    slope, intercept = np.polyfit(xs, arr, 1)
    fit = intercept + slope * xs
    std = float(np.std(arr - fit))
    top, bot = fit[-1] + 2 * std, fit[-1] - 2 * std
    price = float(arr[-1])
    width = top - bot
    pos = (price - bot) / width if width > 0 else 0.5
    my = float(np.mean(arr))
    sp = slope / my * 100 if my else 0.0
    meses = max(1, round(n / 21))
    if sp >= 0.04:
        nm, d = "Canal alcista", "alcista"
    elif sp <= -0.04:
        nm, d = "Canal bajista", "bajista"
    else:
        nm, d = "Rango lateral", "neutral"
    if pos >= 0.8:
        ptxt = "pegado al techo del canal"
    elif pos <= 0.2:
        ptxt = "apoyado en el piso del canal"
    elif pos >= 0.5:
        ptxt = "en la mitad superior del canal"
    else:
        ptxt = "en la mitad inferior del canal"
    return {"name": nm, "direction": d, "status": "vigente",
            "key_level": _r(bot if d == "alcista" else top),
            "breakout": None, "invalidation": None, "target": None,
            "priority": _PRIO["canal"],
            "text": f"{nm} de ~{meses} meses, precio {ptxt}; {struct_txt}"}


# ══════════════════════════════════════════════════════════════
#  FIBONACCI (contexto del ultimo impulso relevante)
# ══════════════════════════════════════════════════════════════

_FIB_RATIOS = [("23.6", 0.236), ("38.2", 0.382), ("50", 0.5),
               ("61.8", 0.618), ("78.6", 0.786)]


def fibonacci(highs, lows, closes, atr, lookback=180):
    """Retrocesos y extensiones del impulso dominante de las ultimas ~180 ruedas.

    Devuelve None si no hay un impulso relevante (>4·ATR), si es prehistorico,
    o si ya retrocedio entero (impulso negado)."""
    n = len(closes)
    if n < 60 or not atr:
        return None
    w = min(n, lookback)
    hs, ls = highs[n - w:], lows[n - w:]
    hi_rel = max(range(w), key=lambda i: hs[i])
    lo_rel = min(range(w), key=lambda i: ls[i])
    hi, lo = float(hs[hi_rel]), float(ls[lo_rel])
    rng = hi - lo
    if rng < 4 * atr:
        return None
    if max(hi_rel, lo_rel) < w - 120:
        return None                      # impulso demasiado viejo
    up = lo_rel < hi_rel                 # el minimo vino primero: impulso alcista
    price = float(closes[-1])
    if up:
        levels = {k: hi - r * rng for k, r in _FIB_RATIOS}
        ext = {"127.2": lo + 1.272 * rng, "161.8": lo + 1.618 * rng}
        retr = (hi - price) / rng
    else:
        levels = {k: lo + r * rng for k, r in _FIB_RATIOS}
        ext = {"127.2": hi - 1.272 * rng, "161.8": hi - 1.618 * rng}
        retr = (price - lo) / rng
    if retr > 1.05:
        return None                      # retrocedio todo: el impulso quedo negado
    tol = max(0.45 * atr, price * 0.004)
    at = next((k for k, v in levels.items() if abs(price - v) <= tol), None)
    dirw = "alcista" if up else "bajista"
    if retr < -0.02:
        pos_txt = ("extendiendo por encima del maximo del impulso" if up
                   else "extendiendo por debajo del minimo del impulso")
    else:
        pos_txt = f"retrocedio el {max(0.0, retr) * 100:.0f}% del impulso"
    txt = (f"impulso {dirw} {_usd(_r(lo))}→{_usd(_r(hi))}: {pos_txt}"
           + (f", apoyado en el nivel fib {at}% ({_usd(_r(levels[at]))})" if at else ""))
    return {
        "dir": dirw,
        "high": _r(hi), "low": _r(lo),
        "levels": {k: _r(v) for k, v in levels.items()},
        "ext": {k: _r(v) for k, v in ext.items()},
        "retr_pct": _r(retr * 100, 1),
        "at": at,
        "text": txt,
    }


# ══════════════════════════════════════════════════════════════
#  API PRINCIPAL
# ══════════════════════════════════════════════════════════════

def detect(highs, lows, closes, rsi=None, sma50=None, sma200=None,
           include_cross=False, include_fallback=False):
    """Corre todos los detectores y elige la figura dominante.

    Returns {"pattern": dict|None, "candidates": [dicts], "fibonacci": dict|None,
             "structure": (dir, texto)}."""
    highs = _sanitize(highs)
    lows = _sanitize(lows)
    closes = _sanitize(closes)
    n = len(closes)
    empty = {"pattern": None, "candidates": [], "fibonacci": None,
             "structure": (0, "sin datos suficientes")}
    if n < 60 or len(highs) != n or len(lows) != n:
        return empty
    price = closes[-1]
    atr = _atr_arr(highs, lows, closes)
    if not atr or atr <= 0:
        atr = price * 0.02 if price else None
    if not atr:
        return empty

    piv_h, piv_l = _pivots_arr(highs, lows)
    tol = max(0.5 * atr, price * 0.005)
    all_levels = _cluster_levels(piv_h + piv_l, tol)
    struct_dir, struct_txt = structure(piv_h, piv_l)

    cands = [
        _pat_breakout(closes, all_levels, atr),
        _pat_double_triple(highs, lows, closes, piv_h, piv_l, atr),
        _pat_hch(highs, lows, closes, piv_h, piv_l, atr),
        _pat_triangle(highs, lows, closes, piv_h, piv_l, atr),
        _pat_flag(highs, lows, closes, atr),
        _pat_divergence(piv_h, piv_l, rsi, n),
    ]
    if include_cross:
        cands.append(_pat_ma_cross(sma50, sma200))
    if include_fallback:
        cands.append(_pat_channel_fallback(closes, struct_txt))
    cands = sorted([c for c in cands if c], key=lambda c: -c["priority"])

    main = None
    if cands:
        main = dict(cands[0])
        secondary = next((c for c in cands[1:] if c["priority"] >= 45), None)
        if secondary:
            main["secondary"] = secondary["text"]

    fib = None
    try:
        fib = fibonacci(highs, lows, closes, atr)
    except Exception:
        fib = None

    return {"pattern": main, "candidates": cands, "fibonacci": fib,
            "structure": (struct_dir, struct_txt)}


def attach_to_analysis(sig):
    """Adjunta sig["pattern"] y sig["fib"] desde sig["chart"] (muta y devuelve).

    Excluye cruce de MAs (la tesis ya lo cuenta) y el fallback de canal (los
    niveles ya usan el canal): solo figuras reales — pattern=None si no hay."""
    if not isinstance(sig, dict):
        return sig
    try:
        chart = sig.get("chart") or {}
        ohlc = chart.get("ohlc") or []
        if len(ohlc) < 60:
            sig["pattern"] = None
            sig["fib"] = None
            return sig
        highs = [b.get("high") for b in ohlc]
        lows = [b.get("low") for b in ohlc]
        closes = [b.get("close") for b in ohlc]
        rsi = chart.get("rsi") or None
        res = detect(highs, lows, closes, rsi=rsi)
        sig["pattern"] = res["pattern"]
        sig["fib"] = res["fibonacci"]
    except Exception:
        sig["pattern"] = None
        sig["fib"] = None
    return sig


# ══════════════════════════════════════════════════════════════
#  VALIDACION HISTORICA (para /api/calibration)
# ══════════════════════════════════════════════════════════════

def validate_history(highs, lows, closes, step=3, horizon=40, warmup=120):
    """Recorre el historial detectando figuras CONFIRMADAS con objetivo medido
    y mide el resultado: ¿el precio alcanzo el target antes que la invalidacion
    dentro de `horizon` ruedas? Devuelve lista de eventos."""
    highs = _sanitize(highs)
    lows = _sanitize(lows)
    closes = _sanitize(closes)
    n = len(closes)
    events = []
    last_seen = {}   # name -> (key_level, idx) para no contar el mismo evento 2 veces
    for i in range(warmup, n - horizon, step):
        res = detect(highs[:i + 1], lows[:i + 1], closes[:i + 1])
        p = res["pattern"]
        if (not p or p.get("status") != "confirmada"
                or not p.get("target") or not p.get("invalidation")):
            continue
        atr = _atr_arr(highs[:i + 1], lows[:i + 1], closes[:i + 1]) or 0
        key = p["name"]
        prev = last_seen.get(key)
        klvl = p.get("key_level") or 0
        if prev and abs(prev[0] - klvl) <= max(atr, 1e-9) and i - prev[1] <= 30:
            continue
        last_seen[key] = (klvl, i)
        up = p["direction"] == "alcista"
        hit = inv = None
        for j in range(i + 1, min(n, i + 1 + horizon)):
            if up:
                if highs[j] >= p["target"]:
                    hit = j
                    break
                if lows[j] <= p["invalidation"]:
                    inv = j
                    break
            else:
                if lows[j] <= p["target"]:
                    hit = j
                    break
                if highs[j] >= p["invalidation"]:
                    inv = j
                    break
        end = hit or inv
        events.append({"type": p["name"], "direction": p["direction"],
                       "hit": hit is not None,
                       "resolved": end is not None,
                       "bars": (end - i) if end else horizon})
    return events


def validate_universe(ohlc_by_symbol):
    """Agrega validate_history sobre {symbol: df_OHLCV} → stats por tipo de
    figura: {type: {n, hits, hit_rate, unresolved, avg_bars}}."""
    all_events = []
    for sym, df in ohlc_by_symbol.items():
        try:
            highs = df["high"].tolist()
            lows = df["low"].tolist()
            closes = df["close"].tolist()
            all_events.extend(validate_history(highs, lows, closes))
        except Exception:
            continue
    by_type = {}
    for e in all_events:
        t = by_type.setdefault(e["type"], {"n": 0, "hits": 0, "unresolved": 0,
                                           "bars_sum": 0})
        t["n"] += 1
        if e["hit"]:
            t["hits"] += 1
        if not e["resolved"]:
            t["unresolved"] += 1
        t["bars_sum"] += e["bars"]
    out = {}
    for t, v in sorted(by_type.items(), key=lambda kv: -kv[1]["n"]):
        resolved = v["n"] - v["unresolved"]
        out[t] = {
            "n": v["n"],
            "hits": v["hits"],
            "hit_rate": round(v["hits"] / resolved * 100, 1) if resolved else None,
            "unresolved": v["unresolved"],
            "avg_bars": round(v["bars_sum"] / v["n"], 1) if v["n"] else None,
        }
    return out
