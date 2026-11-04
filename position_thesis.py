"""
Tesis de entrada de una posicion: que decia el sistema el dia que compraste,
que dice hoy, y que paso en el medio.

Modulo SERVER-SIDE PURO (numpy/pandas + los motores que ya existen: indicators,
signals, patterns). Sin IO, sin IB, sin yfinance: el caller pasa el DataFrame.
Por eso `bridge/` NO se toca — el cloud lo computa al armar el deep analysis.

Por que se puede reconstruir el pasado con el motor de hoy:
  - `indicators.py` es 100% causal (EMA/SMA/WMA/rolling/diff): el valor en la
    barra i es identico lo calcules sobre la serie entera o sobre df[:i+1]. Eso
    permite calcular los indicadores UNA vez y cortar las series ya calculadas.
  - `signals.generate_signal` es puro: no importa datetime ni mira "hoy", lee
    todo de la ULTIMA fila de los DataFrames que recibe. Cortar la serie ES
    evaluar esa fecha.
  - `patterns.detect` recibe listas crudas y ancla todo en closes[-1]
    (patterns.py), asi que detect(highs[:i+1], ...) es el estado del grafico al
    cierre de la barra i. Es lo que ya hace `patterns.validate_history`.

NO se reimplementan las condiciones de compra/venta. `backtester.py` ya es una
copia de ese gatillo y CLAUDE.md advierte sobre las cuatro copias: una quinta
que se desincronice haria que la ficha narre una entrada que el sistema nunca
habria disparado.
"""

import math

import indicators
import patterns
import signals

# Tolerancia del caminado de hitos: cada cuantas ruedas se re-evalua la
# etiqueta. 1 = todas (es barato: los indicadores ya estan calculados y solo se
# cortan las series).
_WALK_STEP = 1

_cache = {}
_CACHE_MAX = 256


# ══════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════

def label_direction(label):
    """'bull' | 'bear' | 'neutral' para un signal_label.

    TRAMPA DE SUBCADENAS (CLAUDE.md): "SOBREVENTA" contiene "VENTA" y
    "SOBRECOMPRA" contiene "COMPRA". ZONA DE SOBREVENTA es una lectura ALCISTA.
    Hay test de paridad con `vista_web._label_is_bearish`.
    """
    lab = (label or "").upper()
    if not lab:
        return "neutral"
    if ("VENTA" in lab and "SOBREVENTA" not in lab) or "SOBRECOMPRA" in lab:
        return "bear"
    if ("COMPRA" in lab and "SOBRECOMPRA" not in lab) or "SOBREVENTA" in lab:
        return "bull"
    return "neutral"


