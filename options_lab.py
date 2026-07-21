"""
Options Laboratory — Motor de analisis de estrategias con opciones.

Dado el estado tecnico de un subyacente (senal, indicadores, IV), genera
las 10 mejores estrategias de opciones rankeadas por riesgo/beneficio,
incluyendo griegas, probabilidad de beneficio, backtesting historico,
y deteccion de desalineacion de IV.

Usa Black-Scholes para pricing teorico y Greeks.
"""

import math
import time
import numpy as np
from scipy.stats import norm
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Tuple


# ══════════════════════════════════════════════════════════════
#  DATOS REALES DE MERCADO (cadena de opciones via yfinance)
# ══════════════════════════════════════════════════════════════

_OPTION_CHAIN_CACHE = {}          # symbol -> (timestamp, OptionMarket|None)
_OPTION_CHAIN_TTL = 600           # 10 min: la cadena cambia lento intradia


class OptionMarket:
    """Cadena de opciones real de un subyacente (bid/ask/IV por strike y vencimiento).

    Permite (a) obtener una IV de mercado ATM para alimentar el analisis IV/HV y
    (b) valuar cada pata al mid real cobrando medio spread bid/ask como coste.
    """

    def __init__(self, symbol, spot, dte_map, atm_iv):
        self.symbol = symbol
        self.spot = spot
        # dte_map: {dte_objetivo: {"expiry": str, "C": {strike:(bid,ask,iv)}, "P": {...}}}
        self.dte_map = dte_map
        self.iv = atm_iv              # IV ATM del vencimiento mas cercano a ~30d

    def _nearest_dte(self, dte):
        if not self.dte_map:
            return None
        return min(self.dte_map.keys(), key=lambda d: abs(d - dte))

    def real_dte(self, dte):
        """Dias reales al vencimiento del contrato usado para este DTE objetivo."""
        d = self._nearest_dte(dte)
        if d is None:
            return None
        return self.dte_map[d].get("dte")

    def lookup(self, dte, right, strike):
        """Devuelve (strike_real, mid, half_spread, iv) del strike mas cercano, o None.

        Devuelve el strike realmente disponible para que el llamador snapee la pata
        a el (premium y strike deben corresponder al MISMO contrato)."""
        d = self._nearest_dte(dte)
        if d is None:
            return None
        table = self.dte_map[d].get(right, {})
        if not table:
            return None
        k = min(table.keys(), key=lambda s: abs(s - strike))
        # Rechaza si el strike disponible dista demasiado del pedido (>15%)
        if strike > 0 and abs(k - strike) / strike > 0.15:
            return None
        bid, ask, iv = table[k]
        if bid is None or ask is None or ask <= 0:
            return None
        mid = (bid + ask) / 2.0
        if mid <= 0:
            return None
        half_spread = max(0.0, (ask - bid) / 2.0)
        return (k, mid, half_spread, iv)


def fetch_option_market(symbol, dte_targets, spot=None):
    """Descarga la cadena de opciones real de yfinance para los DTE objetivo.

    Devuelve un OptionMarket, o None si no hay datos / falla la red. Nunca lanza:
    el analisis de opciones debe seguir funcionando (con pricing teorico) si esto
    falla. Cachea por simbolo durante _OPTION_CHAIN_TTL segundos.
    """
    now = time.time()
    cached = _OPTION_CHAIN_CACHE.get(symbol)
    if cached and (now - cached[0]) < _OPTION_CHAIN_TTL:
        return cached[1]

    market = None
    try:
        import datetime as _dt
        import yfinance as yf

        tk = yf.Ticker(symbol)
        expiries = list(tk.options or [])
        if not expiries:
            _OPTION_CHAIN_CACHE[symbol] = (now, None)
            return None

        today = _dt.date.today()
        exp_dates = []
        for e in expiries:
            try:
                d = _dt.datetime.strptime(e, "%Y-%m-%d").date()
                dte = (d - today).days
                if dte > 0:
                    exp_dates.append((e, dte))
            except Exception:
                continue
        if not exp_dates:
            _OPTION_CHAIN_CACHE[symbol] = (now, None)
            return None

        dte_map = {}
        used_expiries = set()
        for target in dte_targets:
            e, dte = min(exp_dates, key=lambda x: abs(x[1] - target))
            if e in used_expiries:
                dte_map[target] = _extract_chain(tk, e, dte)
                continue
            used_expiries.add(e)
            dte_map[target] = _extract_chain(tk, e, dte)

        # IV ATM: usa el vencimiento mas cercano a 30d y el strike mas cercano al spot
        atm_iv = _atm_iv_from_map(dte_map, spot)
        if any(v for v in dte_map.values()):
            market = OptionMarket(symbol, spot, dte_map, atm_iv)
    except Exception:
        market = None

    _OPTION_CHAIN_CACHE[symbol] = (now, market)
    return market


def _extract_chain(tk, expiry, dte):
    """Extrae {'expiry','C':{strike:(bid,ask,iv)}, 'P':{...}} de un vencimiento."""
    entry = {"expiry": expiry, "dte": dte, "C": {}, "P": {}}
    try:
        oc = tk.option_chain(expiry)
    except Exception:
        return entry
    for df, right in ((oc.calls, "C"), (oc.puts, "P")):
        try:
            for _, row in df.iterrows():
                strike = float(row["strike"])
                bid = row.get("bid")
                ask = row.get("ask")
                iv = row.get("impliedVolatility")
                bid = float(bid) if bid == bid and bid is not None else None
                ask = float(ask) if ask == ask and ask is not None else None
                iv = float(iv) if iv == iv and iv is not None else None
                entry[right][strike] = (bid, ask, iv)
        except Exception:
            continue
    return entry


def _atm_iv_from_map(dte_map, spot):
    """IV ATM del vencimiento mas cercano a 30d (promedio call/put del strike ATM)."""
    if not dte_map or not spot:
        return None
    target = min(dte_map.keys(), key=lambda d: abs(d - 30))
    entry = dte_map[target]
    ivs = []
    for right in ("C", "P"):
        table = entry.get(right, {})
        if not table:
            continue
        k = min(table.keys(), key=lambda s: abs(s - spot))
        iv = table[k][2]
        if iv and 0.01 < iv < 5.0:
            ivs.append(iv)
    if not ivs:
        return None
    return sum(ivs) / len(ivs)


def get_option_market(symbol, dte_targets, spot=None):
    """Wrapper cacheado y seguro para obtener la cadena real (o None)."""
    try:
        return fetch_option_market(symbol, dte_targets, spot)
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════
#  BLACK-SCHOLES & GREEKS
# ══════════════════════════════════════════════════════════════

def bs_d1(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    return (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))


def bs_d2(S, K, T, r, sigma):
    return bs_d1(S, K, T, r, sigma) - sigma * math.sqrt(T)


def bs_call(S, K, T, r, sigma):
    if T <= 0:
        return max(S - K, 0.0)
    d1 = bs_d1(S, K, T, r, sigma)
    d2 = d1 - sigma * math.sqrt(T)
    return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)


