"""Pulso del Mercado — analisis tecnico conciso del SPY (S&P 500).

Compartido por local (vista_web.py) y cloud (cloud/server.py) siguiendo el
patron de enrichment.py: corre server-side con datos de yfinance directo, sin
depender de TWS, del bridge ni del ciclo del scanner (en cloud el bridge no se
toca). Reutiliza indicators.py + signals.py para que las lecturas (MACD/RSI/
Koncorde y las condiciones 3/3 en ambas direcciones) sean las MISMAS del resto
del dashboard, y agrega capas propias:

  - Pisos/techos horizontales por pivotes fractales (misma tecnica que
    _find_sr_levels de vista_web, con indices para datar toques y rupturas).
  - Canal de regresion (120 ruedas, ±2σ) con las series listas para dibujar
    sobre las velas (chart_extras.channel, sufijo alineado al final del ohlc).
  - Figuras tecnicas por reglas, cada una con direccion, estado (en formacion/
    confirmada) y nivel que la confirma o anula: ruptura de nivel, doble
    techo/suelo, triangulo/cuña, cruce dorado/de la muerte, divergencia
    RSI/precio, estructura HH-HL/LH-LL + canal como fallback.
  - Lectura de la sesion en curso o ultima rueda (gap, rango, RVOL proyectado).
  - Veredicto agregado (score con factores explicables) + lectura operativa.

API publica: get_market_pulse() -> dict listo para /api/market-pulse.
Cache in-process con TTL; ante error de red devuelve el ultimo pulso valido.
"""

import math
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import indicators
import patterns
import signals

SYMBOL = "SPY"
NAME = "S&P 500"
_TTL_S = 600           # refresco del pulso (10 min)
_NY = ZoneInfo("America/New_York")

_cache = {"data": None, "ts": 0.0}
_lock = threading.Lock()

_DIAS = ["lun", "mar", "mie", "jue", "vie", "sab", "dom"]


def _r(x, nd=2):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, nd)


def _fmt_usd(x):
    return f"${x:,.2f}" if x is not None else "$--"


def _pct(x, signed=True):
    if x is None:
        return "--%"
    s = "+" if (signed and x >= 0) else ""
    return f"{s}{x:.1f}%"


# ══════════════════════════════════════════════════════════════
#  DESCARGA (yfinance)
# ══════════════════════════════════════════════════════════════

def _download_daily():
    """OHLCV diario 5A (warm-up sobrado para Koncorde EMA 255)."""
    import yfinance as yf
    h = yf.Ticker(SYMBOL).history(period="5y", interval="1d", auto_adjust=False)
    if h is None or h.empty or len(h) < 260:
        return None
    df = pd.DataFrame({
        "date": [d.strftime("%Y-%m-%d") for d in h.index],
        "open": h["Open"].astype(float).values,
        "high": h["High"].astype(float).values,
        "low": h["Low"].astype(float).values,
        "close": h["Close"].astype(float).values,
        "volume": h["Volume"].astype(float).fillna(0).values,
    })
    return df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)


def _download_intraday():
    """Velas 15m de los ultimos dias para leer la sesion (gap/rango)."""
    import yfinance as yf
    try:
        h = yf.Ticker(SYMBOL).history(period="5d", interval="15m", auto_adjust=False)
        if h is None or h.empty:
            return None
        return h
    except Exception:
        return None


def _drop_partial_bar(df, now_et=None):
    """Descarta la barra diaria EN CURSO: el ANALISIS del pulso (indicadores,
    señal, veredicto, figuras, S/R) se calcula solo sobre cierres confirmados
    — espejo de vista_web._drop_partial_bar, mantener paridad. Sin esto, el
    momentum ("MACD subiendo/cayendo"), las condiciones x/3 y el veredicto se
    re-evaluaban cada 10 min sobre una barra a medio formar y parpadeaban
    intradia. La lectura de la SESION (gap/RVOL) y el precio del header siguen
    usando la barra viva a proposito — su funcion es leer la sesion en curso."""
    if df is None or len(df) < 2:
        return df
    try:
        if now_et is None:
            now_et = datetime.now(_NY)
        if now_et.hour >= 16:
            return df
        if str(df["date"].iloc[-1]) == now_et.strftime("%Y-%m-%d"):
            return df.iloc[:-1].reset_index(drop=True)
    except Exception:
        pass
    return df


