"""
option_strategy.py — las patas sueltas, leidas como la estrategia que son.

Por que existe
──────────────
`option_position.py` arreglo la lectura de UNA pata: un put comprado gana si el
subyacente cae, no "VENDER". Pero una pata rara vez es la apuesta. Con un bull
call spread (CAT +C750 / −C850) la ficha mostraba DOS tarjetas que se
contradecian — "gana si CAT SUBE" y "gana si CAT CAE" — cuando en realidad son
una sola posicion alcista con techo. El mismo descalce, un nivel mas arriba.

Como lo resuelve
────────────────
NO enumera formulas por tipo de spread (debito, credito, mariposa...): evalua el
**payoff real** del paquete sobre una grilla de precios del subyacente

    payoff($, S) = Σ_patas  qty_i · mult_i · (intrinseco_i(S) − prima_i)

con `qty_i` CON SIGNO (negativo = vendida). De ahi salen, sin casos especiales,
los breakevens (cruces por cero), la ganancia y la perdida maximas, si alguna de
las dos es ilimitada, y la DIRECCION (la pendiente del payoff lejos del precio
actual). El nombre ("Bull call spread", "Collar", "Strangle") se reconoce aparte
y es solo una etiqueta: si no se reconoce, la lectura sigue siendo correcta
porque sale del payoff.

Puro: sin IO, sin ibapi, sin pandas. El caller pasa las posiciones ya
normalizadas por `option_position.describe`. Asi local y cloud narran identico y
el bridge no se toca.

Dos limites asumidos, documentados a proposito:
  - **Agrupa por (subyacente, vencimiento)**: un calendar/diagonal (mismo strike,
    vencimientos distintos) NO se reconoce como estructura y queda en patas.
  - **La accion NO se absorbe en el grupo**: si tenes la accion y un call
    vendido, la accion sigue siendo su propia ficha (es una tenencia real con su
    propia tesis) y el grupo solo la USA como contexto para distinguir un collar
    de un short sintetico, o una covered call de un call desnudo. Esa distincion
    importa: con la accion el riesgo esta cubierto, sin ella es ILIMITADO.
"""

from collections import defaultdict

import option_position as op

CALL = op.CALL
PUT = op.PUT

# Ganancia/perdida por encima de esto se considera ilimitada (el payoff sigue
# subiendo/bajando en el borde de la grilla).
_GRID_HI = 1.55   # hasta 55% sobre el strike mas alto, para leer la pendiente


# ══════════════════════════════════════════════════════════════
#  PAYOFF (la fuente de verdad: todo lo demas se deriva de aca)
# ══════════════════════════════════════════════════════════════

def _intrinsic(right, strike, s):
    return max(0.0, s - strike) if right == CALL else max(0.0, strike - s)


def payoff_at(legs, s):
    """P&L en DOLARES del paquete si el subyacente termina en `s` al vencimiento.

    `legs`: lista de dicts de `option_position.describe` con `contracts` CON
    SIGNO y `premium` por accion. Una pata sin prima conocida se omite de la
    parte de costo pero no del intrinseco (mejor curva incompleta que ninguna).
    """
    total = 0.0
    for o in legs:
        q = o.get("contracts") or 0
        mult = o.get("multiplier") or 100
        prem = o.get("premium")
        intr = _intrinsic(o["right"], o["strike"], s)
        total += q * mult * (intr - (prem if prem is not None else 0.0))
    return total


def _anchor(legs, spot=None):
    strikes = [o["strike"] for o in legs if o.get("strike")]
    if spot and spot > 0:
        strikes.append(spot)
    return sum(strikes) / len(strikes) if strikes else (spot or 0.0)


