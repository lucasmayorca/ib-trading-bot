"""Enriquecimiento del escaner: contexto de mercado y opinion externa.

Dos familias de metricas por simbolo, servidas como bloque `ext` en
/api/data y /api/etf-data (local y cloud):

- Mercado (beta propio, fuerza relativa 30d, volumen relativo): se calculan
  con UNA descarga batcheada de yfinance (cierres+volumenes diarios de todo
  el universo + SPY) refrescada cada MARKET_TTL. Beta propio = cov/var de
  retornos diarios 12m vs SPY (el de yfinance es 5y mensual y viene vacio
  en muchos ETFs).
- Wall Street (consenso de analistas, precio objetivo, insiders 90d, short
  interest): fetch por simbolo via yf.Ticker con TTL de 24h, llenado
  progresivo en un thread de fondo con rate-limit.

Compartido por vista_web.py (local) y cloud/server.py. El bridge NO lo usa
(la tabla se enriquece server-side, sin reinstalar nada en los usuarios).
"""

import json
import math
import os
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf

MARKET_TTL = 15 * 60      # refresco de beta/RS/RVOL (descarga batcheada)
WALLST_TTL = 24 * 3600    # refresco por simbolo de analistas/insiders/short
FETCH_PAUSE = 0.6         # rate-limit entre simbolos del fetch Wall Street
BETA_WINDOW = 252         # ruedas para el beta (12m)
BETA_MIN_OBS = 60         # minimo de retornos superpuestos con SPY
RS_WINDOW = 30            # ruedas para la fuerza relativa
RVOL_WINDOW = 20          # ruedas del promedio de volumen

_NY = ZoneInfo("America/New_York")

_market_metrics = {}      # {sym: {"beta":..,"rs30":..,"rvol":..}}
_market_lock = threading.Lock()
_wallst_cache = {}        # {sym: {"ts":..,"an_mean":..,...}}
_wallst_lock = threading.Lock()
_persist_path = None
_started = False