# ══════════════════════════════════════════════════════════════
#  PRIMITIVAS: ATR, pivotes, niveles, canal
# ══════════════════════════════════════════════════════════════

def _atr(df, period=14):
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    trs = []
    for i in range(1, len(df)):
        trs.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    if len(trs) < period:
        return None
    return float(np.mean(trs[-period:]))


def _pivots(df, lookback=252, window=3):
    """Pivotes fractales (idx, precio) sobre las ultimas `lookback` ruedas.
    Los indices son absolutos sobre el df completo."""
    n = len(df)
    start = max(window, n - lookback)
    highs, lows = df["high"].values, df["low"].values
    piv_h, piv_l = [], []
    for i in range(start, n - window):
        seg_h = highs[i - window:i + window + 1]
        seg_l = lows[i - window:i + window + 1]
        if highs[i] == seg_h.max():
            piv_h.append((i, float(highs[i])))
        if lows[i] == seg_l.min():
            piv_l.append((i, float(lows[i])))
    return piv_h, piv_l


def _cluster_levels(pivots, tol):
    """Agrupa pivotes en niveles: {level, touches, last_idx}."""
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


def _sr_levels(df, piv_h, piv_l, atr, price, mas):
    """Soportes/resistencias con toques + etiqueta de confluencia con MAs."""
    tol = max(0.5 * (atr or price * 0.01), price * 0.005)
    all_levels = _cluster_levels(piv_h + piv_l, tol)

    def _tag(level):
        tags = []
        for name in ("sma50", "sma200"):
            v = mas.get(name)
            if v and abs(level - v) <= tol:
                tags.append(name.upper())
        return tags

    sups = [dict(c, kind="sup", tags=_tag(c["level"]))
            for c in all_levels if c["level"] < price]
    ress = [dict(c, kind="res", tags=_tag(c["level"]))
            for c in all_levels if c["level"] > price]
    sups.sort(key=lambda c: -c["level"])   # mas cercano abajo primero
    ress.sort(key=lambda c: c["level"])    # mas cercano arriba primero

    def _pick(side):
        strong = [c for c in side if c["touches"] >= 2][:2]
        if not strong and side:
            strong = side[:1]              # al menos el mas cercano
        return strong

    return _pick(sups), _pick(ress), all_levels


def _channel(df, lookback=120):
    """Canal de regresion ±2σ. Devuelve dict con series top/bot (sufijo del
    ohlc, largo=lookback) para dibujar, valores actuales y pendiente %/rueda."""
    closes = df["close"].values[-lookback:]
    n = len(closes)
    if n < 40:
        return None
    xs = np.arange(n, dtype=float)
    slope, intercept = np.polyfit(xs, closes, 1)
    fit = intercept + slope * xs
    resid = closes - fit
    std = float(np.std(resid))
    top = fit + 2 * std
    bot = fit - 2 * std
    price = float(closes[-1])
    width = float(top[-1] - bot[-1])
    pos = (price - float(bot[-1])) / width if width > 0 else 0.5
    my = float(np.mean(closes))
    return {
        "top": [round(float(v), 2) for v in top],
        "bot": [round(float(v), 2) for v in bot],
        "upper": _r(top[-1]), "lower": _r(bot[-1]),
        "slope_pct": _r(slope / my * 100 if my else 0.0, 4),
        "pos": _r(pos, 2),
        "lookback": n,
    }


# ══════════════════════════════════════════════════════════════
#  FIGURAS TECNICAS — motor compartido patterns.py
# ══════════════════════════════════════════════════════════════
# Los detectores por reglas que nacieron aca (ruptura, doble techo/suelo,
# triangulo/cuña, cruce, divergencia, canal/estructura) se EXTRAJERON a
# patterns.py para que todos los analisis (escaner, recomendaciones, cartera)
# usen la misma implementacion — y ganaron figuras nuevas (triple techo, HCH,
# banderas) + contexto Fibonacci. Este wrapper mantiene la interfaz historica.