def bs_put(S, K, T, r, sigma):
    if T <= 0:
        return max(K - S, 0.0)
    d1 = bs_d1(S, K, T, r, sigma)
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def bs_price(S, K, T, r, sigma, right):
    """right = 'C' or 'P'"""
    return bs_call(S, K, T, r, sigma) if right == "C" else bs_put(S, K, T, r, sigma)


def greeks(S, K, T, r, sigma, right):
    """Calcula Delta, Gamma, Theta, Vega, Rho para una opcion vanilla."""
    if T <= 1e-10 or sigma <= 1e-10 or S <= 0 or K <= 0:
        intrinsic = max(S - K, 0) if right == "C" else max(K - S, 0)
        delta = 1.0 if (right == "C" and S > K) else (-1.0 if (right == "P" and S < K) else 0.0)
        return {"delta": delta, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}

    sqrtT = math.sqrt(T)
    d1 = bs_d1(S, K, T, r, sigma)
    d2 = d1 - sigma * sqrtT
    nd1 = norm.pdf(d1)

    if right == "C":
        delta = norm.cdf(d1)
        theta = (-S * nd1 * sigma / (2 * sqrtT)
                 - r * K * math.exp(-r * T) * norm.cdf(d2)) / 365
        rho = K * T * math.exp(-r * T) * norm.cdf(d2) / 100
    else:
        delta = norm.cdf(d1) - 1
        theta = (-S * nd1 * sigma / (2 * sqrtT)
                 + r * K * math.exp(-r * T) * norm.cdf(-d2)) / 365
        rho = -K * T * math.exp(-r * T) * norm.cdf(-d2) / 100

    gamma = nd1 / (S * sigma * sqrtT)
    vega = S * nd1 * sqrtT / 100

    return {
        "delta": round(delta, 4),
        "gamma": round(gamma, 4),
        "theta": round(theta, 4),
        "vega": round(vega, 4),
        "rho": round(rho, 4),
    }


# ══════════════════════════════════════════════════════════════
#  IMPLIED VOLATILITY (Newton-Raphson)
# ══════════════════════════════════════════════════════════════

def implied_volatility(market_price, S, K, T, r, right, max_iter=50, tol=1e-6):
    """Estima IV por Newton-Raphson. Devuelve None si no converge."""
    if T <= 0 or market_price <= 0:
        return None
    intrinsic = max(S - K, 0) if right == "C" else max(K - S, 0)
    if market_price < intrinsic - 0.01:
        return None

    sigma = 0.3
    for _ in range(max_iter):
        price = bs_price(S, K, T, r, sigma, right)
        diff = price - market_price
        if abs(diff) < tol:
            return sigma
        d1 = bs_d1(S, K, T, r, sigma)
        vega_val = S * norm.pdf(d1) * math.sqrt(T)
        if abs(vega_val) < 1e-12:
            break
        sigma -= diff / vega_val
        if sigma <= 0.001:
            sigma = 0.001
        if sigma > 5.0:
            sigma = 5.0
    return sigma if 0.01 < sigma < 5.0 else None


# ══════════════════════════════════════════════════════════════
#  HISTORICAL VOLATILITY & IV ANALYSIS
# ══════════════════════════════════════════════════════════════

def historical_volatility(closes, window=30):
    """HV anualizada sobre ventana de N dias."""
    if len(closes) < window + 1:
        return None
    log_returns = np.diff(np.log(closes[-window - 1:]))
    return float(np.std(log_returns) * math.sqrt(252))


def hv_series(closes, window=30):
    """Serie de HV para calcular percentil."""
    if len(closes) < window + 1:
        return []
    log_rets = np.diff(np.log(closes))
    hvs = []
    for i in range(window, len(log_rets)):
        hv = float(np.std(log_rets[i - window:i]) * math.sqrt(252))
        hvs.append(hv)
    return hvs


def iv_analysis(closes, current_iv=None):
    """Analiza HV vs IV para detectar desalineaciones.

    Returns dict con:
      hv_30: HV 30 dias
      hv_60: HV 60 dias
      hv_rank: percentil de HV actual vs ultimo ano
      iv_vs_hv: ratio IV/HV (>1 = opciones caras, <1 = baratas)
      iv_regime: 'high' | 'normal' | 'low'
      iv_premium: % premium/discount de IV vs HV
      opportunity: descripcion de la oportunidad si hay desalineacion
    """
    hv30 = historical_volatility(closes, 30)
    hv60 = historical_volatility(closes, 60)
    hv10 = historical_volatility(closes, 10)

    # HV rank (percentil vs ultimo ano)
    hv_vals = hv_series(closes, 30)
    hv_rank = None
    if hv_vals and hv30 is not None:
        below = sum(1 for h in hv_vals[-252:] if h < hv30)
        total = min(len(hv_vals), 252)
        hv_rank = round(below / total * 100, 1) if total > 0 else None

    # Si tenemos IV de mercado, comparar
    iv_vs_hv = None
    iv_premium = None
    iv_regime = "normal"
    opportunity = None

    if current_iv is not None and hv30 is not None and hv30 > 0:
        iv_vs_hv = round(current_iv / hv30, 2)
        iv_premium = round((current_iv - hv30) / hv30 * 100, 1)

        if iv_vs_hv > 1.3:
            iv_regime = "high"
            opportunity = (f"IV {iv_premium:+.0f}% sobre HV — opciones caras. "
                           f"Estrategias vendedoras (iron condor, credit spreads) favorecidas.")
        elif iv_vs_hv < 0.75:
            iv_regime = "low"
            opportunity = (f"IV {iv_premium:+.0f}% bajo HV — opciones baratas. "
                           f"Estrategias compradoras (long straddle, debit spreads) favorecidas.")
    elif hv_rank is not None:
        if hv_rank > 80:
            iv_regime = "high"
        elif hv_rank < 20:
            iv_regime = "low"

    # Sin IV de mercado, estimar desde HV
    estimated_iv = current_iv if current_iv else hv30

    return {
        "hv_10": round(hv10, 4) if hv10 else None,
        "hv_30": round(hv30, 4) if hv30 else None,
        "hv_60": round(hv60, 4) if hv60 else None,
        "hv_rank": hv_rank,
        "estimated_iv": round(estimated_iv, 4) if estimated_iv else 0.25,
        "iv_vs_hv": iv_vs_hv,
        "iv_premium": iv_premium,
        "iv_regime": iv_regime,
        "opportunity": opportunity,
    }


# ══════════════════════════════════════════════════════════════
#  STRATEGY DEFINITIONS
# ══════════════════════════════════════════════════════════════

@dataclass
class OptionLeg:
    right: str           # 'C' or 'P'
    strike: float
    action: str          # 'BUY' or 'SELL'
    qty: int = 1
    premium: float = 0.0
    greeks_data: dict = field(default_factory=dict)

    def net_premium(self):
        mult = -1 if self.action == "BUY" else 1
        return mult * self.premium * self.qty * 100


