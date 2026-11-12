"""Serie historica de P/E por ACCION, para dibujarla como panel del stack.

Por que esto si se puede y el P/E de un ETF no (ver CLAUDE.md -> Pulso del
Mercado): una accion reporta EPS, asi que el P/E **tiene serie propia** — y sin
serie no hay percentil, y sin percentil "27x" no dice si esta caro. El ETF de
indice no publica EPS en yfinance (`forwardPE` viene None) y su `trailingPE` es
un escalar que ademas ordena al reves que la fuente del usuario.

Reconstruccion (medida contra el `trailingPE` del propio yfinance sobre 20
acciones: **17 cierran con 0.0-1.1% de error**; los 3 que no son MRNA, INTC y F,
que tienen EPS NEGATIVO y por lo tanto no tienen P/E — yfinance tampoco reporta
uno):

  TTM EPS = anual (4-5 ejercicios de `income_stmt`) refinado, en el tramo
  reciente, por la suma de los 4 trimestres de `quarterly_income_stmt`.

El refinamiento trimestral NO es opcional: con una empresa que crece rapido el
anual queda viejo. Medido en NVDA, cuyo ejercicio cerro en enero: solo con el
anual el P/E daba **33.5 contra un trailing real de 20.7** (60% de error) — un
numero equivocado con cara de dato, que es la peor clase de error de este
proyecto.

**El EPS de yfinance YA viene ajustado por split** (verificado: NVDA FY2024 =
1.19, posterior al 10:1 de junio 2024; WMT FY2024 = 1.913, posterior al 3:1), o
sea que esta en la misma base que los cierres ajustados del chart. Si algun dia
dejara de estarlo, el P/E saltaria 10x en la fecha del split sin ningun motivo
visible: el test de paridad contra `trailingPE` es lo que lo cazaria.

**Lag de reporte**: un EPS no se conoce el dia que cierra el ejercicio. Cada
punto rige desde el cierre del periodo + 60 dias (anual) o + 45 (trimestral),
la misma disciplina point-in-time que el percentil de concentracion. Sin eso la
serie muestra un P/E que nadie podia calcular ese dia.

**Alcance**: la historia llega hasta donde llega el anual, ~3.5-4 años, no 5.
En la vista de 5A el tramo viejo queda vacio a proposito — es lo que hay.
"""
import json
import os
import threading
import time

LAG_ANUAL_D = 60
LAG_TRIM_D = 45
TTL_S = 7 * 24 * 3600          # el EPS solo cambia cuando hay balance
MIN_PUNTOS_BANDA = 120         # ~6 meses de ruedas para que un cuartil signifique algo
CACHE_FILE = "valuation_cache.json"

_lock = threading.Lock()
_mem = {}                      # sym -> {"ts": epoch, "pts": [[iso, eps], ...]}
_persist_path = None


# ───────────────────────── descarga (IO) ─────────────────────────

def _eps_points_fresh(symbol):
    """[(fecha_efectiva_iso, ttm_eps)] de anual + TTM trimestral. [] si no hay."""
    import pandas as pd
    import yfinance as yf
    from enrichment import _yf_symbol

    t = yf.Ticker(_yf_symbol(symbol))
    pts = []

    def _fila_eps(df):
        if df is None or getattr(df, "empty", True):
            return None
        for i in df.index:
            if "Diluted EPS" in str(i):
                return i
        for i in df.index:
            if "Basic EPS" in str(i):
                return i
        return None

    try:
        a = t.income_stmt
        f = _fila_eps(a)
        if f is not None:
            for c, v in zip(a.columns, a.loc[f]):
                if pd.notna(v):
                    eff = pd.Timestamp(c) + pd.Timedelta(days=LAG_ANUAL_D)
                    pts.append((eff.strftime("%Y-%m-%d"), float(v)))
    except Exception:
        pass

    try:
        q = t.quarterly_income_stmt
        f = _fila_eps(q)
        if f is not None:
            cols = list(q.columns)[::-1]              # viejo -> nuevo
            vals = [q.loc[f, c] for c in cols]
            for i in range(3, len(cols)):
                v4 = vals[i - 3:i + 1]
                if all(pd.notna(x) for x in v4):
                    eff = pd.Timestamp(cols[i]) + pd.Timedelta(days=LAG_TRIM_D)
                    pts.append((eff.strftime("%Y-%m-%d"),
                                float(sum(float(x) for x in v4))))
    except Exception:
        pass

    # Mismo dia por las dos vias: gana el trimestral (mas fresco). Se ordena por
    # fecha y el ultimo de cada fecha es el que queda.
    pts.sort(key=lambda p: p[0])
    out, vistos = [], {}
    for f_, v in pts:
        vistos[f_] = v
    for f_ in sorted(vistos):
        out.append((f_, vistos[f_]))
    return out


def eps_points(symbol, refresh=False):
    """Puntos cacheados. None si todavia no se bajaron (no bloquea el scan)."""
    now = time.time()
    with _lock:
        hit = _mem.get(symbol)
        if hit and not refresh and now - hit["ts"] < TTL_S:
            return [(p[0], p[1]) for p in hit["pts"]]
    return None


def fetch_into_cache(symbol):
    """Baja y cachea. Devuelve los puntos (o [] si el simbolo no tiene EPS)."""
    try:
        pts = _eps_points_fresh(symbol)
    except Exception as e:
        print(f"[VAL] {symbol}: {type(e).__name__} {e}", flush=True)
        pts = []
    with _lock:
        _mem[symbol] = {"ts": time.time(), "pts": [[f, v] for f, v in pts]}
    return pts