def _detect_patterns(df, piv_h, piv_l, all_levels, atr, price,
                     sma50_arr, sma200_arr, rsi_series, channel):
    """(figura principal, direccion de estructura, texto, fibonacci) via
    patterns.detect — con cruce de MAs y fallback de canal incluidos (el
    pulso siempre tiene algo que decir). piv/levels/channel se recomputan
    adentro con las mismas primitivas; los params quedan por compatibilidad."""
    res = patterns.detect(
        df["high"].tolist(), df["low"].tolist(), df["close"].tolist(),
        rsi=list(rsi_series) if rsi_series is not None else None,
        sma50=sma50_arr, sma200=sma200_arr,
        include_cross=True, include_fallback=True,
        dates=df["date"].tolist())
    struct_dir, struct_txt = res["structure"]
    return res["pattern"], struct_dir, struct_txt, res["fibonacci"]


# ══════════════════════════════════════════════════════════════
#  CONDICIONES DEL SISTEMA (compra y venta, con "que falta")
# ══════════════════════════════════════════════════════════════

def _condition_items(ind):
    macd_df, rsi_df, konc_df = ind["macd"], ind["rsi"], ind["koncorde"]
    hist, hist_prev = float(macd_df["hist"].iloc[-1]), float(macd_df["hist"].iloc[-2])
    rsi_v, rsi_prev3 = float(rsi_df["rsi"].iloc[-1]), float(rsi_df["rsi"].iloc[-4])
    marron = float(konc_df["marron"].iloc[-1])
    marron_prev = float(konc_df["marron"].iloc[-2])
    media = float(konc_df["media"].iloc[-1])

    _, bd = signals.check_buy_conditions(konc_df, macd_df, rsi_df)
    _, sd = signals.check_sell_conditions(konc_df, macd_df, rsi_df)

    def _buy_items():
        items = []
        if bd.get("macd_ok"):
            t = f"MACD: hist {hist:+.2f} negativo y girando al alza (cumple)"
        elif hist >= 0:
            t = f"MACD: hist {hist:+.2f} positivo — necesita zona negativa"
            if hist < hist_prev:
                t += ", viene cayendo (acercandose)"
        else:
            t = f"MACD: hist {hist:+.2f} negativo pero aun cayendo — falta el giro"
        items.append({"name": "MACD", "ok": bool(bd.get("macd_ok")), "text": t})
        if bd.get("rsi_ok"):
            t = f"RSI {rsi_v:.0f} < 30 (cumple)"
        else:
            t = f"RSI {rsi_v:.0f} — necesita <30"
            if rsi_v < 40 and rsi_v < rsi_prev3:
                t += " (acercandose)"
        items.append({"name": "RSI", "ok": bool(bd.get("rsi_ok")), "text": t})
        if bd.get("konc_ok"):
            t = "Koncorde: marron bajo media y girando al alza (cumple)"
        elif marron >= media:
            t = f"Koncorde: marron {marron:.1f} sobre media {media:.1f} — necesita zona baja"
        else:
            t = "Koncorde: marron bajo media pero sin giro al alza todavia"
        items.append({"name": "Koncorde", "ok": bool(bd.get("konc_ok")), "text": t})
        return items

    def _sell_items():
        items = []
        if sd.get("macd_ok"):
            t = f"MACD: hist {hist:+.2f} positivo y girando a la baja (cumple)"
        elif hist <= 0:
            t = f"MACD: hist {hist:+.2f} negativo — necesita zona positiva"
        else:
            t = f"MACD: hist {hist:+.2f} positivo pero aun subiendo — falta el giro"
            if hist < hist_prev:
                t = f"MACD: hist {hist:+.2f} positivo y aflojando (acercandose)"
        items.append({"name": "MACD", "ok": bool(sd.get("macd_ok")), "text": t})
        if sd.get("rsi_ok"):
            t = f"RSI {rsi_v:.0f} > 70 (cumple)"
        else:
            t = f"RSI {rsi_v:.0f} — necesita >70"
            if rsi_v > 60 and rsi_v > rsi_prev3:
                t += " (acercandose)"
        items.append({"name": "RSI", "ok": bool(sd.get("rsi_ok")), "text": t})
        if sd.get("konc_ok"):
            t = "Koncorde: marron sobre media y girando a la baja (cumple)"
        elif marron <= media:
            t = f"Koncorde: marron {marron:.1f} bajo media {media:.1f} — necesita zona alta"
        else:
            t = "Koncorde: marron sobre media pero sin giro a la baja todavia"
        items.append({"name": "Koncorde", "ok": bool(sd.get("konc_ok")), "text": t})
        return items

    buy_items, sell_items = _buy_items(), _sell_items()
    return (
        {"met": int(bd.get("conditions_met", 0)), "items": buy_items},
        {"met": int(sd.get("conditions_met", 0)), "items": sell_items},
    )


