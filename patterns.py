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
        # Conviccion: romper un nivel de 2 toques es poco — exigir 3+ toques
        # (un nivel realmente defendido en el grafico global)
        if lvl["touches"] < 3:
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
    broke = "techo" if d == "alcista" else "piso"
    return {
        "name": ("Ruptura de techo" if d == "alcista" else "Perdida de piso"),
        "scale": "major", "direction": d, "status": st, "key_level": _r(L),
        "breakout": _r(L), "invalidation": _r(L), "target": None,
        "priority": _PRIO["ruptura_conf"] if confirmed else _PRIO["ruptura_por_conf"],
        "text": (f"{'Ruptura del' if d == 'alcista' else 'Perdida del'} {broke} "
                 f"{_usd(_r(L))} ({lvl['touches']} swings mayores lo tocaron), {st} — "
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
                    and i3 >= n - 70):
                triple = True
        if triple:
            idx_a, idx_b = i1, i3
            level = (p1 + p2 + p3) / 3
            touch_pts = [(i1, p1), (i2, p2), (i3, p3)]
        else:
            (i1, p1), (i2, p2) = pivs[-2:]
            if abs(p1 - p2) > 0.6 * atr or (i2 - i1) < 12 or i2 < n - 70:
                return None
            idx_a, idx_b = i1, i2
            level = (p1 + p2) / 2
            touch_pts = [(i1, p1), (i2, p2)]
        seg = lows[idx_a:idx_b + 1] if is_top else highs[idx_a:idx_b + 1]
        neck = float(min(seg)) if is_top else float(max(seg))
        depth = (level - neck) if is_top else (neck - level)
        # Conviccion: solo estructuras PROMINENTES del grafico global — la
        # figura debe medir >= max(2.5·ATR, 3.5% del precio)
        if depth < max(2.5 * atr, 0.035 * (closes[-1] or 1)):
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
                {"x0": off_a, "y0": _r(level), "x1": 0, "y1": _r(level), "dash": True,
                 "lbl": ("Techos" if is_top else "Pisos") + f" tocados ({len(touch_pts)})"},
                # neckline (el nivel que confirma)
                {"x0": off_a, "y0": _r(neck), "x1": 0, "y1": _r(neck), "dash": False,
                 "lbl": f"Neckline del {nm.lower()}"},
            ],
            "points": [{"x": int(n - 1 - i), "y": _r(pv), "label": "T",
                        "pos": "above" if is_top else "below"}
                       for i, pv in touch_pts],
        }
        return {"name": nm, "scale": "major", "direction": d, "status": st,
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
        rec = [(i, p) for i, p in pivs if i >= n - 220]
        if len(rec) < 3:
            return None
        (i1, p1), (i2, p2), (i3, p3) = rec[-3:]
        if i3 < n - 70 or i2 - i1 < 5 or i3 - i2 < 5:
            return None
        if not inverted:
            if not (p2 > p1 + 1.2 * atr and p2 > p3 + 1.2 * atr):
                return None
            if abs(p1 - p3) > 1.6 * atr:        # hombros muy desparejos
                return None
            seg1, seg2 = lows[i1:i2 + 1], lows[i2:i3 + 1]
            v1, v2 = float(min(seg1)), float(min(seg2))
            i_v1, i_v2 = i1 + seg1.index(min(seg1)), i2 + seg2.index(min(seg2))
            neck = (v1 + v2) / 2
            depth = p2 - neck
            if depth < max(3 * atr, 0.05 * (closes[-1] or 1)):
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
            if not (p2 < p1 - 1.2 * atr and p2 < p3 - 1.2 * atr):
                return None
            if abs(p1 - p3) > 1.6 * atr:
                return None
            seg1, seg2 = highs[i1:i2 + 1], highs[i2:i3 + 1]
            v1, v2 = float(max(seg1)), float(max(seg2))
            i_v1, i_v2 = i1 + seg1.index(max(seg1)), i2 + seg2.index(max(seg2))
            neck = (v1 + v2) / 2
            depth = neck - p2
            if depth < max(3 * atr, 0.05 * (closes[-1] or 1)):
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
        # Neckline como la traza un chartista (estilo investing.com): la recta
        # que une los DOS VALLES reales, extendida hasta hoy — no un promedio plano
        if i_v2 > i_v1:
            neck_slope = (v2 - v1) / (i_v2 - i_v1)
            neck_now = v2 + neck_slope * ((n - 1) - i_v2)
        else:
            neck_now = v2
        draw = {
            "segments": [
                {"x0": int(n - 1 - i_v1), "y0": _r(v1), "x1": 0, "y1": _r(neck_now),
                 "dash": False, "w": 2, "lbl": f"Neckline del {nm} (une los dos valles)"},
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
        return {"name": nm, "scale": "major", "direction": d, "status": st,
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
    ph = [(i, p) for i, p in piv_h if i >= n - 160]
    pl = [(i, p) for i, p in piv_l if i >= n - 160]
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
    # Conviccion: la figura debe nacer con altura >= max(4·ATR, 6% del precio)
    if spread0 < max(4 * atr, 0.06 * price):
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
        {"x0": int(n - 1 - xh0), "y0": _r(ih + sh * xh0), "x1": 0, "y1": _r(line_h_now),
         "dash": False, "lbl": f"Techo de la figura ({nm.lower()})"},
        {"x0": int(n - 1 - xl0), "y0": _r(il + sl_ * xl0), "x1": 0, "y1": _r(line_l_now),
         "dash": False, "lbl": f"Piso de la figura ({nm.lower()})"},
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
        return {"name": nm + " rota", "scale": "major", "direction": d2, "status": "confirmada",
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
    return {"name": nm, "scale": "major", "direction": d, "status": "en formacion",
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
            if (abs(move) >= max(5 * atr, 0.08 * (closes[-1] or 1))
                    and (best is None or abs(move) > abs(best[2]))):
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
         "dash": False, "w": 2,
         "lbl": f"Palo del impulso ({move_pct:.0f}% en {e - s} ruedas)"},
        # canal de consolidacion (la bandera)
        {"x0": off_e, "y0": _r(cons_h), "x1": 0, "y1": _r(cons_h), "dash": True,
         "lbl": "Techo de la consolidacion"},
        {"x0": off_e, "y0": _r(cons_l), "x1": 0, "y1": _r(cons_l), "dash": True,
         "lbl": "Piso de la consolidacion"},
    ]}
    return {"name": nm, "scale": "short", "direction": d, "status": st,
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
                "name": nm, "scale": "major", "direction": "alcista" if golden else "bajista",
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
        recent = [(i, p) for i, p in pivs if i >= n - 90]
        if len(recent) < 2:
            return None
        (i1, p1), (i2, p2) = recent[-2], recent[-1]
        if i2 < n - 40 or i1 >= len(rsi) or i2 >= len(rsi):
            return None
        r1, r2 = rsi[i1], rsi[i2]
        if np.isnan(r1) or np.isnan(r2):
            return None
        dv_draw = {"segments": [
            # recta uniendo los dos pivotes de precio que divergen del RSI
            {"x0": int(n - 1 - i1), "y0": _r(p1),
             "x1": int(n - 1 - i2), "y1": _r(p2), "dash": True,
             "lbl": "Pivotes que divergen del RSI"},
        ]}
        if bearish and p2 > p1 and r2 < r1 - 5:
            return {"name": "Divergencia bajista", "scale": "short", "direction": "bajista",
                    "status": "vigente", "key_level": None,
                    "breakout": None, "invalidation": None, "target": None,
                    "priority": _PRIO["divergencia"],
                    "text": ("Divergencia bajista: el precio hizo un maximo mas alto "
                             f"pero el RSI no acompaño ({r1:.0f} → {r2:.0f}) — "
                             "el impulso pierde fuerza"),
                    "draw": dv_draw}
        if not bearish and p2 < p1 and r2 > r1 + 5:
            return {"name": "Divergencia alcista", "scale": "short", "direction": "alcista",
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
        return {"name": "Estructura", "scale": "major", "direction": "neutral", "status": "vigente",
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
    return {"name": nm, "scale": "major", "direction": d, "status": "vigente",
            "key_level": _r(bot if d == "alcista" else top),
            "breakout": None, "invalidation": None, "target": None,
            "priority": _PRIO["canal"],
            "text": f"{nm} de ~{meses} meses, precio {ptxt}; {struct_txt}"}


# ══════════════════════════════════════════════════════════════
#  FIBONACCI (contexto del ultimo impulso relevante)
# ══════════════════════════════════════════════════════════════

_FIB_RATIOS = [("23.6", 0.236), ("38.2", 0.382), ("50", 0.5),
               ("61.8", 0.618), ("78.6", 0.786)]


def _is_critical(p, price, atr):
    """¿La figura esta en su PUNTO DE DECISION? Las figuras solo son relevantes
    para operar cuando el precio esta cerca de resolverlas:
      - confirmada / por confirmar: el evento es AHORA (los detectores ya
        descartan figuras viejas o jugadas),
      - vigente (divergencia, cruce): por construccion son recientes,
      - en formacion: solo si el precio esta a <=1.5·ATR de la ruptura o de la
        anulacion — un triangulo a mitad de camino del vertice es contexto,
        no decision, y no debe ensuciar analisis ni graficos."""
    if not p:
        return False
    if p.get("status") in ("confirmada", "por confirmar", "vigente"):
        return True
    for lvl in (p.get("breakout"), p.get("invalidation")):
        try:
            if lvl is not None and abs(price - float(lvl)) <= 1.5 * atr:
                return True
        except (TypeError, ValueError):
            pass
    return False


def _zigzag(highs, lows, min_swing, window=5):
    """Pivotes MAYORES alternados (zigzag clasico): [[idx, precio, 'H'|'L']].

    Semilla: se trackean el mejor maximo y el mejor minimo hasta que su
    distancia supera `min_swing` (asi una tendencia monotona tras un lateral
    ancla en el extremo REAL de origen, no en un pivote menor arbitrario).
    Despues, giro confirmado solo cuando el swing contrario >= min_swing;
    extremos del mismo lado se reemplazan por el mas extremo."""
    n = len(highs)
    piv_h, piv_l = _pivots_arr(highs, lows, lookback=n, window=window)
    pts = sorted([(i, p, "H") for i, p in piv_h] + [(i, p, "L") for i, p in piv_l])
    zig = []
    best_h = best_l = None
    for i, p, t in pts:
        if not zig:
            if t == "H" and (best_h is None or p >= best_h[1]):
                best_h = [i, p, "H"]
            if t == "L" and (best_l is None or p <= best_l[1]):
                best_l = [i, p, "L"]
            if best_h and best_l and (best_h[1] - best_l[1]) >= min_swing:
                zig = sorted([best_h, best_l], key=lambda z: z[0])
            continue
        li, lp, lt = zig[-1]
        if t == lt:
            if (t == "H" and p > lp) or (t == "L" and p < lp):
                zig[-1] = [i, p, t]
        elif abs(p - lp) >= min_swing:
            zig.append([i, p, t])
    return zig


def fibonacci(highs, lows, closes, atr, lookback=250, dates=None):
    """Retrocesos y extensiones del SWING DOMINANTE reciente.

    Regla (simple, deterministica y auditable a ojo en el chart): el fib se
    traza entre el MAXIMO y el MINIMO del grafico reciente, en el orden en que
    ocurrieron. La ventana arranca en `lookback` (~1 año, la vista por defecto)
    y se AGRANDA mientras alguno de los dos extremos caiga pegado al borde
    izquierdo: si el maximo esta en el borde, el impulso real empezo antes y
    recortarlo ahi anclaria a mitad de una tendencia (bug TEAM/USO/SLV — el
    zigzag fragmentaba el movimiento y elegia un tramo interno, dejando afuera
    el techo que se ve a simple vista).

    Devuelve None si el swing no es relevante (< max(4·ATR, 8% del precio)), si
    el extremo final ya es viejo (>150 ruedas: el retroceso dejo de mandar) o
    si el precio ya retrocedio todo el impulso (queda negado)."""
    n = len(closes)
    if n < 60 or not atr:
        return None
    price = float(closes[-1])
    if price <= 0:
        return None

    w = min(n, max(60, lookback))
    i_hi = i_lo = 0
    while True:
        hs, ls = highs[n - w:], lows[n - w:]
        i_hi = max(range(w), key=lambda i: hs[i])
        i_lo = min(range(w), key=lambda i: ls[i])
        # Extremo pegado al borde izquierdo => el impulso viene de mas atras
        if min(i_hi, i_lo) > 2 or w >= n:
            break
        w = min(n, int(w * 1.5) + 5)

    hi, lo = float(hs[i_hi]), float(ls[i_lo])
    rng = hi - lo
    if rng < max(4 * atr, 0.08 * price):
        return None                      # swing chico: no es estructura

    term = max(i_hi, i_lo)               # extremo que cierra el impulso
    if (w - 1 - term) > 150:
        return None                      # impulso viejo: su retroceso ya no manda

    up = i_lo < i_hi                     # el minimo vino primero: impulso alcista
    hi_abs = (n - w) + i_hi
    lo_abs = (n - w) + i_lo
    if up:
        levels = {k: hi - r * rng for k, r in _FIB_RATIOS}
        ext = {"127.2": lo + 1.272 * rng, "161.8": lo + 1.618 * rng}
        retr = (hi - price) / rng
    else:
        levels = {k: lo + r * rng for k, r in _FIB_RATIOS}
        ext = {"127.2": hi - 1.272 * rng, "161.8": hi - 1.618 * rng}
        retr = (price - lo) / rng
    if retr > 1.05 or retr < -0.35:
        return None                      # impulso negado o precio muy extendido

    tol = max(0.45 * atr, price * 0.004)
    at = next((k for k, v in levels.items() if abs(price - v) <= tol), None)
    near = min(abs(price - v) for v in list(levels.values()) + list(ext.values()))
    relevant = (at is not None) or (near <= 1.2 * atr)

    dirw = "alcista" if up else "bajista"
    if retr < -0.02:
        pos_txt = ("extendiendo por encima del maximo del impulso" if up
                   else "extendiendo por debajo del minimo del impulso")
    else:
        pos_txt = f"retrocedio el {max(0.0, retr) * 100:.0f}% del impulso"

    def _dt(idx):
        try:
            s = str(dates[idx])[:10].replace("-", "")
            return f"{s[6:8]}-{s[4:6]}-{s[:4]}" if len(s) >= 8 and s[:8].isdigit() else str(dates[idx])[:10]
        except Exception:
            return None
    d_start = _dt(lo_abs if up else hi_abs) if dates else None
    d_end = _dt(hi_abs if up else lo_abs) if dates else None
    if up:
        anchor_txt = (f"del piso {_usd(_r(lo))}" + (f" ({d_start})" if d_start else "")
                      + f" al techo {_usd(_r(hi))}" + (f" ({d_end})" if d_end else ""))
    else:
        anchor_txt = (f"del techo {_usd(_r(hi))}" + (f" ({d_start})" if d_start else "")
                      + f" al piso {_usd(_r(lo))}" + (f" ({d_end})" if d_end else ""))
    txt = (f"impulso {dirw} {anchor_txt}: {pos_txt}"
           + (f", apoyado en el nivel fib {at}% ({_usd(_r(levels[at]))})" if at else ""))

    start_abs = lo_abs if up else hi_abs      # origen del impulso (100%)
    end_abs = hi_abs if up else lo_abs        # extremo final (0%)
    return {
        "dir": dirw,
        "high": _r(hi), "low": _r(lo),
        "levels": {k: _r(v) for k, v in levels.items()},
        "ext": {k: _r(v) for k, v in ext.items()},
        "retr_pct": _r(retr * 100, 1),
        "at": at,
        "relevant": bool(relevant),
        "start_off": int(n - 1 - start_abs),
        "end_off": int(n - 1 - end_abs),
        "text": txt,
    }


# ══════════════════════════════════════════════════════════════
#  PATRONES DE VELAS (timing de entrada de corto plazo, sobre el diario)
# ══════════════════════════════════════════════════════════════

_CANDLE_MEANING = {
    "Martillo": "tras la caida los vendedores empujaron fuerte, pero el cierre volvio arriba: los compradores defendieron la zona — posible piso",
    "Hombre colgado": "tras la subida aparecio venta agresiva intradia aunque el cierre aguanto — primer aviso de techo",
    "Martillo invertido": "los compradores intentaron revertir la caida; vale si el cierre siguiente lo confirma",
    "Estrella fugaz": "el precio marco maximos pero cerro abajo: rechazo del nivel — posible techo",
    "Envolvente alcista": "el cuerpo verde engulle a toda la vela roja previa: los compradores absorbieron la venta",
    "Envolvente bajista": "el cuerpo rojo engulle a toda la vela verde previa: los vendedores tomaron el control",
    "Estrella de la mañana": "caida → indecision → recuperacion fuerte: giro de piso clasico en 3 velas",
    "Estrella de la tarde": "subida → indecision → caida fuerte: giro de techo clasico en 3 velas",
    "Linea penetrante": "la vela verde recupero mas de la mitad del cuerpo rojo previo: demanda apareciendo",
    "Nube oscura": "la vela roja borro mas de la mitad del cuerpo verde previo: oferta apareciendo",
    "Harami alcista": "cuerpo chico dentro de la vela roja grande: la presion vendedora se seco",
    "Harami bajista": "cuerpo chico dentro de la vela verde grande: la presion compradora se seco",
    "Pinzas de piso": "dos minimos casi identicos: el mercado rechazo dos veces el mismo precio",
    "Pinzas de techo": "dos maximos casi identicos: doble rechazo del mismo nivel",
    "Tres soldados blancos": "tres cuerpos verdes consecutivos con cierres crecientes: demanda sostenida",
    "Tres cuervos negros": "tres cuerpos rojos consecutivos con cierres decrecientes: distribucion sostenida",
    "Doji": "apertura y cierre casi iguales tras la tendencia: equilibrio — el proximo cierre define",
    "Marubozu alcista": "cuerpo pleno casi sin mechas: conviccion compradora de punta a punta",
    "Marubozu bajista": "cuerpo pleno casi sin mechas: conviccion vendedora de punta a punta",
}


def detect_candles(opens, highs, lows, closes, atr=None, lookback=10, max_out=2):
    """Patrones de VELAS japonesas sobre las ultimas `lookback` ruedas del
    grafico DIARIO: reversion (martillo, envolvente, estrella de la mañana/
    tarde, penetrante, pinzas, harami), continuacion (tres soldados/cuervos,
    marubozu) e indecision (doji). Son señales de TIMING de corto plazo para
    ajustar la entrada — no pesan en score ni veredicto.

    Confirmacion: un patron direccional queda "confirmada" si el cierre
    SIGUIENTE avanza en su direccion; si el cierre siguiente lo niega
    (cierra mas alla del extremo opuesto del patron) se descarta; la vela de
    hoy queda "por confirmar". Patrones viejos sin confirmar (>2 ruedas) se
    descartan — el timing caduca rapido.

    Cada item: {name, short, direction, kind, off, status, text}
    ordenados del mas reciente al mas viejo, maximo `max_out`."""
    opens = _sanitize(opens)
    highs = _sanitize(highs)
    lows = _sanitize(lows)
    closes = _sanitize(closes)
    n = len(closes)
    if n < 30 or len(opens) != n or len(highs) != n or len(lows) != n:
        return []
    if not atr:
        atr = _atr_arr(highs, lows, closes) or (closes[-1] * 0.02 if closes[-1] else None)
    if not atr or atr <= 0:
        return []

    def body(i):
        return abs(closes[i] - opens[i])

    def rng(i):
        return highs[i] - lows[i]

    def upper(i):
        return highs[i] - max(opens[i], closes[i])

    def lower(i):
        return min(opens[i], closes[i]) - lows[i]

    def bull(i):
        return closes[i] > opens[i]

    def bear(i):
        return closes[i] < opens[i]

    def drift(i, k=5):
        """Deriva del precio en las ~k ruedas previas a i (incluida i)."""
        j = max(0, i - k)
        return closes[i] - closes[j]

    def _engulf(i):
        return (max(opens[i], closes[i]) >= max(opens[i - 1], closes[i - 1])
                and min(opens[i], closes[i]) <= min(opens[i - 1], closes[i - 1]))

    def _at(i):
        """Mejor patron que TERMINA en la vela i (o None). Tuplas
        (name, short, direction, kind, prio)."""
        if i < 8:
            return None
        b, r = body(i), rng(i)
        if r <= 0:
            return None
        b1 = body(i - 1)
        cands = []
        # Conviccion: la tendencia previa debe ser CLARA (>=1.2 ATR de deriva)
        pre_dn1, pre_up1 = drift(i - 1) <= -1.2 * atr, drift(i - 1) >= 1.2 * atr
        pre_dn2, pre_up2 = drift(i - 2) <= -1.2 * atr, drift(i - 2) >= 1.2 * atr
        pre_dn3, pre_up3 = drift(i - 3) <= -1.2 * atr, drift(i - 3) >= 1.2 * atr

        # ── 3 velas ──
        b2 = body(i - 2)
        mid2 = (opens[i - 2] + closes[i - 2]) / 2
        if (b2 >= 1.2 * atr and bear(i - 2) and b1 <= 0.4 * b2
                and bull(i) and closes[i] >= mid2 and pre_dn3):
            cands.append(("Estrella de la mañana", "Estr. mañana", "alcista", "reversion", 86, 3))
        if (b2 >= 1.2 * atr and bull(i - 2) and b1 <= 0.4 * b2
                and bear(i) and closes[i] <= mid2 and pre_up3):
            cands.append(("Estrella de la tarde", "Estr. tarde", "bajista", "reversion", 86, 3))
        if (all(bull(i - k) for k in (0, 1, 2))
                and all(body(i - k) >= 0.8 * atr for k in (0, 1, 2))
                and closes[i] > closes[i - 1] > closes[i - 2]
                and upper(i) <= 0.35 * max(b, 1e-9)):
            cands.append(("Tres soldados blancos", "3 soldados", "alcista", "continuacion", 80, 3))
        if (all(bear(i - k) for k in (0, 1, 2))
                and all(body(i - k) >= 0.8 * atr for k in (0, 1, 2))
                and closes[i] < closes[i - 1] < closes[i - 2]
                and lower(i) <= 0.35 * max(b, 1e-9)):
            cands.append(("Tres cuervos negros", "3 cuervos", "bajista", "continuacion", 80, 3))

        # ── 2 velas ──
        if b1 >= 0.5 * atr and b >= 1.15 * b1 and _engulf(i):
            if bull(i) and bear(i - 1) and pre_dn2:
                cands.append(("Envolvente alcista", "Envolvente", "alcista", "reversion", 84, 2))
            if bear(i) and bull(i - 1) and pre_up2:
                cands.append(("Envolvente bajista", "Envolvente", "bajista", "reversion", 84, 2))
        mid1 = (opens[i - 1] + closes[i - 1]) / 2
        if (bear(i - 1) and b1 >= 1.0 * atr and bull(i) and opens[i] <= closes[i - 1]
                and mid1 <= closes[i] < opens[i - 1] and pre_dn2):
            cands.append(("Linea penetrante", "Penetrante", "alcista", "reversion", 70, 2))
        if (bull(i - 1) and b1 >= 1.0 * atr and bear(i) and opens[i] >= closes[i - 1]
                and opens[i - 1] < closes[i] <= mid1 and pre_up2):
            cands.append(("Nube oscura", "Nube oscura", "bajista", "reversion", 70, 2))
        if (b1 >= 1.4 * atr and b <= 0.5 * b1
                and max(opens[i], closes[i]) <= max(opens[i - 1], closes[i - 1])
                and min(opens[i], closes[i]) >= min(opens[i - 1], closes[i - 1])):
            if bear(i - 1) and pre_dn2:
                cands.append(("Harami alcista", "Harami", "alcista", "reversion", 62, 2))
            elif bull(i - 1) and pre_up2:
                cands.append(("Harami bajista", "Harami", "bajista", "reversion", 62, 2))
        if (abs(lows[i] - lows[i - 1]) <= 0.10 * atr and pre_dn2
                and min(lower(i), lower(i - 1)) >= 0.4 * atr):
            cands.append(("Pinzas de piso", "Pinzas", "alcista", "reversion", 60, 2))
        if (abs(highs[i] - highs[i - 1]) <= 0.10 * atr and pre_up2
                and min(upper(i), upper(i - 1)) >= 0.4 * atr):
            cands.append(("Pinzas de techo", "Pinzas", "bajista", "reversion", 60, 2))

        # ── 1 vela ──
        if r >= 0.8 * atr:
            small = b <= 0.35 * r
            if small and lower(i) >= 2 * max(b, 0.05 * r) and upper(i) <= 0.2 * r:
                if pre_dn1:
                    cands.append(("Martillo", "Martillo", "alcista", "reversion", 75, 1))
                elif pre_up1:
                    cands.append(("Hombre colgado", "H. colgado", "bajista", "reversion", 65, 1))
            if small and upper(i) >= 2 * max(b, 0.05 * r) and lower(i) <= 0.2 * r:
                if pre_up1:
                    cands.append(("Estrella fugaz", "Estr. fugaz", "bajista", "reversion", 75, 1))
                elif pre_dn1:
                    cands.append(("Martillo invertido", "Mart. inv.", "alcista", "reversion", 65, 1))
            if b <= 0.1 * r and r >= 1.2 * atr and (pre_dn1 or pre_up1):
                cands.append(("Doji", "Doji", "neutral", "indecision", 50, 1))
            if b >= 0.85 * r and r >= 1.6 * atr:
                d = "alcista" if bull(i) else "bajista"
                cands.append((f"Marubozu {d}", "Marubozu", d, "continuacion", 55, 1))

        if not cands:
            return None
        return max(cands, key=lambda t: t[4])

    out = []
    seen = set()   # un patron multi-vela se re-detecta en ruedas consecutivas: contarlo una vez
    for i in range(n - 1, max(8, n - 1 - lookback), -1):
        t = _at(i)
        if not t:
            continue
        name, short, d, kind, _prio, span = t
        if name in seen:
            continue
        seen.add(name)
        off = n - 1 - i
        # Confirmacion con el cierre siguiente (si existe)
        if off == 0:
            status = "por confirmar"
        elif d in ("alcista", "bajista"):
            nxt = closes[i + 1]
            hi_ref = max(opens[i], closes[i])
            lo_ref = min(opens[i], closes[i])
            if d == "alcista":
                if nxt >= hi_ref + 0.1 * atr:
                    status = "confirmada"
                elif nxt <= lows[i] - 0.1 * atr:
                    continue                      # negada por el cierre siguiente
                else:
                    status = "sin confirmacion"
            else:
                if nxt <= lo_ref - 0.1 * atr:
                    status = "confirmada"
                elif nxt >= highs[i] + 0.1 * atr:
                    continue
                else:
                    status = "sin confirmacion"
            if status == "sin confirmacion" and off > 2:
                continue                          # timing caducado
        else:
            if off > 1:
                continue                          # la indecision caduca en 1 rueda
            status = "por confirmar"
        if d == "neutral" and off > 1:
            continue
        cuando = "en la ultima vela" if off == 0 else (
            "hace 1 rueda" if off == 1 else f"hace {off} ruedas")
        kind_txt = {"reversion": f"patron de reversion {d}",
                    "continuacion": f"continuacion {d}",
                    "indecision": "indecision — posible giro"}[kind]
        out.append({
            "name": name, "short": short, "direction": d, "kind": kind,
            "off": int(off), "span": int(span), "status": status,
            "meaning": _CANDLE_MEANING.get(name, ""),
            "text": f"{name} {cuando} ({status}) — {kind_txt}",
        })
        if len(out) >= max_out:
            break
    return out


# ══════════════════════════════════════════════════════════════
#  API PRINCIPAL
# ══════════════════════════════════════════════════════════════

def detect(highs, lows, closes, rsi=None, sma50=None, sma200=None,
           include_cross=False, include_fallback=False, dates=None):
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

    # ANCLAJE UNIFICADO (2026-08): todas las figuras se construyen sobre los
    # MISMOS pivotes MAYORES del zigzag, no sobre extremos locales de ±3 barras
    # (eso era ruido en un grafico de 5 años y anclaba figuras en cualquier
    # wiggle). Un pivote mayor exige un swing >= max(2.5·ATR, 4% del precio):
    # asi "3 toques" significa 3 swings reales al mismo precio, y los techos/
    # pisos de una figura son estructura que se ve a simple vista en el chart.
    zig = _zigzag(highs, lows, max(2.5 * atr, 0.04 * price), window=5)
    piv_h = [(i, p) for i, p, t in zig if t == "H"]
    piv_l = [(i, p) for i, p, t in zig if t == "L"]
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
    # ¿Esta cada figura en su punto de decision? (el consumidor decide si
    # filtra por esto — el pulso muestra contexto siempre, el escaner filtra)
    for c in cands:
        c["critical"] = _is_critical(c, price, atr)

    main = None
    if cands:
        main = dict(cands[0])
        secondary = next((c for c in cands[1:] if c["priority"] >= 45), None)
        if secondary:
            main["secondary"] = secondary["text"]

    fib = None
    try:
        fib = fibonacci(highs, lows, closes, atr, dates=dates)
    except Exception:
        fib = None

    return {"pattern": main, "candidates": cands, "fibonacci": fib,
            "structure": (struct_dir, struct_txt)}


def attach_to_analysis(sig):
    """Adjunta sig["pattern"] y sig["fib"] desde sig["chart"] (muta y devuelve).

    Excluye cruce de MAs (la tesis ya lo cuenta) y el fallback de canal (los
    niveles ya usan el canal), y SOLO adjunta figuras en su punto de decision
    (`critical`): una figura a mitad de formarse, lejos de ruptura/anulacion,
    es contexto y no debe ensuciar tesis, score, veredicto ni graficos —
    pattern=None si no hay nada accionable."""
    if not isinstance(sig, dict):
        return sig
    try:
        chart = sig.get("chart") or {}
        ohlc = chart.get("ohlc") or []
        if len(ohlc) < 60:
            sig["pattern"] = None
            sig["fib"] = None
            sig["candles"] = []
            return sig
        opens = [b.get("open") for b in ohlc]
        highs = [b.get("high") for b in ohlc]
        lows = [b.get("low") for b in ohlc]
        closes = [b.get("close") for b in ohlc]
        rsi = chart.get("rsi") or None
        res = detect(highs, lows, closes, rsi=rsi, dates=chart.get("dates"))
        cands = res["candidates"]
        main = next((dict(c) for c in cands if c.get("critical")), None)
        if main:
            sec = next((c for c in cands
                        if c.get("critical") and c["priority"] >= 45
                        and c["name"] != main["name"]), None)
            if sec:
                main["secondary"] = sec["text"]
        sig["pattern"] = main
        sig["fib"] = res["fibonacci"]
        # Velas japonesas de las ultimas ruedas: timing de entrada de corto
        try:
            sig["candles"] = detect_candles(opens, highs, lows, closes)
        except Exception:
            sig["candles"] = []
    except Exception:
        sig["pattern"] = None
        sig["fib"] = None
        sig["candles"] = []
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
