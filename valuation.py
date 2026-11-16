"""Serie historica de P/E por ACCION, para dibujarla como panel del stack.

Por que esto si se puede y el P/E de un ETF no (ver CLAUDE.md -> Pulso del
Mercado): una accion reporta EPS, asi que el P/E **tiene serie propia** — y sin
serie no hay percentil, y sin percentil "27x" no dice si esta caro. El ETF de
indice no publica EPS en yfinance (`forwardPE` viene None) y su `trailingPE` es
un escalar que ademas ordena al reves que la fuente del usuario.

FUENTES, en orden. La primera existe porque la pregunta correcta no era "que
me da yfinance" sino "de donde sale el dato":

  (1) **SEC EDGAR XBRL** (`data.sec.gov`, gratis y sin API key): cada EPS
      reportado, con la **fecha de presentacion real**. Da ~18 AÑOS de
      trimestres contra los 4-5 ejercicios de yfinance, y ademas elimina el
      supuesto del lag — no hay que estimar cuando se supo el dato, viene el
      `filed`. Tres cosas que hay que hacerle y que no son opcionales:
      a. **DEDUP POR PRIMERA PUBLICACION**: el mismo trimestre aparece muchas
         veces (el 10-K repite los trimestres del año, y las reexpresiones lo
         vuelven a publicar). AAPL: 341 registros para 73 trimestres. Se toma
         el de `filed` MAS VIEJO — es cuando el numero se supo.
      b. **Q4 DERIVADO**: nadie presenta un 10-Q del cuarto trimestre; va
         dentro del 10-K como ANUAL. Sin derivarlo (`anual − los 3 trimestres`)
         el TTM queda corrido un trimestre: medido, 6-9% bajo (AAPL 8.44 contra
         un trailing real de 8.98).
      c. **AJUSTE POR SPLIT, A MANO**: EDGAR publica el EPS en acciones **de la
         epoca**. El Q3-2008 de AAPL figura en 1.19 y hoy, tras el 7:1 de 2014
         y el 4:1 de 2020, equivale a 0.0425 — **28x**. Los cierres del chart si
         vienen ajustados, asi que sin dividir por el factor acumulado el P/E de
         2008 saldria 28 veces mas chico. Es la trampa que yfinance tapaba
         (su EPS ya viene ajustado) y que esta fuente deja al descubierto.
  (2) **yfinance** (`income_stmt` anual + `quarterly_income_stmt`): el camino
      viejo, ~4 ejercicios. Queda como respaldo para los emisores cuyos XBRL no
      tienen historia (medido: XOM publica 4 trimestres y ningun anual).

**GUARDA EN LOS DOS CAMINOS**: el TTM reconstruido se compara contra el
`trailingEps` que informa yfinance y, si no cierra, la serie **no se publica**.
La validacion que al principio hice a mano pasa a correr en runtime: una serie
de P/E que no cuadra con la fuente es exactamente "un numero equivocado que se
ve bien", que es lo que este proyecto trata como el peor error posible.

El refinamiento trimestral NO es opcional: con una empresa que crece rapido el
anual queda viejo. Medido en NVDA, cuyo ejercicio cerro en enero: solo con el
anual el P/E daba **33.5 contra un trailing real de 20.7** (60% de error) — un
numero equivocado con cara de dato, que es la peor clase de error de este
proyecto.

**El EPS de yfinance YA viene ajustado por split** (verificado: NVDA FY2024 =
1.19, posterior al 10:1 de junio 2024; WMT FY2024 = 1.913, posterior al 3:1).
El de EDGAR **no** — ver (1c). Son dos bases distintas y por eso cada camino
tiene su propio tratamiento.

**Lag de reporte**: un EPS no se conoce el dia que cierra el ejercicio. Por SEC
cada punto rige desde su `filed` REAL; por yfinance, que no lo trae, se estima
con cierre + 60 dias (anual) o + 45 (trimestral). Es la misma disciplina
point-in-time que el percentil de concentracion: sin eso la serie muestra un
P/E que nadie podia calcular ese dia.
"""
import json
import os
import threading
import time

LAG_ANUAL_D = 60          # solo para el camino yfinance, que no trae `filed`
LAG_TRIM_D = 45
SEC_UA = "ib-trading-bot (contacto: lucas.mayorca@r2capital.co)"   # la SEC lo exige
SEC_MIN_TRIM = 12         # con menos de 3 años, no vale la pena contra yfinance
SEC_CONCEPTS = ("EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted",
                "EarningsPerShareBasic")