# ══════════════════════════════════════════════════════════════
#  SESION (intradia) Y VEREDICTO
# ══════════════════════════════════════════════════════════════

def _session_fraction():
    now = datetime.now(_NY)
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now <= open_t:
        return None
    if now >= close_t:
        return 1.0
    return max((now - open_t).total_seconds() / (6.5 * 3600), 0.12)


def _session_read(df, intra):
    """Gap, posicion en el rango y RVOL de la sesion en curso o ultima rueda."""
    closes, vols = df["close"].values, df["volume"].values
    last_date = df["date"].iloc[-1]
    prev_close = float(closes[-2])
    d = datetime.strptime(last_date, "%Y-%m-%d")
    today_ny = datetime.now(_NY).date()
    frac = _session_fraction()
    is_today = (d.date() == today_ny)
    is_live = bool(is_today and frac is not None and frac < 1.0)
    label = "hoy" if is_today else f"{_DIAS[d.weekday()]} {d.day:02d}/{d.month:02d}"

    o = float(df["open"].iloc[-1])
    hi, lo = float(df["high"].iloc[-1]), float(df["low"].iloc[-1])
    c = float(closes[-1])
    if intra is not None and not intra.empty:
        try:
            day = intra[intra.index.date == d.date()]
            if len(day) >= 2:
                o = float(day["Open"].iloc[0])
                hi = float(day["High"].max())
                lo = float(day["Low"].min())
                c = float(day["Close"].iloc[-1])
        except Exception:
            pass

    gap = (o / prev_close - 1) * 100 if prev_close else None
    vs_open = (c / o - 1) * 100 if o else None
    rng = hi - lo
    range_pos = ((c - lo) / rng * 100) if rng > 0 else None

    avg_vol = float(np.mean(vols[-21:-1])) if len(vols) >= 21 else None
    rvol = None
    if avg_vol:
        v = float(vols[-1])
        if is_live and frac:
            v = v / frac
        rvol = v / avg_vol

    parts = []
    if gap is not None and abs(gap) >= 0.15:
        parts.append(f"abrio con gap {_pct(gap)}")
    else:
        parts.append("abrio plano")
    if vs_open is not None:
        parts.append(f"opera {_pct(vs_open)} desde la apertura" if is_live
                     else f"cerro {_pct(vs_open)} desde la apertura")
    if range_pos is not None:
        parts.append(f"en el {range_pos:.0f}% del rango del dia")
    if rvol is not None:
        vol_tag = " (volumen alto)" if rvol >= 1.5 else \
                  (" (volumen bajo)" if rvol <= 0.6 else "")
        parts.append(f"RVOL {rvol:.1f}x{vol_tag}")
    prefix = "Sesion en curso" if is_live else f"Ultima rueda ({label})"
    return {
        "date": last_date, "label": label, "is_live": is_live,
        "gap_pct": _r(gap, 2), "vs_open_pct": _r(vs_open, 2),
        "range_pos": _r(range_pos, 0), "rvol": _r(rvol, 2),
        "text": f"{prefix}: " + ", ".join(parts),
    }