@dataclass
class Strategy:
    name: str
    name_es: str
    legs: List[OptionLeg]
    dte: int
    description: str = ""
    bias: str = ""              # 'bullish' | 'bearish' | 'neutral'
    max_profit: float = 0.0
    max_loss: float = 0.0
    breakevens: List[float] = field(default_factory=list)
    prob_profit: float = 0.0
    risk_reward: float = 0.0
    capital_required: float = 0.0
    net_premium: float = 0.0
    score: float = 0.0
    greeks_agg: dict = field(default_factory=dict)
    payoff_points: List[dict] = field(default_factory=list)
    backtest_result: dict = field(default_factory=dict)
    iv_edge: str = ""
    complexity: int = 1         # 1-3
    expected_value: float = 0.0     # EV en $ (media del Monte Carlo, neto de spread)
    market_priced: bool = False     # True si las patas se valuaron con precios reales de mercado
    spread_cost: float = 0.0        # coste estimado de cruzar el spread bid/ask ($ por posicion)

    def to_dict(self):
        d = asdict(self)
        d["payoff_points"] = self.payoff_points
        return d


def _round_strike(price, step=1.0):
    """Redondea al strike mas cercano."""
    return round(price / step) * step


def _strike_step(price):
    """Determina el paso de strikes segun precio."""
    if price < 20:
        return 0.5
    elif price < 50:
        return 1.0
    elif price < 200:
        return 2.5
    else:
        return 5.0


def _compute_payoff(legs, price_range, per_share=True):
    """Calcula P&L para un rango de precios al vencimiento.
    Returns list of {price, pnl}."""
    points = []
    for S in price_range:
        pnl = 0.0
        for leg in legs:
            if leg.right == "C":
                intrinsic = max(S - leg.strike, 0)
            else:
                intrinsic = max(leg.strike - S, 0)

            if leg.action == "BUY":
                pnl += (intrinsic - leg.premium) * leg.qty
            else:
                pnl += (leg.premium - intrinsic) * leg.qty

        if not per_share:
            pnl *= 100
        points.append({"price": round(S, 2), "pnl": round(pnl, 2)})
    return points


def _find_breakevens(payoff_points):
    """Encuentra breakevens donde P&L cruza por 0."""
    bkevens = []
    for i in range(1, len(payoff_points)):
        p0 = payoff_points[i - 1]["pnl"]
        p1 = payoff_points[i]["pnl"]
        if p0 * p1 < 0:
            s0 = payoff_points[i - 1]["price"]
            s1 = payoff_points[i]["price"]
            if abs(p1 - p0) > 0.001:
                bk = s0 + (s1 - s0) * abs(p0) / abs(p1 - p0)
                bkevens.append(round(bk, 2))
    return bkevens


def _aggregate_greeks(legs):
    """Suma griegas de todas las patas."""
    agg = {"delta": 0, "gamma": 0, "theta": 0, "vega": 0, "rho": 0}
    for leg in legs:
        g = leg.greeks_data
        mult = leg.qty if leg.action == "BUY" else -leg.qty
        for k in agg:
            agg[k] += g.get(k, 0) * mult
    return {k: round(v, 4) for k, v in agg.items()}