def analyze_payoff(legs, spot=None):
    """Breakevens, extremos y direccion, leidos de la curva.

    El payoff es lineal a trozos con quiebres EXACTAMENTE en los strikes, asi
    que no hace falta una grilla fina: alcanza con evaluar en 0, en cada strike
    y en un punto lejano. Maximos, minimos y breakevens caen todos ahi, y la
    interpolacion entre dos quiebres consecutivos es exacta.

    Una grilla uniforme daba dos lecturas FALSAS: (1) "perdida ilimitada" en 15
    calls comprados, porque el tramo plano de abajo tocaba el borde, y (2)
    "ganancia ilimitada" en un put comprado, porque la grilla cortaba antes de
    S=0. Hacia abajo el precio TIENE piso (cero); el unico lado realmente
    abierto es el alza.

    Devuelve dict con: breakevens, max_profit, max_loss (negativo),
    profit_unbounded, loss_unbounded, direction, capped.
    """
    anchor = _anchor(legs, spot)
    if anchor <= 0:
        return {}
    strikes = sorted({o["strike"] for o in legs if o.get("strike")})
    hi = max(anchor, (strikes[-1] if strikes else anchor)) * _GRID_HI
    xs = [0.0] + strikes + [hi]
    ys = [payoff_at(legs, x) for x in xs]

    # Breakevens: cruces por cero entre quiebres (interpolacion exacta)
    bes = []
    for i in range(1, len(xs)):
        y0, y1 = ys[i - 1], ys[i]
        if y0 == 0.0:
            bes.append(xs[i - 1])
        elif (y0 < 0) != (y1 < 0):
            t = y0 / (y0 - y1) if (y0 - y1) else 0.0
            bes.append(xs[i - 1] + t * (xs[i] - xs[i - 1]))
    if ys and ys[-1] == 0.0:
        bes.append(xs[-1])
    ded = []
    for b in sorted(bes):
        if not ded or abs(b - ded[-1]) / anchor > 0.001:
            ded.append(b)

    # Lo unico abierto es el alza: la pendiente del ultimo tramo lo dice.
    slope_up = (ys[-1] - ys[-2]) / (xs[-1] - xs[-2]) if xs[-1] > xs[-2] else 0.0
    tol = max(1e-6, abs(anchor) * 1e-9)
    prof_unb = slope_up > tol
    loss_unb = slope_up < -tol
    # Con un lado abierto el extremo de ese lado no existe; el otro sigue valiendo
    finite = ys[:-1] if (prof_unb or loss_unb) else ys
    mx, mn = max(finite), min(finite)

    # Direccion: como se comporta el payoff lejos, a cada lado del precio actual
    ref = spot if (spot and spot > 0) else anchor
    y_ref = payoff_at(legs, ref)
    up = payoff_at(legs, ref * 1.12) - y_ref
    dn = payoff_at(legs, ref * 0.88) - y_ref
    tol = max(1e-6, abs(y_ref) * 1e-6)
    if up > tol and dn <= tol:
        direction = "alcista"
    elif dn > tol and up <= tol:
        direction = "bajista"
    elif up > tol and dn > tol:
        direction = "volatilidad"      # gana si se mueve fuerte, para cualquier lado
    else:
        direction = "rango"            # gana si NO se mueve

    return {
        "breakevens": [round(b, 2) for b in ded],
        "max_profit": None if prof_unb else round(mx, 2),
        "max_loss": None if loss_unb else round(mn, 2),
        "profit_unbounded": prof_unb,
        "loss_unbounded": loss_unb,
        "direction": direction,
        "capped": (not prof_unb) and (not loss_unb),
    }


# ══════════════════════════════════════════════════════════════
#  RECONOCIMIENTO DEL NOMBRE (etiqueta; la lectura sale del payoff)
# ══════════════════════════════════════════════════════════════