def _clean(v, digits=2):
    """float redondeado o None (nunca NaN/inf — rompen el JSON)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, digits)


# ══════════════════════════════════════════════════════════════
#  MERCADO: beta propio + RS 30d + RVOL (descarga batcheada)
# ══════════════════════════════════════════════════════════════

def _session_fraction():
    """Fraccion de la sesion USA transcurrida (para proyectar el volumen del
    dia en curso). None si todavia no abrio (la ultima vela seria de ayer)."""
    now = datetime.now(_NY)
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now <= open_t:
        return None
    if now >= close_t:
        return 1.0
    frac = (now - open_t).total_seconds() / (6.5 * 3600)
    return max(frac, 0.12)  # piso: los primeros minutos no proyectan x8


def _extract_series(df, sym, field):
    try:
        if isinstance(df.columns, pd.MultiIndex):
            s = df[sym][field]
        else:
            s = df[field]
        return s.dropna()
    except (KeyError, TypeError):
        return None


def _compute_symbol_metrics(closes, volumes, spy_ret):
    out = {}
    # Beta propio + RS 30d vs SPY
    if closes is not None and len(closes) >= 2 and spy_ret is not None:
        ret = closes.pct_change().dropna()
        both = pd.concat([ret, spy_ret], axis=1, join="inner").dropna()
        both = both.iloc[-BETA_WINDOW:]
        if len(both) >= BETA_MIN_OBS:
            r_sym = both.iloc[:, 0].to_numpy()
            r_spy = both.iloc[:, 1].to_numpy()
            var = float(np.var(r_spy))
            if var > 0:
                out["beta"] = _clean(float(np.cov(r_sym, r_spy)[0][1]) / var)
        if len(both) > RS_WINDOW:
            r30_sym = (1 + both.iloc[-RS_WINDOW:, 0]).prod() - 1
            r30_spy = (1 + both.iloc[-RS_WINDOW:, 1]).prod() - 1
            out["rs30"] = _clean((r30_sym - r30_spy) * 100, 1)
    # RVOL: ultima rueda vs promedio 20, proyectando el dia en curso
    if volumes is not None and len(volumes) >= RVOL_WINDOW + 1:
        avg = float(volumes.iloc[-(RVOL_WINDOW + 1):-1].mean())
        last = float(volumes.iloc[-1])
        if avg > 0 and last > 0:
            try:
                last_date = pd.Timestamp(volumes.index[-1]).date()
                if last_date == datetime.now(_NY).date():
                    frac = _session_fraction()
                    if frac:
                        last = last / frac
            except Exception:
                pass
            out["rvol"] = _clean(last / avg)
    return out


def refresh_market_metrics(symbols):
    """Una descarga batcheada de yfinance para todo el universo + SPY y
    recalculo de beta/RS/RVOL. Best-effort: nunca levanta excepcion."""
    syms = sorted({s for s in symbols if s and isinstance(s, str)})
    if not syms:
        return
    if "SPY" not in syms:
        syms.append("SPY")
    try:
        df = yf.download(
            tickers=syms, period="2y", interval="1d", group_by="ticker",
            threads=False, progress=False, auto_adjust=True,
        )
    except Exception as e:
        print(f"[ENRICH] market download fallo: {e}", flush=True)
        return
    if df is None or df.empty:
        return

    spy_close = _extract_series(df, "SPY", "Close")
    spy_ret = spy_close.pct_change().dropna() if spy_close is not None else None

    fresh = {}
    for sym in syms:
        try:
            closes = _extract_series(df, sym, "Close")
            volumes = _extract_series(df, sym, "Volume")
            m = _compute_symbol_metrics(closes, volumes, spy_ret)
            if m:
                fresh[sym] = m
        except Exception:
            continue
    if fresh:
        with _market_lock:
            _market_metrics.update(fresh)
        print(f"[ENRICH] mercado actualizado: {len(fresh)} simbolos", flush=True)


# ══════════════════════════════════════════════════════════════
#  WALL STREET: analistas + insiders + short interest (TTL 24h)
# ══════════════════════════════════════════════════════════════

def _fetch_wallst(sym):
    t = yf.Ticker(sym)
    try:
        info = t.info or {}
    except Exception:
        info = {}
    out = {"ts": time.time()}
    out["an_mean"] = _clean(info.get("recommendationMean"), 2)
    out["an_key"] = info.get("recommendationKey") or None
    n = info.get("numberOfAnalystOpinions")
    out["an_n"] = int(n) if isinstance(n, (int, float)) and n > 0 else None
    out["an_target"] = _clean(info.get("targetMeanPrice"))
    out["an_low"] = _clean(info.get("targetLowPrice"))
    out["an_high"] = _clean(info.get("targetHighPrice"))
    sp = info.get("shortPercentOfFloat")
    out["short_pct"] = _clean(sp * 100, 1) if isinstance(sp, (int, float)) else None
    is_etf = (info.get("quoteType") or "").upper() == "ETF"

    # Insiders 90d (misma logica que _fetch_fundamentals de vista_web)
    if not is_etf:
        try:
            ins = t.insider_transactions
            if ins is not None and not ins.empty:
                if "Start Date" in ins.columns:
                    ins = ins.copy()
                    ins["Start Date"] = pd.to_datetime(ins["Start Date"], errors="coerce")
                    cutoff = pd.Timestamp.now() - pd.Timedelta(days=90)
                    recent = ins[ins["Start Date"] >= cutoff]
                else:
                    recent = ins.head(10)
                if "Text" in recent.columns and len(recent) > 0:
                    out["ins_buys"] = int(recent["Text"].str.contains(
                        "Purchase|Buy|Acquisition", case=False, na=False).sum())
                    out["ins_sells"] = int(recent["Text"].str.contains(
                        "Sale|Sell|Disposition", case=False, na=False).sum())
                    last = []
                    for _, row in recent.head(3).iterrows():
                        d = row.get("Start Date")
                        d_str = d.strftime("%d/%m") if isinstance(d, pd.Timestamp) and not pd.isna(d) else ""
                        txt = str(row.get("Text", ""))[:80]
                        if txt:
                            last.append((d_str + " " + txt).strip())
                    if last:
                        out["ins_last"] = last
        except Exception:
            pass
    return out


def _load_persisted():
    if not _persist_path or not os.path.exists(_persist_path):
        return
    try:
        with open(_persist_path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            now = time.time()
            valid = {s: v for s, v in data.items()
                     if isinstance(v, dict) and now - v.get("ts", 0) < WALLST_TTL}
            with _wallst_lock:
                _wallst_cache.update(valid)
            print(f"[ENRICH] wall street restaurado: {len(valid)} simbolos", flush=True)
    except Exception as e:
        print(f"[ENRICH] no se pudo restaurar cache: {e}", flush=True)


def _persist():
    if not _persist_path:
        return
    try:
        with _wallst_lock:
            snapshot = dict(_wallst_cache)
        tmp = _persist_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snapshot, f)
        os.replace(tmp, _persist_path)
    except Exception as e:
        print(f"[ENRICH] persistencia fallo: {e}", flush=True)


# ══════════════════════════════════════════════════════════════
#  THREADS DE FONDO + API PUBLICA
# ══════════════════════════════════════════════════════════════

def _market_loop(symbols_getter):
    while True:
        try:
            syms = symbols_getter() or []
            if syms:
                refresh_market_metrics(syms)
                time.sleep(MARKET_TTL)
            else:
                time.sleep(30)  # universo todavia vacio (boot)
        except Exception as e:
            print(f"[ENRICH] market loop error: {e}", flush=True)
            time.sleep(60)


def _wallst_loop(symbols_getter):
    while True:
        try:
            syms = list(dict.fromkeys(symbols_getter() or []))
            fetched = 0
            for sym in syms:
                with _wallst_lock:
                    entry = _wallst_cache.get(sym)
                if entry and time.time() - entry.get("ts", 0) < WALLST_TTL:
                    continue
                try:
                    data = _fetch_wallst(sym)
                    with _wallst_lock:
                        _wallst_cache[sym] = data
                    fetched += 1
                except Exception:
                    pass
                time.sleep(FETCH_PAUSE)
            if fetched:
                print(f"[ENRICH] wall street: {fetched} simbolos actualizados", flush=True)
                _persist()
            time.sleep(120)
        except Exception as e:
            print(f"[ENRICH] wallst loop error: {e}", flush=True)
            time.sleep(120)


def start_background(symbols_getter, persist_path=None):
    """Lanza los dos threads de fondo (daemon). Idempotente.

    symbols_getter: callable sin args que devuelve el universo actual de
    simbolos (puede estar vacio durante el boot; se reintenta).
    persist_path: JSON para sobrevivir reinicios (None = sin disco, p.ej.
    cloud en Railway donde el filesystem es efimero)."""
    global _started, _persist_path
    if _started:
        return
    _started = True
    _persist_path = persist_path
    _load_persisted()
    threading.Thread(target=_market_loop, args=(symbols_getter,), daemon=True).start()
    threading.Thread(target=_wallst_loop, args=(symbols_getter,), daemon=True).start()
    print("[ENRICH] threads de enriquecimiento iniciados", flush=True)


def get_ext(sym):
    """Bloque `ext` para el payload del escaner. Campos ausentes = None/omitidos
    (el frontend muestra '---' mientras el fondo los completa)."""
    out = {}
    with _market_lock:
        m = _market_metrics.get(sym)
    if m:
        out.update(m)
    with _wallst_lock:
        w = _wallst_cache.get(sym)
    if w:
        out.update({k: v for k, v in w.items() if k != "ts" and v is not None})
    return out or None