def _verdict(price, mas, channel, hist, hist_prev, rsi_v, struct_dir, pattern):
    score, factors = 0, []

    def add(pts, txt):
        nonlocal score
        score += pts
        factors.append(f"{txt} ({pts:+d})")

    if mas.get("sma200"):
        add(2 if price > mas["sma200"] else -2,
            "Precio " + ("sobre" if price > mas["sma200"] else "bajo") + " SMA200")
    if mas.get("sma50"):
        add(1 if price > mas["sma50"] else -1,
            "Precio " + ("sobre" if price > mas["sma50"] else "bajo") + " SMA50")
    if channel and channel["slope_pct"] is not None:
        if channel["slope_pct"] >= 0.04:
            add(1, "Canal de tendencia ascendente")
        elif channel["slope_pct"] <= -0.04:
            add(-1, "Canal de tendencia descendente")
    add(1 if hist > 0 else -1, "MACD hist " + ("positivo" if hist > 0 else "negativo"))
    add(1 if hist > hist_prev else -1,
        "Momentum MACD " + ("mejorando" if hist > hist_prev else "empeorando"))
    if rsi_v >= 55:
        add(1, f"RSI {rsi_v:.0f} en zona alta")
    elif rsi_v <= 45:
        add(-1, f"RSI {rsi_v:.0f} en zona baja")
    if struct_dir:
        add(2 * struct_dir, "Estructura de pivotes " +
            ("alcista" if struct_dir > 0 else "bajista"))
    if pattern and pattern["name"].startswith("Divergencia"):
        add(1 if pattern["direction"] == "alcista" else -1, pattern["name"])

    if score >= 5:
        bias, label = "alcista", "SESGO ALCISTA FUERTE"
    elif score >= 2:
        bias, label = "alcista", "SESGO ALCISTA"
    elif score <= -5:
        bias, label = "bajista", "SESGO BAJISTA FUERTE"
    elif score <= -2:
        bias, label = "bajista", "SESGO BAJISTA"
    else:
        bias, label = "neutral", "SIN SESGO CLARO"
    return {"bias": bias, "label": label, "score": score, "factors": factors}


def _build_reading(verdict, sups, ress, sig, buy_c, sell_c, price, high_52w):
    frases = []
    # 1) sesgo con sus factores mas fuertes en la direccion del score
    sign = "+" if verdict["score"] >= 0 else "-"

    def _decap(f):
        return f[0].lower() + f[1:] if len(f) > 1 and f[1].islower() else f

    fac = [_decap(f.rsplit(" (", 1)[0]) for f in verdict["factors"]
           if f"({sign}" in f][:3]
    if verdict["bias"] == "neutral":
        frases.append("Cuadro mixto: " + (", ".join(fac[:2])
                      if fac else "señales cruzadas entre tendencia y momentum") + ".")
    else:
        frases.append(f"Sesgo {verdict['bias']}: " + ", ".join(fac) + ".")

    # 2) niveles operativos
    sup = sups[0] if sups else None
    res = ress[0] if ress else None
    if verdict["bias"] == "neutral" and sup and res:
        frases.append(
            f"Rango operativo {_fmt_usd(_r(sup['level']))}–{_fmt_usd(_r(res['level']))}: "
            f"la ruptura de un extremo define la proxima pierna.")
    elif verdict["bias"] == "bajista":
        if res and sup:
            frases.append(
                f"Mientras no recupere {_fmt_usd(_r(res['level']))} el rebote es "
                f"vulnerable; bajo {_fmt_usd(_r(sup['level']))} se acelera la caida.")
        elif sup:
            frases.append(f"El piso a vigilar es {_fmt_usd(_r(sup['level']))} "
                          f"({sup['touches']} toques): perderlo abre mas caida.")
    else:
        if sup and res:
            frases.append(
                f"Mientras aguante {_fmt_usd(_r(sup['level']))} ({sup['touches']} toques) "
                f"el cuadro sigue constructivo; sobre {_fmt_usd(_r(res['level']))} "
                f"hay continuacion, perder el piso lo anula.")
        elif sup:
            extra = ""
            if high_52w and price >= 0.99 * high_52w:
                extra = " — precio en zona de maximos del año, sin techos por encima"
            frases.append(f"Soporte clave {_fmt_usd(_r(sup['level']))} "
                          f"({sup['touches']} toques){extra}.")

    # 3) estado del sistema (señal 3/3 o cuanto falta)
    if sig["signal"] == "BUY":
        frases.append("El sistema tiene señal de COMPRA activa (3/3 condiciones).")
    elif sig["signal"] == "SELL":
        frases.append("El sistema tiene señal de VENTA activa (3/3 condiciones).")
    else:
        b, s = buy_c["met"], sell_c["met"]
        if b >= 2:
            falta = next((i["text"] for i in buy_c["items"] if not i["ok"]), "")
            frases.append(f"Al sistema le falta 1 condicion para señal de compra: {falta}.")
        elif s >= 2:
            falta = next((i["text"] for i in sell_c["items"] if not i["ok"]), "")
            frases.append(f"Al sistema le falta 1 condicion para señal de venta: {falta}.")
    return " ".join(frases)