def _name_of(legs, has_stock, stock_qty, direction):
    n = len(legs)
    longs = [o for o in legs if (o.get("contracts") or 0) > 0]
    shorts = [o for o in legs if (o.get("contracts") or 0) < 0]
    rights = {o["right"] for o in legs}

    def _k(o):
        return o["strike"]

    if n == 1:
        o = legs[0]
        base = f"{o['right_label']} {'comprado' if o in longs else 'vendido'}"
        if o in shorts and has_stock and o["right"] == CALL and stock_qty > 0:
            return "Covered call", "la tenes cubierta con tus acciones"
        if o in longs and has_stock and o["right"] == PUT and stock_qty > 0:
            return "Protective put", "cubre tus acciones a la baja"
        if o in shorts:
            return base, ("riesgo ILIMITADO al alza, sin acciones que lo cubran"
                          if o["right"] == CALL else "te pueden asignar las acciones")
        return base, ""

    # Varias patas del MISMO tipo y lado: no es un spread, son tramos
    if len(rights) == 1 and (not shorts or not longs):
        lado = "comprados" if longs else "vendidos"
        return (f"{int(sum(abs(o['contracts']) for o in legs))} "
                f"{legs[0]['right_label']}s {lado}"), "dos tramos de la misma apuesta"

    if n == 2 and len(rights) == 1 and len(longs) == 1 and len(shorts) == 1:
        r = legs[0]["right"]
        lo_leg, hi_leg = sorted(legs, key=_k)
        long_is_low = (lo_leg in longs)
        if r == CALL:
            if long_is_low:
                return "Bull call spread", "alcista con techo: el call vendido financia la compra"
            return "Bear call spread", "bajista por credito: cobras prima y el riesgo esta topeado"
        if long_is_low:
            return "Bull put spread", "alcista por credito: cobras prima y el riesgo esta topeado"
        return "Bear put spread", "bajista con piso: el put vendido financia la compra"

    if n == 2 and len(rights) == 2:
        put = next(o for o in legs if o["right"] == PUT)
        call = next(o for o in legs if o["right"] == CALL)
        p_long = (put.get("contracts") or 0) > 0
        c_long = (call.get("contracts") or 0) > 0
        if p_long and c_long:
            kind = "Straddle" if abs(put["strike"] - call["strike"]) < 1e-6 else "Strangle"
            return f"{kind} comprado", "gana si se mueve fuerte, para cualquier lado"
        if (not p_long) and (not c_long):
            kind = "Straddle" if abs(put["strike"] - call["strike"]) < 1e-6 else "Strangle"
            return f"{kind} vendido", "gana si NO se mueve; riesgo ILIMITADO si se dispara"
        if p_long and not c_long:
            if has_stock and stock_qty > 0:
                return "Collar", "tus acciones protegidas abajo y con techo arriba"
            return ("Risk reversal bajista (short sintetico)",
                    "equivale a estar corto en la accion: el call vendido tiene "
                    "riesgo ILIMITADO al alza y no hay acciones que lo cubran")
        if has_stock and stock_qty < 0:
            return "Collar sobre un corto", "tu posicion corta protegida arriba"
        return ("Risk reversal alcista (long sintetico)",
                "equivale a estar largo en la accion, con el put vendido como obligacion")

    if n in (3, 4) and len(rights) == 1:
        return (f"Estructura de {n} patas ({legs[0]['right_label']})",
                "mariposa / condor segun los strikes")
    return f"Estructura de {n} patas", f"lectura {direction} del payoff combinado"


# ══════════════════════════════════════════════════════════════
#  AGRUPADO
# ══════════════════════════════════════════════════════════════

def group_key(option):
    """(subyacente, vencimiento). Un calendar/diagonal cae en grupos distintos
    a proposito: ver el limite documentado arriba."""
    return (option.get("underlying") or "", option.get("expiry") or "")


def build(option_positions, stock_positions=None):
    """Agrupa patas en estrategias.

    `option_positions`: posiciones ya enriquecidas, cada una con `option`
    (de `option_position.describe`) y los campos de dinero del portafolio
    (`costo_total`, `valor_mercado`, `pnl`).
    `stock_positions`: posiciones de acciones (para distinguir un collar de un
    short sintetico). NO se absorben: son fichas propias.

    Devuelve [{key, underlying, expiry, members, legs, strategy}] — `members`
    son las posiciones originales que lo componen.
    """
    stock_qty = {}
    for p in (stock_positions or []):
        s = (p.get("symbol") or "").upper()
        stock_qty[s] = stock_qty.get(s, 0) + (p.get("cantidad") or 0)

    buckets = defaultdict(list)
    for p in option_positions:
        o = p.get("option")
        if not o:
            continue
        buckets[group_key(o)].append(p)

    out = []
    for (sym, exp), members in buckets.items():
        legs = [m["option"] for m in members]
        sq = stock_qty.get(sym, 0)
        pay = analyze_payoff(legs, spot=legs[0].get("underlying_price"))
        direction = pay.get("direction") or legs[0].get("exposure")
        name, note = _name_of(legs, bool(sq), sq, direction)
        out.append({
            "key": f"{sym}_{(exp or '').replace('-', '')}_STRAT",
            "underlying": sym,
            "expiry": exp,
            "members": members,
            "legs": legs,
            "strategy": _summarize(name, note, legs, members, pay, direction, sq),
        })
    return out