VALID_TOL_PCT = 8.0       # el TTM reconstruido contra el trailingEps de yfinance
TTL_S = 7 * 24 * 3600          # el EPS solo cambia cuando hay balance
MIN_PUNTOS_BANDA = 120         # ~6 meses de ruedas para que un cuartil signifique algo
CACHE_FILE = "valuation_cache.json"

_lock = threading.Lock()
_mem = {}                      # sym -> {"ts": epoch, "pts": [[iso, eps], ...]}
_persist_path = None


# ───────────────────────── descarga (IO) ─────────────────────────

def _sec_cik_map(_cache={}):
    """ticker -> CIK. Una sola descarga por proceso."""
    if _cache:
        return _cache
    import requests
    r = requests.get("https://www.sec.gov/files/company_tickers.json",
                     headers={"User-Agent": SEC_UA}, timeout=30)
    r.raise_for_status()
    for v in r.json().values():
        _cache[v["ticker"]] = str(v["cik_str"]).zfill(10)
    return _cache


def _dur_dias(r):
    from datetime import date
    return (date.fromisoformat(r["end"]) - date.fromisoformat(r["start"])).days


def _primera_publicacion(rows, lo, hi):
    """Periodos de duracion [lo,hi] dias, quedandose con el `filed` MAS VIEJO.

    El mismo trimestre aparece muchas veces (el 10-K repite los del año, y una
    reexpresion lo vuelve a publicar): AAPL trae 341 registros para 73
    trimestres. El primero es cuando el numero se SUPO, que es lo unico que
    sirve para una serie point-in-time.
    """
    best = {}
    for r in rows:
        if not r.get("start") or not r.get("end"):
            continue
        try:
            d = _dur_dias(r)
        except Exception:
            continue
        if not (lo <= d <= hi):
            continue
        k = (r["start"], r["end"])
        if k not in best or r["filed"] < best[k]["filed"]:
            best[k] = r
    return best


def _sec_quarters(symbol):
    """[(fin_periodo, eps_crudo, filed)] trimestral, con el Q4 DERIVADO.

    Nadie presenta un 10-Q del cuarto trimestre: va dentro del 10-K como anual.
    Sin derivarlo (`anual − los 3 trimestres`) el TTM queda corrido un trimestre
    — medido, 6-9% bajo.
    """
    import time
    import requests
    from enrichment import _yf_symbol

    t = _yf_symbol(symbol)
    cik = _sec_cik_map().get(t) or _sec_cik_map().get(t.replace("-", "."))
    if not cik:
        return []
    rows = None
    for c in SEC_CONCEPTS:
        try:
            r = requests.get(
                f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{c}.json",
                headers={"User-Agent": SEC_UA}, timeout=30)
            if r.status_code == 200:
                u = r.json().get("units", {}).get("USD/shares")
                if u:
                    rows = u
                    break
        except Exception:
            pass
        finally:
            time.sleep(0.12)          # la SEC pide <=10 req/s
    if not rows:
        return []

    q = _primera_publicacion(rows, 80, 100)
    anual = _primera_publicacion(rows, 350, 380)
    out = {r["end"]: (r["val"], r["filed"]) for r in q.values()}
    for (sa, ea), ra in anual.items():
        dentro = [r for (s2, e2), r in q.items() if s2 >= sa and e2 <= ea]
        if len(dentro) == 3:
            v4 = ra["val"] - sum(r["val"] for r in dentro)
            # se supo cuando se presento el 10-K, no cuando cerro el trimestre
            if ea not in out or ra["filed"] < out[ea][1]:
                out[ea] = (round(v4, 4), ra["filed"])
    return sorted((k, v, f) for k, (v, f) in out.items())


def _factor_split(splits, fin_periodo):
    """Producto de los splits POSTERIORES al periodo.

    EDGAR publica el EPS en acciones de LA EPOCA y los cierres del chart vienen
    ajustados: sin esto, el P/E de 2008 de AAPL saldria 28 veces mas chico.
    """
    f = 1.0
    if splits is None:
        return f
    try:
        for ix, v in splits.items():
            if str(ix)[:10] > fin_periodo:
                f *= float(v)
    except Exception:
        pass
    return f


def _eps_from_sec(symbol, splits):
    """Puntos TTM desde SEC EDGAR: ~18 años y con la fecha de presentacion real."""
    q = _sec_quarters(symbol)
    if len(q) < SEC_MIN_TRIM:
        return []
    adj = [(fin, val / _factor_split(splits, fin), filed) for fin, val, filed in q]
    pts = []
    for i in range(3, len(adj)):
        v4 = adj[i - 3:i + 1]
        # rige desde que se publico el ULTIMO de los cuatro trimestres
        pts.append((max(x[2] for x in v4), round(sum(x[1] for x in v4), 4)))
    pts.sort(key=lambda p: p[0])
    # una misma fecha de publicacion puede cerrar dos TTM: gana el mas nuevo
    ult = {}
    for f, v in pts:
        ult[f] = v
    return sorted(ult.items())