def _num(x, nd=None):
    """float JSON-safe: NaN/inf -> None (los NaN envenenan el payload)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return round(v, nd) if nd is not None else v


def _norm_date(raw):
    """'20260716' | '2026-07-16' | '2026-07-16 10:00:00' -> '2026-07-16'."""
    s = str(raw).strip().split()[0]
    if "-" in s:
        return s[:10]
    s = s.replace("/", "")
    if len(s) >= 8 and s[:8].isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def _dates_of(df):
    return [_norm_date(d) for d in df["date"].tolist()]


def _index_asof(dates, target):
    """Ultimo indice con fecha <= target (as-of hacia atras, tolera feriados).

    Mismo criterio que `_get_indicator_values_at_date` en vista_web: una compra
    un feriado o un fin de semana debe resolver a la rueda anterior, no fallar.
    """
    if not target:
        return None
    tgt = _norm_date(target)
    found = None
    for i, d in enumerate(dates):
        if d <= tgt:
            found = i
        else:
            break
    return found


def _slice_ind(ind, i):
    """Corta las series YA calculadas hasta la barra i inclusive."""
    return {k: v.iloc[:i + 1] for k, v in ind.items()}


def _pick_pattern(res):
    """La figura que el escaner habria mostrado ese dia: la primera en su punto
    de decision (`critical`), mismo criterio que `patterns.attach_to_analysis`."""
    cands = (res or {}).get("candidates") or []
    main = next((dict(c) for c in cands if c.get("critical")), None)
    if main:
        sec = next((c for c in cands
                    if c.get("critical") and c["priority"] >= 45
                    and c["name"] != main["name"]), None)
        if sec:
            main["secondary"] = sec["text"]
    return main


def _snapshot(ind, i, highs, lows, closes, rsi_list, dates, with_pattern=True):
    """Estado del sistema al cierre de la barra i."""
    sig = signals.generate_signal(_slice_ind(ind, i))
    snap = {
        "as_of": dates[i],
        "price": _num(closes[i], 2),
        "signal": sig["signal"],
        "signal_label": sig["signal_label"],
        "direction": label_direction(sig["signal_label"]),
        "strength": _num(sig.get("strength"), 1) or 0,
        "conditions_met": int(sig.get("conditions_met") or 0),
        "macd_ok": bool(sig.get("macd_ok")),
        "rsi_ok": bool(sig.get("rsi_ok")),
        "konc_ok": bool(sig.get("konc_ok")),
        "macd_detail": sig.get("macd_detail", ""),
        "rsi_detail": sig.get("rsi_detail", ""),
        "konc_detail": sig.get("konc_detail", ""),
        "values": _clean_values(sig.get("values") or {}),
        "pattern": None,
        "structure": "",
    }
    if with_pattern:
        try:
            res = patterns.detect(
                highs[:i + 1], lows[:i + 1], closes[:i + 1],
                rsi=rsi_list[:i + 1], dates=dates[:i + 1])
            snap["pattern"] = _pick_pattern(res)
            snap["structure"] = (res.get("structure") or (0, ""))[1]
        except Exception:
            pass
    return snap


def _clean_values(vals):
    out = {}
    k = vals.get("koncorde") or {}
    if k:
        out["koncorde"] = {kk: _num(vv, 2) for kk, vv in k.items()}
    m = vals.get("macd") or {}
    if m:
        out["macd"] = {kk: _num(vv, 3) for kk, vv in m.items()}
    if "rsi" in vals:
        out["rsi"] = _num(vals["rsi"], 1)
    return out


# ══════════════════════════════════════════════════════════════
#  Hitos entre la entrada y hoy
# ══════════════════════════════════════════════════════════════

# Prioridad al recortar: lo que contradice o confirma la tesis manda sobre el
# ruido de etiquetas. Una ficha con 30 hitos no se lee.
_KIND_PRIO = {"figura": 5, "senal": 4, "giro": 3, "sma200": 2, "extremo": 1}


def _walk(ind, i0, dates, closes, sma200, entry_pat, entry_dir):
    """Camina de la entrada a hoy registrando lo que le paso a la tesis."""
    n = len(closes)
    events = []
    flips = 0
    prev_dir = entry_dir
    seen_signal = set()

    # Nivel de anulacion / objetivo de la figura que acompanaba la entrada:
    # son los dos hechos que responden "¿la tesis de entrada se cumplio o se
    # rompio?" sin tener que re-detectar figuras barra por barra.
    pat_inval = (entry_pat or {}).get("invalidation")
    pat_target = (entry_pat or {}).get("target")
    pat_dir = (entry_pat or {}).get("direction")
    pat_name = (entry_pat or {}).get("name") or "la figura"
    pat_bull = pat_dir in ("alcista", "bull", 1)
    pat_broken = pat_hit = False

    prev_above = None
    if sma200 is not None and i0 < len(sma200):
        m0 = sma200[i0]
        prev_above = (closes[i0] > m0) if m0 == m0 else None

    for i in range(i0 + 1, n, _WALK_STEP):
        d = dates[i]
        c = closes[i]

        # 1. Etiqueta del sistema en esa rueda
        sig = signals.generate_signal(_slice_ind(ind, i))
        lab = sig["signal_label"]
        dr = label_direction(lab)
        if dr != "neutral" and prev_dir != "neutral" and dr != prev_dir:
            flips += 1
            events.append({"date": d, "kind": "giro", "side": dr,
                           "text": f"El cuadro tecnico paso a {lab}"})
        if dr != "neutral":
            prev_dir = dr

        # 2. Senal completa del sistema (3/3): es el gatillo real del bot
        if sig["signal"] in ("BUY", "SELL") and sig["signal"] not in seen_signal:
            seen_signal.add(sig["signal"])
            side = "bull" if sig["signal"] == "BUY" else "bear"
            events.append({"date": d, "kind": "senal", "side": side,
                           "text": f"Senal completa de {'COMPRA' if side == 'bull' else 'VENTA'} "
                                   f"(3/3, fuerza {sig.get('strength', 0):.1f})"})

        # 3. La figura de la entrada: ¿se cumplio o se anulo?
        if pat_inval and not pat_broken:
            if (pat_bull and c < pat_inval) or (not pat_bull and c > pat_inval):
                pat_broken = True
                events.append({"date": d, "kind": "figura", "side": "bear" if pat_bull else "bull",
                               "text": f"{pat_name} perdio su nivel de anulacion "
                                       f"(${pat_inval:.2f}): la figura de la entrada quedo rota"})
        if pat_target and not pat_hit:
            if (pat_bull and c >= pat_target) or (not pat_bull and c <= pat_target):
                pat_hit = True
                events.append({"date": d, "kind": "figura", "side": "bull" if pat_bull else "bear",
                               "text": f"{pat_name} alcanzo su objetivo medido (${pat_target:.2f})"})

        # 4. Cruce de la SMA200 (cambio de regimen de fondo)
        if sma200 is not None and i < len(sma200):
            m = sma200[i]
            if m == m and prev_above is not None:
                above = c > m
                if above != prev_above:
                    events.append({"date": d, "kind": "sma200",
                                   "side": "bull" if above else "bear",
                                   "text": f"El precio {'recupero' if above else 'perdio'} "
                                           f"la SMA200 (${m:.2f})"})
                    prev_above = above
            elif m == m and prev_above is None:
                prev_above = c > m

    return events, flips, pat_broken, pat_hit


def _trim(events, limit):
    """Deja los `limit` hitos mas significativos, en orden cronologico."""
    if len(events) <= limit:
        return events
    ranked = sorted(events, key=lambda e: (_KIND_PRIO.get(e["kind"], 0), e["date"]),
                    reverse=True)[:limit]
    return sorted(ranked, key=lambda e: e["date"])


# ══════════════════════════════════════════════════════════════
#  Veredicto de la tesis + narrativa
# ══════════════════════════════════════════════════════════════

_STATUS_LABEL = {
    "sin_senal": "LA COMPRA NO SALIO DE UNA SENAL",
    "intacta": "TESIS INTACTA",
    "debilitada": "TESIS DEBILITADA",
    "invalidada": "TESIS INVALIDADA",
    "dada_vuelta": "TESIS DADA VUELTA",
    "recien_abierta": "RECIEN ABIERTA",
    "sin_historial": "SIN HISTORIAL DE ENTRADA",
}


def _status_of(entry, now, pat_broken, flips):
    e_dir, n_dir = entry["direction"], now["direction"]
    # Una entrada sin lectura direccional no tiene tesis que sostener: decirlo
    # es mas util que inventarle una. Es ademas la respuesta literal a "¿los
    # indicadores estaban dando senal cuando compre?".
    if e_dir == "neutral" and entry["conditions_met"] <= 1:
        return "sin_senal"
    if e_dir != "neutral" and n_dir != "neutral" and n_dir != e_dir:
        return "dada_vuelta"
    if pat_broken:
        return "invalidada"
    if n_dir == e_dir and e_dir != "neutral":
        return "intacta"
    return "debilitada"


def _summary(sym, entry, now, status, flips, held, degraded):
    e_lab, n_lab = entry["signal_label"], now["signal_label"]
    e_day = entry["as_of"]
    bits = []

    if status == "sin_senal":
        bits.append(
            f"El {e_day}, cuando entraste, el sistema marcaba {e_lab} con "
            f"{entry['conditions_met']}/3 condiciones: la compra no salio de una senal "
            f"de este sistema. Lo que sigue mide la posicion contra el cuadro tecnico, "
            f"no contra una tesis propia.")
    elif status == "intacta":
        txt = (f"Entraste el {e_day} con {e_lab} ({entry['conditions_met']}/3, fuerza "
               f"{entry['strength']:.1f}) y hoy el cuadro sigue del mismo lado ({n_lab}). "
               f"La lectura que te hizo entrar sigue viva")
        # Mismo lado no es lo mismo que misma conviccion: decir "intacta" sobre
        # una lectura que se desinflo de 2/3 a 1/3 seria tranquilizar de mas.
        if degraded:
            txt += (f", aunque mas floja: pasaste de {entry['conditions_met']}/3 "
                    f"fuerza {entry['strength']:.1f} a {now['conditions_met']}/3 "
                    f"fuerza {now['strength']:.1f}")
        bits.append(txt + ".")
    elif status == "dada_vuelta":
        bits.append(
            f"Entraste el {e_day} con {e_lab} y hoy el cuadro es {n_lab}: la lectura "
            f"se dio vuelta. Ya no estas en el trade que compraste.")
    elif status == "invalidada":
        txt = (f"Entraste el {e_day} con {e_lab}, pero la figura que acompanaba esa "
               f"entrada perdio su nivel de anulacion: la tesis original quedo rota. ")
        # Que hoy la etiqueta coincida con la de la entrada no significa que
        # siga siendo el mismo trade — la estructura que lo sostenia ya no esta.
        if now["direction"] == entry["direction"] and entry["direction"] != "neutral":
            txt += (f"Hoy el cuadro volvio a ser {n_lab}, pero apoyado en una estructura "
                    f"nueva, no en la que compraste.")
        else:
            txt += f"Hoy el cuadro es {n_lab}."
        bits.append(txt)
    else:
        bits.append(
            f"Entraste el {e_day} con {e_lab} y hoy el cuadro es {n_lab}: la lectura se "
            f"diluyo sin darse vuelta.")

    if held:
        # "sin conviccion" es un juicio fuerte: exige muestra. 3 giros en 40
        # ruedas puede ser un mercado nervioso; 3 en 200 es otra cosa.
        if flips >= 3 and held >= 60:
            bits.append(
                f"En las {held} ruedas que llevas adentro el cuadro cambio de lado "
                f"{flips} veces: es una posicion que el sistema viene leyendo sin "
                f"conviccion, no una tesis sostenida.")
        elif flips > 0:
            bits.append(f"En {held} ruedas el cuadro cambio de lado {flips} "
                        f"{'vez' if flips == 1 else 'veces'}.")
        else:
            bits.append(f"En {held} ruedas el cuadro no cambio de lado ni una vez.")

    return " ".join(bits)


def _pnl_bit(pnl_pct, dd_pct):
    """La frase del P&L va APARTE del resumen cacheado: el resto de la tesis se
    calcula una vez por dia, pero el P&L depende del precio vivo de IB. Y tiene
    que coincidir con el del header de la ficha — mezclar el cierre del analisis
    con el precio vivo es el bug que CLAUDE.md ya documento en Mi Cartera."""
    if pnl_pct is None:
        return ""
    if dd_pct is not None and dd_pct < -1:
        return (f" Vas {pnl_pct:+.1f}% desde tu costo, con una caida maxima de "
                f"{dd_pct:.1f}% en el camino.")
    return f" Vas {pnl_pct:+.1f}% desde tu costo." 


# ══════════════════════════════════════════════════════════════
#  API publica
# ══════════════════════════════════════════════════════════════

def build(sym, entry_date, df, avg_cost=None, today=None, max_milestones=6,
          pnl_pct=None):
    """Tesis de entrada vs. hoy para una posicion abierta.

    df: DataFrame OHLCV diario (idealmente 5Y) con columnas
        date/open/high/low/close/volume — el MISMO shape que consume
        `indicators.calculate_all`. El caller decide de donde sale (IB o
        yfinance); este modulo no hace IO.

    Devuelve None solo si el df es inservible. Si falta la fecha de entrada
    devuelve status="sin_historial" con el motivo, NUNCA una entrada inventada.
    """
    if df is None or len(df) < 60 or "close" not in df:
        return None

    key = (sym, _norm_date(entry_date) if entry_date else None,
           today or _norm_date(df["date"].iloc[-1]), round(float(avg_cost or 0), 4))
    if key in _cache:
        return _with_pnl(_cache[key], pnl_pct)

    try:
        out = _build(sym, entry_date, df, avg_cost, max_milestones)
    except Exception as e:
        print(f"[POSITION_THESIS] {sym}: {e}", flush=True)
        return None

    if len(_cache) > _CACHE_MAX:
        _cache.clear()
    _cache[key] = out
    return _with_pnl(out, pnl_pct)


def _with_pnl(out, pnl_pct):
    """Copia con la frase del P&L vivo pegada al resumen (el cacheado no la lleva)."""
    if not out:
        return out
    r = dict(out)
    live = pnl_pct if pnl_pct is not None else out.get("pnl_pct")
    r["pnl_pct"] = live
    r["summary"] = (out.get("summary") or "") + _pnl_bit(live, out.get("drawdown_pct"))
    return r


def _build(sym, entry_date, df, avg_cost, max_milestones):
    dates = _dates_of(df)
    closes = [float(x) for x in df["close"].tolist()]
    highs = [float(x) for x in df["high"].tolist()]
    lows = [float(x) for x in df["low"].tolist()]
    n = len(closes)

    ind = indicators.calculate_all(df)
    rsi_list = [_num(x) for x in ind["rsi"]["rsi"].tolist()]
    try:
        sma200 = [float(x) for x in indicators.sma(df["close"], 200).tolist()]
    except Exception:
        sma200 = None

    now = _snapshot(ind, n - 1, highs, lows, closes, rsi_list, dates)

    i0 = _index_asof(dates, entry_date)
    if i0 is None:
        return {
            "symbol": sym, "status": "sin_historial",
            "status_label": _STATUS_LABEL["sin_historial"],
            "entry": None, "now": now, "milestones": [], "flips": 0,
            "bars_held": None, "entry_date": None,
            "summary": (
                "No hay fill de apertura para esta posicion en el historial importado, "
                "asi que no se puede reconstruir que decia el sistema el dia que entraste. "
                "Conectando el historial completo de IB (Flex) la ficha lo calcula sola."),
        }

    if i0 >= n - 1:
        return {
            "symbol": sym, "status": "recien_abierta",
            "status_label": _STATUS_LABEL["recien_abierta"],
            "entry": now, "now": now, "milestones": [], "flips": 0,
            "bars_held": 0, "entry_date": dates[i0],
            "summary": (f"Entraste hoy mismo ({dates[i0]}) con {now['signal_label']} "
                        f"({now['conditions_met']}/3). Todavia no hay recorrido que medir."),
        }

    entry = _snapshot(ind, i0, highs, lows, closes, rsi_list, dates,
                      with_pattern=(i0 >= 60))

    events, flips, pat_broken, pat_hit = _walk(
        ind, i0, dates, closes, sma200, entry.get("pattern"), entry["direction"])

    # Recorrido del precio desde la entrada (sobre el costo real si lo hay;
    # si no, sobre el cierre del dia de entrada)
    base = float(avg_cost) if avg_cost else closes[i0]
    seg = closes[i0 + 1:]
    dd_pct = run_pct = None
    if seg and base > 0:
        dd_pct = (min(seg) / base - 1) * 100
        run_pct = (max(seg) / base - 1) * 100
        lo_i = i0 + 1 + seg.index(min(seg))
        if dd_pct < -8:
            events.append({"date": dates[lo_i], "kind": "extremo", "side": "bear",
                           "text": f"Piso del recorrido: ${min(seg):.2f} ({dd_pct:.1f}% "
                                   f"desde tu {'costo' if avg_cost else 'entrada'})"})
    pnl_pct = ((closes[-1] / base - 1) * 100) if base > 0 else None

    status = _status_of(entry, now, pat_broken, flips)
    held = n - 1 - i0
    degraded = (entry["strength"] - now["strength"] >= 1.5
                or now["conditions_met"] < entry["conditions_met"])

    return {
        "symbol": sym,
        "status": status,
        "status_label": _STATUS_LABEL[status],
        "entry_date": dates[i0],
        "bars_held": held,
        "entry": entry,
        "now": now,
        "flips": flips,
        "pattern_broken": pat_broken,
        "pattern_hit": pat_hit,
        "drawdown_pct": _num(dd_pct, 1),
        "runup_pct": _num(run_pct, 1),
        "pnl_pct": _num(pnl_pct, 1),
        "milestones": _trim(events, max_milestones),
        "milestones_total": len(events),
        "degraded": degraded,
        "summary": _summary(sym, entry, now, status, flips, held, degraded),
        # el resumen se guarda SIN la frase del P&L: _with_pnl la pega con el
        # precio vivo cada vez que se sirve
    }