def _summarize(name, note, legs, members, pay, direction, stock_qty):
    mult = legs[0].get("multiplier") or 100
    # Costo NETO con signo: positivo = debito pagado, negativo = credito cobrado
    net_cost = 0.0
    net_value = 0.0
    pnl = 0.0
    for m, o in zip(members, legs):
        q = o.get("contracts") or 0
        sgn = 1 if q > 0 else -1
        net_cost += sgn * abs(m.get("costo_total") or 0)
        net_value += sgn * abs(m.get("valor_mercado") or 0)
        pnl += m.get("pnl") or 0
    dtes = [o["dte"] for o in legs if o.get("dte") is not None]
    # En un spread cada pata es un lado del MISMO contrato (3x750/850 = 3
    # spreads); en varios tramos de la misma apuesta son contratos distintos
    # que se suman (3+12 calls = 15).
    _sides = {1 if (o.get("contracts") or 0) > 0 else -1 for o in legs}
    _abs = [abs(o.get("contracts") or 0) for o in legs]
    contracts = (sum(_abs) if len(_sides) == 1 else max(_abs or [0]))
    return {
        "name": name,
        "note": note,
        "direction": direction,
        "exposure": direction if direction in ("alcista", "bajista") else None,
        # Ordenadas por STRIKE: es como se lee un spread, y es el orden en que
        # la narrativa los nombra.
        "legs": [{
            "right": o["right"], "right_label": o["right_label"],
            "strike": o["strike"], "contracts": o.get("contracts"),
            "side": o.get("side"), "premium": o.get("premium"),
            "premium_now": o.get("premium_now"), "expiry": o.get("expiry"),
            "moneyness": o.get("moneyness"),
        } for o in sorted(legs, key=lambda x: (x["strike"], x["right"]))],
        "n_legs": len(legs),
        "contracts": contracts,
        "multiplier": mult,
        "expiry": legs[0].get("expiry"),
        "dte": min(dtes) if dtes else None,
        "net_cost": round(net_cost, 2),
        "net_value": round(net_value, 2),
        "is_debit": net_cost > 0,
        "pnl": round(pnl, 2),
        "stock_qty": stock_qty,
        "label": f"{name} {legs[0].get('underlying', '')} " + _strikes_label(legs),
        **(pay or {}),
    }


def _strikes_label(legs):
    ks = sorted({o["strike"] for o in legs})
    return "/".join(op._fmt_strike(k) for k in ks)


# ══════════════════════════════════════════════════════════════
#  LA ESTRATEGIA, VISTA COMO UNA "OPCION"
# ══════════════════════════════════════════════════════════════

