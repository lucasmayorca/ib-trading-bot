"""
option_position.py — una tenencia de OPCIONES, normalizada.

Por que existe
──────────────
Mi Cartera analizaba las opciones como si fueran acciones: tomaba el `symbol`
que manda IB (que para un contrato de opciones es el SUBYACENTE), corria el
analisis tecnico del subyacente y escribia el veredicto desde la optica de un
tenedor LARGO de esa accion. Con un PUT comprado eso se lee al reves: una
lectura bajista del subyacente — "VENTA", "perdiendo la SMA200" — es
exactamente la tesis del tenedor cumpliendose, y la ficha decia "VENDER".

Una opcion es una apuesta DIRECCIONAL sobre el subyacente mas un reloj. Este
modulo traduce la tenencia a esos terminos:

  - `exposure`: en que direccion del SUBYACENTE gana el tenedor.
        call comprado / put vendido  -> alcista
        put comprado  / call vendido -> bajista
  - el reloj: dias al vencimiento (DTE) y cuanto del precio es valor temporal.
  - la vara: cuanto tiene que moverse el subyacente hasta el breakeven, contra
    cuanto SUELE moverse en los dias que quedan (ATR·√DTE).

Es puro (sin IO, sin ibapi, sin pandas): el caller pasa los datos del contrato
y el precio del subyacente. Asi el local y el cloud narran identico y el bridge
solo tiene que reportar los campos del contrato.

Unidades (la trampa clasica, ya documentada en Options Lab y en Trades
Historicos): IB manda `averageCost` de una opcion POR CONTRATO (prima x100) y
`marketPrice` POR ACCION. Todo lo que este modulo expone como `premium*` esta
POR ACCION, para que sea comparable con el strike y con el precio del
subyacente; los totales en dolares se calculan con `multiplier`.
"""

from datetime import datetime
import math
import re

CALL = "C"
PUT = "P"

# 'ROP   270116P00500000' (formato OCC de IB, con padding) o sin espacios.
_OCC_RE = re.compile(r"^([A-Z0-9.\-]{1,6})\s*(\d{6})([CP])(\d{8})$")


def parse_occ(raw):
    """'ROP   270116P00500000' -> dict con root/expiry/right/strike.

    Devuelve None si no parsea (no es un symbol de opcion).
    """
    if not raw:
        return None
    s = str(raw).strip().upper()
    m = _OCC_RE.match(s)
    if not m:
        # IB a veces manda el local symbol con los campos separados por espacio
        parts = s.split()
        if len(parts) >= 2:
            m = _OCC_RE.match(parts[0] + parts[-1])
        if not m:
            return None
    root, exp_raw, right, strike_raw = m.groups()
    try:
        strike = int(strike_raw) / 1000.0
        expiry = f"20{exp_raw[:2]}-{exp_raw[2:4]}-{exp_raw[4:6]}"
    except (ValueError, IndexError):
        return None
    return {"root": root, "expiry": expiry, "right": right, "strike": strike}