def _eps_from_yf(symbol, t=None):
    """Respaldo: anual + TTM trimestral de yfinance (~4 ejercicios).

    Su EPS YA viene ajustado por split, asi que no se le aplica `_factor_split`.
    No trae fecha de presentacion, asi que el lag se estima.
    """
    import pandas as pd
    import yfinance as yf
    from enrichment import _yf_symbol

    t = t or yf.Ticker(_yf_symbol(symbol))
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


def _valida(pts, trailing_eps):
    """El TTM reconstruido tiene que cerrar contra el `trailingEps` de yfinance.

    Es la validacion que al principio se hizo a mano, corriendo en runtime. Una
    serie de P/E que no cuadra con la fuente es exactamente "un numero
    equivocado que se ve bien" — mejor no publicarla.

    Sin referencia (yfinance no informa `trailingEps` cuando el EPS es
    negativo) no se puede validar NI invalidar: se acepta. El panel ya maneja
    el EPS negativo como hueco.
    """
    if not pts:
        return False
    if trailing_eps in (None, 0):
        return True
    try:
        dif = abs(pts[-1][1] - float(trailing_eps)) / abs(float(trailing_eps)) * 100
    except Exception:
        return True
    return dif <= VALID_TOL_PCT


def _eps_points_fresh(symbol):
    """[(fecha_efectiva_iso, ttm_eps)]. SEC primero, yfinance de respaldo.

    Devuelve tambien de donde salio, para poder decirlo en el log y en el cache.
    """
    import yfinance as yf
    from enrichment import _yf_symbol

    t = yf.Ticker(_yf_symbol(symbol))
    try:
        splits = t.splits
    except Exception:
        splits = None
    try:
        tr_eps = (t.info or {}).get("trailingEps")
    except Exception:
        tr_eps = None

    try:
        pts = _eps_from_sec(symbol, splits)
    except Exception as e:
        print(f"[VAL] {symbol}: SEC fallo ({type(e).__name__}) — voy a yfinance",
              flush=True)
        pts = []
    if pts and _valida(pts, tr_eps):
        return pts, "sec"
    if pts:
        print(f"[VAL] {symbol}: SEC no valida contra trailingEps "
              f"({pts[-1][1]:.2f} vs {tr_eps}) — voy a yfinance", flush=True)

    pts = _eps_from_yf(symbol, t)
    if pts and _valida(pts, tr_eps):
        return pts, "yf"
    if pts:
        print(f"[VAL] {symbol}: yfinance tampoco valida "
              f"({pts[-1][1]:.2f} vs {tr_eps}) — sin panel", flush=True)
    return [], None


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
        pts, src = _eps_points_fresh(symbol)
    except Exception as e:
        print(f"[VAL] {symbol}: {type(e).__name__} {e}", flush=True)
        pts, src = [], None
    with _lock:
        _mem[symbol] = {"ts": time.time(), "src": src,
                        "pts": [[f, v] for f, v in pts]}
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
    ventana = f"{len(v) / 252:.0f}A"

    def _pct_en(seg):
        if hoy is None or len(seg) < 60:
            return None
        return int(round(sum(1 for x in seg if x < hoy) / len(seg) * 100))

    pct = _pct_en(v)
    # DOS ventanas, por la misma razon que la concentracion muestra tres: un
    # percentil contra 16 años mide en parte "en que año estamos" — las
    # valuaciones subieron, asi que casi todo sale en el cuarto alto contra
    # 2010-2015. La de 5 años dice si esta caro para el REGIMEN de hoy. Un
    # percentil sin su ventana al lado no informa, miente.
    recientes = sorted(x for x in vals[-1260:] if x is not None)
    pct5 = _pct_en(recientes)
    pcts = []
    if pct5 is not None and len(recientes) >= 60 and len(recientes) < len(v):
        pcts.append({"ventana": "5A", "pct": pct5, "ruedas": len(recientes)})
    if pct is not None:
        pcts.append({"ventana": ventana, "pct": pct, "ruedas": len(v)})
    return {
        "pair": "P/E",
        "vals": vals,
        # Las bandas salen de la historia COMPLETA (es la que se dibuja).
        "q1": _p(0.25), "med": _p(0.50), "q3": _p(0.75),
        "pct": pct,
        "sin_pe_hoy": hoy is None,
        "ventana": ventana,
        "ruedas": len(v),
        "pcts": pcts,
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