def collapse(positions):
    """Reemplaza las patas de una misma estrategia por UNA posicion.

    Entra la lista de posiciones ya enriquecidas del portafolio y sale la misma
    lista con los grupos de 2+ patas fusionados. Una pata sola se devuelve tal
    cual, pero con `strategy` adjunta igual: ahi vive la advertencia que
    cambia la lectura (una call vendida SIN acciones tiene riesgo ilimitado;
    con acciones es una covered call).

    Lo usan el local (`portfolio.py`) y el cloud (`cloud/server.py`) — misma
    funcion, para que la ficha diga lo mismo en los dos.
    """
    opts = [p for p in positions if p.get("option")]
    if not opts:
        return positions
    stocks = [p for p in positions
              if not p.get("option") and (p.get("tipo") or "STK") == "STK"]
    groups = build(opts, stocks)

    merged_by_member = {}
    new_items = []
    for g in groups:
        st = g["strategy"]
        if len(g["members"]) == 1:
            m = g["members"][0]
            m["strategy"] = st          # solo contexto/advertencia
            continue
        merged = _merge(g)
        for m in g["members"]:
            merged_by_member[id(m)] = merged
        new_items.append(merged)

    out, seen = [], set()
    for p in positions:
        mg = merged_by_member.get(id(p))
        if mg is None:
            out.append(p)
        elif id(mg) not in seen:
            seen.add(id(mg))
            out.append(mg)
    return out


def _merge(group):
    """Una posicion unica a partir de las patas del grupo."""
    st = group["strategy"]
    members = group["members"]
    sym = group["underlying"]
    synth = as_option(st, sym)
    cost = abs(st.get("net_cost") or 0)
    val = abs(st.get("net_value") or 0)
    pnl = st.get("pnl") or 0
    first = members[0]
    return {
        "symbol": sym,
        "tipo": "OPT",
        "key": group["key"],
        "underlying": sym,
        "display_symbol": st.get("label") or sym,
        "cuenta": first.get("cuenta", ""),
        "moneda": first.get("moneda", "USD"),
        "cantidad": st.get("contracts") or 0,
        # La "prima" del paquete es su costo NETO por accion-equivalente: en un
        # spread a credito es lo que cobraste, no lo que pagaste.
        "costo_promedio": synth.get("premium") or 0,
        "precio_actual": synth.get("premium_now"),
        "costo_total": round(cost, 2),
        "valor_mercado": round(val, 2),
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl / cost * 100, 2) if cost > 0 else 0.0,
        "pnl_realizado": round(sum(m.get("pnl_realizado") or 0 for m in members), 2),
        "es_etf": first.get("es_etf", False),
        "sector": "Opciones",
        "peso_portafolio": 0,
        "stop_loss": None,
        "take_profit": None,
        "option": synth,
        "strategy": st,
    }


def as_option(strategy, underlying):
    """Dict con la forma de `option_position.describe` para que el veredicto y
    la narrativa funcionen SIN UN CAMINO NUEVO.

    Una estrategia es, para el motor, lo mismo que una pata: una direccion, un
    reloj y un breakeven. Lo unico que no generaliza es el strike/right (son
    varios), asi que van en None y `option_position.enrich` los saltea.
    """
    bes = strategy.get("breakevens") or []
    mult = strategy.get("multiplier") or 100
    ctr = strategy.get("contracts") or 1
    denom = (ctr * mult) or 1
    return {
        "underlying": underlying,
        "right": None,
        "right_label": strategy.get("name", "estrategia"),
        "side": "long" if strategy.get("is_debit") else "short",
        "side_label": "a debito" if strategy.get("is_debit") else "a credito",
        # El decay juega en contra de quien PAGO el paquete y a favor de quien
        # lo cobro — igual que en una pata suelta.
        "is_long": bool(strategy.get("is_debit")),
        "strike": None,
        "expiry": strategy.get("expiry"),
        "dte": strategy.get("dte"),
        "multiplier": mult,
        "contracts": ctr,
        "exposure": strategy.get("exposure"),
        "premium": round(abs(strategy.get("net_cost") or 0) / denom, 4) or None,
        "premium_now": round(abs(strategy.get("net_value") or 0) / denom, 4) or None,
        "breakeven": bes[0] if len(bes) == 1 else None,
        "breakevens": bes,
        "intrinsic": None, "extrinsic": None, "extrinsic_pct": None,
        "moneyness": None,
        "underlying_price": None,
        "pct_to_strike": None, "pct_to_breakeven": None,
        "expected_move_pct": None, "breakeven_in_moves": None,
        "label": strategy.get("label", ""),
        "label_full": strategy.get("label", ""),
        "is_strategy": True,
    }