def _norm_expiry(raw):
    """IB manda 'YYYYMMDD' en lastTradeDateOrContractMonth; tambien aceptamos
    'YYYY-MM-DD'. Devuelve 'YYYY-MM-DD' o None."""
    if not raw:
        return None
    s = str(raw).strip()[:10]
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    if re.match(r"^\d{8}$", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return None


def _norm_right(raw):
    if not raw:
        return None
    s = str(raw).strip().upper()
    if s.startswith("C"):
        return CALL
    if s.startswith("P"):
        return PUT
    return None


def is_option(pos):
    """True si la posicion es un contrato de opciones."""
    t = (pos or {}).get("tipo") or (pos or {}).get("sec_type") or (pos or {}).get("secType") or ""
    return str(t).strip().upper() in ("OPT", "FOP")


def unresolved(pos):
    """True si es una opcion de la que NO se pudo sacar el contrato.

    Pasa con un bridge viejo que no reporta right/strike/expiry ni localSymbol.
    Importa distinguirlo: sin esos datos no se sabe en que direccion gana la
    tenencia, y narrarla igual es exactamente el bug que esto viene a cerrar —
    mejor decir "no puedo leer este contrato" que inventar una lectura larga.
    """
    return is_option(pos) and describe(pos) is None


def exposure_of(right, qty):
    """Direccion del SUBYACENTE en la que gana el tenedor.

    call comprado / put vendido  -> 'alcista'
    put comprado  / call vendido -> 'bajista'
    """
    if right not in (CALL, PUT) or not qty:
        return None
    long_side = qty > 0
    bullish = (right == CALL) == long_side
    return "alcista" if bullish else "bajista"


def _dte(expiry, today=None):
    if not expiry:
        return None
    try:
        d1 = datetime.strptime(expiry, "%Y-%m-%d")
    except ValueError:
        return None
    d0 = today or datetime.now()
    if isinstance(d0, str):
        try:
            d0 = datetime.strptime(d0[:10], "%Y-%m-%d")
        except ValueError:
            d0 = datetime.now()
    return (d1.date() - d0.date()).days


def describe(pos, underlying_price=None, atr=None, today=None):
    """Normaliza una tenencia de opciones. None si no es una opcion o no se
    pudo resolver el contrato (sin right/strike no hay direccion que narrar).

    `pos` acepta los campos como los deja `portfolio.py` (tipo/cantidad/
    costo_promedio/precio_actual + right/strike/expiry/multiplier/local_symbol)
    o como los manda el bridge (secType/position/averageCost/marketPrice).
    """
    if not is_option(pos):
        return None
    p = pos or {}

    right = _norm_right(p.get("right"))
    strike = p.get("strike")
    expiry = _norm_expiry(p.get("expiry") or p.get("lastTradeDateOrContractMonth"))
    underlying = (p.get("underlying") or p.get("symbol") or "").strip().upper()

    # Respaldo: reconstruir del local symbol OCC. Un bridge viejo no manda los
    # campos del contrato y esto es lo unico que queda — mejor eso que mostrar
    # la opcion como si fuera la accion.
    if right is None or not strike or not expiry:
        occ = parse_occ(p.get("local_symbol") or p.get("localSymbol") or p.get("symbol"))
        if occ:
            right = right or occ["right"]
            strike = strike or occ["strike"]
            expiry = expiry or occ["expiry"]
            underlying = underlying or occ["root"]
    if right not in (CALL, PUT) or not strike:
        return None
    try:
        strike = float(strike)
    except (TypeError, ValueError):
        return None

    qty = float(p.get("cantidad", p.get("position", 0)) or 0)
    try:
        mult = float(p.get("multiplier") or 100) or 100.0
    except (TypeError, ValueError):
        mult = 100.0

    # Prima POR ACCION. IB manda averageCost por CONTRATO (prima x multiplier)
    # y marketPrice por accion: sin dividir, el "costo" sale 100x y cualquier
    # comparacion contra el strike o el precio del subyacente es basura.
    raw_cost = p.get("costo_promedio", p.get("averageCost"))
    premium = None
    if raw_cost not in (None, 0):
        try:
            premium = abs(float(raw_cost)) / mult
        except (TypeError, ValueError):
            premium = None
    premium_now = None
    raw_px = p.get("precio_actual") or p.get("marketPrice") or p.get("precio_mercado")
    if raw_px not in (None, 0):
        try:
            premium_now = abs(float(raw_px))
        except (TypeError, ValueError):
            premium_now = None

    is_long = qty > 0
    exposure = exposure_of(right, qty)
    dte = _dte(expiry, today)

    # Breakeven al vencimiento: la prima pagada (o cobrada) corre el nivel.
    # Mismo formula para largo y corto — lo que cambia es de que lado del
    # breakeven se gana, y eso ya lo dice `exposure`.
    be = None
    if premium is not None:
        be = strike + premium if right == CALL else strike - premium

    spot = None
    if underlying_price:
        try:
            spot = float(underlying_price)
        except (TypeError, ValueError):
            spot = None

    intrinsic = extrinsic = moneyness = None
    pct_to_strike = pct_to_be = None
    if spot and spot > 0:
        intrinsic = max(0.0, spot - strike) if right == CALL else max(0.0, strike - spot)
        if premium_now is not None:
            extrinsic = max(0.0, premium_now - intrinsic)
        if intrinsic > 0:
            moneyness = "ITM"
        elif abs(spot - strike) / spot <= 0.01:
            moneyness = "ATM"
        else:
            moneyness = "OTM"
        pct_to_strike = (strike - spot) / spot * 100
        if be is not None:
            pct_to_be = (be - spot) / spot * 100

    # ¿Cuanto tiene que moverse el subyacente hasta el breakeven, medido contra
    # lo que SUELE moverse en los dias que quedan? Es la vara honesta: "necesita
    # caer 7% y su movimiento tipico a 38 dias es 9%" dice mucho mas que el
    # porcentaje solo.
    exp_move_pct = None
    be_in_moves = None
    if atr and spot and spot > 0 and dte and dte > 0:
        try:
            exp_move_pct = float(atr) * math.sqrt(dte) / spot * 100
            if exp_move_pct > 0 and pct_to_be is not None:
                # Positivo = falta moverse A FAVOR; negativo = ya paso el breakeven
                need = pct_to_be if exposure == "alcista" else -pct_to_be
                be_in_moves = need / exp_move_pct
        except (TypeError, ValueError):
            pass

    right_label = "CALL" if right == CALL else "PUT"
    side_label = "comprado" if is_long else "vendido"
    label = f"{right_label} {underlying} ${_fmt_strike(strike)}"
    if expiry:
        label += f" {_fmt_date(expiry)}"

    return {
        "underlying": underlying,
        "right": right,
        "right_label": right_label,
        "side": "long" if is_long else "short",
        "side_label": side_label,
        "is_long": is_long,
        "strike": round(strike, 4),
        "expiry": expiry,
        "dte": dte,
        "multiplier": mult,
        "contracts": qty,
        "exposure": exposure,
        "premium": round(premium, 4) if premium is not None else None,
        "premium_now": round(premium_now, 4) if premium_now is not None else None,
        "breakeven": round(be, 4) if be is not None else None,
        "intrinsic": round(intrinsic, 4) if intrinsic is not None else None,
        "extrinsic": round(extrinsic, 4) if extrinsic is not None else None,
        "extrinsic_pct": (round(extrinsic / premium_now * 100, 1)
                          if (extrinsic is not None and premium_now) else None),
        "moneyness": moneyness,
        "underlying_price": spot,
        "pct_to_strike": round(pct_to_strike, 2) if pct_to_strike is not None else None,
        "pct_to_breakeven": round(pct_to_be, 2) if pct_to_be is not None else None,
        "expected_move_pct": round(exp_move_pct, 2) if exp_move_pct is not None else None,
        "breakeven_in_moves": round(be_in_moves, 2) if be_in_moves is not None else None,
        "label": label,
        "label_full": f"{label} ({side_label})",
    }


def enrich(option, underlying_price=None, atr=None, today=None):
    """Recalcula los campos que dependen del MERCADO (moneyness, intrinseco/
    extrinseco, distancia al breakeven, movimiento tipico) sobre un dict que
    ya devolvio `describe`.

    Existe porque el precio del subyacente y su ATR recien se conocen cuando
    corre el analisis tecnico, bastante despues de armar la posicion.
    """
    if not option:
        return option
    o = dict(option)
    spot = None
    if underlying_price:
        try:
            spot = float(underlying_price)
        except (TypeError, ValueError):
            spot = None
    strike = o.get("strike")
    right = o.get("right")
    be = o.get("breakeven")
    premium_now = o.get("premium_now")
    if o.get("dte") is None or today is not None:
        o["dte"] = _dte(o.get("expiry"), today)
    if not (spot and spot > 0):
        return o
    o["underlying_price"] = spot
    # Strike/right no generalizan a una ESTRATEGIA de varias patas (option_
    # strategy.as_option los manda en None): intrinseco y moneyness se saltean,
    # pero el breakeven y el movimiento tipico valen igual y son lo que decide.
    if strike and right in (CALL, PUT):
        intrinsic = max(0.0, spot - strike) if right == CALL else max(0.0, strike - spot)
        o["intrinsic"] = round(intrinsic, 4)
        if premium_now is not None:
            extr = max(0.0, premium_now - intrinsic)
            o["extrinsic"] = round(extr, 4)
            o["extrinsic_pct"] = round(extr / premium_now * 100, 1) if premium_now else None
        o["moneyness"] = ("ITM" if intrinsic > 0
                          else ("ATM" if abs(spot - strike) / spot <= 0.01 else "OTM"))
        o["pct_to_strike"] = round((strike - spot) / spot * 100, 2)
    if be is not None:
        pct_to_be = (be - spot) / spot * 100
        o["pct_to_breakeven"] = round(pct_to_be, 2)
        dte = o.get("dte")
        if atr and dte and dte > 0:
            try:
                em = float(atr) * math.sqrt(dte) / spot * 100
                o["expected_move_pct"] = round(em, 2)
                if em > 0:
                    need = pct_to_be if o.get("exposure") == "alcista" else -pct_to_be
                    o["breakeven_in_moves"] = round(need / em, 2)
            except (TypeError, ValueError):
                pass
    return o


def _fmt_strike(v):
    return f"{v:.0f}" if abs(v - round(v)) < 0.005 else f"{v:.2f}"


def _fmt_date(iso):
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d-%m-%Y")
    except (ValueError, TypeError):
        return iso


def position_key(pos):
    """Clave UNICA de la posicion para el DOM y los charts.

    El `symbol` de una opcion es el subyacente, asi que dos contratos del mismo
    activo (o la accion y una opcion sobre ella) colisionaban: misma key de
    chart, mismo id de acordeon, y `_findPortPosition` devolvia siempre el
    primero.
    """
    p = pos or {}
    sym = (p.get("symbol") or "").strip().upper()
    if not is_option(p):
        return sym
    o = describe(p)
    if not o:
        return sym
    exp = (o["expiry"] or "").replace("-", "")
    return f"{o['underlying']}_{exp}{o['right']}{int(round(o['strike'] * 1000)):08d}"


def since_key(underlying, expiry, right, strike):
    """Clave para cruzar una tenencia de opciones con el episodio flat->flat
    del historial de trades (que indexa por pata, no por ticker)."""
    try:
        k = int(round(float(strike) * 1000))
    except (TypeError, ValueError):
        return None
    exp = _norm_expiry(expiry) or ""
    r = _norm_right(right)
    if not (underlying and exp and r):
        return None
    return f"{str(underlying).upper()}|{exp}|{r}|{k}"


def since_key_of(option):
    """`since_key` a partir del dict que devuelve `describe`."""
    if not option:
        return None
    return since_key(option.get("underlying"), option.get("expiry"),
                     option.get("right"), option.get("strike"))


def display_symbol(pos):
    """Como se nombra la tenencia en la UI: 'ROP PUT $500 16-01-2027'."""
    o = describe(pos)
    if not o:
        return (pos or {}).get("symbol", "")
    return o["label"]


def thesis_direction(option):
    """La direccion del SUBYACENTE que le conviene al tenedor ('alcista' /
    'bajista'), para comparar contra `signal_label`."""
    return (option or {}).get("exposure")