def _prob_below(S, target, T, sigma, r=0.05):
    """Probabilidad de que el precio este por debajo de target en T anos."""
    if T <= 0 or sigma <= 0:
        return 1.0 if S < target else 0.0
    d2 = (math.log(target / S) - (r - 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return norm.cdf(d2)


def _prob_between(S, lo, hi, T, sigma, r=0.05):
    """Probabilidad de que el precio este entre lo y hi."""
    return _prob_below(S, hi, T, sigma, r) - _prob_below(S, lo, T, sigma, r)


# ══════════════════════════════════════════════════════════════
#  STRATEGY BUILDERS
# ══════════════════════════════════════════════════════════════

def _derive_metrics(legs, S, T, r, sigma):
    """Calcula payoff, PoP, EV, breakevens, capital y griegas a partir de los
    premiums YA asignados en cada pata. Reutilizado por _build_strategy (precios
    teoricos) y por _apply_market_pricing (precios reales de mercado)."""
    # Net premium (positivo = credito, negativo = debito)
    net = sum(leg.net_premium() for leg in legs) / 100  # per-share

    # Payoff
    step = _strike_step(S)
    price_range = np.arange(S * 0.7, S * 1.3, step * 0.2)
    payoff = _compute_payoff(legs, price_range)

    pnls = [p["pnl"] for p in payoff]
    max_profit = max(pnls) * 100
    max_loss = min(pnls) * 100
    breakevens = _find_breakevens(payoff)

    # Monte Carlo log-normal: PoP y valor esperado (EV)
    n_sims = 10000
    np.random.seed(42)
    drift = (r - 0.5 * sigma ** 2) * T
    diffusion = sigma * math.sqrt(T) * np.random.randn(n_sims)
    final_prices = S * np.exp(drift + diffusion)

    pnl_sum = 0.0
    profitable = 0
    for fp in final_prices:
        pnl = 0.0
        for leg in legs:
            if leg.right == "C":
                intrinsic = max(fp - leg.strike, 0)
            else:
                intrinsic = max(leg.strike - fp, 0)
            if leg.action == "BUY":
                pnl += (intrinsic - leg.premium) * leg.qty
            else:
                pnl += (leg.premium - intrinsic) * leg.qty
        pnl_sum += pnl
        if pnl > 0:
            profitable += 1
    prob_profit = round(profitable / n_sims * 100, 1)
    expected_value = (pnl_sum / n_sims) * 100   # EV en $ por posicion (1 contrato)

    risk_reward = round(abs(max_profit / max_loss), 2) if max_loss != 0 else 99.0
    capital = abs(max_loss) if max_loss < 0 else abs(net * 100)

    return {
        "net": net,
        "payoff": payoff,
        "max_profit": round(max_profit, 2),
        "max_loss": round(max_loss, 2),
        "breakevens": breakevens,
        "prob_profit": prob_profit,
        "expected_value": round(expected_value, 2),
        "risk_reward": risk_reward,
        "capital": round(abs(capital), 2),
        "net_premium": round(net * 100, 2),
        "greeks_agg": _aggregate_greeks(legs),
    }


def _build_strategy(name, name_es, legs, S, T, r, sigma, bias, dte,
                    description="", complexity=1, iv_edge=""):
    """Construye Strategy completa con payoff, greeks, breakevens, etc."""
    # Calcular premiums y greeks teoricos (Black-Scholes) para cada pata
    for leg in legs:
        leg.premium = bs_price(S, leg.strike, T, r, sigma, leg.right)
        leg.greeks_data = greeks(S, leg.strike, T, r, sigma, leg.right)

    m = _derive_metrics(legs, S, T, r, sigma)

    strat = Strategy(
        name=name,
        name_es=name_es,
        legs=legs,
        dte=dte,
        description=description,
        bias=bias,
        max_profit=m["max_profit"],
        max_loss=m["max_loss"],
        breakevens=m["breakevens"],
        prob_profit=m["prob_profit"],
        risk_reward=m["risk_reward"],
        capital_required=m["capital"],
        net_premium=m["net_premium"],
        greeks_agg=m["greeks_agg"],
        payoff_points=m["payoff"],
        complexity=complexity,
        iv_edge=iv_edge,
        expected_value=m["expected_value"],
    )
    return strat


def _apply_market_pricing(strat, market, S, T, r, sigma):
    """Reprecio las patas de una estrategia con precios reales de mercado.

    Si TODAS las patas tienen mid real disponible, sustituye los premiums
    teoricos por los mid reales, cobra medio spread bid/ask por pata como coste,
    y recalcula payoff/PoP/EV/capital. Si falta alguna pata, deja el pricing
    teorico intacto (no mezcla ambos). Devuelve el strat (mutado o no)."""
    if market is None:
        return strat

    # Usar el DTE REAL del vencimiento elegido (no el objetivo): los premios reales
    # corresponden a ese vencimiento, asi que payoff/PoP/EV/griegas deben usar su T.
    real_dte = market.real_dte(strat.dte)
    if real_dte and real_dte > 0:
        strat.dte = int(real_dte)
        T = real_dte / 365.0

    reals = []
    total_half_spread = 0.0
    for leg in strat.legs:
        res = market.lookup(strat.dte, leg.right, leg.strike)
        if res is None:
            return strat   # falta liquidez en alguna pata -> conservar teorico
        real_strike, mid, half_spread, _iv = res
        reals.append((leg, real_strike, mid))
        total_half_spread += half_spread * leg.qty

    # Todas las patas tienen precio real: aplicar (snapear strike al contrato real
    # para que premium y strike correspondan al mismo contrato)
    for leg, real_strike, mid in reals:
        leg.strike = real_strike
        leg.premium = mid
        # Griegas: recomputar con la IV de mercado (sigma ya es la IV ATM real)
        leg.greeks_data = greeks(S, leg.strike, T, r, sigma, leg.right)

    m = _derive_metrics(strat.legs, S, T, r, sigma)
    spread_cost = round(total_half_spread * 100, 2)   # $ por posicion (round trip aprox una via)

    strat.max_profit = m["max_profit"]
    strat.max_loss = m["max_loss"]
    strat.breakevens = m["breakevens"]
    strat.prob_profit = m["prob_profit"]
    strat.risk_reward = m["risk_reward"]
    strat.capital_required = m["capital"]
    strat.net_premium = m["net_premium"]
    strat.greeks_agg = m["greeks_agg"]
    strat.payoff_points = m["payoff"]
    # EV neto del coste de cruzar el spread al abrir la posicion
    strat.expected_value = round(m["expected_value"] - spread_cost, 2)
    strat.spread_cost = spread_cost
    strat.market_priced = True
    return strat


# ---------- Individual strategy generators ----------

def long_call(S, T, r, sigma, dte, otm_pct=0.02):
    step = _strike_step(S)
    K = _round_strike(S * (1 + otm_pct), step)
    legs = [OptionLeg(right="C", strike=K, action="BUY")]
    return _build_strategy(
        "Long Call", "Call Comprada",
        legs, S, T, r, sigma, "bullish", dte,
        f"Comprar Call strike ${K:.0f}. Beneficio ilimitado si sube. Pierde prima si no.",
        complexity=1,
    )


def long_put(S, T, r, sigma, dte, otm_pct=0.02):
    step = _strike_step(S)
    K = _round_strike(S * (1 - otm_pct), step)
    legs = [OptionLeg(right="P", strike=K, action="BUY")]
    return _build_strategy(
        "Long Put", "Put Comprada",
        legs, S, T, r, sigma, "bearish", dte,
        f"Comprar Put strike ${K:.0f}. Beneficio si baja. Pierde prima si no.",
        complexity=1,
    )


def bull_call_spread(S, T, r, sigma, dte, width_pct=0.05):
    step = _strike_step(S)
    K1 = _round_strike(S, step)
    K2 = _round_strike(S * (1 + width_pct), step)
    if K2 <= K1:
        K2 = K1 + step
    legs = [
        OptionLeg(right="C", strike=K1, action="BUY"),
        OptionLeg(right="C", strike=K2, action="SELL"),
    ]
    return _build_strategy(
        "Bull Call Spread", "Bull Call Spread",
        legs, S, T, r, sigma, "bullish", dte,
        f"Comprar Call ${K1:.0f} / Vender Call ${K2:.0f}. Riesgo limitado, alcista moderado.",
        complexity=2,
    )


def bear_put_spread(S, T, r, sigma, dte, width_pct=0.05):
    step = _strike_step(S)
    K1 = _round_strike(S, step)
    K2 = _round_strike(S * (1 - width_pct), step)
    if K2 >= K1:
        K2 = K1 - step
    legs = [
        OptionLeg(right="P", strike=K1, action="BUY"),
        OptionLeg(right="P", strike=K2, action="SELL"),
    ]
    return _build_strategy(
        "Bear Put Spread", "Bear Put Spread",
        legs, S, T, r, sigma, "bearish", dte,
        f"Comprar Put ${K1:.0f} / Vender Put ${K2:.0f}. Riesgo limitado, bajista moderado.",
        complexity=2,
    )


def bull_put_spread(S, T, r, sigma, dte, width_pct=0.05):
    step = _strike_step(S)
    K_sell = _round_strike(S * (1 - 0.03), step)
    K_buy = _round_strike(S * (1 - 0.03 - width_pct), step)
    if K_buy >= K_sell:
        K_buy = K_sell - step
    legs = [
        OptionLeg(right="P", strike=K_sell, action="SELL"),
        OptionLeg(right="P", strike=K_buy, action="BUY"),
    ]
    return _build_strategy(
        "Bull Put Spread", "Bull Put Credit Spread",
        legs, S, T, r, sigma, "bullish", dte,
        f"Vender Put ${K_sell:.0f} / Comprar Put ${K_buy:.0f}. Cobra prima si no baja. Alcista.",
        complexity=2,
        iv_edge="Favorecido con IV alta (cobra mas prima)",
    )


def bear_call_spread(S, T, r, sigma, dte, width_pct=0.05):
    step = _strike_step(S)
    K_sell = _round_strike(S * (1 + 0.03), step)
    K_buy = _round_strike(S * (1 + 0.03 + width_pct), step)
    if K_buy <= K_sell:
        K_buy = K_sell + step
    legs = [
        OptionLeg(right="C", strike=K_sell, action="SELL"),
        OptionLeg(right="C", strike=K_buy, action="BUY"),
    ]
    return _build_strategy(
        "Bear Call Spread", "Bear Call Credit Spread",
        legs, S, T, r, sigma, "bearish", dte,
        f"Vender Call ${K_sell:.0f} / Comprar Call ${K_buy:.0f}. Cobra prima si no sube. Bajista.",
        complexity=2,
        iv_edge="Favorecido con IV alta (cobra mas prima)",
    )


def iron_condor(S, T, r, sigma, dte, wing_pct=0.05, width_pct=0.03):
    step = _strike_step(S)
    put_sell = _round_strike(S * (1 - wing_pct), step)
    put_buy = _round_strike(S * (1 - wing_pct - width_pct), step)
    call_sell = _round_strike(S * (1 + wing_pct), step)
    call_buy = _round_strike(S * (1 + wing_pct + width_pct), step)
    if put_buy >= put_sell:
        put_buy = put_sell - step
    if call_buy <= call_sell:
        call_buy = call_sell + step
    legs = [
        OptionLeg(right="P", strike=put_buy, action="BUY"),
        OptionLeg(right="P", strike=put_sell, action="SELL"),
        OptionLeg(right="C", strike=call_sell, action="SELL"),
        OptionLeg(right="C", strike=call_buy, action="BUY"),
    ]
    return _build_strategy(
        "Iron Condor", "Iron Condor",
        legs, S, T, r, sigma, "neutral", dte,
        f"Vende Put ${put_sell:.0f}/Call ${call_sell:.0f}, protege con alas. Gana si se queda en rango.",
        complexity=3,
        iv_edge="Mejor con IV alta (prima cobrada mayor)",
    )


def iron_butterfly(S, T, r, sigma, dte, width_pct=0.05):
    step = _strike_step(S)
    K_atm = _round_strike(S, step)
    K_put = _round_strike(S * (1 - width_pct), step)
    K_call = _round_strike(S * (1 + width_pct), step)
    if K_put >= K_atm:
        K_put = K_atm - step
    if K_call <= K_atm:
        K_call = K_atm + step
    legs = [
        OptionLeg(right="P", strike=K_put, action="BUY"),
        OptionLeg(right="P", strike=K_atm, action="SELL"),
        OptionLeg(right="C", strike=K_atm, action="SELL"),
        OptionLeg(right="C", strike=K_call, action="BUY"),
    ]
    return _build_strategy(
        "Iron Butterfly", "Iron Butterfly",
        legs, S, T, r, sigma, "neutral", dte,
        f"Vende ATM Put+Call ${K_atm:.0f}, compra alas. Alta prima, zona estrecha.",
        complexity=3,
        iv_edge="Excelente con IV alta (maximo theta decay)",
    )


def long_straddle(S, T, r, sigma, dte):
    step = _strike_step(S)
    K = _round_strike(S, step)
    legs = [
        OptionLeg(right="C", strike=K, action="BUY"),
        OptionLeg(right="P", strike=K, action="BUY"),
    ]
    return _build_strategy(
        "Long Straddle", "Straddle Comprado",
        legs, S, T, r, sigma, "neutral", dte,
        f"Comprar Call+Put ATM ${K:.0f}. Gana con movimiento grande en cualquier direccion.",
        complexity=2,
        iv_edge="Mejor con IV baja (prima pagada menor)",
    )


def long_strangle(S, T, r, sigma, dte, otm_pct=0.04):
    step = _strike_step(S)
    K_put = _round_strike(S * (1 - otm_pct), step)
    K_call = _round_strike(S * (1 + otm_pct), step)
    legs = [
        OptionLeg(right="P", strike=K_put, action="BUY"),
        OptionLeg(right="C", strike=K_call, action="BUY"),
    ]
    return _build_strategy(
        "Long Strangle", "Strangle Comprado",
        legs, S, T, r, sigma, "neutral", dte,
        f"Comprar Put ${K_put:.0f} + Call ${K_call:.0f}. Mas barato que straddle, necesita mas movimiento.",
        complexity=2,
        iv_edge="Mejor con IV baja (opciones baratas)",
    )


def short_strangle(S, T, r, sigma, dte, otm_pct=0.05):
    step = _strike_step(S)
    K_put = _round_strike(S * (1 - otm_pct), step)
    K_call = _round_strike(S * (1 + otm_pct), step)
    legs = [
        OptionLeg(right="P", strike=K_put, action="SELL"),
        OptionLeg(right="C", strike=K_call, action="SELL"),
    ]
    return _build_strategy(
        "Short Strangle", "Strangle Vendido",
        legs, S, T, r, sigma, "neutral", dte,
        f"Vender Put ${K_put:.0f} + Call ${K_call:.0f}. Cobra prima, riesgo ilimitado. Solo con IV alta.",
        complexity=3,
        iv_edge="Solo con IV alta — cobra prima inflada",
    )


def calendar_spread(S, T_short, T_long, r, sigma, dte_short, dte_long):
    """Calendar spread: vende near-term, compra far-term."""
    step = _strike_step(S)
    K = _round_strike(S, step)
    # Premium de pata corta (near-term)
    prem_short = bs_call(S, K, T_short, r, sigma)
    greeks_short = greeks(S, K, T_short, r, sigma, "C")
    # Premium de pata larga (far-term)
    prem_long = bs_call(S, K, T_long, r, sigma)
    greeks_long = greeks(S, K, T_long, r, sigma, "C")

    leg_short = OptionLeg(right="C", strike=K, action="SELL", premium=prem_short,
                          greeks_data=greeks_short)
    leg_long = OptionLeg(right="C", strike=K, action="BUY", premium=prem_long,
                         greeks_data=greeks_long)

    net = (prem_short - prem_long) * 100
    capital = abs(net)

    # Payoff aproximado al vencimiento de la pata corta
    step_p = _strike_step(S)
    price_range = np.arange(S * 0.85, S * 1.15, step_p * 0.2)
    payoff = []
    for s_price in price_range:
        # At short expiry: short call expires, long call has remaining value
        short_val = -max(s_price - K, 0) + prem_short
        remaining_T = T_long - T_short
        long_val = bs_call(s_price, K, remaining_T, r, sigma) - prem_long
        pnl = (short_val + long_val)
        payoff.append({"price": round(s_price, 2), "pnl": round(pnl, 2)})

    breakevens = _find_breakevens(payoff)
    pnls = [p["pnl"] for p in payoff]
    max_profit = max(pnls) * 100
    max_loss = min(pnls) * 100

    greeks_agg = {
        "delta": round(greeks_long["delta"] - greeks_short["delta"], 4),
        "gamma": round(greeks_long["gamma"] - greeks_short["gamma"], 4),
        "theta": round(-greeks_short["theta"] + greeks_long["theta"], 4),
        "vega": round(greeks_long["vega"] - greeks_short["vega"], 4),
        "rho": round(greeks_long["rho"] - greeks_short["rho"], 4),
    }

    # Prob profit: price stays near K
    prob_profit = round(_prob_between(S, K * 0.97, K * 1.03, T_short, sigma) * 100, 1)

    return Strategy(
        name="Calendar Spread",
        name_es="Calendar Spread",
        legs=[leg_short, leg_long],
        dte=dte_short,
        description=f"Vende Call ${K:.0f} a {dte_short}d / Compra Call ${K:.0f} a {dte_long}d. Gana con theta y si precio queda cerca de ${K:.0f}.",
        bias="neutral",
        max_profit=round(max_profit, 2),
        max_loss=round(max_loss, 2),
        breakevens=breakevens,
        prob_profit=prob_profit,
        risk_reward=round(abs(max_profit / max_loss), 2) if max_loss != 0 else 0,
        capital_required=round(capital, 2),
        net_premium=round(net, 2),
        greeks_agg=greeks_agg,
        payoff_points=payoff,
        complexity=3,
        iv_edge="Excelente para capturar IV alta en corto plazo vs IV baja largo plazo",
    )


def protective_put(S, T, r, sigma, dte, otm_pct=0.05):
    step = _strike_step(S)
    K = _round_strike(S * (1 - otm_pct), step)
    legs = [OptionLeg(right="P", strike=K, action="BUY")]
    strat = _build_strategy(
        "Protective Put", "Put Protectora",
        legs, S, T, r, sigma, "bullish", dte,
        f"Comprar Put ${K:.0f} como seguro. Protege cartera con piso en ${K:.0f}.",
        complexity=1,
    )
    strat.description += " Requiere tener las acciones."
    return strat


def covered_call(S, T, r, sigma, dte, otm_pct=0.04):
    step = _strike_step(S)
    K = _round_strike(S * (1 + otm_pct), step)
    legs = [OptionLeg(right="C", strike=K, action="SELL")]
    strat = _build_strategy(
        "Covered Call", "Call Cubierta",
        legs, S, T, r, sigma, "neutral", dte,
        f"Vender Call ${K:.0f} contra acciones. Cobra prima, limita subida. Income strategy.",
        complexity=1,
        iv_edge="Mejor con IV alta (cobra mas prima)",
    )
    return strat


def butterfly_spread(S, T, r, sigma, dte, width_pct=0.04):
    step = _strike_step(S)
    K_mid = _round_strike(S, step)
    K_lo = _round_strike(S * (1 - width_pct), step)
    K_hi = _round_strike(S * (1 + width_pct), step)
    if K_lo >= K_mid:
        K_lo = K_mid - step
    if K_hi <= K_mid:
        K_hi = K_mid + step
    legs = [
        OptionLeg(right="C", strike=K_lo, action="BUY"),
        OptionLeg(right="C", strike=K_mid, action="SELL", qty=2),
        OptionLeg(right="C", strike=K_hi, action="BUY"),
    ]
    return _build_strategy(
        "Butterfly Spread", "Butterfly Spread",
        legs, S, T, r, sigma, "neutral", dte,
        f"Compra Call ${K_lo:.0f}+${K_hi:.0f}, vende 2x Call ${K_mid:.0f}. Bajo costo, gana si queda en ${K_mid:.0f}.",
        complexity=3,
    )


def ratio_put_spread(S, T, r, sigma, dte, width_pct=0.06):
    step = _strike_step(S)
    K1 = _round_strike(S * (1 - 0.02), step)
    K2 = _round_strike(S * (1 - 0.02 - width_pct), step)
    if K2 >= K1:
        K2 = K1 - step
    legs = [
        OptionLeg(right="P", strike=K1, action="BUY"),
        OptionLeg(right="P", strike=K2, action="SELL", qty=2),
    ]
    return _build_strategy(
        "Ratio Put Spread", "Put Ratio Spread",
        legs, S, T, r, sigma, "bearish", dte,
        f"Compra Put ${K1:.0f}, vende 2x Put ${K2:.0f}. Bajista con credito. Riesgo si cae mucho.",
        complexity=3,
    )


# ══════════════════════════════════════════════════════════════
#  BACKTESTING ON SIMILAR CONDITIONS
# ══════════════════════════════════════════════════════════════

def backtest_similar_conditions(closes, highs, lows, signal_data, lookforward_days=None):
    """Busca situaciones historicas similares a la actual y calcula
    la distribucion de resultados.

    Parametros:
      closes, highs, lows: arrays de precios
      signal_data: dict con valores actuales de indicadores
      lookforward_days: lista de horizontes a evaluar [5, 10, 20, 30, 45]

    Returns dict con:
      similar_count: cuantas situaciones similares se encontraron
      outcomes: {days: {avg_return, median_return, win_rate, worst, best, distribution}}
      current_vs_history: comparacion del contexto actual vs historico
    """
    if lookforward_days is None:
        lookforward_days = [5, 10, 20, 30, 45]

    n = len(closes)
    if n < 260:
        return {"similar_count": 0, "outcomes": {}, "current_vs_history": "Datos insuficientes"}

    # Criterios de similitud basados en la senal actual
    current_rsi = signal_data.get("rsi")
    current_macd_hist = signal_data.get("macd_hist")
    current_macd_hist_prev = signal_data.get("macd_hist_prev")
    signal_type = signal_data.get("signal", "HOLD")

    # Calcular RSI y MACD sobre closes para matching historico
    import pandas as pd
    from indicators import calculate_rsi, calculate_macd

    df_closes = pd.DataFrame({"close": closes})
    rsi_df = calculate_rsi(df_closes)
    rsi_series = rsi_df["rsi"].values if rsi_df is not None else None

    macd_df = calculate_macd(df_closes)
    hist_series = macd_df["hist"].values if macd_df is not None else None

    if rsi_series is None or hist_series is None:
        return {"similar_count": 0, "outcomes": {}, "current_vs_history": "Error en indicadores"}

    similar_indices = []

    for i in range(260, n - max(lookforward_days) - 1):
        rsi_i = rsi_series[i] if i < len(rsi_series) else None
        hist_i = hist_series[i] if i < len(hist_series) else None
        hist_prev = hist_series[i - 1] if i - 1 < len(hist_series) else None

        if rsi_i is None or hist_i is None or hist_prev is None:
            continue

        match = False
        if signal_type == "BUY":
            if (rsi_i < 35 and hist_i < 0 and hist_i > hist_prev):
                match = True
        elif signal_type == "SELL":
            if (rsi_i > 65 and hist_i > 0 and hist_i < hist_prev):
                match = True
        else:
            # Para HOLD, buscar condiciones similares de RSI
            if current_rsi is not None:
                rsi_lo = current_rsi - 10
                rsi_hi = current_rsi + 10
                if rsi_lo <= rsi_i <= rsi_hi:
                    # Y que el histograma tenga la misma direccion
                    if current_macd_hist is not None and current_macd_hist_prev is not None:
                        current_dir = 1 if current_macd_hist > current_macd_hist_prev else -1
                        hist_dir = 1 if hist_i > hist_prev else -1
                        if current_dir == hist_dir:
                            match = True

        if match:
            similar_indices.append(i)

    # Calcular resultados para cada horizonte
    outcomes = {}
    for days in lookforward_days:
        returns = []
        max_ups = []
        max_downs = []

        for idx in similar_indices:
            if idx + days >= n:
                continue
            entry = closes[idx]
            exit_p = closes[idx + days]
            ret = (exit_p - entry) / entry * 100
            returns.append(ret)

            # Max favorable / adverse excursion
            period_highs = highs[idx:idx + days + 1]
            period_lows = lows[idx:idx + days + 1]
            if len(period_highs) > 0:
                max_up = (max(period_highs) - entry) / entry * 100
                max_down = (min(period_lows) - entry) / entry * 100
                max_ups.append(max_up)
                max_downs.append(max_down)

        if returns:
            returns_arr = np.array(returns)
            wins = sum(1 for r in returns if r > 0)

            # Distribucion para histograma
            hist_counts, hist_edges = np.histogram(returns_arr, bins=20)

            outcomes[days] = {
                "avg_return": round(float(np.mean(returns_arr)), 2),
                "median_return": round(float(np.median(returns_arr)), 2),
                "win_rate": round(wins / len(returns) * 100, 1),
                "worst": round(float(np.min(returns_arr)), 2),
                "best": round(float(np.max(returns_arr)), 2),
                "std_dev": round(float(np.std(returns_arr)), 2),
                "count": len(returns),
                "max_favorable": round(float(np.mean(max_ups)), 2) if max_ups else 0,
                "max_adverse": round(float(np.mean(max_downs)), 2) if max_downs else 0,
                "distribution": {
                    "counts": hist_counts.tolist(),
                    "edges": [round(float(e), 2) for e in hist_edges.tolist()],
                },
                "percentiles": {
                    "p10": round(float(np.percentile(returns_arr, 10)), 2),
                    "p25": round(float(np.percentile(returns_arr, 25)), 2),
                    "p50": round(float(np.percentile(returns_arr, 50)), 2),
                    "p75": round(float(np.percentile(returns_arr, 75)), 2),
                    "p90": round(float(np.percentile(returns_arr, 90)), 2),
                },
            }

    # Contexto actual vs historico
    context = []
    if similar_indices:
        context.append(f"Se encontraron {len(similar_indices)} situaciones similares en 5 anos")
        if 20 in outcomes:
            o = outcomes[20]
            context.append(f"A 20 dias: retorno promedio {o['avg_return']:+.1f}%, win rate {o['win_rate']:.0f}%")
        if 45 in outcomes:
            o = outcomes[45]
            context.append(f"A 45 dias: retorno promedio {o['avg_return']:+.1f}%, win rate {o['win_rate']:.0f}%")

    return {
        "similar_count": len(similar_indices),
        "outcomes": outcomes,
        "current_vs_history": ". ".join(context) if context else "Sin datos suficientes",
    }


# ══════════════════════════════════════════════════════════════
#  STRATEGY SCORING & RANKING
# ══════════════════════════════════════════════════════════════

def _score_strategy(strat, signal_type, iv_regime, backtest_outcomes):
    """Puntua una estrategia 0-100 considerando:
      - Alineacion con la senal del subyacente (25 pts)
      - Valor esperado / EV sobre el capital (15 pts)  ← corrige el sesgo de PoP alto y EV negativo
      - Probabilidad de beneficio (20 pts)
      - Risk/reward (15 pts)
      - Alineacion con regimen de IV (15 pts)
      - Backtest support (10 pts)
    Menos penalizacion por complejidad y por spread bid/ask ancho.
    """
    score = 0.0

    # 1. Alineacion con senal (25 pts)
    bias = strat.bias
    if signal_type == "BUY" and bias == "bullish":
        score += 25
    elif signal_type == "SELL" and bias == "bearish":
        score += 25
    elif signal_type == "HOLD" and bias == "neutral":
        score += 21
    elif signal_type == "BUY" and bias == "neutral":
        score += 13
    elif signal_type == "SELL" and bias == "neutral":
        score += 13
    elif signal_type == "HOLD" and bias in ("bullish", "bearish"):
        score += 8

    # 2. Valor esperado sobre capital (15 pts). El EV es la media del Monte Carlo
    #    (neto del coste de spread si se valuo a mercado). Un PoP alto con EV
    #    negativo (tipico de vender prima barata) deja de puntuar bien aqui.
    cap = strat.capital_required if strat.capital_required and strat.capital_required > 0 else None
    if cap:
        ev_roc = strat.expected_value / cap    # retorno esperado sobre capital
    else:
        ev_roc = 0.0
    score += max(0.0, min(1.0, ev_roc / 0.15)) * 15   # >=15% EV/capital -> full

    # 3. Probabilidad de beneficio (20 pts)
    pp = strat.prob_profit
    score += min(pp / 100, 1.0) * 20

    # 4b. Risk/reward (15 pts)
    rr = min(strat.risk_reward, 5.0)
    score += (rr / 5.0) * 15

    # 4. IV regime alignment (15 pts)
    if iv_regime == "high":
        if strat.name in ("Iron Condor", "Iron Butterfly", "Short Strangle",
                          "Bull Put Spread", "Bear Call Spread", "Covered Call",
                          "Calendar Spread"):
            score += 15
        elif strat.name in ("Long Straddle", "Long Strangle", "Long Call", "Long Put"):
            score += 0  # desfavorecido
        else:
            score += 7
    elif iv_regime == "low":
        if strat.name in ("Long Straddle", "Long Strangle", "Long Call", "Long Put",
                          "Bull Call Spread", "Bear Put Spread"):
            score += 15
        elif strat.name in ("Iron Condor", "Short Strangle"):
            score += 3
        else:
            score += 7
    else:
        score += 7

    # 5. Backtest support (10 pts)
    if backtest_outcomes:
        # Usar resultado a 20-30 dias
        for days in [30, 20, 45]:
            if days in backtest_outcomes:
                o = backtest_outcomes[days]
                wr = o.get("win_rate", 50) / 100
                if bias == "bullish" and o.get("avg_return", 0) > 0:
                    score += wr * 10
                elif bias == "bearish" and o.get("avg_return", 0) < 0:
                    score += wr * 10
                elif bias == "neutral":
                    score += 5 + wr * 5
                break

    # Penalizar complejidad ligeramente
    score -= (strat.complexity - 1) * 2

    # Penalizar spread bid/ask ancho (solo si se valuo a mercado): un spread que
    # se come una fraccion grande del capital hace la estrategia poco ejecutable.
    if strat.market_priced and strat.capital_required and strat.capital_required > 0:
        spread_frac = strat.spread_cost / strat.capital_required
        score -= min(1.0, spread_frac / 0.10) * 8   # spread >=10% del capital -> -8

    return max(0, min(100, round(score, 1)))


# ══════════════════════════════════════════════════════════════
#  MAIN ENGINE: generate_options_lab
# ══════════════════════════════════════════════════════════════

def generate_options_lab(symbol, price, signal_data, closes, highs=None, lows=None,
                         market_iv=None, risk_free_rate=0.05,
                         dte_options=None, option_market=None):
    """Genera el analisis completo del Options Lab para un simbolo.

    Parametros:
      symbol: ticker
      price: precio actual
      signal_data: dict con signal, strength, rsi, macd_hist, etc.
      closes: array de precios de cierre historicos
      highs/lows: arrays de highs/lows (para backtest)
      market_iv: IV de mercado si disponible
      risk_free_rate: tasa libre de riesgo
      dte_options: lista de DTEs a evaluar [21, 30, 45]

    Returns dict con:
      symbol, price, signal context
      iv_analysis: analisis de IV y desalineaciones
      strategies: lista de top 10 estrategias rankeadas
      backtest: resultados del backtesting historico
      summary: resumen ejecutivo
    """
    if dte_options is None:
        dte_options = [21, 30, 45]

    S = price
    r = risk_free_rate
    signal_type = signal_data.get("signal", "HOLD")

    # IV de mercado real: si viene una cadena (option_market) y no se paso IV
    # explicita, usar su IV ATM. Esto activa el analisis IV/HV real (antes muerto,
    # porque market_iv nunca se pasaba y estimated_iv caia siempre en HV30).
    if market_iv is None and option_market is not None and option_market.iv:
        market_iv = option_market.iv

    # 1. IV Analysis
    iv_data = iv_analysis(closes, market_iv)
    sigma = iv_data["estimated_iv"]
    if sigma is None or sigma <= 0:
        sigma = 0.25
    iv_regime = iv_data["iv_regime"]

    # 2. Backtesting on similar conditions
    h = highs if highs is not None else closes
    l = lows if lows is not None else closes
    bt = backtest_similar_conditions(closes, h, l, signal_data)
    bt_outcomes = bt.get("outcomes", {})

    # 3. Generate all candidate strategies for each DTE
    all_strategies = []

    for dte in dte_options:
        T = dte / 365.0

        # Bullish strategies
        try:
            all_strategies.append(long_call(S, T, r, sigma, dte))
        except Exception:
            pass
        try:
            all_strategies.append(bull_call_spread(S, T, r, sigma, dte))
        except Exception:
            pass
        try:
            all_strategies.append(bull_put_spread(S, T, r, sigma, dte))
        except Exception:
            pass

        # Bearish strategies
        try:
            all_strategies.append(long_put(S, T, r, sigma, dte))
        except Exception:
            pass
        try:
            all_strategies.append(bear_put_spread(S, T, r, sigma, dte))
        except Exception:
            pass
        try:
            all_strategies.append(bear_call_spread(S, T, r, sigma, dte))
        except Exception:
            pass

        # Neutral strategies
        try:
            all_strategies.append(iron_condor(S, T, r, sigma, dte))
        except Exception:
            pass
        try:
            all_strategies.append(iron_butterfly(S, T, r, sigma, dte))
        except Exception:
            pass
        try:
            all_strategies.append(long_straddle(S, T, r, sigma, dte))
        except Exception:
            pass
        try:
            all_strategies.append(long_strangle(S, T, r, sigma, dte))
        except Exception:
            pass
        try:
            all_strategies.append(butterfly_spread(S, T, r, sigma, dte))
        except Exception:
            pass
        try:
            all_strategies.append(covered_call(S, T, r, sigma, dte))
        except Exception:
            pass
        try:
            all_strategies.append(protective_put(S, T, r, sigma, dte))
        except Exception:
            pass

        # Calendar spread (necesita 2 DTEs)
        if dte < max(dte_options):
            long_dte = max(dte_options)
            try:
                all_strategies.append(
                    calendar_spread(S, T, long_dte / 365.0, r, sigma, dte, long_dte))
            except Exception:
                pass

        # Strategies only for high IV
        if iv_regime == "high":
            try:
                all_strategies.append(short_strangle(S, T, r, sigma, dte))
            except Exception:
                pass

        # Ratio spread for strong signals
        if signal_type in ("BUY", "SELL"):
            try:
                all_strategies.append(ratio_put_spread(S, T, r, sigma, dte))
            except Exception:
                pass

    # 3b. Reprecio a mercado real cada estrategia (mid bid/ask + coste de spread).
    #     Si no hay cadena, quedan con pricing teorico Black-Scholes.
    if option_market is not None:
        for strat in all_strategies:
            try:
                _apply_market_pricing(strat, option_market, S, strat.dte / 365.0, r, sigma)
            except Exception:
                pass

    # 4. Score and rank all strategies
    for strat in all_strategies:
        strat.score = _score_strategy(strat, signal_type, iv_regime, bt_outcomes)
        strat.backtest_result = bt

    # Deduplicate: keep best DTE per strategy name
    best_by_name = {}
    for strat in all_strategies:
        key = strat.name
        if key not in best_by_name or strat.score > best_by_name[key].score:
            best_by_name[key] = strat

    ranked = sorted(best_by_name.values(), key=lambda s: s.score, reverse=True)
    top10 = ranked[:10]

    # Assign rank
    for i, strat in enumerate(top10):
        strat.rank = i + 1

    # 5. IV misalignment opportunities
    iv_opportunities = []
    if iv_data.get("opportunity"):
        iv_opportunities.append(iv_data["opportunity"])

    # HV10 vs HV30 divergence (short-term vol spike or contraction)
    hv10 = iv_data.get("hv_10")
    hv30 = iv_data.get("hv_30")
    if hv10 and hv30 and hv30 > 0:
        ratio = hv10 / hv30
        if ratio > 1.5:
            iv_opportunities.append(
                f"HV corto plazo (10d) {ratio:.1f}x mayor que HV 30d — "
                f"volatilidad reciente elevada, calendar spreads favorecidos."
            )
        elif ratio < 0.6:
            iv_opportunities.append(
                f"HV corto plazo (10d) comprimida ({ratio:.1f}x vs HV 30d) — "
                f"calma antes de tormenta? Long straddle/strangle pueden ser oportunidad."
            )

    # 6. Summary
    summary_parts = []
    if signal_type == "BUY":
        summary_parts.append(f"{symbol} tiene senal de COMPRA activa")
    elif signal_type == "SELL":
        summary_parts.append(f"{symbol} tiene senal de VENTA activa")
    else:
        summary_parts.append(f"{symbol} en posicion NEUTRAL")

    iv_src = "mercado" if market_iv is not None else "estimada de HV"
    summary_parts.append(f"IV {iv_src} {sigma * 100:.0f}% (regimen {iv_regime})")

    if bt.get("similar_count", 0) > 0:
        summary_parts.append(f"{bt['similar_count']} situaciones historicas similares encontradas")

    if top10:
        summary_parts.append(f"Mejor estrategia: {top10[0].name_es} (score {top10[0].score})")

    summary = ". ".join(summary_parts) + "."

    # 7. Build result
    strategies_out = []
    for strat in top10:
        s = strat.to_dict()
        s["rank"] = getattr(strat, "rank", 0)
        # Simplificar backtest en cada estrategia (no repetir todo)
        s["backtest_result"] = {
            "similar_count": bt.get("similar_count", 0),
            "current_vs_history": bt.get("current_vs_history", ""),
        }
        strategies_out.append(s)

    return {
        "symbol": symbol,
        "price": price,
        "signal": signal_type,
        "signal_label": signal_data.get("signal_label", signal_type),
        "strength": signal_data.get("strength", 0),
        "iv_analysis": iv_data,
        "iv_opportunities": iv_opportunities,
        "iv_source": "market" if market_iv is not None else "hv",
        "market_priced": bool(option_market is not None and any(
            s.get("market_priced") for s in strategies_out)),
        "strategies": strategies_out,
        "backtest": bt,
        "summary": summary,
    }