# ══════════════════════════════════════════════════════════════
#  PULSO COMPLETO
# ══════════════════════════════════════════════════════════════

def _build_pulse():
    df_full = _download_daily()
    if df_full is None:
        return {"error": "yfinance no devolvio datos para SPY"}
    intra = _download_intraday()

    # ANALISIS sobre cierres confirmados; la barra viva queda solo para el
    # header (precio/Δ%) y la lectura de sesion (df_full)
    df = _drop_partial_bar(df_full)

    ind = indicators.calculate_all(df)
    sig = signals.generate_signal(ind)

    closes = df["close"]
    price = float(closes.iloc[-1])                     # ultimo cierre confirmado
    live_close = float(df_full["close"].iloc[-1])      # cotizacion viva (header)
    dropped = len(df) < len(df_full)
    prev_close = price if dropped else float(closes.iloc[-2])
    change_pct = (live_close / prev_close - 1) * 100 if prev_close else None
    atr = _atr(df)
    n = len(df)

    # MAs (series completas para el chart, ultimo valor para el analisis)
    mas_payload, mas_val = {}, {}
    for p in (20, 50, 200):
        s = indicators.sma(closes, p)
        mas_payload[f"sma{p}"] = [_r(x) for x in s.tolist()]
        v = _r(s.iloc[-1])
        mas_payload[f"sma{p}_val"] = v
        mas_val[f"sma{p}"] = v

    piv_h, piv_l = _pivots(df)
    sups, ress, all_levels = _sr_levels(df, piv_h, piv_l, atr, price, mas_val)
    channel = _channel(df)
    rsi_series = ind["rsi"]["rsi"].values
    pattern, struct_dir, struct_txt, fib = _detect_patterns(
        df, piv_h, piv_l, all_levels, atr, price,
        mas_payload["sma50"], mas_payload["sma200"], rsi_series, channel)

    hist = float(ind["macd"]["hist"].iloc[-1])
    hist_prev = float(ind["macd"]["hist"].iloc[-2])
    rsi_v = float(ind["rsi"]["rsi"].iloc[-1])
    marron = float(ind["koncorde"]["marron"].iloc[-1])
    media = float(ind["koncorde"]["media"].iloc[-1])
    azul = float(ind["koncorde"]["azul"].iloc[-1])

    buy_c, sell_c = _condition_items(ind)
    session = _session_read(df_full, intra)  # sesion en curso: barra viva
    verdict = _verdict(price, mas_val, channel, hist, hist_prev, rsi_v,
                       struct_dir, pattern)
    high_52w = (float(df_full["high"].iloc[-252:].max())
                if len(df_full) >= 252 else None)  # un maximo 52w es un hecho, no una señal

    # --- textos de las lineas ---
    rsi_zone = ("sobreventa" if rsi_v < 30 else "zona baja" if rsi_v < 45 else
                "neutral" if rsi_v < 55 else "neutral-alto" if rsi_v < 65 else
                "alto" if rsi_v <= 70 else "sobrecompra")
    mom = (f"MACD hist {hist:+.2f} {'subiendo' if hist > hist_prev else 'cayendo'} · "
           f"RSI {rsi_v:.0f} ({rsi_zone}) · Koncorde: manos fuertes "
           f"{'comprando' if azul > 0 else 'vendiendo'} (azul {azul:+.1f}), "
           f"marron {'sobre' if marron > media else 'bajo'} su media")

    def _lvl_txt(c, kind):
        dist = (c["level"] / price - 1) * 100
        tags = (" + " + "/".join(c["tags"])) if c.get("tags") else ""
        return (f"{'Soporte' if kind == 'sup' else 'Resistencia'} "
                f"{_fmt_usd(_r(c['level']))} ({c['touches']} toques{tags}) "
                f"a {_pct(dist)}")

    lvl_parts = []
    if sups:
        lvl_parts.append(_lvl_txt(sups[0], "sup"))
    if ress:
        lvl_parts.append(_lvl_txt(ress[0], "res"))
    else:
        lvl_parts.append("sin techos en el ultimo año — precio en zona de maximos")
    levels_text = " · ".join(lvl_parts)

    reading = _build_reading(verdict, sups, ress, sig, buy_c, sell_c, price, high_52w)

    # --- payload de chart (mismo shape que el scanner + extras del pulso) ---
    ohlc = [{"time": df["date"].iloc[i],
             "open": _r(df["open"].iloc[i]), "high": _r(df["high"].iloc[i]),
             "low": _r(df["low"].iloc[i]), "close": _r(df["close"].iloc[i])}
            for i in range(n)]
    macd_df, rsi_df, konc_df = ind["macd"], ind["rsi"], ind["koncorde"]
    chart = {
        "ohlc": ohlc,
        "mas": mas_payload,
        "macd": {k: [_r(x) for x in macd_df[k].tolist()]
                 for k in ("macd", "signal", "hist")},
        "rsi": [_r(x, 1) for x in rsi_df["rsi"].tolist()],
        "koncorde": {k: [_r(x, 1) for x in konc_df[k].tolist()]
                     for k in ("verde", "marron", "azul", "media")},
    }
    sr_lines = ([{"level": _r(c["level"]), "touches": c["touches"], "kind": c["kind"]}
                 for c in sups + ress])

    return {
        "symbol": SYMBOL, "name": NAME,
        "updated": datetime.now(_NY).strftime("%Y-%m-%d %H:%M:%S"),
        # price = cotizacion viva (header); el analisis corre al cierre confirmado
        "price": _r(live_close), "prev_close": _r(prev_close),
        "change_pct": _r(change_pct, 2),
        "analysis_as_of": df["date"].iloc[-1],
        "high_52w": _r(high_52w),
        "verdict": verdict,
        "system": {"signal": sig["signal"], "label": sig["signal_label"],
                   "strength": _r(sig.get("strength"), 1)},
        "momentum": {"macd_hist": _r(hist), "rsi": _r(rsi_v, 1),
                     "koncorde_azul": _r(azul, 1), "text": mom},
        "levels": {"supports": [{"level": _r(c["level"]), "touches": c["touches"],
                                 "tags": c.get("tags", [])} for c in sups],
                   "resistances": [{"level": _r(c["level"]), "touches": c["touches"],
                                    "tags": c.get("tags", [])} for c in ress],
                   "text": levels_text},
        "pattern": pattern,
        "fibonacci": fib,
        # Velas japonesas recientes (timing corto, cierres confirmados)
        "candles": patterns.detect_candles(
            df["open"].tolist(), df["high"].tolist(),
            df["low"].tolist(), df["close"].tolist()),
        "conditions": {"buy": buy_c, "sell": sell_c},
        "session": session,
        "reading": reading,
        "chart": chart,
        "chart_extras": {
            "sr_lines": sr_lines,
            "channel": {"top": channel["top"], "bot": channel["bot"]} if channel else None,
        },
    }


def get_market_pulse(force=False):
    """Pulso cacheado (TTL 10 min). Ante error devuelve el ultimo valido."""
    now = time.time()
    if not force and _cache["data"] and now - _cache["ts"] < _TTL_S:
        return _cache["data"]
    with _lock:
        if not force and _cache["data"] and time.time() - _cache["ts"] < _TTL_S:
            return _cache["data"]
        try:
            data = _build_pulse()
        except Exception as e:
            data = {"error": f"{type(e).__name__}: {e}"}
        if data.get("error") and _cache["data"] and not _cache["data"].get("error"):
            return _cache["data"]      # stale > nada
        _cache["data"] = data
        _cache["ts"] = time.time()
        return data


if __name__ == "__main__":
    import json
    p = get_market_pulse()
    slim = {k: v for k, v in p.items() if k not in ("chart", "chart_extras")}
    print(json.dumps(slim, indent=2, ensure_ascii=False, default=str))
    if p.get("chart"):
        print(f"\nchart: {len(p['chart']['ohlc'])} velas | "
              f"sr_lines: {p['chart_extras']['sr_lines']} | "
              f"channel: {'si' if p['chart_extras']['channel'] else 'no'}")