# ───────────────────────── calculo (puro) ─────────────────────────

def pe_series(dates, closes, pts):
    """P/E alineado a `dates` (indice-paralelo). None donde no hay P/E.

    Puro: sin IO ni red, para que local y cloud den exactamente lo mismo y el
    test pueda fijar casos a mano.

    Devuelve None (no una lista de nulls) si no hay un solo valor: una serie
    entera vacia no es un panel, es un panel que no va.
    """
    if not dates or not closes or not pts or len(dates) != len(closes):
        return None
    pts = sorted(pts, key=lambda p: p[0])
    out, j, eps = [], 0, None
    for d, c in zip(dates, closes):
        ds = str(d)[:10]
        while j < len(pts) and pts[j][0] <= ds:
            eps = pts[j][1]
            j += 1
        # EPS <= 0 => la empresa no tiene ganancias: el P/E NO EXISTE. Es un
        # hueco honesto, no un cero ni un numero grande.
        if eps is None or eps <= 0 or c is None:
            out.append(None)
            continue
        try:
            out.append(round(float(c) / eps, 2))
        except Exception:
            out.append(None)
    return out if any(v is not None for v in out) else None


def pe_panel(dates, closes, pts):
    """Payload del panel: misma forma que `chart.conc` del pulso.

    Comparten forma A PROPOSITO: el JS dibuja los dos con el mismo codigo, asi
    que un arreglo en el panel vale para los dos y no hay una segunda copia que
    se desincronice.
    """
    vals = pe_series(dates, closes, pts)
    if vals is None:
        return None
    v = sorted(x for x in vals if x is not None)
    if len(v) < MIN_PUNTOS_BANDA:
        return None

    def _p(q):
        i = (len(v) - 1) * q
        lo, hi = int(i), min(int(i) + 1, len(v) - 1)
        return round(v[lo] + (v[hi] - v[lo]) * (i - lo), 2)

    # El P/E de HOY es el de la ULTIMA barra, no el ultimo valor no-nulo: una
    # empresa que entro en perdidas NO tiene P/E hoy aunque lo haya tenido hace
    # seis meses. Cazado con INTC, que mostraba "59.32, p74" con EPS negativo
    # (yfinance tampoco reporta trailingPE ahi). La historia se sigue
    # dibujando — es real — pero sin valor ni percentil de hoy.
    hoy = vals[-1]
    ventana = f"{len(v) / 252:.1f}A"
    pct = (int(round(sum(1 for x in v if x < hoy) / len(v) * 100))
           if hoy is not None else None)
    return {
        "pair": "P/E",
        "vals": vals,
        "q1": _p(0.25), "med": _p(0.50), "q3": _p(0.75),
        "pct": pct,
        "sin_pe_hoy": hoy is None,
        "ventana": ventana,
        "ruedas": len(v),
        "pcts": ([{"ventana": ventana, "pct": pct, "ruedas": len(v)}]
                 if pct is not None else []),
    }


def attach_pe(analysis, symbol):
    """Adjunta `chart.pe` si el simbolo tiene EPS cacheado. Server-side puro
    sobre el chart que YA viaja en cada analisis ⇒ el bridge no se toca."""
    if not analysis:
        return analysis
    ch = analysis.get("chart") or {}
    ohlc = ch.get("ohlc") or []
    if not ohlc:
        return analysis
    pts = eps_points(symbol)
    if not pts:
        return analysis
    panel = pe_panel([b.get("time") for b in ohlc],
                     [b.get("close") for b in ohlc], pts)
    if panel:
        ch["pe"] = panel
        analysis["chart"] = ch
    return analysis


# ───────────────────────── persistencia + warmer ─────────────────────────

def load(path):
    global _persist_path
    _persist_path = path
    try:
        with open(path) as f:
            data = json.load(f)
        with _lock:
            _mem.update({k: v for k, v in data.items() if isinstance(v, dict)})
        print(f"[VAL] cache: {len(_mem)} simbolos", flush=True)
    except Exception:
        pass


def save():
    if not _persist_path:
        return
    try:
        with _lock:
            data = dict(_mem)
        tmp = _persist_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, _persist_path)
    except Exception as e:
        print(f"[VAL] no se pudo persistir: {e}", flush=True)


def start_background(symbols_getter, persist_path=None, rate_s=0.6):
    """Thread que baja el EPS que falta, con rate-limit. El scan nunca espera:
    hasta que el simbolo este en cache, `attach_pe` no adjunta nada y el panel
    simplemente no aparece."""
    if persist_path:
        load(persist_path)

    def _loop():
        while True:
            try:
                syms = list(symbols_getter() or [])
                now = time.time()
                pend = []
                with _lock:
                    for s in syms:
                        h = _mem.get(s)
                        if not h or now - h["ts"] >= TTL_S:
                            pend.append(s)
                for s in pend:
                    fetch_into_cache(s)
                    time.sleep(rate_s)
                if pend:
                    save()
                    print(f"[VAL] EPS actualizado: {len(pend)} simbolos", flush=True)
            except Exception as e:
                print(f"[VAL] loop: {type(e).__name__} {e}", flush=True)
            time.sleep(600)

    threading.Thread(target=_loop, daemon=True).start()
