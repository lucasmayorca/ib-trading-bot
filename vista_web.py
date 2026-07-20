"""
Vista Web - Dashboard local para usar junto a TWS
Escanea top 100 acciones por volumen, calcula MACD+RSI+Koncorde,
muestra graficos de velas con medias moviles.

Uso:
    python vista_web.py
"""

import json
import math
import numpy as np
from flask import Flask, Response
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
import threading
import time
from datetime import datetime

import pandas as pd

import config
import indicators
import signals
from scanner import get_top_volume_stocks
import backtester
import yfinance as yf
import portfolio
import options_lab

CHART_BARS = 252  # ~1 year of trading days
MA_PERIODS = [200, 100, 50, 20]  # SMA periods
EMA_PERIOD = 9


# ══════════════════════════════════════════════════════════════
#  JSON HELPER
# ══════════════════════════════════════════════════════════════

def _clean(obj):
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        v = float(obj)
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, np.ndarray):
        return _clean(obj.tolist())
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    return obj


def to_json(obj):
    return json.dumps(_clean(obj))


# ══════════════════════════════════════════════════════════════
#  IB CONNECTION
# ══════════════════════════════════════════════════════════════

class VistaIB(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.connected_event = threading.Event()
        self.historical_data = {}
        self.hist_done = {}
        self.market_data = {}
        # Portfolio tracking state (reqAccountUpdates)
        self.portfolio_positions = []
        self.portfolio_positions_done = False
        self.account_values = {}
        self.account_values_done = False
        self.account_summary_data = {}
        self.account_summary_done = False
        # Open orders (para SL / TP)
        self.open_orders = []
        self.open_orders_done = False
        # Executions (fills historicos - para marcadores de entrada)
        self.executions = []
        self.executions_done = False

    def nextValidId(self, orderId):
        self.connected_event.set()

    # --- Portfolio callbacks (reqAccountUpdates) ---
    def updatePortfolio(self, contract, position, marketPrice, marketValue,
                        averageCost, unrealizedPNL, realizedPNL, accountName):
        portfolio.PortfolioMixin.updatePortfolio(
            self, contract, position, marketPrice, marketValue,
            averageCost, unrealizedPNL, realizedPNL, accountName)

    def updateAccountValue(self, key, val, currency, accountName):
        portfolio.PortfolioMixin.updateAccountValue(self, key, val, currency, accountName)

    def accountDownloadEnd(self, accountName):
        portfolio.PortfolioMixin.accountDownloadEnd(self, accountName)

    def accountSummary(self, reqId, account, tag, value, currency):
        portfolio.PortfolioMixin.accountSummary(self, reqId, account, tag, value, currency)

    def accountSummaryEnd(self, reqId):
        portfolio.PortfolioMixin.accountSummaryEnd(self, reqId)

    def openOrder(self, orderId, contract, order, orderState):
        portfolio.PortfolioMixin.openOrder(self, orderId, contract, order, orderState)

    def orderStatus(self, orderId, status, filled, remaining, avgFillPrice,
                    permId, parentId, lastFillPrice, clientId, whyHeld, mktCapPrice):
        portfolio.PortfolioMixin.orderStatus(
            self, orderId, status, filled, remaining, avgFillPrice,
            permId, parentId, lastFillPrice, clientId, whyHeld, mktCapPrice)

    def openOrderEnd(self):
        portfolio.PortfolioMixin.openOrderEnd(self)

    def execDetails(self, reqId, contract, execution):
        portfolio.PortfolioMixin.execDetails(self, reqId, contract, execution)

    def execDetailsEnd(self, reqId):
        portfolio.PortfolioMixin.execDetailsEnd(self, reqId)

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        ignored = [2104, 2106, 2158, 2119, 2108, 2103, 2105, 2174, 2176]
        if errorCode in ignored:
            return
        if errorCode in [162, 200]:
            self.hist_done[reqId] = True

    def historicalData(self, reqId, bar):
        if reqId not in self.historical_data:
            self.historical_data[reqId] = []
        self.historical_data[reqId].append({
            "date": bar.date, "open": bar.open, "high": bar.high,
            "low": bar.low, "close": bar.close, "volume": float(bar.volume),
        })

    def historicalDataEnd(self, reqId, start, end):
        self.hist_done[reqId] = True

    def tickPrice(self, reqId, tickType, price, attrib):
        if reqId not in self.market_data:
            self.market_data[reqId] = {}
        types = {1: "bid", 2: "ask", 4: "last",
                 66: "delayed_bid", 67: "delayed_ask", 68: "delayed_last"}
        if tickType in types:
            self.market_data[reqId][types[tickType]] = price

    def tickSize(self, reqId, tickType, size):
        if reqId not in self.market_data:
            self.market_data[reqId] = {}
        types = {8: "volume", 72: "delayed_volume"}
        if tickType in types:
            self.market_data[reqId][types[tickType]] = size


def make_contract(symbol):
    c = Contract()
    c.symbol = symbol
    c.secType = "STK"
    c.exchange = "SMART"
    c.currency = "USD"
    return c


# ══════════════════════════════════════════════════════════════
#  DATA + ANALYSIS
# ══════════════════════════════════════════════════════════════

ib_app = None
stock_list = []            # list of symbol strings
analysis_cache = {}        # {symbol: result_dict}
fundamentals_cache = {}    # {symbol: {"data": {...}, "ts": timestamp}}
last_update_time = ""
update_lock = threading.Lock()


def fetch_historical(app, symbol, req_id, duration=None):
    contract = make_contract(symbol)
    app.historical_data[req_id] = []
    app.hist_done[req_id] = False
    dur = duration or config.HIST_DURATION
    app.reqHistoricalData(
        req_id, contract, "",
        dur, config.HIST_BAR_SIZE,
        config.HIST_WHAT_TO_SHOW, 1, 1, False, []
    )
    timeout = 60 if "5" in dur else 30
    start = time.time()
    while not app.hist_done.get(req_id, False) and time.time() - start < timeout:
        time.sleep(0.2)
    data = app.historical_data.get(req_id, [])
    if not data:
        return None
    return pd.DataFrame(data)


def analyze_symbol(df):
    if df is None or len(df) < 50:
        return None
    try:
        ind = indicators.calculate_all(df)
        sig = signals.generate_signal(ind)
        sig["price"] = float(df["close"].iloc[-1])

        # Backtesting (usa indicadores pre-computados sobre todo el DataFrame)
        bt = backtester.run_backtest(df, indicators_dict=ind)
        sig["backtest"] = bt

        n = len(df)  # enviar TODOS los datos (5Y) para selector de periodo
        close = df["close"]

        # Dollar volume (proxy for market cap sorting)
        avg_vol = df["volume"].iloc[-20:].mean()
        dv = float(df["close"].iloc[-1] * avg_vol)
        sig["dollar_vol"] = dv if not (math.isnan(dv) or math.isinf(dv)) else 0.0

        # Moving averages (full series, then slice last N)
        mas = {}
        for p in MA_PERIODS:
            if len(close) >= p:
                ma_series = indicators.sma(close, p)
                mas[f"sma{p}"] = [round(float(x), 2) for x in ma_series.iloc[-n:].tolist()]
                mas[f"sma{p}_val"] = round(float(ma_series.iloc[-1]), 2)
            else:
                mas[f"sma{p}"] = []
                mas[f"sma{p}_val"] = None

        if len(close) >= EMA_PERIOD:
            ema_series = indicators.ema(close, EMA_PERIOD)
            mas["ema9"] = [round(float(x), 2) for x in ema_series.iloc[-n:].tolist()]
            mas["ema9_val"] = round(float(ema_series.iloc[-1]), 2)
        else:
            mas["ema9"] = []
            mas["ema9_val"] = None

        # OHLC for candlestick
        ohlc = []
        for _, row in df.iloc[-n:].iterrows():
            d = str(row["date"]).replace(" ", "").replace("-", "")
            if len(d) >= 8:
                ts = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
            else:
                ts = str(row["date"])
            ohlc.append({
                "time": ts,
                "open": round(float(row["open"]), 2),
                "high": round(float(row["high"]), 2),
                "low": round(float(row["low"]), 2),
                "close": round(float(row["close"]), 2),
            })

        dates = df["date"].iloc[-n:].tolist()
        macd_df = ind["macd"].iloc[-n:]
        rsi_df = ind["rsi"].iloc[-n:]
        konc_df = ind["koncorde"].iloc[-n:]

        sig["chart"] = {
            "dates": dates,
            "ohlc": ohlc,
            "mas": mas,
            "macd": {
                "macd": [round(float(x), 2) for x in macd_df["macd"].tolist()],
                "signal": [round(float(x), 2) for x in macd_df["signal"].tolist()],
                "hist": [round(float(x), 2) for x in macd_df["hist"].tolist()],
            },
            "rsi": [round(float(x), 1) for x in rsi_df["rsi"].tolist()],
            "koncorde": {
                "verde": [round(float(x), 1) for x in konc_df["verde"].tolist()],
                "marron": [round(float(x), 1) for x in konc_df["marron"].tolist()],
                "azul": [round(float(x), 1) for x in konc_df["azul"].tolist()],
                "media": [round(float(x), 1) for x in konc_df["media"].tolist()],
            },
        }
        return sig
    except Exception as e:
        print(f"  Error analizando: {e}")
        return None


def get_rt_price(symbol):
    idx = None
    for i, s in enumerate(stock_list):
        if s == symbol:
            idx = i
            break
    if idx is None:
        return None, {}
    mkt = ib_app.market_data.get(5000 + idx, {})
    rt = mkt.get("delayed_last") or mkt.get("last")
    return rt if rt and rt > 0 else None, mkt


def run_analysis():
    global analysis_cache, last_update_time
    total = len(stock_list)
    for i, symbol in enumerate(stock_list):
        req_id = 2000 + i
        print(f"  Analizando {symbol}... ({i + 1}/{total})")
        df = fetch_historical(ib_app, symbol, req_id,
                              duration=config.BACKTEST_DURATION)
        result = analyze_symbol(df)
        # Incremental update: each stock available as soon as analyzed
        with update_lock:
            analysis_cache[symbol] = result
            last_update_time = datetime.now().strftime("%H:%M:%S")
        time.sleep(1)

    print(f"  Analisis completo: {last_update_time}")


def analysis_loop():
    while True:
        try:
            run_analysis()
        except Exception as e:
            print(f"  Error en analisis: {e}")
        time.sleep(config.VISTA_REFRESH_SECONDS)


# ══════════════════════════════════════════════════════════════
#  TOP 3 RECOMMENDATIONS ENGINE
# ══════════════════════════════════════════════════════════════

def _fetch_fundamentals(symbols):
    """Fetch fundamental data for top candidates using yfinance.
    Cached with 1-hour TTL to avoid redundant API calls."""
    global fundamentals_cache
    now = time.time()
    for sym in symbols:
        cached = fundamentals_cache.get(sym)
        if cached and now - cached.get("ts", 0) < 3600:
            continue  # fresh enough
        try:
            ticker = yf.Ticker(sym)
            info = ticker.info or {}
            fund_data = {
                "market_cap": info.get("marketCap"),
                "trailing_pe": info.get("trailingPE"),
                "forward_pe": info.get("forwardPE"),
                "eps": info.get("trailingEps"),
                "dividend_yield": info.get("dividendYield"),
                "beta": info.get("beta"),
                "fifty_two_week_high": info.get("fiftyTwoWeekHigh"),
                "fifty_two_week_low": info.get("fiftyTwoWeekLow"),
                "sector": info.get("sector"),
                "industry": info.get("industry"),
                # Additional ratios
                "roe": info.get("returnOnEquity"),
                "debt_to_equity": info.get("debtToEquity"),
                "current_ratio": info.get("currentRatio"),
                "revenue_growth": info.get("revenueGrowth"),
                "profit_margin": info.get("profitMargins"),
                "operating_margin": info.get("operatingMargins"),
            }
            # Analyst price targets (returns dict)
            try:
                apt = ticker.analyst_price_targets
                if apt and isinstance(apt, dict) and apt.get("mean") is not None:
                    fund_data["analyst_targets"] = {
                        "current": float(apt["current"]) if apt.get("current") is not None else None,
                        "low": float(apt["low"]) if apt.get("low") is not None else None,
                        "mean": float(apt["mean"]) if apt.get("mean") is not None else None,
                        "median": float(apt["median"]) if apt.get("median") is not None else None,
                        "high": float(apt["high"]) if apt.get("high") is not None else None,
                    }
            except Exception:
                pass
            # Insider transactions (last 90 days)
            try:
                ins = ticker.insider_transactions
                if ins is not None and not ins.empty:
                    from datetime import timedelta
                    cutoff = datetime.now() - timedelta(days=90)
                    if "Start Date" in ins.columns:
                        ins["Start Date"] = pd.to_datetime(ins["Start Date"], errors="coerce")
                        recent = ins[ins["Start Date"] >= cutoff]
                    else:
                        recent = ins.head(10)
                    buys = 0
                    sells = 0
                    if "Text" in recent.columns:
                        buys = int(recent["Text"].str.contains("Purchase|Buy|Acquisition", case=False, na=False).sum())
                        sells = int(recent["Text"].str.contains("Sale|Sell|Disposition", case=False, na=False).sum())
                    sentiment = "bullish" if buys > sells else ("bearish" if sells > buys else "neutral")
                    transactions = []
                    for _, row in recent.head(5).iterrows():
                        shares_val = row.get("Shares", 0)
                        value_val = row.get("Value", 0)
                        transactions.append({
                            "insider": str(row.get("Insider", "")),
                            "text": str(row.get("Text", "")),
                            "shares": int(shares_val) if shares_val and not (isinstance(shares_val, float) and shares_val != shares_val) else 0,
                            "value": float(value_val) if value_val and not (isinstance(value_val, float) and value_val != value_val) else 0,
                        })
                    fund_data["insider_trades"] = {
                        "buys": buys, "sells": sells,
                        "sentiment": sentiment,
                        "transactions": transactions,
                    }
            except Exception:
                pass
            # Earnings dates (next future date)
            try:
                ed = ticker.get_earnings_dates(limit=4)
                if ed is not None and not ed.empty:
                    now_dt = datetime.now()
                    future = [d for d in ed.index if d.to_pydatetime().replace(tzinfo=None) > now_dt]
                    if future:
                        nxt = min(future)
                        days_until = (nxt.to_pydatetime().replace(tzinfo=None) - now_dt).days
                        eps_est = ed.loc[nxt].get("EPS Estimate") if nxt in ed.index else None
                        fund_data["earnings"] = {
                            "next_date": nxt.strftime("%Y-%m-%d"),
                            "days_until": days_until,
                            "eps_estimate": float(eps_est) if eps_est is not None and not (isinstance(eps_est, float) and eps_est != eps_est) else None,
                        }
            except Exception:
                pass
            fundamentals_cache[sym] = {"data": fund_data, "ts": now}
        except Exception as e:
            print(f"  yfinance error for {sym}: {e}")
            fundamentals_cache[sym] = {"data": {}, "ts": now}


def _compute_atr(ohlc, period=14):
    """Average True Range from OHLC list-of-dicts."""
    if len(ohlc) < period + 1:
        return None
    trs = []
    for i in range(-period, 0):
        h = ohlc[i]["high"]
        l = ohlc[i]["low"]
        pc = ohlc[i - 1]["close"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else None


def _recent_swing(ohlc, lookback=20):
    """High/low from last N bars."""
    recent = ohlc[-lookback:]
    return (max(b["high"] for b in recent),
            min(b["low"] for b in recent))


def _label_is_bearish(label):
    """True if a signal_label represents bearish/sell-side positioning.

    Used instead of the raw BUY/SELL/HOLD `signal` field so that partial
    signals (INMINENTE, VIRANDO, ZONA DE...) keep the same directional
    bias as the label shown to the user — a HOLD with signal_label
    "VENTA INMINENTE" must still be treated as bearish for entry/target/
    stop and backtest stat selection, or the numbers contradict the label.
    """
    return "VENTA" in label or "SOBRECOMPRA" in label


def _score_stock(sym, data):
    """Score a stock 0-100 for top-3 ranking. Returns None if ineligible."""
    sig = data.get("signal", "HOLD")
    label = data.get("signal_label", sig)
    is_bearish = _label_is_bearish(label)
    strength = data.get("strength", 0) or 0
    conditions = data.get("conditions_met", 0) or 0
    bt = data.get("backtest", {}) or {}
    confidence = bt.get("confidence", 0) or 0

    # Eligibility
    if sig == "HOLD" and (conditions < 2 or confidence < 30):
        return None
    ohlc = (data.get("chart") or {}).get("ohlc", [])
    if len(ohlc) < 20:
        return None

    # Component 1: strength (0-30)
    s1 = min(strength / 5.1, 1.0) * 30

    # Component 2: backtest confidence (0-25)
    s2 = min(confidence / 100, 1.0) * 25

    # Component 3: win rate (0-25)
    if is_bearish:
        wr = bt.get("sell_win_rate", 0) or 0
        avg_ret = bt.get("sell_avg_return") or 0
    else:
        wr = bt.get("buy_win_rate", 0) or 0
        avg_ret = bt.get("buy_avg_return") or 0
    s3 = min(wr, 1.0) * 25

    # Component 4: avg return quality (±10)
    capped = max(-15.0, min(15.0, float(avg_ret)))
    s4 = (capped / 15.0) * 10

    # Component 5: active signal bonus (0 or 10)
    s5 = 10.0 if sig in ("BUY", "SELL") else 0.0

    return max(0.0, min(100.0, s1 + s2 + s3 + s4 + s5))


def _compute_price_levels(data):
    """Compute entry zone, target, stop loss from MAs + ATR."""
    price = data.get("price", 0)
    sig = data.get("signal", "HOLD")
    label = data.get("signal_label", sig)
    is_bearish = _label_is_bearish(label)
    mas = (data.get("chart") or {}).get("mas", {})
    ohlc = (data.get("chart") or {}).get("ohlc", [])

    atr = _compute_atr(ohlc) if len(ohlc) >= 15 else None
    if atr is None or atr <= 0:
        atr = price * 0.02  # fallback 2%
    swing_h, swing_l = _recent_swing(ohlc) if len(ohlc) >= 5 else (price * 1.05, price * 0.95)

    # Collect MA values
    ma_vals = {}
    for k in ("sma200_val", "sma100_val", "sma50_val", "sma20_val", "ema9_val"):
        v = mas.get(k)
        if v is not None:
            ma_vals[k] = v

    # Map MA keys to readable names
    ma_names = {"sma200_val": "SMA200", "sma100_val": "SMA100", "sma50_val": "SMA50",
                "sma20_val": "SMA20", "ema9_val": "EMA9"}
    min_target_pct = 0.10  # 10% minimum target

    if is_bearish:
        # Resistances above for entry ceiling
        res_above = sorted([v for v in ma_vals.values() if v > price])
        entry_high = min(res_above[0], price + 1.5 * atr) if res_above else price + atr
        entry_low = price

        # Supports below for target
        sup_below = sorted([(k, v) for k, v in ma_vals.items() if v < price], key=lambda x: x[1], reverse=True)
        if sup_below:
            target = sup_below[0][1]
            target_basis = f"Soporte {ma_names.get(sup_below[0][0], sup_below[0][0])} en ${target:.2f}"
        elif swing_l < price - 2 * atr:
            target = swing_l
            target_basis = f"Swing low reciente en ${swing_l:.2f}"
        else:
            target = price - 2 * atr
            target_basis = f"Extension ATR 2x bajo precio"

        # Enforce 10% minimum
        orig_target_pct = (price - target) / price if price > 0 else 0
        if orig_target_pct < min_target_pct and price > 0:
            target = round(price * (1 - min_target_pct), 2)
            target_basis = f"Minimo 10% aplicado (tecnico sugeria {orig_target_pct*100:.1f}%)"

        stop = max(entry_high + atr, swing_h + 0.5 * atr)
        risk = stop - price
        reward = price - target
    else:
        # Supports below for entry floor
        sup_below = sorted([v for v in ma_vals.values() if v < price], reverse=True)
        entry_low = max(sup_below[0], price - 1.5 * atr) if sup_below else price - atr
        entry_high = price

        # Resistances above for target
        res_above = sorted([(k, v) for k, v in ma_vals.items() if v > price], key=lambda x: x[1])
        if res_above:
            target = res_above[0][1]
            target_basis = f"Resistencia {ma_names.get(res_above[0][0], res_above[0][0])} en ${target:.2f}"
        elif swing_h > price + 2 * atr:
            target = swing_h
            target_basis = f"Swing high reciente en ${swing_h:.2f}"
        else:
            target = price + 2 * atr
            target_basis = f"Extension ATR 2x sobre precio"

        # Enforce 10% minimum
        orig_target_pct = (target - price) / price if price > 0 else 0
        if orig_target_pct < min_target_pct and price > 0:
            target = round(price * (1 + min_target_pct), 2)
            target_basis = f"Minimo 10% aplicado (tecnico sugeria {orig_target_pct*100:.1f}%)"

        stop = min(entry_low - atr, swing_l - 0.5 * atr)
        risk = price - stop
        reward = target - price

    rr = round(reward / risk, 2) if risk > 0 else 0.0
    target_pct = reward / price * 100 if price > 0 else 0

    # Horizon estimate: distance_to_target / (ATR * efficiency_factor)
    horizon_weeks = ""
    if atr > 0 and price > 0:
        distance = abs(target - price)
        days_est = distance / (atr * 0.6)
        weeks = max(1, round(days_est / 5))
        if weeks <= 2:
            horizon_weeks = "1-2 semanas"
        elif weeks <= 4:
            horizon_weeks = "2-4 semanas"
        elif weeks <= 8:
            horizon_weeks = "4-8 semanas"
        elif weeks <= 12:
            horizon_weeks = "2-3 meses"
        else:
            m_lo = weeks // 4
            horizon_weeks = f"{m_lo}-{m_lo + 2} meses"

    return {
        "entry_low": round(entry_low, 2),
        "entry_high": round(entry_high, 2),
        "target": round(target, 2),
        "stop_loss": round(stop, 2),
        "atr": round(atr, 2),
        "risk_reward": rr,
        "target_pct": round(target_pct, 1),
        "target_basis": target_basis,
        "horizon_weeks": horizon_weeks,
    }


def _generate_rationale(sym, data, levels=None):
    """Build rationale bullet list from existing indicator data."""
    sig = data.get("signal", "HOLD")
    bt = data.get("backtest", {}) or {}
    vals = data.get("values", {}) or {}
    mas = (data.get("chart") or {}).get("mas", {})
    ohlc = (data.get("chart") or {}).get("ohlc", [])
    price = data.get("price", 0)
    parts = []

    # 1. Signal summary (consistent with signal_label)
    label = data.get("signal_label", sig)
    conds = data.get("conditions_met", 0)
    if sig == "BUY":
        parts.append(f"Senal de {label}: 3/3 indicadores alineados al alza (fuerza {data.get('strength', 0):.1f})")
    elif sig == "SELL":
        parts.append(f"Senal de {label}: 3/3 indicadores alineados a la baja (fuerza {data.get('strength', 0):.1f})")
    else:
        parts.append(f"{label} — {conds}/3 indicadores activos")

    # 2. Target justification (from levels)
    is_bearish = _label_is_bearish(label)
    if levels:
        tp = levels.get("target_pct", 0)
        tb = levels.get("target_basis", "")
        hz = levels.get("horizon_weeks", "")
        tgt = levels.get("target", 0)
        sign = "-" if is_bearish else "+"
        target_text = f"Objetivo: ${tgt:.2f} ({sign}{abs(tp):.0f}%)"
        if tb:
            target_text += f" — {tb}"
        if hz:
            target_text += f". Horizonte estimado: {hz}"
        parts.append(target_text)

    # 2. Indicator details
    md = data.get("macd_detail", "")
    rd = data.get("rsi_detail", "")
    kd = data.get("konc_detail", "")
    if md:
        chk = "✓" if data.get("macd_ok") else "✗"
        parts.append(f"MACD {chk}: {md}")
    if rd:
        chk = "✓" if data.get("rsi_ok") else "✗"
        parts.append(f"RSI {chk}: {rd}")
    if kd:
        chk = "✓" if data.get("konc_ok") else "✗"
        parts.append(f"Koncorde {chk}: {kd}")

    # 3. Trend from MAs
    sma200 = mas.get("sma200_val")
    sma50 = mas.get("sma50_val")
    trend = []
    if sma200 and price:
        pct200 = (price - sma200) / sma200 * 100
        trend.append(f"{'sobre' if price > sma200 else 'bajo'} SMA200 ({pct200:+.1f}%)")
    if sma50 and sma200:
        if sma50 > sma200:
            trend.append("golden cross activo")
        else:
            trend.append("death cross activo")
    if trend:
        parts.append("Tendencia: " + ", ".join(trend))

    # 4. Volatility
    atr_val = _compute_atr(ohlc) if len(ohlc) >= 15 else None
    if atr_val and price > 0:
        atr_pct = (atr_val / price) * 100
        vlabel = "baja" if atr_pct < 1.5 else ("moderada" if atr_pct < 3.0 else "alta")
        parts.append(f"Volatilidad {vlabel}: ATR ${atr_val:.2f} ({atr_pct:.1f}% del precio)")

    # 5. Institutional flow
    konc = vals.get("koncorde", {})
    azul = konc.get("azul")
    if azul is not None:
        if azul > 5:
            parts.append(f"Flujo institucional: fuerte entrada (azul={azul:.1f})")
        elif azul > 0:
            parts.append(f"Flujo institucional: entrada moderada (azul={azul:.1f})")
        elif azul < -5:
            parts.append(f"Flujo institucional: fuerte salida (azul={azul:.1f})")
        elif azul < 0:
            parts.append(f"Flujo institucional: salida moderada (azul={azul:.1f})")
        else:
            parts.append("Flujo institucional: neutral")

    # 6. Liquidity
    dv = data.get("dollar_vol", 0) or 0
    if dv > 0:
        parts.append(f"Liquidez: ${dv / 1e6:.0f}M volumen diario promedio")

    # 7. Backtest summary
    if sig == "BUY" or sig == "HOLD":
        cnt = bt.get("buy_count", 0)
        wr = bt.get("buy_win_rate", 0) or 0
        ar = bt.get("buy_avg_return")
        direction = "compra"
    else:
        cnt = bt.get("sell_count", 0)
        wr = bt.get("sell_win_rate", 0) or 0
        ar = bt.get("sell_avg_return")
        direction = "venta"
    bt_text = f"Backtest 5Y: {cnt} senales de {direction}, win rate {wr * 100:.0f}%"
    if ar is not None:
        bt_text += f", retorno promedio {ar:+.1f}%"
    parts.append(bt_text)

    # 8. Fundamentals (from cache)
    fund_entry = fundamentals_cache.get(sym, {})
    fund = fund_entry.get("data", {}) if isinstance(fund_entry, dict) else {}
    if fund:
        fparts = []
        pe = fund.get("trailing_pe")
        if pe is not None:
            pe_label = "baja" if pe < 15 else ("moderada" if pe < 25 else "alta")
            fparts.append(f"P/E {pe:.1f} (valuacion {pe_label})")
        mc = fund.get("market_cap")
        if mc is not None:
            if mc >= 1e12:
                fparts.append(f"Market Cap ${mc/1e12:.1f}T")
            elif mc >= 1e9:
                fparts.append(f"Market Cap ${mc/1e9:.1f}B")
            else:
                fparts.append(f"Market Cap ${mc/1e6:.0f}M")
        dy = fund.get("dividend_yield")
        if dy is not None:
            fparts.append(f"Div Yield {dy:.2f}%")
        beta = fund.get("beta")
        if beta is not None:
            risk = "bajo" if beta < 0.8 else ("moderado" if beta < 1.3 else "alto")
            fparts.append(f"Beta {beta:.2f} (riesgo {risk})")
        if fparts:
            parts.append("Fundamentales: " + ", ".join(fparts))

        # 9. Analyst price targets
        apt = fund.get("analyst_targets")
        if apt and apt.get("mean"):
            upside = ((apt["mean"] - price) / price * 100) if price else 0
            direction = "potencial subida" if upside > 0 else "potencial baja"
            parts.append(f"Analistas: target promedio ${apt['mean']:.2f} ({direction} {abs(upside):.1f}%), rango ${apt.get('low', '?')} — ${apt.get('high', '?')}")

        # 10. Insider activity (90 days)
        ins = fund.get("insider_trades")
        if ins:
            s = ins["sentiment"]
            emoji = "alcista" if s == "bullish" else ("bajista" if s == "bearish" else "neutral")
            parts.append(f"Insiders (90d): {ins['buys']} compras, {ins['sells']} ventas — sentimiento {emoji}")

        # 11. Earnings proximity
        earn = fund.get("earnings")
        if earn and earn.get("days_until") is not None:
            d = earn["days_until"]
            if d <= 14:
                parts.append(f"ATENCION: Earnings en {d} dias ({earn['next_date']}). Volatilidad esperada alta.")
            elif d <= 30:
                parts.append(f"Earnings proximos: {earn['next_date']} (en {d} dias)")

        # 12. Additional ratios
        rparts = []
        roe = fund.get("roe")
        if roe is not None:
            rparts.append(f"ROE {roe * 100:.1f}%")
        de = fund.get("debt_to_equity")
        if de is not None:
            rparts.append(f"D/E {de:.1f}")
        rg = fund.get("revenue_growth")
        if rg is not None:
            rparts.append(f"Rev Growth {rg * 100:.1f}%")
        pm = fund.get("profit_margin")
        if pm is not None:
            rparts.append(f"Profit Margin {pm * 100:.1f}%")
        if rparts:
            parts.append("Ratios: " + ", ".join(rparts))

    return parts


def _generate_thesis(sym, data, levels, fund):
    """Generate investment thesis in Spanish, consistent with signal_label."""
    sig = data.get("signal", "HOLD")
    label = data.get("signal_label", sig)
    price = data.get("price", 0)
    strength = data.get("strength", 0) or 0
    conds = data.get("conditions_met", 0)
    mas = (data.get("chart") or {}).get("mas", {})
    vals = data.get("values", {}) or {}
    bt = data.get("backtest", {}) or {}

    lines = []

    # --- Line 1: Signal label + direction (consistent with label) ---
    is_bearish = _label_is_bearish(label)

    if sig == "BUY":
        lines.append(f"{sym} presenta senal de {label} con fuerza {strength:.1f}/5.1 — los 3 indicadores alineados al alza.")
    elif sig == "SELL":
        lines.append(f"{sym} presenta senal de {label} con fuerza {strength:.1f}/5.1 — los 3 indicadores alineados a la baja.")
    elif "INMINENTE" in label and "COMPRA" in label:
        lines.append(f"{sym} esta en {label}: {conds}/3 indicadores alineados al alza, a punto de confirmar senal de compra.")
    elif "INMINENTE" in label and "VENTA" in label:
        lines.append(f"{sym} esta en {label}: {conds}/3 indicadores alineados a la baja, a punto de confirmar senal de venta.")
    elif "VIRANDO" in label and "COMPRA" in label:
        lines.append(f"{sym} muestra indicadores {label.lower()}: los tecnicos empiezan a girar al alza.")
    elif "VIRANDO" in label and "VENTA" in label:
        lines.append(f"{sym} muestra indicadores {label.lower()}: los tecnicos empiezan a girar a la baja.")
    elif "SOBREVENTA" in label:
        lines.append(f"{sym} se encuentra en {label.lower()}, lo que podria anticipar un rebote alcista si los indicadores confirman.")
    elif "SOBRECOMPRA" in label:
        lines.append(f"{sym} se encuentra en {label.lower()}, lo que podria anticipar una correccion si los indicadores confirman.")
    else:
        lines.append(f"{sym} se mantiene NEUTRAL — {conds}/3 indicadores activos, sin tendencia definida.")

    # --- Line 2: Indicator status (MACD, RSI, Koncorde) ---
    ind_parts = []
    macd_v = vals.get("macd", {})
    rsi_v = vals.get("rsi")
    konc = vals.get("koncorde", {})

    if macd_v:
        hist = macd_v.get("hist")
        if hist is not None:
            macd_ok = data.get("macd_ok", False)
            if hist > 0:
                if macd_ok:
                    ind_parts.append(f"MACD positivo ({hist:.2f}) pero girando a la baja")
                else:
                    ind_parts.append(f"MACD positivo ({hist:.2f})")
            else:
                if macd_ok:
                    ind_parts.append(f"MACD negativo ({hist:.2f}) pero recuperando")
                else:
                    ind_parts.append(f"MACD negativo ({hist:.2f})")

    if rsi_v is not None:
        rsi_ok = data.get("rsi_ok", False)
        if rsi_v < 25:
            ind_parts.append(f"RSI en sobreventa extrema ({rsi_v:.0f})")
        elif rsi_v < 30:
            ind_parts.append(f"RSI en sobreventa ({rsi_v:.0f})")
        elif rsi_v < 40:
            ind_parts.append(f"RSI bajo ({rsi_v:.0f}), cerca de sobreventa")
        elif rsi_v > 80:
            ind_parts.append(f"RSI en sobrecompra extrema ({rsi_v:.0f})")
        elif rsi_v > 70:
            ind_parts.append(f"RSI en sobrecompra ({rsi_v:.0f})")
        elif rsi_v > 60:
            ind_parts.append(f"RSI elevado ({rsi_v:.0f}), cerca de sobrecompra")
        else:
            ind_parts.append(f"RSI neutral ({rsi_v:.0f})")

    marron = konc.get("marron")
    media = konc.get("media")
    azul = konc.get("azul")
    if marron is not None and media is not None:
        konc_ok = data.get("konc_ok", False)
        if marron > media:
            pos_txt = "sobre la media"
            if konc_ok:
                pos_txt += " pero girando a la baja"
        else:
            pos_txt = "bajo la media"
            if konc_ok:
                pos_txt += " pero recuperando"
        ind_parts.append(f"Koncorde {pos_txt} ({marron:.1f} vs {media:.1f})")

    if ind_parts:
        lines.append("Indicadores: " + "; ".join(ind_parts) + ".")

    # --- Line 3: Moving averages position ---
    sma200 = mas.get("sma200_val")
    sma100 = mas.get("sma100_val")
    sma50 = mas.get("sma50_val")
    sma20 = mas.get("sma20_val")
    ma_parts = []
    if sma200 and price:
        pct200 = (price - sma200) / sma200 * 100
        ma_parts.append(f"{'+'if pct200>=0 else ''}{pct200:.0f}% vs SMA200")
    if sma50 and price:
        pct50 = (price - sma50) / sma50 * 100
        ma_parts.append(f"{'+'if pct50>=0 else ''}{pct50:.0f}% vs SMA50")
    if sma20 and price:
        pct20 = (price - sma20) / sma20 * 100
        ma_parts.append(f"{'+'if pct20>=0 else ''}{pct20:.0f}% vs SMA20")
    cross_txt = ""
    if sma50 and sma200:
        if sma50 > sma200:
            cross_txt = " Golden cross activo."
        else:
            cross_txt = " Death cross activo."
    if ma_parts:
        lines.append("Medias moviles: " + ", ".join(ma_parts) + "." + cross_txt)

    # --- Line 4: Institutional flow (Koncorde azul) ---
    if azul is not None:
        if azul > 5:
            lines.append(f"Flujo institucional: fuerte entrada (azul={azul:.1f}).")
        elif azul > 0:
            lines.append(f"Flujo institucional: entrada moderada (azul={azul:.1f}).")
        elif azul < -5:
            lines.append(f"Flujo institucional: salida marcada (azul={azul:.1f}) — presion vendedora institucional.")
        elif azul < 0:
            lines.append(f"Flujo institucional: leve salida (azul={azul:.1f}).")

    # --- Line 5: Target (direction consistent with label) ---
    target = levels.get("target", 0)
    target_pct = levels.get("target_pct", 0)
    target_basis = levels.get("target_basis", "")
    horizon = levels.get("horizon_weeks", "")
    atr_val = levels.get("atr", 0)

    if is_bearish:
        dir_word = "baja"
        sign_char = "-"
    else:
        dir_word = "subida"
        sign_char = "+"
    target_line = f"Objetivo en ${target:.2f} ({sign_char}{abs(target_pct):.0f}% de {dir_word})"
    if target_basis:
        target_line += f", basado en {target_basis.lower()}"
    target_line += "."
    if horizon:
        target_line += f" Horizonte estimado: {horizon}."
    lines.append(target_line)

    # --- Line 6 (optional): Fundamental support ---
    if fund:
        fline_parts = []
        apt = fund.get("analyst_targets")
        if apt and apt.get("mean") and price:
            analyst_upside = ((apt["mean"] - price) / price * 100)
            if analyst_upside > 5:
                fline_parts.append(f"Los analistas ven upside de {analyst_upside:.0f}% con target promedio ${apt['mean']:.2f}")
            elif analyst_upside < -5:
                fline_parts.append(f"Los analistas ven downside de {abs(analyst_upside):.0f}%")

        ins = fund.get("insider_trades")
        if ins:
            s = ins.get("sentiment", "neutral")
            if s == "bullish":
                fline_parts.append("insiders comprando recientemente")
            elif s == "bearish":
                fline_parts.append("insider selling reciente es un factor de riesgo")

        earn = fund.get("earnings")
        if earn and earn.get("days_until") is not None:
            d = earn["days_until"]
            if d <= 14:
                fline_parts.append(f"ATENCION: earnings en {d} dias")
            elif d <= 30:
                fline_parts.append(f"earnings proximos en {d} dias")

        if fline_parts:
            fline = fline_parts[0][0].upper() + fline_parts[0][1:]
            if len(fline_parts) > 1:
                fline += ". " + ". ".join(p[0].upper() + p[1:] for p in fline_parts[1:])
            lines.append(fline + ".")

    return " ".join(lines)


def _compute_signal_markers(data, n_bars=90):
    """Scan last n_bars of indicator data for historical BUY/SELL signals.
    Returns list of {time, position, color, shape, text} for LW markers."""
    chart = data.get("chart") or {}
    ohlc = chart.get("ohlc", [])
    macd_hist = (chart.get("macd") or {}).get("hist", [])
    rsi_vals = chart.get("rsi", [])
    konc = chart.get("koncorde") or {}
    marron_vals = konc.get("marron", [])
    media_vals = konc.get("media", [])

    total = len(ohlc)
    if total < 5 or len(macd_hist) < 5 or len(rsi_vals) < 5:
        return []

    start = max(0, total - n_bars)
    markers = []

    for i in range(max(start, 2), total):
        # Need hist, rsi, marron, media at indices i, i-1
        if i >= len(macd_hist) or i - 1 >= len(macd_hist):
            continue
        if i >= len(rsi_vals) or i >= len(marron_vals) or i >= len(media_vals):
            continue
        if i - 1 >= len(marron_vals) or i - 1 >= len(media_vals):
            continue

        h = macd_hist[i]
        h1 = macd_hist[i - 1]
        r = rsi_vals[i]
        m = marron_vals[i]
        m1 = marron_vals[i - 1]
        med = media_vals[i]

        if any(v is None for v in [h, h1, r, m, m1, med]):
            continue

        # BUY signal
        if h < 0 and h > h1 and r < 30 and m < med and m > m1:
            markers.append({
                "time": ohlc[i]["time"],
                "position": "belowBar",
                "color": "#10b981",
                "shape": "arrowUp",
                "text": "BUY",
            })

        # SELL signal
        if h > 0 and h < h1 and r > 70 and m > med and m < m1:
            markers.append({
                "time": ohlc[i]["time"],
                "position": "aboveBar",
                "color": "#ef4444",
                "shape": "arrowDown",
                "text": "SELL",
            })

    return markers


def _extract_chart_data(data, n_bars=90):
    """Extract last n_bars of OHLC + MAs for the rec chart."""
    chart = data.get("chart") or {}
    ohlc = chart.get("ohlc", [])
    mas_full = chart.get("mas", {})

    total = len(ohlc)
    ohlc_slice = ohlc[-n_bars:] if total > n_bars else ohlc
    start_idx = total - len(ohlc_slice)

    # Slice MA arrays to align with ohlc_slice
    mas_sliced = {}
    for name in ("sma200", "sma100", "sma50", "sma20", "ema9"):
        arr = mas_full.get(name, [])
        if arr and len(arr) > start_idx:
            mas_sliced[name] = arr[start_idx:]
        else:
            mas_sliced[name] = []

    return ohlc_slice, mas_sliced, start_idx


def compute_top3(cache):
    """Compute top 3 stock recommendations from analysis_cache."""
    scored = []
    for sym, data in cache.items():
        if data is None:
            continue
        score = _score_stock(sym, data)
        if score is not None:
            scored.append((sym, data, score))

    scored.sort(key=lambda x: x[2], reverse=True)

    # If fewer than 3 eligible, relax filter: include best HOLDs with score > 0
    if len(scored) < 3:
        for sym, data in cache.items():
            if data is None:
                continue
            if any(s == sym for s, _, _ in scored):
                continue  # already scored
            ohlc = (data.get("chart") or {}).get("ohlc", [])
            if len(ohlc) < 20:
                continue
            strength = data.get("strength", 0) or 0
            bt = data.get("backtest", {}) or {}
            wr = bt.get("buy_win_rate", 0) or 0
            confidence = bt.get("confidence", 0) or 0
            # Relaxed score for HOLDs
            s = min(strength / 5.1, 1.0) * 30 + min(confidence / 100, 1.0) * 25 + min(wr, 1.0) * 25
            if s > 0:
                scored.append((sym, data, round(s, 1)))
        scored.sort(key=lambda x: x[2], reverse=True)

    # Fetch fundamentals only for top candidates (not all 100)
    top_syms = [sym for sym, _, _ in scored[:8]]
    try:
        _fetch_fundamentals(top_syms)
    except Exception as e:
        print(f"  Fundamentals fetch error: {e}")

    n_bars = 90
    top3 = []
    for sym, data, score in scored[:3]:
        levels = _compute_price_levels(data)
        rationale = _generate_rationale(sym, data, levels)
        bt = data.get("backtest", {}) or {}
        sig = data.get("signal", "HOLD")

        # Chart data for recommendation card
        ohlc_slice, mas_sliced, _ = _extract_chart_data(data, n_bars)
        sig_markers = _compute_signal_markers(data, n_bars)

        # Indicator data for MACD/RSI/Koncorde charts
        chart = data.get("chart") or {}
        total = len(chart.get("ohlc", []))
        start = max(0, total - n_bars)

        all_dates = chart.get("dates", [])
        dates_slice = all_dates[start:] if len(all_dates) > start else all_dates

        macd_full = chart.get("macd", {})
        chart_macd = {
            "macd": (macd_full.get("macd") or [])[start:],
            "signal": (macd_full.get("signal") or [])[start:],
            "hist": (macd_full.get("hist") or [])[start:],
        }

        chart_rsi = (chart.get("rsi") or [])[start:]

        konc_full = chart.get("koncorde", {})
        chart_koncorde = {
            "verde": (konc_full.get("verde") or [])[start:],
            "marron": (konc_full.get("marron") or [])[start:],
            "azul": (konc_full.get("azul") or [])[start:],
            "media": (konc_full.get("media") or [])[start:],
        }

        # Fundamentals
        fund_entry = fundamentals_cache.get(sym, {})
        fund = fund_entry.get("data", {}) if isinstance(fund_entry, dict) else {}

        # Generate investment thesis
        thesis = _generate_thesis(sym, data, levels, fund)

        rec = {
            "symbol": sym,
            "signal": sig,
            "signal_label": data.get("signal_label", sig),
            "price": data.get("price", 0),
            "strength": data.get("strength", 0) or 0,
            "conditions_met": data.get("conditions_met", 0) or 0,
            "confidence": bt.get("confidence", 0) or 0,
            "score": round(score, 1),
            "entry_low": levels["entry_low"],
            "entry_high": levels["entry_high"],
            "target": levels["target"],
            "stop_loss": levels["stop_loss"],
            "risk_reward": levels["risk_reward"],
            "atr": levels["atr"],
            "target_pct": levels.get("target_pct", 0),
            "target_basis": levels.get("target_basis", ""),
            "horizon": levels.get("horizon_weeks", ""),
            "thesis": thesis,
            "win_rate": (bt.get("sell_win_rate", 0) if _label_is_bearish(data.get("signal_label", sig))
                         else bt.get("buy_win_rate", 0)) or 0,
            "avg_return": (bt.get("sell_avg_return") if _label_is_bearish(data.get("signal_label", sig))
                           else bt.get("buy_avg_return")),
            "rationale": rationale,
            "chart_ohlc": ohlc_slice,
            "chart_mas": mas_sliced,
            "chart_markers": sig_markers,
            "chart_dates": dates_slice,
            "chart_macd": chart_macd,
            "chart_rsi": chart_rsi,
            "chart_koncorde": chart_koncorde,
            "fundamentals": fund,
        }
        top3.append(rec)
    return top3


def compute_secondary_opps(cache):
    """Scan analysis_cache for individual technical patterns.
    Returns dict with 5 categories, each a list of max 5 stocks."""
    sobreventa = []
    sobrecompra = []
    cerca_sma200 = []
    death_cross = []
    golden_cross = []

    for sym, data in cache.items():
        if data is None:
            continue
        price = data.get("price")
        if not price or price <= 0:
            continue
        signal = data.get("signal", "HOLD")

        vals = data.get("values") or {}
        rsi = vals.get("rsi")
        mas = ((data.get("chart") or {}).get("mas") or {})
        sma200_val = mas.get("sma200_val")
        sma50_val = mas.get("sma50_val")
        sma200_series = mas.get("sma200", [])
        sma50_series = mas.get("sma50", [])

        # --- Sobreventa (RSI < 35) ---
        if rsi is not None and not math.isnan(rsi) and rsi < 35:
            label = "SOBREVENTA EXTREMA" if rsi < 25 else ("SOBREVENTA" if rsi < 30 else "Acercandose a sobreventa")
            sobreventa.append({
                "symbol": sym, "price": round(price, 2),
                "rsi": round(rsi, 1), "signal": signal,
                "detail": f"RSI {rsi:.1f} — {label}",
                "relevance": round(100 - rsi, 1),
            })

        # --- Sobrecompra (RSI > 65) ---
        if rsi is not None and not math.isnan(rsi) and rsi > 65:
            label = "SOBRECOMPRA EXTREMA" if rsi > 80 else ("SOBRECOMPRA" if rsi > 70 else "Acercandose a sobrecompra")
            sobrecompra.append({
                "symbol": sym, "price": round(price, 2),
                "rsi": round(rsi, 1), "signal": signal,
                "detail": f"RSI {rsi:.1f} — {label}",
                "relevance": round(rsi, 1),
            })

        # --- Cerca de SMA200 (dentro de 3%) ---
        if sma200_val is not None and not math.isnan(sma200_val) and sma200_val > 0:
            pct_diff = abs(price - sma200_val) / sma200_val * 100
            if pct_diff < 3.0:
                side = "sobre" if price >= sma200_val else "bajo"
                cerca_sma200.append({
                    "symbol": sym, "price": round(price, 2),
                    "sma200": round(sma200_val, 2), "pct_diff": round(pct_diff, 1),
                    "signal": signal,
                    "detail": f"Precio {pct_diff:.1f}% {side} SMA200 (${sma200_val:.2f})",
                    "relevance": round(100 - pct_diff * 33, 1),
                })

        # --- Death Cross / Golden Cross inminente (gap SMA50-SMA200 < 2%) ---
        if (sma50_val is not None and sma200_val is not None
                and not math.isnan(sma50_val) and not math.isnan(sma200_val)
                and sma200_val > 0):
            pct_gap = abs(sma50_val - sma200_val) / sma200_val * 100
            if pct_gap < 2.0:
                # Verificar convergencia: gap actual vs 5 barras atras
                converging = False
                if len(sma50_series) >= 6 and len(sma200_series) >= 6:
                    gap_now = abs(sma50_series[-1] - sma200_series[-1])
                    gap_prev = abs(sma50_series[-6] - sma200_series[-6])
                    converging = gap_now < gap_prev

                conv_txt = "convergiendo" if converging else "estable"

                if sma50_val > sma200_val:
                    death_cross.append({
                        "symbol": sym, "price": round(price, 2),
                        "sma50": round(sma50_val, 2), "sma200": round(sma200_val, 2),
                        "pct_gap": round(pct_gap, 1), "signal": signal,
                        "detail": f"SMA50 {pct_gap:.1f}% sobre SMA200 — {conv_txt}",
                        "relevance": round(100 - pct_gap * 50, 1),
                    })
                else:
                    golden_cross.append({
                        "symbol": sym, "price": round(price, 2),
                        "sma50": round(sma50_val, 2), "sma200": round(sma200_val, 2),
                        "pct_gap": round(pct_gap, 1), "signal": signal,
                        "detail": f"SMA50 {pct_gap:.1f}% bajo SMA200 — {conv_txt}",
                        "relevance": round(100 - pct_gap * 50, 1),
                    })

    # Ordenar por relevancia y limitar a 5
    sobreventa.sort(key=lambda x: x["relevance"], reverse=True)
    sobrecompra.sort(key=lambda x: x["relevance"], reverse=True)
    cerca_sma200.sort(key=lambda x: x["relevance"], reverse=True)
    death_cross.sort(key=lambda x: x["relevance"], reverse=True)
    golden_cross.sort(key=lambda x: x["relevance"], reverse=True)

    return {
        "sobreventa": sobreventa[:5],
        "sobrecompra": sobrecompra[:5],
        "cerca_sma200": cerca_sma200[:5],
        "death_cross": death_cross[:5],
        "golden_cross": golden_cross[:5],
    }


# ══════════════════════════════════════════════════════════════
#  FLASK APP
# ══════════════════════════════════════════════════════════════

flask_app = Flask(__name__)

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vista Analisis - Top 100 Volumen</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;600;700&display=swap');
:root{--bg:#080b12;--surface:#0e1219;--card:#141a24;--border:#1e2736;--border-subtle:#161d28;--accent:#6366f1;--accent-glow:#6366f140;--accent-soft:#818cf830;--buy:#10b981;--sell:#ef4444;--hold:#f59e0b;--text:#e8ecf2;--muted:#7c8898;--dim:#4b5668;--radius:10px;--radius-lg:14px;--shadow-sm:0 1px 2px rgba(0,0,0,.3);--shadow-md:0 4px 12px rgba(0,0,0,.25);--shadow-lg:0 8px 32px rgba(0,0,0,.35);--glass:rgba(255,255,255,.03);--glass-border:rgba(255,255,255,.06)}
*{margin:0;padding:0;box-sizing:border-box}
html{font-size:14px;-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;text-rendering:optimizeLegibility}
body{background:var(--bg);color:var(--text);font-family:'Inter',system-ui,-apple-system,sans-serif;font-size:14px;line-height:1.5;overflow-x:hidden}
::selection{background:var(--accent);color:#fff}
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:var(--dim)}

/* === HEADER === */
.header{background:var(--surface);padding:18px 32px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;backdrop-filter:blur(12px)}
.header h1{font-size:17px;color:#fff;font-weight:800;letter-spacing:-.3px}
.header h1 em{font-style:normal;color:var(--accent);font-weight:800}
.header .sub{color:var(--muted);font-size:12px;text-align:right;font-weight:500}

/* === COUNTERS === */
.counters{display:flex;gap:8px;padding:12px 32px;background:var(--bg);border-bottom:1px solid var(--border);flex-wrap:wrap;align-items:center}
.counter{padding:5px 14px;border-radius:8px;font-weight:700;font-size:12px;letter-spacing:.3px;border:1px solid transparent;transition:all .15s;cursor:default}
.c-buy{background:rgba(16,185,129,.1);color:var(--buy);border-color:rgba(16,185,129,.2)}
.c-sell{background:rgba(239,68,68,.1);color:var(--sell);border-color:rgba(239,68,68,.2)}
.c-hold{background:rgba(245,158,11,.1);color:var(--hold);border-color:rgba(245,158,11,.2)}
.c-nodata{background:rgba(75,86,104,.15);color:var(--muted);border-color:var(--border)}
.c-total{background:rgba(99,102,241,.1);color:#a5b4fc;border-color:rgba(99,102,241,.2)}
.c-buy-near{background:rgba(16,185,129,.08);color:#6ee7b7;border-color:rgba(16,185,129,.15)}
.c-sell-near{background:rgba(239,68,68,.08);color:#fca5a5;border-color:rgba(239,68,68,.15)}
.c-turning-buy{background:rgba(134,239,172,.06);color:#86efac;border-color:rgba(134,239,172,.15)}
.c-turning-sell{background:rgba(253,164,175,.06);color:#fda4af;border-color:rgba(253,164,175,.15)}
.c-zone{background:rgba(125,211,252,.06);color:#7dd3fc;border-color:rgba(125,211,252,.15)}
.c-neutral{background:rgba(148,163,184,.08);color:#94a3b8;border-color:rgba(148,163,184,.2)}

/* === GRID TABLE === */
.content{padding:0 32px 20px;overflow-x:auto}
.list-header,.stock-row{
  display:grid;
  grid-template-columns:20px 70px 84px 90px 40px 52px 52px 52px 52px 52px 60px 48px 60px 40px 44px 56px 56px;
  gap:4px;align-items:center;padding:8px 14px;min-width:960px;
}
.list-header{
  background:var(--surface);
  border-bottom:1px solid var(--accent);color:var(--muted);
  font-size:10px;text-transform:uppercase;letter-spacing:.7px;font-weight:700;
  position:sticky;top:0;z-index:10;border-radius:8px 8px 0 0;
}
/* Section separators in header */
.list-header .sep{border-left:1px solid var(--dim);padding-left:8px}

/* === ACCORDION === */
details{background:var(--surface);border-bottom:1px solid var(--border-subtle);margin:0;transition:all .2s ease}
details:first-child{border-top:1px solid var(--border-subtle)}
details[open]{background:var(--card);border-color:rgba(99,102,241,.15)}
details[open]+details{border-top:1px solid rgba(99,102,241,.15)}
summary{cursor:pointer;list-style:none;transition:background .15s}
summary::-webkit-details-marker{display:none}
summary:hover{background:rgba(255,255,255,.025)}
.arrow{color:var(--dim);font-size:9px;transition:transform .2s ease;text-align:center}
details[open] .arrow{transform:rotate(90deg);color:var(--accent)}

/* === CELLS === */
.sym{font-weight:800;color:#fff;font-size:14px;letter-spacing:-.3px}
.price{font-family:'JetBrains Mono',monospace;font-weight:600;color:#cbd5e1;text-align:right;font-size:13px}
.badge{display:inline-block;padding:3px 10px;border-radius:6px;font-weight:800;font-size:9.5px;text-transform:uppercase;text-align:center;letter-spacing:.3px;white-space:nowrap;min-width:64px}
.b-buy{background:rgba(16,185,129,.12);color:var(--buy);border:1px solid rgba(16,185,129,.25)}
.b-buy-strong{background:rgba(16,185,129,.18);color:#34d399;border:1px solid rgba(16,185,129,.35);box-shadow:0 0 12px rgba(16,185,129,.15)}
.b-sell{background:rgba(239,68,68,.12);color:var(--sell);border:1px solid rgba(239,68,68,.25)}
.b-sell-strong{background:rgba(239,68,68,.18);color:#f87171;border:1px solid rgba(239,68,68,.35);box-shadow:0 0 12px rgba(239,68,68,.15)}
.b-buy-near{background:rgba(16,185,129,.08);color:#6ee7b7;border:1px solid rgba(16,185,129,.18)}
.b-sell-near{background:rgba(239,68,68,.08);color:#fca5a5;border:1px solid rgba(239,68,68,.18)}
.b-turning-buy{background:rgba(167,243,208,.06);color:#86efac;border:1px solid rgba(134,239,172,.15)}
.b-turning-sell{background:rgba(254,202,202,.06);color:#fda4af;border:1px solid rgba(253,164,175,.15)}
.b-oversold{background:rgba(56,189,248,.06);color:#7dd3fc;border:1px solid rgba(125,211,252,.15)}
.b-overbought{background:rgba(192,132,252,.06);color:#d8b4fe;border:1px solid rgba(216,180,254,.15)}
.b-hold{background:rgba(245,158,11,.1);color:var(--hold);border:1px solid rgba(245,158,11,.22)}
.iv{font-family:'JetBrains Mono',monospace;font-weight:600;font-size:11px;text-align:right}
.v-ok{color:var(--buy)}.v-no{color:var(--sell)}.v-na{color:var(--dim)}.v-warn{color:var(--hold)}
.cond{font-weight:800;text-align:center;font-size:12px}
.cond-3{color:var(--buy)}.cond-2{color:var(--hold)}.cond-1{color:var(--sell)}.cond-0{color:var(--dim)}

/* === DETAIL BODY === */
.detail-body{padding:18px 16px;border-top:1px solid var(--border)}
.cond-line{font-size:13px;display:flex;gap:10px;margin-bottom:6px;font-family:'JetBrains Mono',monospace;align-items:center}
.cond-label{min-width:75px;font-weight:700;font-size:12px}
.bt-line{margin-top:10px;padding:10px 14px;background:var(--glass);border-radius:var(--radius);border:1px solid var(--glass-border);font-size:12px;font-family:'JetBrains Mono',monospace;color:var(--muted)}
.bt-line b{color:#a5b4fc;font-weight:700}

/* === PERIOD SELECTOR === */
.period-bar{display:flex;gap:2px;margin:10px 0;border-radius:var(--radius);overflow:hidden;border:1px solid var(--border);width:fit-content;padding:3px;background:var(--surface)}
.period-btn{padding:6px 16px;font-size:11px;font-weight:700;font-family:inherit;cursor:pointer;background:transparent;color:var(--muted);border:none;transition:all .15s;letter-spacing:.3px;border-radius:7px}
.period-btn:hover{background:rgba(255,255,255,.06);color:var(--text)}
.period-btn.active{background:var(--accent);color:#fff;box-shadow:0 2px 8px rgba(99,102,241,.3)}

/* === CHARTS === */
.candle-box{width:100%;height:340px;margin:8px 0;border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;background:#060910}
.charts-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-top:14px}
.chart-box{background:var(--bg);border:1px solid var(--border);border-radius:var(--radius);padding:12px}
.chart-box h4{font-size:10px;color:var(--muted);text-transform:uppercase;margin-bottom:8px;letter-spacing:.8px;font-weight:700}
.chart-box canvas{width:100%!important;height:150px!important}

/* === MA LEGEND (inside detail) === */
.ma-legend{display:flex;gap:14px;flex-wrap:wrap;margin:8px 0;font-size:12px;font-family:'JetBrains Mono',monospace}
.ma-legend span{display:flex;align-items:center;gap:5px}
.ma-legend .dot{width:8px;height:8px;border-radius:50%;display:inline-block}

/* === FOOTER === */
.footer{padding:12px 32px;background:var(--surface);border-top:1px solid var(--border);color:var(--dim);font-size:12px;display:flex;justify-content:space-between;font-weight:500}

/* === TOP 3 RECOMMENDATIONS — ACCORDION === */
.top3-section{padding:18px 32px;background:var(--bg);border-bottom:1px solid var(--border)}
.top3-title{font-size:13px;font-weight:800;color:#a5b4fc;text-transform:uppercase;letter-spacing:.8px;margin-bottom:12px;display:flex;align-items:center;gap:8px}
.top3-title::before{content:'';display:inline-block;width:3px;height:16px;background:var(--accent);border-radius:2px}
.top3-empty{color:var(--dim);font-size:12px;font-style:italic;padding:8px 0}
.rec-details{background:var(--card);border:1px solid var(--border);border-radius:var(--radius-lg);margin-bottom:12px;overflow:hidden;transition:all .3s ease}
.rec-details[open]{border-color:rgba(99,102,241,.25);box-shadow:0 4px 24px rgba(99,102,241,.08)}
.rec-details.rec-buy{border-left:3px solid var(--buy)}
.rec-details.rec-sell{border-left:3px solid var(--sell)}
.rec-details.rec-hold{border-left:3px solid var(--hold)}
.rec-details summary{cursor:pointer;padding:16px 20px;display:flex;align-items:center;gap:14px;list-style:none;user-select:none;transition:background .2s}
.rec-details summary:hover{background:rgba(255,255,255,.025)}
.rec-details summary::-webkit-details-marker{display:none}
.rec-details summary::marker{display:none;content:''}
.rec-arrow{font-size:11px;color:var(--muted);transition:transform .2s;flex-shrink:0;width:16px;text-align:center}
.rec-details[open] .rec-arrow{transform:rotate(90deg)}
.rec-rank-badge{display:inline-flex;align-items:center;justify-content:center;width:30px;height:30px;border-radius:8px;font-weight:900;font-size:14px;background:rgba(99,102,241,.12);color:var(--accent);flex-shrink:0}
.rec-sym{font-size:17px;font-weight:900;color:#fff;letter-spacing:-.5px}
.rec-price{font-family:'JetBrains Mono',monospace;font-size:14px;font-weight:600;color:#cbd5e1}
.rec-badge{display:inline-block;padding:3px 12px;border-radius:6px;font-weight:800;font-size:10px;text-transform:uppercase;letter-spacing:.5px}
.rb-buy{background:rgba(16,185,129,.12);color:var(--buy);border:1px solid rgba(16,185,129,.25)}
.rb-sell{background:rgba(239,68,68,.12);color:var(--sell);border:1px solid rgba(239,68,68,.25)}
.rb-hold{background:rgba(245,158,11,.1);color:var(--hold);border:1px solid rgba(245,158,11,.22)}
.rb-buy-near{background:rgba(16,185,129,.08);color:#6ee7b7;border:1px solid rgba(16,185,129,.18)}
.rb-sell-near{background:rgba(239,68,68,.08);color:#fca5a5;border:1px solid rgba(239,68,68,.18)}
.rec-sum-metrics{display:flex;gap:16px;margin-left:auto;font-size:12px;flex-shrink:0;flex-wrap:wrap}
.rec-sm{display:flex;gap:5px;align-items:center}
.rec-sm .lab{color:var(--muted);font-weight:600}
.rec-sm .val{font-family:'JetBrains Mono',monospace;font-weight:700}
.rec-body{padding:0 22px 22px}
.rec-top-row{display:grid;grid-template-columns:1fr 280px;gap:16px;margin-bottom:16px}
.rec-candle-wrap{border-radius:var(--radius);overflow:hidden;border:1px solid var(--border);background:#060910}
.rec-candle-box{width:100%;height:380px}
.rec-candle-legend{display:flex;gap:12px;padding:6px 10px;background:rgba(6,9,16,.6);font-size:10px;flex-wrap:wrap}
.rec-candle-legend span{display:flex;align-items:center;gap:4px;color:var(--muted)}
.rec-candle-legend i{display:inline-block;width:18px;height:2px;border-radius:1px}
.rec-right-panel{display:flex;flex-direction:column;gap:12px}
.rec-metrics{display:grid;grid-template-columns:1fr 1fr;gap:8px 16px}
.rec-m{font-size:13px;display:flex;justify-content:space-between}
.rec-ml{color:var(--muted);font-weight:600}
.rec-mv{font-family:'JetBrains Mono',monospace;font-weight:700}
.rec-levels{background:var(--glass);border:1px solid var(--glass-border);border-radius:var(--radius);padding:14px}
.rec-lt{font-size:10px;font-weight:700;color:#a5b4fc;text-transform:uppercase;letter-spacing:.6px;margin-bottom:8px}
.rec-lr{display:flex;justify-content:space-between;align-items:center;font-size:13px;padding:4px 0}
.rec-ll{color:var(--muted);font-weight:600}
.rec-lv{font-family:'JetBrains Mono',monospace;font-weight:700}
.lv-entry{color:#93c5fd}.lv-target{color:var(--buy)}.lv-stop{color:var(--sell)}.lv-rr{color:var(--accent)}
/* Thesis - prominent summary */
.rec-thesis{background:linear-gradient(135deg,rgba(99,102,241,.06),rgba(99,102,241,.02));border:1px solid rgba(99,102,241,.15);border-radius:var(--radius);padding:18px 20px;margin-bottom:16px;line-height:1.6}
.rec-thesis-title{font-size:11px;font-weight:800;color:#a5b4fc;text-transform:uppercase;letter-spacing:.8px;margin-bottom:10px}
.rec-thesis-text{font-size:14px;color:var(--text);font-weight:500}
.rec-thesis-meta{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap}
.rec-thesis-horizon{display:inline-block;padding:4px 12px;border-radius:6px;font-size:11px;font-weight:700;background:rgba(99,102,241,.12);color:var(--accent)}
.rec-thesis-target{display:inline-block;padding:4px 12px;border-radius:6px;font-size:11px;font-weight:700;background:rgba(16,185,129,.12);color:var(--buy)}
.rec-sell .rec-thesis-target{background:rgba(239,68,68,.12);color:var(--sell)}
/* Research row (below chart) */
.rec-research-row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;margin-bottom:16px}
.rec-research-panel{background:var(--glass);border:1px solid var(--glass-border);border-radius:var(--radius);padding:16px;min-height:80px}
/* Fundamentals in research row */
.rec-fund{background:var(--glass);border:1px solid var(--glass-border);border-radius:var(--radius);padding:16px}
.rec-fund-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:5px 18px}
.rec-fl{font-size:13px;display:flex;justify-content:space-between;padding:2px 0}
.rec-fll{color:var(--muted);font-weight:600}
.rec-flv{font-family:'JetBrains Mono',monospace;font-weight:700;color:var(--text)}
.rec-period-bar{display:flex;gap:2px;margin-bottom:12px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:3px;width:fit-content}
.rec-period-btn{padding:6px 16px;font-size:11px;font-weight:700;font-family:inherit;cursor:pointer;background:transparent;color:var(--muted);border:none;border-radius:7px;transition:all .15s;letter-spacing:.3px}
.rec-period-btn:hover{background:rgba(255,255,255,.06);color:var(--text)}
.rec-period-btn.active{background:var(--accent);color:#fff;box-shadow:0 2px 8px rgba(99,102,241,.3)}
.rec-ind-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:14px}
.rec-ind-wrap{border-radius:var(--radius);overflow:hidden;border:1px solid var(--border);background:var(--card)}
.rec-ind-title{font-size:10px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;padding:6px 10px;background:var(--glass)}
.rec-ind-canvas{width:100%;height:190px;display:block}
.rec-rat{border-top:1px solid var(--border);padding-top:12px;margin-top:6px}
.rec-rt{font-size:10px;font-weight:700;color:#a5b4fc;text-transform:uppercase;letter-spacing:.7px;margin-bottom:8px}
.rec-ri{font-size:13px;color:var(--text);padding:4px 0;line-height:1.5}
.rec-ri::before{content:'';display:inline-block;width:4px;height:4px;border-radius:50%;background:var(--accent);margin-right:8px;vertical-align:middle}
.rec-sb{height:4px;border-radius:2px;background:var(--border);margin-top:14px;overflow:hidden}
.rec-sf{height:100%;border-radius:2px;background:linear-gradient(90deg,var(--accent),#818cf8)}

/* Analyst Targets */
.rec-analyst{margin-bottom:8px}
.rec-analyst-bar{height:8px;border-radius:4px;background:var(--border);position:relative;margin:10px 0 6px}
.rec-analyst-fill{position:absolute;top:0;height:100%;border-radius:4px;background:linear-gradient(90deg,var(--sell),var(--hold),var(--buy))}
.rec-analyst-marker{position:absolute;top:-5px;width:4px;height:18px;border-radius:2px;background:#fff;box-shadow:0 0 6px #fff8}
.rec-analyst-labels{display:flex;justify-content:space-between;font-size:10px;color:var(--muted);font-family:'JetBrains Mono',monospace}
.rec-upside{font-size:16px;font-weight:900;text-align:center;margin:6px 0 2px}
/* Insider Trades */
.rec-insider-header{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.rec-sent-badge{display:inline-block;padding:3px 10px;border-radius:6px;font-weight:800;font-size:10px;text-transform:uppercase;letter-spacing:.5px}
.sent-bullish{background:#34d39928;color:var(--buy);border:1px solid #34d39950}
.sent-bearish{background:#f8717128;color:var(--sell);border:1px solid #f8717150}
.sent-neutral{background:#fbbf2422;color:var(--hold);border:1px solid #fbbf2440}
.rec-ins-summary{display:flex;gap:14px;font-size:12px;margin-bottom:6px;font-weight:600}
.rec-ins-tx{font-size:11px;color:#b8c5d6;padding:3px 0;border-top:1px solid #ffffff14;line-height:1.4}
/* Earnings */
.rec-earnings{display:flex;align-items:center;gap:12px}
.rec-earn-badge{display:inline-flex;align-items:center;justify-content:center;min-width:42px;height:42px;border-radius:10px;font-weight:900;font-size:15px;background:#818cf828;color:var(--accent)}
.rec-earn-info{font-size:12px}
.rec-earn-warn{color:var(--sell);font-weight:700}
/* Ratio divider */
.rec-ratio-divider{border:none;border-top:1px solid var(--border);margin:8px 0}
/* List header sorting */
.list-header span[data-col]{cursor:pointer;user-select:none;transition:color .15s}
.list-header span[data-col]:hover{color:var(--text)}
.sort-arrow{font-size:8px;margin-left:2px;opacity:.7}

/* === NAV TABS === */
.nav-tabs{display:flex;gap:0;padding:0 32px;background:var(--surface);border-bottom:1px solid var(--border)}
.nav-tab{padding:12px 24px;font-size:13px;font-weight:700;color:var(--muted);cursor:pointer;border:none;background:none;border-bottom:2px solid transparent;margin-bottom:-1px;transition:all .2s;letter-spacing:.3px;position:relative}
.nav-tab:hover{color:var(--text);background:rgba(255,255,255,.02)}
.nav-tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.tab-content{display:none}.tab-content.active{display:block}

/* === PORTFOLIO SECTION === */
.portfolio-section{padding:18px 32px}
.port-title{font-size:17px;font-weight:800;color:#fff;margin-bottom:16px;letter-spacing:-.3px}
.port-title em{font-style:normal;color:var(--accent)}

/* Summary cards */
.port-summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:20px}
.port-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius-lg);padding:16px 18px;transition:border-color .2s}
.port-card:hover{border-color:rgba(99,102,241,.2)}
.port-card-label{font-size:10px;text-transform:uppercase;letter-spacing:.7px;color:var(--muted);margin-bottom:6px;font-weight:600}
.port-card-value{font-size:22px;font-weight:800;letter-spacing:-.5px}
.port-card-sub{font-size:12px;color:var(--muted);margin-top:3px}

/* Alerts / Calls-to-action */
.port-alerts{margin-bottom:24px;display:flex;flex-direction:column;gap:10px}
.port-alerts:empty{display:none}
.port-alert{display:flex;align-items:center;gap:16px;padding:16px 20px;border-radius:var(--radius-lg);font-size:13px;border-left:4px solid;background:var(--card)}
.port-alert-cta{display:flex;flex-direction:column;flex:1;gap:3px}
.port-alert-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.port-alert-action-badge{font-size:11px;font-weight:800;padding:4px 12px;border-radius:6px;letter-spacing:.6px;text-transform:uppercase}
.port-alert-symbol{font-size:17px;font-weight:800;color:var(--text);letter-spacing:-.3px}
.port-alert-price{font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums}
.port-alert-reason{font-size:13px;color:var(--muted);line-height:1.6}
.port-alert-danger{background:rgba(239,68,68,.04);border-color:#ef4444}
.port-alert-danger .port-alert-action-badge{background:#ef4444;color:#fff}
.port-alert-warning{background:rgba(245,158,11,.04);border-color:#f59e0b}
.port-alert-warning .port-alert-action-badge{background:#f59e0b;color:#1a1a1a}
.port-alert-success{background:rgba(16,185,129,.04);border-color:#10b981}
.port-alert-success .port-alert-action-badge{background:#10b981;color:#0b1120}
.port-alert-info{background:rgba(99,102,241,.04);border-color:#6366f1}
.port-alert-info .port-alert-action-badge{background:#6366f1;color:#fff}
.port-alert-jump{background:transparent;border:1px solid var(--border);color:var(--text);font-size:11px;font-weight:700;padding:7px 14px;border-radius:8px;cursor:pointer;letter-spacing:.4px;text-transform:uppercase;transition:all .2s}
.port-alert-jump:hover{background:var(--accent);border-color:var(--accent);color:#fff}
.port-alerts-empty{color:var(--muted);font-size:13px;padding:14px 18px;background:var(--card);border:1px dashed var(--border);border-radius:var(--radius);text-align:center}

/* Verdict dashboard grid */
.port-verdicts{margin-bottom:28px}
.port-verdicts-title{font-size:14px;font-weight:700;color:var(--text);margin-bottom:12px;letter-spacing:-.2px}
.port-verdicts-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}
.port-verdict-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius-lg);padding:14px 16px;cursor:pointer;transition:all .2s ease;border-left:4px solid var(--border);position:relative}
.port-verdict-card:hover{transform:translateY(-2px);border-color:rgba(99,102,241,.3);box-shadow:var(--shadow-md)}
.port-verdict-card.v-sell{border-left-color:#ef4444}
.port-verdict-card.v-add{border-left-color:#10b981}
.port-verdict-card.v-hold{border-left-color:#6366f1}
.port-verdict-card.v-reduce{border-left-color:#f59e0b}
.port-verdict-head{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px}
.port-verdict-sym{font-size:17px;font-weight:800;color:var(--text);letter-spacing:-.3px}
.port-verdict-sub{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px;margin-top:3px}
.port-verdict-action{font-size:11px;font-weight:800;padding:4px 10px;border-radius:6px;letter-spacing:.5px}
.port-verdict-action.v-sell{background:rgba(239,68,68,.12);color:#fca5a5}
.port-verdict-action.v-add{background:rgba(16,185,129,.12);color:#34d399}
.port-verdict-action.v-hold{background:rgba(99,102,241,.12);color:#a5b4fc}
.port-verdict-action.v-reduce{background:rgba(245,158,11,.12);color:#fcd34d}
.port-verdict-metrics{display:flex;gap:16px;font-size:12px;color:var(--muted);flex-wrap:wrap;margin-bottom:8px}
.port-verdict-metrics b{color:var(--text);font-variant-numeric:tabular-nums;font-weight:700}
.port-verdict-trend{display:flex;align-items:center;gap:5px;font-size:11px;text-transform:uppercase;letter-spacing:.5px;font-weight:700}
.port-verdict-trend.t-up{color:#10b981}
.port-verdict-trend.t-down{color:#ef4444}
.port-verdict-trend.t-flat{color:var(--muted)}
.port-verdict-reason{font-size:12px;color:var(--muted);line-height:1.5}
.port-verdict-indi{display:flex;gap:6px;margin-top:10px}
.port-verdict-indi-chip{font-size:9px;padding:3px 8px;border-radius:5px;font-weight:700;letter-spacing:.4px}
.port-verdict-indi-chip.ok{background:rgba(16,185,129,.1);color:#34d399}
.port-verdict-indi-chip.no{background:rgba(239,68,68,.1);color:#f87171}

/* Deep per-position analysis (accordions estilo Top 3 Pick) */
.port-analysis{margin-top:12px}
.port-analysis-title{font-size:14px;font-weight:700;color:var(--text);margin-bottom:12px;letter-spacing:-.2px}
.port-analysis-list-empty{font-size:13px;color:var(--muted);padding:24px;text-align:center;background:var(--card);border:1px dashed var(--border);border-radius:var(--radius)}

/* Holdings table */
.port-holdings{margin-bottom:24px}
.port-h-title{font-size:14px;font-weight:700;color:var(--text);margin-bottom:10px}
.port-table{width:100%;border-collapse:collapse;font-size:13px}
.port-table th{text-align:left;padding:10px 12px;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.6px;border-bottom:1px solid var(--border);background:var(--surface);position:sticky;top:0;font-weight:700}
.port-table td{padding:9px 12px;border-bottom:1px solid rgba(30,39,54,.5)}
.port-table tr:hover{background:rgba(255,255,255,.02)}
.port-table .sym-col{font-weight:700;color:var(--text)}
.port-table .num-col{text-align:right;font-variant-numeric:tabular-nums}
.port-etf-badge{font-size:9px;padding:1px 5px;border-radius:4px;background:#818cf825;color:#a5b4fc;margin-left:4px;font-weight:600}
.port-signal-badge{font-size:9px;padding:1px 6px;border-radius:4px;font-weight:700}
.port-signal-buy{background:#34d39920;color:#34d399}
.port-signal-sell{background:#f8717120;color:#f87171}
.port-signal-hold{background:#fbbf2420;color:#fbbf24}

/* Expandable row (chart per asset) */
.port-row-main{cursor:pointer;transition:background .15s}
.port-row-main:hover{background:rgba(255,255,255,.04)}
.port-row-main.expanded{background:rgba(99,102,241,.08)}
.port-expand-arrow{display:inline-block;width:12px;color:var(--muted);font-size:10px;margin-right:4px;transition:transform .2s}
.port-row-main.expanded .port-expand-arrow{transform:rotate(90deg);color:var(--accent)}
.port-row-chart td{padding:0!important;border-bottom:1px solid var(--border)22;background:var(--surface)}
.port-chart-wrap{padding:14px 18px;display:flex;flex-direction:column;gap:10px}
.port-chart-top{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px}
.port-chart-periods{display:flex;gap:4px}
.port-chart-period-btn{background:var(--card);color:var(--muted);border:1px solid var(--border);padding:4px 10px;border-radius:6px;font-size:10px;font-weight:600;cursor:pointer;letter-spacing:.5px;transition:all .15s}
.port-chart-period-btn:hover{color:var(--text);border-color:var(--accent)}
.port-chart-period-btn.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.port-chart-levels{display:flex;gap:14px;font-size:11px;flex-wrap:wrap}
.port-chart-level{display:flex;align-items:center;gap:4px}
.port-chart-level-dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.port-chart-container{height:300px;background:var(--card);border-radius:var(--radius);border:1px solid var(--border)}
.port-chart-loading{color:var(--muted);font-size:12px;text-align:center;padding:40px}
.port-chart-err{color:#f87171;font-size:12px;text-align:center;padding:20px}
.port-sltp-inline{font-size:10px;color:var(--muted);display:block;margin-top:2px}
.port-sltp-sl{color:#f87171}
.port-sltp-tp{color:#34d399}

/* Composition charts */
.port-comp-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:24px}
.port-comp-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius-lg);padding:16px}
.port-comp-title{font-size:12px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:12px}
.port-bar{display:flex;height:28px;border-radius:6px;overflow:hidden;margin-bottom:8px}
.port-bar-seg{display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;color:#fff;min-width:30px;transition:width .3s}
.port-sector-row{display:flex;align-items:center;margin-bottom:5px;font-size:12px}
.port-sector-bar{height:6px;border-radius:3px;margin-right:8px;min-width:4px;transition:width .3s}
.port-sector-label{flex:1;color:var(--muted)}
.port-sector-pct{font-weight:700;font-variant-numeric:tabular-nums;min-width:45px;text-align:right}

/* History chart */
.port-history{background:var(--card);border:1px solid var(--border);border-radius:var(--radius-lg);padding:16px;margin-bottom:24px}
.port-hist-title{font-size:12px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:12px}
.port-hist-chart{height:280px}

/* Concentration */
.port-conc-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:10px}
.port-conc-item{text-align:center;padding:10px;background:var(--surface);border-radius:var(--radius)}
.port-conc-val{font-size:18px;font-weight:800;color:var(--accent)}
.port-conc-lab{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-top:3px}

/* Patterns */
.port-patterns{background:var(--card);border:1px solid var(--border);border-radius:var(--radius-lg);padding:16px;margin-bottom:24px}
.port-pat-title{font-size:12px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:12px}
.port-pat-summary{font-size:14px;color:var(--text);line-height:1.6;margin-bottom:14px}
.port-pat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.port-pat-card{background:var(--surface);border-radius:var(--radius);padding:12px 16px}
.port-pat-label{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:5px}
.port-pat-stat{font-size:15px;font-weight:700}

/* Drawdowns */
.port-dd-table{width:100%;border-collapse:collapse;font-size:11px;margin-top:8px}
.port-dd-table th{text-align:left;padding:6px 8px;color:var(--muted);font-size:9px;text-transform:uppercase;border-bottom:1px solid var(--border)}
.port-dd-table td{padding:5px 8px;border-bottom:1px solid var(--border)22}

/* === PORTFOLIO SUB-TABS === */
.port-subtabs{display:flex;gap:0;margin-bottom:20px;border-bottom:1px solid var(--border);flex-wrap:wrap}
.port-subtab{padding:10px 18px;font-size:11px;font-weight:700;color:var(--muted);cursor:pointer;border:none;background:none;border-bottom:2px solid transparent;margin-bottom:-1px;transition:all .2s;letter-spacing:.4px;text-transform:uppercase}
.port-subtab:hover{color:var(--text);background:rgba(255,255,255,.02)}
.port-subtab.active{color:var(--accent);border-bottom-color:var(--accent)}
.port-subcontent{display:none}.port-subcontent.active{display:block}

/* Benchmark */
.bench-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:20px}
.bench-card{background:var(--surface);border-radius:var(--radius);padding:12px 16px;text-align:center}
.bench-card-label{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:5px;font-weight:600}
.bench-card-value{font-size:20px;font-weight:800}
.bench-chart{height:300px;background:var(--card);border-radius:var(--radius);border:1px solid var(--border)}

/* News */
.news-list{max-height:500px;overflow-y:auto}
.news-item{padding:10px 14px;border-bottom:1px solid var(--border)22;display:flex;gap:10px;align-items:flex-start}
.news-item:hover{background:rgba(255,255,255,.02)}
.news-sym{font-size:11px;font-weight:700;color:var(--accent);min-width:50px;flex-shrink:0}
.news-title{font-size:12px;color:var(--text);flex:1}
.news-title a{color:var(--text);text-decoration:none}.news-title a:hover{color:var(--accent)}
.news-meta{font-size:10px;color:var(--muted);margin-top:2px}
.news-earn{background:#f8717115;border:1px solid #f8717130;border-radius:8px;padding:10px 14px;margin-bottom:14px;font-size:12px;color:#fca5a5}

/* Returns heatmap */
.ret-heatmap{display:grid;grid-template-columns:repeat(auto-fill,minmax(28px,1fr));gap:2px;margin-bottom:16px}
.ret-cell{width:100%;aspect-ratio:1;border-radius:3px;display:flex;align-items:center;justify-content:center;font-size:8px;font-weight:600;color:#fff;cursor:default}
.ret-month-table{width:100%;border-collapse:collapse;font-size:12px;margin-bottom:16px}
.ret-month-table th{text-align:left;padding:6px 10px;color:var(--muted);font-size:10px;text-transform:uppercase;border-bottom:1px solid var(--border)}
.ret-month-table td{padding:5px 10px;border-bottom:1px solid var(--border)22}
.ret-stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px}
.ret-stat{background:var(--surface);border-radius:var(--radius);padding:12px 16px;text-align:center}
.ret-stat-val{font-size:18px;font-weight:800}
.ret-stat-lab{font-size:10px;color:var(--muted);text-transform:uppercase;margin-top:3px;letter-spacing:.4px}

/* Correlation */
.corr-table-wrap{overflow-x:auto;margin-bottom:16px}
.corr-table{border-collapse:collapse;font-size:11px}
.corr-table th{padding:6px 8px;color:var(--muted);font-size:10px;text-transform:uppercase;background:var(--surface);position:sticky;top:0}
.corr-table td{padding:5px 8px;text-align:center;font-weight:600;min-width:50px}
.corr-score{background:var(--surface);border-radius:8px;padding:12px 16px;margin-bottom:16px;display:flex;align-items:center;gap:16px}
.corr-score-val{font-size:28px;font-weight:900}

/* Rebalancing */
.rebal-alloc{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px}
.rebal-card{background:var(--surface);border-radius:var(--radius);padding:16px}
.rebal-bar-wrap{height:8px;border-radius:4px;background:var(--border);margin:10px 0;position:relative;overflow:visible}
.rebal-bar-actual{height:100%;border-radius:4px;transition:width .3s}
.rebal-bar-target{position:absolute;top:-3px;width:2px;height:14px;background:#fff;border-radius:1px}
.rebal-sug{background:rgba(99,102,241,.06);border:1px solid rgba(99,102,241,.15);border-radius:var(--radius);padding:12px 16px;margin-bottom:10px;font-size:13px;color:#a5b4fc}

/* Trades Analysis */
.trades-stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:20px}
.trades-stat{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:14px;text-align:center;transition:border-color .2s}
.trades-stat:hover{border-color:rgba(99,102,241,.2)}
.trades-stat-val{font-size:22px;font-weight:700;margin-bottom:3px}
.trades-stat-lab{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px}
.trades-equity-chart{height:320px;margin-bottom:12px}
.trades-table{width:100%;border-collapse:collapse;font-size:11px}
.trades-table th{text-align:left;padding:6px 10px;font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--border)}
.trades-table td{padding:6px 10px;border-bottom:1px solid var(--border)22}
.trades-table tr:hover{background:rgba(255,255,255,.02)}
.trades-win{color:#34d399}
.trades-loss{color:#f87171}
.trades-neutral{color:var(--muted)}
.trades-badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700}
.trades-badge-win{background:#34d39920;color:#34d399}
.trades-badge-loss{background:#f8717120;color:#f87171}
.trades-badge-neutral{background:#94a3b820;color:#94a3b8}
.trades-pnl-bar{height:6px;border-radius:3px;margin-top:2px}
.trades-note{margin-top:4px}
.trades-note textarea{width:100%;background:var(--surface);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:6px 10px;font-size:11px;resize:vertical;min-height:28px;font-family:inherit}
.trades-note textarea:focus{outline:none;border-color:var(--accent)}

/* Journal */
.journal-list{max-height:500px;overflow-y:auto}
.journal-item{padding:10px 14px;border-bottom:1px solid var(--border)22;font-size:12px}
.journal-item:hover{background:rgba(255,255,255,.02)}
.journal-header{display:flex;align-items:center;gap:10px;margin-bottom:4px}
.journal-sym{font-weight:700;color:var(--text);min-width:50px}
.journal-side-buy{color:#34d399;font-weight:700;font-size:10px}
.journal-side-sell{color:#f87171;font-weight:700;font-size:10px}
.journal-note{margin-top:6px}
.journal-note textarea{width:100%;background:var(--surface);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:6px 10px;font-size:11px;resize:vertical;min-height:32px;font-family:inherit}
.journal-note textarea:focus{outline:none;border-color:var(--accent)}
.journal-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:16px}

/* VaR */
.var-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px}
.var-card{background:var(--surface);border-radius:var(--radius-lg);padding:16px;text-align:center}
.var-value{font-size:24px;font-weight:900}
.var-label{font-size:10px;color:var(--muted);text-transform:uppercase;margin-top:5px;letter-spacing:.5px}
.var-desc{font-size:12px;color:var(--muted);margin-top:8px}
.stress-table{width:100%;border-collapse:collapse;font-size:12px}
.stress-table th{text-align:left;padding:8px 10px;color:var(--muted);font-size:10px;text-transform:uppercase;border-bottom:1px solid var(--border)}
.stress-table td{padding:7px 10px;border-bottom:1px solid var(--border)22}

/* Telegram */
.tg-config{background:var(--surface);border-radius:var(--radius-lg);padding:18px;margin-bottom:20px}
.tg-input{width:100%;background:var(--card);border:1px solid var(--border);border-radius:8px;color:var(--text);padding:10px 14px;font-size:13px;margin-bottom:10px;font-family:inherit;transition:border-color .2s}
.tg-input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(99,102,241,.1)}
.tg-btn{padding:10px 20px;border:none;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;transition:all .2s}
.tg-btn-primary{background:var(--accent);color:#fff}.tg-btn-primary:hover{opacity:.9;box-shadow:0 2px 8px rgba(99,102,241,.3)}
.tg-btn-secondary{background:var(--border);color:var(--text)}.tg-btn-secondary:hover{background:var(--muted)}
.tg-status{font-size:13px;margin-top:10px;padding:10px 14px;border-radius:8px}

/* === RESPONSIVE === */
@media(max-width:1100px){.charts-grid{grid-template-columns:1fr}.content{padding:0 12px 12px}.rec-top-row{grid-template-columns:1fr}.rec-ind-grid{grid-template-columns:1fr}.rec-research-row{grid-template-columns:1fr}.rec-fund-grid{grid-template-columns:1fr 1fr}.port-comp-grid{grid-template-columns:1fr}.port-conc-grid{grid-template-columns:repeat(2,1fr)}.var-grid{grid-template-columns:1fr}.rebal-alloc{grid-template-columns:1fr}}
/* === TECHNICAL OBSERVATIONS (inline in table) === */
.obs-row{display:flex;gap:8px;padding:3px 14px 5px 32px;flex-wrap:wrap;align-items:center}
.obs-tag{display:inline-flex;align-items:center;gap:4px;padding:3px 10px;border-radius:5px;font-size:9px;font-weight:700;letter-spacing:.4px;border:1px solid;flex-shrink:0}
.obs-text{font-size:11px;color:var(--muted);line-height:1.4}
.obs-spark{width:60px;height:18px;vertical-align:middle;border-radius:3px}

@media(max-width:700px){.header{flex-direction:column;gap:4px}.header .sub{text-align:left}.top3-section{padding:12px}.rec-sum-metrics{display:none}.portfolio-section{padding:12px}.port-summary{grid-template-columns:1fr 1fr}.port-pat-grid{grid-template-columns:1fr}.nav-tabs{padding:0 12px}.nav-tab{padding:8px 14px;font-size:12px}}

/* === OPTIONS LAB === */
.olab-summary{padding:18px 22px;background:linear-gradient(135deg,rgba(99,102,241,.06),rgba(139,92,246,.06));border:1px solid rgba(99,102,241,.15);border-radius:var(--radius-lg);margin:14px 22px;display:flex;gap:18px;align-items:center;flex-wrap:wrap}
.olab-summary-icon{font-size:28px;width:52px;height:52px;display:flex;align-items:center;justify-content:center;border-radius:var(--radius);background:rgba(99,102,241,.1)}
.olab-summary-text{flex:1;min-width:200px}
.olab-summary-text h3{margin:0 0 6px;font-size:16px;color:var(--text)}
.olab-summary-text p{margin:0;font-size:13px;color:var(--muted);line-height:1.6}
.olab-section{margin:14px 22px}
.olab-section-title{font-size:15px;font-weight:800;color:var(--text);margin-bottom:14px;letter-spacing:.3px;display:flex;align-items:center;gap:10px}
.olab-section-title .olab-badge{font-size:10px;padding:3px 10px;border-radius:5px;font-weight:700}

/* IV Cards */
.olab-iv-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.olab-iv-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px;transition:border-color .2s}
.olab-iv-card:hover{border-color:rgba(99,102,241,.2)}
.olab-iv-card .label{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:5px;font-weight:600}
.olab-iv-card .value{font-size:22px;font-weight:800;color:var(--text);font-variant-numeric:tabular-nums}
.olab-iv-card .sub{font-size:11px;color:var(--muted);margin-top:3px}

/* IV Opportunity alert */
.olab-iv-alert{background:rgba(245,158,11,.08);border:1px solid rgba(245,158,11,.25);border-radius:8px;padding:12px 16px;margin-bottom:10px;font-size:12px;color:var(--text);line-height:1.5}
.olab-iv-alert strong{color:#f59e0b}

/* Strategy cards */
.olab-strat-list{display:flex;flex-direction:column;gap:12px}
.olab-strat-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);overflow:hidden;transition:all .2s ease}
.olab-strat-card:hover{border-color:rgba(99,102,241,.25)}
.olab-strat-header{padding:16px 18px;display:flex;align-items:center;gap:14px;cursor:pointer}
.olab-strat-rank{width:34px;height:34px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-weight:900;font-size:15px;flex-shrink:0}
.olab-strat-rank.gold{background:linear-gradient(135deg,#f59e0b,#d97706);color:#000}
.olab-strat-rank.silver{background:linear-gradient(135deg,#94a3b8,#64748b);color:#fff}
.olab-strat-rank.bronze{background:linear-gradient(135deg,#d97706,#92400e);color:#fff}
.olab-strat-rank.normal{background:var(--border);color:var(--text)}
.olab-strat-info{flex:1;min-width:0}
.olab-strat-name{font-size:15px;font-weight:800;color:var(--text)}
.olab-strat-name-es{font-size:12px;color:var(--muted);margin-top:2px}
.olab-strat-desc{font-size:12px;color:var(--muted);margin-top:4px;line-height:1.5}
.olab-strat-metrics{display:flex;gap:18px;flex-wrap:wrap;align-items:center}
.olab-strat-metric{text-align:center}
.olab-strat-metric .val{font-size:17px;font-weight:800;font-variant-numeric:tabular-nums}
.olab-strat-metric .lbl{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px}
.olab-strat-score{width:50px;height:50px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:900;font-size:16px;flex-shrink:0}
.olab-strat-expand{font-size:18px;color:var(--muted);flex-shrink:0;transition:transform .2s}
.olab-strat-card.open .olab-strat-expand{transform:rotate(180deg)}
.olab-strat-body{display:none;padding:0 18px 18px;border-top:1px solid var(--border)}
.olab-strat-card.open .olab-strat-body{display:block}

/* Strategy detail grid */
.olab-detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:14px}
@media(max-width:900px){.olab-detail-grid{grid-template-columns:1fr}}
.olab-payoff-chart{background:var(--bg);border-radius:8px;border:1px solid var(--border);padding:12px;min-height:200px}
.olab-payoff-canvas{width:100%;height:200px}
.olab-greeks-table{width:100%;border-collapse:collapse;font-size:12px}
.olab-greeks-table th{text-align:left;padding:6px 10px;color:var(--muted);font-size:10px;text-transform:uppercase;border-bottom:1px solid var(--border)}
.olab-greeks-table td{padding:6px 10px;border-bottom:1px solid var(--border)22;font-variant-numeric:tabular-nums}
.olab-legs-table{width:100%;border-collapse:collapse;font-size:11px;margin-top:10px}
.olab-legs-table th{text-align:left;padding:5px 8px;color:var(--muted);font-size:9px;text-transform:uppercase;border-bottom:1px solid var(--border)}
.olab-legs-table td{padding:5px 8px;border-bottom:1px solid var(--border)22}

/* Backtest section */
.olab-bt-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.olab-bt-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:12px 14px;text-align:center;transition:border-color .2s}
.olab-bt-card:hover{border-color:rgba(99,102,241,.2)}
.olab-bt-card .days{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:7px;font-weight:600}
.olab-bt-card .ret{font-size:20px;font-weight:800;font-variant-numeric:tabular-nums}
.olab-bt-card .wr{font-size:12px;margin-top:3px}
.olab-bt-card .range{font-size:11px;color:var(--muted);margin-top:5px}
.olab-bt-hist{margin-top:10px;height:44px;display:flex;align-items:flex-end;gap:1px}
.olab-bt-bar{flex:1;border-radius:1px 1px 0 0;min-width:2px}
.olab-bt-context{font-size:13px;color:var(--muted);line-height:1.6;padding:12px 0}

/* Bias tags */
.olab-bias{display:inline-block;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700;letter-spacing:.3px}
.olab-bias.bullish{background:rgba(16,185,129,.15);color:#10b981;border:1px solid rgba(16,185,129,.3)}
.olab-bias.bearish{background:rgba(239,68,68,.15);color:#ef4444;border:1px solid rgba(239,68,68,.3)}
.olab-bias.neutral{background:rgba(99,102,241,.15);color:#818cf8;border:1px solid rgba(99,102,241,.3)}

/* Multi-symbol cards */
.olab-multi-card{background:var(--surface);border:1px solid var(--border);border-radius:10px;margin-bottom:16px;overflow:hidden}
.olab-multi-header{padding:14px 16px;display:flex;align-items:center;gap:12px;cursor:pointer;border-bottom:1px solid var(--border)}
.olab-multi-header h3{margin:0;font-size:15px;font-weight:800;color:var(--text)}
.olab-multi-header .signal-tag{margin-left:auto}
.olab-multi-body{padding:16px;display:none}
.olab-multi-card.open .olab-multi-body{display:block}

/* === TRADES HISTORY === */
.th-section{padding:18px 32px}
.th-title{font-size:17px;font-weight:800;color:#fff;margin-bottom:16px;letter-spacing:-.3px}
.th-title em{font-style:normal;color:var(--accent)}
.th-summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:20px}
.th-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius-lg);padding:14px 16px}
.th-card .label{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:4px;font-weight:600}
.th-card .value{font-size:22px;font-weight:800;color:var(--text);font-variant-numeric:tabular-nums}
.th-card .sub{font-size:11px;color:var(--muted);margin-top:3px}
.th-filters{display:flex;gap:8px;margin-bottom:18px;flex-wrap:wrap}
.th-filter-btn{padding:7px 16px;font-size:11px;font-weight:700;color:var(--muted);cursor:pointer;border:1px solid var(--border);background:var(--surface);border-radius:6px;transition:all .2s;letter-spacing:.3px;text-transform:uppercase}
.th-filter-btn:hover{color:var(--text);border-color:var(--accent)44}
.th-filter-btn.active{color:var(--accent);border-color:var(--accent);background:rgba(99,102,241,.08)}
.th-list{display:flex;flex-direction:column;gap:10px}
.th-trade{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);overflow:hidden;transition:all .2s}
.th-trade:hover{border-color:rgba(99,102,241,.2)}
.th-trade-header{padding:14px 18px;display:flex;align-items:center;gap:14px;cursor:pointer}
.th-trade-sym{font-size:16px;font-weight:900;color:var(--text);min-width:60px}
.th-trade-badge{padding:3px 10px;border-radius:5px;font-size:10px;font-weight:700;letter-spacing:.3px}
.th-trade-badge.win{background:rgba(16,185,129,.15);color:#10b981;border:1px solid rgba(16,185,129,.3)}
.th-trade-badge.loss{background:rgba(239,68,68,.15);color:#ef4444;border:1px solid rgba(239,68,68,.3)}
.th-trade-badge.stk{background:rgba(99,102,241,.12);color:var(--accent);border:1px solid rgba(99,102,241,.25)}
.th-trade-badge.opt{background:rgba(251,191,36,.12);color:#fbbf24;border:1px solid rgba(251,191,36,.25)}
.th-trade-badge.spread{background:rgba(192,132,252,.12);color:#c084fc;border:1px solid rgba(192,132,252,.25)}
.th-trade-badge.estimated{background:rgba(251,191,36,.12);color:#fbbf24;border:1px solid rgba(251,191,36,.25);font-size:9px}
.th-trade-dates{font-size:12px;color:var(--muted);flex:1;min-width:0}
.th-trade-metrics{display:flex;gap:18px;align-items:center}
.th-trade-metric{text-align:right}
.th-trade-metric .val{font-size:15px;font-weight:800;font-variant-numeric:tabular-nums}
.th-trade-metric .lbl{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px}
.th-trade-expand{font-size:18px;color:var(--muted);flex-shrink:0;transition:transform .2s}
.th-trade.open .th-trade-expand{transform:rotate(180deg)}
.th-trade-body{display:none;padding:0 18px 18px;border-top:1px solid var(--border)}
.th-trade.open .th-trade-body{display:block}
.th-detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:14px}
@media(max-width:900px){.th-detail-grid{grid-template-columns:1fr}}
.th-detail-section{margin-bottom:14px}
.th-detail-title{font-size:12px;font-weight:800;color:var(--accent);text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px;display:flex;align-items:center;gap:6px}
.th-detail-text{font-size:13px;color:#b8c5d6;line-height:1.7}
.th-fills-table{width:100%;border-collapse:collapse;font-size:11px;margin-top:6px}
.th-fills-table th{text-align:left;padding:5px 8px;color:var(--muted);font-size:9px;text-transform:uppercase;border-bottom:1px solid var(--border)}
.th-fills-table td{padding:5px 8px;border-bottom:1px solid var(--border)22;font-variant-numeric:tabular-nums}
.th-chart-container{background:var(--bg);border-radius:8px;border:1px solid var(--border);min-height:300px;margin-bottom:12px}
.th-chart-row{display:grid;grid-template-columns:1fr;gap:8px}
.th-ind-charts{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
.th-ind-chart{background:var(--bg);border-radius:6px;border:1px solid var(--border);padding:6px;min-height:140px}
.th-ind-chart canvas{width:100%!important;height:130px!important}
.th-context-box{background:rgba(99,102,241,.04);border:1px solid rgba(99,102,241,.12);border-radius:8px;padding:12px 16px;margin-top:8px}
.th-context-box .th-ctx-label{font-size:10px;color:var(--accent);text-transform:uppercase;letter-spacing:.5px;font-weight:700;margin-bottom:4px}
.th-context-box .th-ctx-text{font-size:12px;color:var(--muted);line-height:1.6}
.th-lessons-box{background:rgba(251,191,36,.04);border:1px solid rgba(251,191,36,.12);border-radius:8px;padding:12px 16px;margin-top:8px}
.th-lessons-box .th-les-label{font-size:10px;color:#fbbf24;text-transform:uppercase;letter-spacing:.5px;font-weight:700;margin-bottom:4px}
.th-lessons-box .th-les-text{font-size:12px;color:var(--muted);line-height:1.6}
.th-trade-pnl-bar{height:4px;border-radius:2px;margin-top:6px;background:var(--border)}
.th-trade-pnl-fill{height:100%;border-radius:2px}
@media(max-width:700px){.th-section{padding:12px}.th-summary{grid-template-columns:1fr 1fr}.th-trade-metrics{gap:10px}.th-ind-charts{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="header">
  <h1><em>VISTA</em> ANALISIS &mdash; TOP 100 VOLUMEN</h1>
  <div class="sub">MACD + RSI + KONCORDE &nbsp;&bull;&nbsp; <span id="port-info"></span></div>
</div>
<div class="counters" id="counters"></div>
<div class="nav-tabs">
  <button class="nav-tab active" onclick="switchTab('scanner')">Scanner</button>
  <button class="nav-tab" onclick="switchTab('portfolio')">Mi Cartera</button>
  <button class="nav-tab" onclick="switchTab('optionslab')">Options Lab</button>
  <button class="nav-tab" onclick="switchTab('trades')">Trades Historicos</button>
</div>

<!-- TAB: SCANNER -->
<div id="tab-scanner" class="tab-content active">
<div id="top3-section" class="top3-section" style="display:none"></div>
<div class="content">
  <div class="list-header" id="list-header">
    <span></span><span data-col="sym" onclick="sortListBy('sym')">Ticker</span><span data-col="price" style="text-align:right" onclick="sortListBy('price')">Precio</span>
    <span data-col="signal" onclick="sortListBy('signal')">Senal</span><span data-col="strength" style="text-align:right" title="Fuerza de la senal (0-5.1)" onclick="sortListBy('strength')">Str</span>
    <span class="sep" data-col="sma200" style="text-align:right" onclick="sortListBy('sma200')">200</span><span data-col="sma100" style="text-align:right" onclick="sortListBy('sma100')">100</span>
    <span data-col="sma50" style="text-align:right" onclick="sortListBy('sma50')">50</span><span data-col="sma20" style="text-align:right" onclick="sortListBy('sma20')">20</span>
    <span data-col="ema9" style="text-align:right" onclick="sortListBy('ema9')">9e</span>
    <span class="sep" data-col="macd" style="text-align:right" onclick="sortListBy('macd')">MACD</span><span data-col="rsi" style="text-align:right" onclick="sortListBy('rsi')">RSI</span>
    <span data-col="konc" style="text-align:right" onclick="sortListBy('konc')">Konc</span><span data-col="cond" onclick="sortListBy('cond')">C</span>
    <span class="sep" data-col="conf" style="text-align:right" onclick="sortListBy('conf')">Conf</span>
    <span data-col="buy_ret" style="text-align:right" title="Retorno prom. senales de compra (backtest 5Y)" onclick="sortListBy('buy_ret')">Ret.C</span><span data-col="sell_ret" style="text-align:right" title="Retorno prom. senales de venta (backtest 5Y)" onclick="sortListBy('sell_ret')">Ret.V</span>
  </div>
  <div id="stock-list"></div>
</div>
</div>

<!-- TAB: MI CARTERA -->
<div id="tab-portfolio" class="tab-content">
<div class="portfolio-section" id="portfolio-section">
  <div class="port-title"><em>MI CARTERA</em> &mdash; Posiciones & Analisis</div>
  <div id="port-loading" style="color:var(--muted);text-align:center;padding:40px">Cargando cartera...</div>
  <div id="port-content" style="display:none">
    <!-- Summary cards -->
    <div class="port-summary" id="port-summary"></div>
    <!-- Alerts -->
    <div class="port-alerts" id="port-alerts"></div>

    <!-- Tablero de veredictos por posicion -->
    <div class="port-verdicts">
      <div class="port-verdicts-title">Que hacer con cada posicion</div>
      <div class="port-verdicts-grid" id="port-verdicts"></div>
    </div>

    <!-- Analisis profundo por posicion (accordions estilo Top 3 Pick) -->
    <div class="port-analysis">
      <div class="port-analysis-title">Analisis detallado por posicion</div>
      <div id="port-analysis-list"></div>
    </div>

  </div>
</div>
</div>

<!-- TAB: OPTIONS LAB -->
<div id="tab-optionslab" class="tab-content">
<div class="portfolio-section" id="optionslab-section">
  <div class="port-title"><em>OPTIONS LAB</em> &mdash; Estrategias de Opciones</div>
  <div id="olab-loading" style="color:var(--muted);text-align:center;padding:40px">Analizando mejores oportunidades de opciones...</div>

  <!-- Controles (secundario, para buscar un simbolo especifico) -->
  <div id="olab-controls" style="padding:8px 20px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
    <input type="text" id="olab-symbol-input" placeholder="Analizar otro ticker..." style="background:var(--surface);border:1px solid var(--border);color:var(--text);padding:6px 12px;border-radius:6px;font-size:12px;width:160px;font-family:inherit" onkeydown="if(event.key==='Enter')loadOptionsLab()">
    <button onclick="loadOptionsLab()" style="background:var(--accent);color:#000;border:none;padding:6px 14px;border-radius:6px;font-weight:700;font-size:12px;cursor:pointer">Analizar</button>
    <button onclick="loadOptionsLabTop()" style="background:var(--surface);color:var(--text);border:1px solid var(--border);padding:6px 14px;border-radius:6px;font-weight:700;font-size:12px;cursor:pointer">&#x21BB; Recargar Top</button>
    <span id="olab-status" style="color:var(--muted);font-size:11px"></span>
  </div>

  <div id="olab-content" style="display:none">
    <!-- Summary banner -->
    <div id="olab-summary" class="olab-summary"></div>

    <!-- IV Analysis -->
    <div id="olab-iv-section" class="olab-section"></div>

    <!-- IV Opportunities (misalignments) -->
    <div id="olab-iv-opps" class="olab-section"></div>

    <!-- Backtest historico -->
    <div id="olab-backtest" class="olab-section"></div>

    <!-- Top 10 strategies -->
    <div id="olab-strategies" class="olab-section"></div>
  </div>

  <!-- Multi-symbol view -->
  <div id="olab-multi" style="display:none"></div>
</div>
</div>

<!-- TAB: TRADES HISTORICOS -->
<div id="tab-trades" class="tab-content">
<div class="th-section" id="trades-section">
  <div class="th-title"><em>TRADES HISTORICOS</em> &mdash; Analisis de Operaciones Cerradas</div>
  <div id="th-loading" style="color:var(--muted);text-align:center;padding:40px">Cargando trades historicos...</div>
  <div id="th-content" style="display:none">
    <div class="th-summary" id="th-summary"></div>
    <div class="th-filters" id="th-filters">
      <button class="th-filter-btn active" onclick="filterTrades('all')">Todos</button>
      <button class="th-filter-btn" onclick="filterTrades('stk')">Acciones</button>
      <button class="th-filter-btn" onclick="filterTrades('opt')">Opciones</button>
      <button class="th-filter-btn" onclick="filterTrades('win')">Ganadores</button>
      <button class="th-filter-btn" onclick="filterTrades('loss')">Perdedores</button>
    </div>
    <div class="th-list" id="th-list"></div>
  </div>
</div>
</div>

<div class="footer">
  <span>Actualizado: <span id="last-update">--</span> &bull; Proximo: <span id="next-update">--</span></span>
  <span id="footer-port"></span>
</div>
<script>
const REFRESH_MS=300000;
const DAILY_BARS={'ALL':9999,'5Y':9999,'1Y':252,'3M':63,'1M':22,'1W':5,'1D':1};
let _data=null,_charts={},_periods={},_intradayCache={};
let _activeTab='scanner';
let _portData=null;
let _portHistChart=null;
let _portLoaded=false;
let _thData=null;
let _thLoaded=false;
let _thFilter='all';
let _thCharts={};
let _olabLoaded=false;

function switchTab(tab){
  _activeTab=tab;
  document.querySelectorAll('.nav-tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));
  document.querySelector('.nav-tab[onclick*="'+tab+'"]').classList.add('active');
  document.getElementById('tab-'+tab).classList.add('active');
  if(tab==='portfolio'&&!_portLoaded){_portLoaded=true;loadPortfolio();}
  if(tab==='trades'&&!_thLoaded){_thLoaded=true;loadTradesHistory();}
  if(tab==='optionslab'&&!_olabLoaded){_olabLoaded=true;loadOptionsLabTop();}
}

function loadPortfolio(){
  document.getElementById('port-loading').style.display='';
  document.getElementById('port-content').style.display='none';
  fetch('/api/portfolio').then(r=>r.json()).then(data=>{
    _portData=data;
    if(data.error){
      document.getElementById('port-loading').textContent='Error: '+data.error;
      return;
    }
    document.getElementById('port-loading').style.display='none';
    document.getElementById('port-content').style.display='';
    renderPortfolio(data);
  }).catch(err=>{
    document.getElementById('port-loading').textContent='Error cargando cartera: '+err.message;
  });
}

// Map de accion a class CSS del verdict card
function _portVerdictClass(v){
  if(v==='SELL')return 'v-sell';
  if(v==='ADD'||v==='BUY')return 'v-add';
  if(v==='REDUCE')return 'v-reduce';
  return 'v-hold';
}
function _portVerdictLabel(v){
  if(v==='SELL')return 'VENDER';
  if(v==='ADD')return 'SUMAR';
  if(v==='BUY')return 'COMPRAR';
  if(v==='REDUCE')return 'REDUCIR';
  return 'HOLD';
}

function renderPortfolio(d){
  // Summary cards
  let pnlCol=d.total_pnl>=0?'var(--buy)':'var(--sell)';
  let pnlSign=d.total_pnl>=0?'+':'';
  let sumHtml='';
  sumHtml+='<div class="port-card"><div class="port-card-label">Valor Total</div><div class="port-card-value" style="color:var(--accent)">$'+fmtN(d.total_value)+'</div></div>';
  sumHtml+='<div class="port-card"><div class="port-card-label">Costo Total</div><div class="port-card-value" style="color:var(--muted)">$'+fmtN(d.total_cost)+'</div></div>';
  sumHtml+='<div class="port-card"><div class="port-card-label">P&L No Realizado</div><div class="port-card-value" style="color:'+pnlCol+'">'+pnlSign+'$'+fmtN(Math.abs(d.total_pnl))+'</div><div class="port-card-sub" style="color:'+pnlCol+'">'+pnlSign+d.total_pnl_pct.toFixed(2)+'%</div></div>';
  sumHtml+='<div class="port-card"><div class="port-card-label">Posiciones</div><div class="port-card-value" style="color:var(--text)">'+d.num_positions+'</div></div>';
  let acct=d.account||{};
  if(acct.NetLiquidation){
    sumHtml+='<div class="port-card"><div class="port-card-label">Liquidacion Neta</div><div class="port-card-value" style="color:var(--text)">$'+fmtN(acct.NetLiquidation.value)+'</div></div>';
  }
  if(acct.TotalCashValue){
    sumHtml+='<div class="port-card"><div class="port-card-label">Efectivo</div><div class="port-card-value" style="color:var(--text)">$'+fmtN(acct.TotalCashValue.value)+'</div></div>';
  }
  if(acct.BuyingPower){
    sumHtml+='<div class="port-card"><div class="port-card-label">Poder de Compra</div><div class="port-card-value" style="color:var(--text)">$'+fmtN(acct.BuyingPower.value)+'</div></div>';
  }
  document.getElementById('port-summary').innerHTML=sumHtml;

  // Prominent CTA banner (accionable) — solo BUY/SELL/REDUCE/WATCH
  let alertsHtml='';
  if(d.alerts&&d.alerts.length>0){
    for(let a of d.alerts){
      let jump='';
      if(a.symbol){
        jump='<button class="port-alert-jump" onclick="scrollToPortPosition(\''+a.symbol+'\')">Ver analisis</button>';
      }
      let priceHtml=(a.price!=null&&a.price>0)?'<span class="port-alert-price">$'+a.price.toFixed(2)+'</span>':'';
      let action=a.action||a.level||'';
      let head=a.headline||'';
      let reason=a.reason||a.message||'';
      alertsHtml+='<div class="port-alert port-alert-'+a.level+'">';
      alertsHtml+='  <div class="port-alert-cta">';
      alertsHtml+='    <div class="port-alert-head">';
      if(action)alertsHtml+='      <span class="port-alert-action-badge">'+action+'</span>';
      if(head)alertsHtml+='      <span class="port-alert-symbol">'+head+'</span>';
      alertsHtml+='      '+priceHtml;
      alertsHtml+='    </div>';
      alertsHtml+='    <div class="port-alert-reason">'+reason+'</div>';
      alertsHtml+='  </div>';
      alertsHtml+='  '+jump;
      alertsHtml+='</div>';
    }
  }else{
    alertsHtml='<div class="port-alerts-empty">No hay acciones pendientes. Todas las posiciones dentro de parametros.</div>';
  }
  document.getElementById('port-alerts').innerHTML=alertsHtml;

  // Tablero de veredictos por posicion
  let vHtml='';
  let positions=d.positions||[];
  if(positions.length===0){
    vHtml='<div class="port-analysis-list-empty">Sin posiciones abiertas.</div>';
  }else{
    for(let p of positions){
      let deep=p.analysis||{};
      let verdict=deep.verdict||'HOLD';
      let cls=_portVerdictClass(verdict);
      let label=_portVerdictLabel(verdict);
      let head=deep.headline||label;
      let reason=deep.verdict_reason||'';
      let trend=deep.trend||'flat';
      let trendIcon=trend==='up'?'&#9650;':(trend==='down'?'&#9660;':'&#8226;');
      let trendLabel=trend==='up'?'Momentum alcista':(trend==='down'?'Momentum bajista':'Sin momentum claro');
      let strength=(deep.strength||0);
      let conds=(deep.conditions_met||0);
      let pnlPct=(p.pnl_pct||0);
      let pnlCol2=pnlPct>=0?'#34d399':'#f87171';
      let indi='';
      indi+='<span class="port-verdict-indi-chip '+(deep.macd_ok?'ok':'no')+'">MACD</span>';
      indi+='<span class="port-verdict-indi-chip '+(deep.rsi_ok?'ok':'no')+'">RSI</span>';
      indi+='<span class="port-verdict-indi-chip '+(deep.konc_ok?'ok':'no')+'">KONC</span>';
      vHtml+='<div class="port-verdict-card '+cls+'" data-sym="'+p.symbol+'" onclick="scrollToPortPosition(\''+p.symbol+'\')">';
      vHtml+='  <div class="port-verdict-head">';
      vHtml+='    <div><div class="port-verdict-sym">'+p.symbol+'</div><div class="port-verdict-sub">'+p.sector+'</div></div>';
      vHtml+='    <span class="port-verdict-action '+cls+'">'+label+'</span>';
      vHtml+='  </div>';
      vHtml+='  <div class="port-verdict-metrics">';
      vHtml+='    <span>P&L <b style="color:'+pnlCol2+'">'+(pnlPct>=0?'+':'')+pnlPct.toFixed(1)+'%</b></span>';
      vHtml+='    <span>Fuerza <b>'+strength.toFixed(1)+'</b></span>';
      vHtml+='    <span>Indi <b>'+conds+'/3</b></span>';
      vHtml+='  </div>';
      vHtml+='  <div class="port-verdict-trend t-'+trend+'">'+trendIcon+' '+trendLabel+'</div>';
      vHtml+='  <div class="port-verdict-reason">'+reason+'</div>';
      vHtml+='  <div class="port-verdict-indi">'+indi+'</div>';
      vHtml+='</div>';
    }
  }
  document.getElementById('port-verdicts').innerHTML=vHtml;

  // Analisis profundo por posicion (accordions estilo Top 3 Pick)
  renderPortAnalysisList(positions);
}

function scrollToPortPosition(sym){
  let el=document.getElementById('port-anal-'+sym);
  if(!el)return;
  if(!el.open)el.open=true;
  el.scrollIntoView({behavior:'smooth',block:'start'});
}

// ===== Per-position deep analysis (Top3-style accordions) =====
let _portAnalCharts={};
let _portAnalPeriods={};

function _portAnalPriceLines(cs,rec,avgCost){
  try{
    if(avgCost!=null&&avgCost>0){
      cs.createPriceLine({price:avgCost,color:'#93c5fd',lineWidth:2,lineStyle:LightweightCharts.LineStyle.Dashed,axisLabelVisible:true,title:'Costo Prom.'});
    }
    if(rec.target){
      cs.createPriceLine({price:rec.target,color:'#34d399',lineWidth:2,lineStyle:LightweightCharts.LineStyle.Solid,axisLabelVisible:true,title:'Target'});
    }
    if(rec.stop_loss){
      cs.createPriceLine({price:rec.stop_loss,color:'#f87171',lineWidth:2,lineStyle:LightweightCharts.LineStyle.Solid,axisLabelVisible:true,title:'Stop'});
    }
  }catch(e){}
}

function _portAnalDestroy(sym){
  let e=_portAnalCharts[sym];
  if(!e)return;
  try{if(e.lw)e.lw.remove();}catch(err){}
  try{if(e.macd)e.macd.destroy();}catch(err){}
  try{if(e.rsi)e.rsi.destroy();}catch(err){}
  try{if(e.konc)e.konc.destroy();}catch(err){}
  delete _portAnalCharts[sym];
}

function renderPortAnalCharts(sym,rec,pos,period){
  _portAnalDestroy(sym);
  if(!rec)return;
  if(!period)period=_portAnalPeriods[sym]||'1Y';
  _portAnalPeriods[sym]=period;
  let bar=document.getElementById('portanal_pb_'+sym);
  if(bar)bar.querySelectorAll('.rec-period-btn').forEach(b=>b.classList.toggle('active',b.dataset.p===period));

  let e={lw:null,macd:null,rsi:null,konc:null};

  // Prefer scanner cache if available (full 5Y), else use rec pre-sliced data
  let fullData=(_data&&_data.results)?_data.results[sym]:null;
  let ch=fullData?fullData.chart:null;
  let indBars=DAILY_BARS[period]||252;

  let candleEl=document.getElementById('portanal_candle_'+sym);
  if(candleEl){
    candleEl.innerHTML='';
    if(period==='1M'||period==='1D'){
      let apiP=period==='1M'?'4h':'15m';
      let cacheKey=sym+'_'+apiP;
      if(_recIntradayCache[cacheKey]){
        let chart=_createLW(candleEl,true);chart.applyOptions({height:340});
        let cs=chart.addCandlestickSeries({upColor:'#34d399',downColor:'#f87171',borderUpColor:'#34d399',borderDownColor:'#f87171',wickUpColor:'#34d39988',wickDownColor:'#f8717188'});
        cs.setData(_recIntradayCache[cacheKey]);
        _portAnalPriceLines(cs,rec,pos.costo_promedio);
        chart.timeScale().fitContent();e.lw=chart;
      }else{
        candleEl.innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted);font-size:12px">Cargando '+apiP+'...</div>';
        fetch('/api/bars/'+sym+'/'+apiP).then(r=>r.json()).then(d=>{
          _recIntradayCache[cacheKey]=d.ohlc||[];
          if(_portAnalPeriods[sym]===period)renderPortAnalCharts(sym,rec,pos,period);
        }).catch(err=>console.error('port intraday err:',err));
      }
    }else if(ch&&ch.ohlc&&ch.ohlc.length>=5){
      let chart=_createLW(candleEl,false);chart.applyOptions({height:340});
      let cs=chart.addCandlestickSeries({upColor:'#34d399',downColor:'#f87171',borderUpColor:'#34d399',borderDownColor:'#f87171',wickUpColor:'#34d39988',wickDownColor:'#f8717188'});
      if(period==='5Y'){cs.setData(toWeekly(ch.ohlc));}
      else if(period==='ALL'){cs.setData(ch.ohlc);_addMAs(chart,ch.ohlc,ch.mas,0);}
      else{let o=sl(ch.ohlc,indBars);cs.setData(o);_addMAs(chart,o,ch.mas,ch.ohlc.length-o.length);}
      _portAnalPriceLines(cs,rec,pos.costo_promedio);
      chart.timeScale().fitContent();e.lw=chart;
    }else if(rec.chart_ohlc&&rec.chart_ohlc.length>=5){
      let chart=_createLW(candleEl,false);chart.applyOptions({height:340});
      let cs=chart.addCandlestickSeries({upColor:'#34d399',downColor:'#f87171',borderUpColor:'#34d399',borderDownColor:'#f87171',wickUpColor:'#34d39988',wickDownColor:'#f8717188'});
      cs.setData(rec.chart_ohlc);
      let mas=rec.chart_mas||{};
      let maColors={"sma200":"#f87171","sma100":"#fb923c","sma50":"#facc15","sma20":"#60a5fa","ema9":"#c084fc"};
      for(let name of["sma200","sma100","sma50","sma20","ema9"]){
        let vals=mas[name];if(!vals||vals.length===0)continue;
        let line=chart.addLineSeries({color:maColors[name],lineWidth:1,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false});
        let ld=[];for(let j=0;j<rec.chart_ohlc.length;j++){if(j>=vals.length||vals[j]==null)continue;ld.push({time:rec.chart_ohlc[j].time,value:vals[j]});}
        if(ld.length>0)line.setData(ld);
      }
      _portAnalPriceLines(cs,rec,pos.costo_promedio);
      chart.timeScale().fitContent();e.lw=chart;
    }else{
      candleEl.innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted);font-size:12px">Sin datos historicos disponibles.</div>';
    }
  }

  let dates=ch?ch.dates:rec.chart_dates;
  let macdData=ch?ch.macd:rec.chart_macd;
  let rsiData=ch?ch.rsi:rec.chart_rsi;
  let koncData=ch?ch.koncorde:rec.chart_koncorde;
  if(dates&&dates.length>0){
    if(macdData)e.macd=createMACDChart('portanal_macd_'+sym,dates,macdData,indBars);
    if(rsiData&&rsiData.length>0)e.rsi=createRSIChart('portanal_rsi_'+sym,dates,rsiData,indBars);
    if(koncData)e.konc=createKoncordeChart('portanal_konc_'+sym,dates,koncData,indBars);
  }
  _portAnalCharts[sym]=e;
}

function setPortAnalPeriod(sym,period){
  _portAnalPeriods[sym]=period;
  let pos=_findPortPosition(sym);
  if(!pos)return;
  renderPortAnalCharts(sym,pos.analysis||{},pos,period);
}

function _findPortPosition(sym){
  if(!_portData||!_portData.positions)return null;
  for(let p of _portData.positions){if(p.symbol===sym)return p;}
  return null;
}

function renderPortAnalysisList(positions){
  // Cleanup previous charts
  for(let s in _portAnalCharts)_portAnalDestroy(s);
  let container=document.getElementById('port-analysis-list');
  if(!positions||positions.length===0){
    container.innerHTML='<div class="port-analysis-list-empty">Sin posiciones para analizar.</div>';
    return;
  }
  let periods=['ALL','5Y','1Y','3M','1M','1W','1D'];
  let html='';
  for(let p of positions){
    let sym=p.symbol;
    let rec=p.analysis||{};
    let sig=rec.signal||'HOLD';
    let verdict=rec.verdict||'HOLD';
    let vc=_portVerdictClass(verdict);
    let vlabel=_portVerdictLabel(verdict);
    let sc=sig==='BUY'?'rec-buy':(sig==='SELL'?'rec-sell':'rec-hold');
    let curP=_portAnalPeriods[sym]||'1Y';
    let wr=((rec.win_rate||0)*100).toFixed(0);
    let ar=rec.avg_return!=null?(rec.avg_return>=0?'+':'')+rec.avg_return.toFixed(1)+'%':'N/A';
    let arCol=rec.avg_return!=null&&rec.avg_return>=0?'var(--buy)':'var(--sell)';
    let pnlPct=p.pnl_pct||0;
    let pnlCol=pnlPct>=0?'var(--buy)':'var(--sell)';
    let curPrice=(p.precio_actual||rec.price||0);
    let avgCost=p.costo_promedio||0;

    html+='<details class="rec-details '+sc+'" id="port-anal-'+sym+'" data-sym="'+sym+'">';
    html+='<summary>';
    html+='<span class="rec-arrow">&#9654;</span>';
    html+='<span class="rec-rank-badge '+vc+'" style="min-width:70px;text-align:center">'+vlabel+'</span>';
    html+='<span class="rec-sym">'+sym+'</span>';
    html+='<span class="rec-price">$'+curPrice.toFixed(2)+'</span>';
    let sl2=rec.signal_label||sig;
    let bc2=sig==='BUY'?'rb-buy':(sig==='SELL'?'rb-sell':(sl2.includes('INMINENTE')&&sl2.includes('COMPRA')?'rb-buy-near':(sl2.includes('INMINENTE')&&sl2.includes('VENTA')?'rb-sell-near':'rb-hold')));
    html+='<span class="rec-badge '+bc2+'">'+sl2+'</span>';
    html+='<span class="rec-sum-metrics">';
    html+='<span class="rec-sm"><span class="lab">P&L</span><span class="val" style="color:'+pnlCol+'">'+(pnlPct>=0?'+':'')+pnlPct.toFixed(1)+'%</span></span>';
    html+='<span class="rec-sm"><span class="lab">Fuerza</span><span class="val">'+(rec.strength||0).toFixed(1)+'</span></span>';
    html+='<span class="rec-sm"><span class="lab">Indi</span><span class="val">'+(rec.conditions_met||0)+'/3</span></span>';
    html+='<span class="rec-sm"><span class="lab">WR</span><span class="val">'+wr+'%</span></span>';
    if(rec.risk_reward)html+='<span class="rec-sm"><span class="lab">R/R</span><span class="val">'+rec.risk_reward.toFixed(1)+':1</span></span>';
    html+='</span>';
    html+='</summary>';
    html+='<div class="rec-body">';

    // Veredicto grande (call to action)
    if(rec.verdict_reason){
      html+='<div class="rec-thesis" style="border-left:4px solid '+(verdict==='SELL'?'#ef4444':(verdict==='ADD'||verdict==='BUY'?'#34d399':(verdict==='REDUCE'?'#f59e0b':'#818cf8')))+'">';
      html+='<div class="rec-thesis-title">Que hacer con esta posicion</div>';
      html+='<div class="rec-thesis-text"><b>'+vlabel+'.</b> '+rec.verdict_reason+'</div>';
      html+='</div>';
    }

    // Thesis
    if(rec.thesis){
      html+='<div class="rec-thesis">';
      html+='<div class="rec-thesis-title">Tesis Tecnica</div>';
      html+='<div class="rec-thesis-text">'+rec.thesis+'</div>';
      html+='<div class="rec-thesis-meta">';
      if(rec.horizon)html+='<span class="rec-thesis-horizon">Horizonte: '+rec.horizon+'</span>';
      if(rec.target_pct){let sgn=sig==='SELL'?'-':'+';html+='<span class="rec-thesis-target">Objetivo: '+sgn+Math.abs(rec.target_pct).toFixed(0)+'%</span>';}
      html+='</div></div>';
    }

    // Period buttons
    html+='<div class="rec-period-bar" id="portanal_pb_'+sym+'">';
    for(let per of periods){
      html+='<button class="rec-period-btn'+(per===curP?' active':'')+'" data-p="'+per+'" onclick="setPortAnalPeriod(\''+sym+'\',\''+per+'\')">'+per+'</button>';
    }
    html+='</div>';

    // Candle + right panel
    html+='<div class="rec-top-row">';
    html+='<div class="rec-candle-wrap"><div class="rec-candle-box" id="portanal_candle_'+sym+'"></div>';
    html+='<div class="rec-candle-legend">';
    html+='<span><i style="background:#93c5fd"></i>Costo Prom.</span>';
    html+='<span><i style="background:#34d399"></i>Target</span>';
    html+='<span><i style="background:#f87171"></i>Stop</span>';
    html+='<span><i style="background:#f87171"></i>SMA200</span>';
    html+='<span><i style="background:#f97316"></i>SMA100</span>';
    html+='<span><i style="background:#eab308"></i>SMA50</span>';
    html+='<span><i style="background:#3b82f6"></i>SMA20</span>';
    html+='<span><i style="background:#a855f7"></i>EMA9</span>';
    html+='</div></div>';

    // Right panel
    html+='<div class="rec-right-panel">';
    html+='<div class="rec-metrics">';
    html+='<div class="rec-m"><span class="rec-ml">Cantidad</span><span class="rec-mv">'+(p.cantidad||0).toFixed(0)+'</span></div>';
    html+='<div class="rec-m"><span class="rec-ml">Valor</span><span class="rec-mv">$'+fmtN(p.valor_mercado||0)+'</span></div>';
    html+='<div class="rec-m"><span class="rec-ml">P&L</span><span class="rec-mv" style="color:'+pnlCol+'">'+(pnlPct>=0?'+':'')+'$'+fmtN(Math.abs(p.pnl||0))+'</span></div>';
    html+='<div class="rec-m"><span class="rec-ml">Ret. Prom.</span><span class="rec-mv" style="color:'+arCol+'">'+ar+'</span></div>';
    html+='</div>';
    html+='<div class="rec-levels"><div class="rec-lt">Niveles de Precio</div>';
    html+='<div class="rec-lr"><span class="rec-ll">Costo Prom.</span><span class="rec-lv lv-entry">$'+avgCost.toFixed(2)+'</span></div>';
    html+='<div class="rec-lr"><span class="rec-ll">Precio Actual</span><span class="rec-lv">$'+curPrice.toFixed(2)+'</span></div>';
    if(rec.target){
      let tSign=sig==='SELL'?'-':'+';
      let tPct=rec.target_pct?(' ('+tSign+Math.abs(rec.target_pct).toFixed(0)+'%)'):'';
      html+='<div class="rec-lr"><span class="rec-ll">'+(sig==='SELL'?'Obj. (baja)':'Target')+'</span><span class="rec-lv lv-target">$'+rec.target.toFixed(2)+tPct+'</span></div>';
    }
    if(rec.stop_loss)html+='<div class="rec-lr"><span class="rec-ll">Stop Loss sug.</span><span class="rec-lv lv-stop">$'+rec.stop_loss.toFixed(2)+'</span></div>';
    if(p.stop_loss)html+='<div class="rec-lr"><span class="rec-ll">Stop IB activo</span><span class="rec-lv" style="color:#f87171">$'+p.stop_loss.toFixed(2)+'</span></div>';
    if(p.take_profit)html+='<div class="rec-lr"><span class="rec-ll">Take-Profit IB</span><span class="rec-lv" style="color:#34d399">$'+p.take_profit.toFixed(2)+'</span></div>';
    if(rec.risk_reward)html+='<div class="rec-lr"><span class="rec-ll">R/R</span><span class="rec-lv lv-rr">'+rec.risk_reward.toFixed(1)+':1</span></div>';
    html+='</div>';
    html+='</div></div>';

    // Research row (idem top3)
    html+='<div class="rec-research-row">';
    html+='<div class="rec-research-panel">';
    html+=renderRecAnalystTargets(rec.fundamentals||{},curPrice);
    html+=renderRecEarnings(rec.fundamentals||{});
    html+='</div>';
    html+='<div class="rec-research-panel">';
    html+=renderRecInsiderTrades(rec.fundamentals||{});
    html+='</div>';
    html+=renderRecFundamentals(rec.fundamentals||{});
    html+='</div>';

    // Indicator row
    html+='<div class="rec-ind-grid">';
    html+='<div class="rec-ind-wrap"><div class="rec-ind-title">MACD (12,26,9)</div><div style="padding:4px"><canvas class="rec-ind-canvas" id="portanal_macd_'+sym+'"></canvas></div></div>';
    html+='<div class="rec-ind-wrap"><div class="rec-ind-title">RSI (14)</div><div style="padding:4px"><canvas class="rec-ind-canvas" id="portanal_rsi_'+sym+'"></canvas></div></div>';
    html+='<div class="rec-ind-wrap"><div class="rec-ind-title">Koncorde</div><div style="padding:4px"><canvas class="rec-ind-canvas" id="portanal_konc_'+sym+'"></canvas></div></div>';
    html+='</div>';

    // Rationale
    html+='<div class="rec-rat"><div class="rec-rt">Detalle del Analisis</div>';
    if(rec.rationale&&rec.rationale.length>0){for(let l of rec.rationale)html+='<div class="rec-ri">'+l+'</div>';}
    html+='</div>';

    html+='</div></details>';
  }
  container.innerHTML=html;

  // Hook toggle to render charts lazily
  container.querySelectorAll('.rec-details').forEach(det=>{
    let sym=det.dataset.sym;
    let pos=_findPortPosition(sym);
    if(!pos)return;
    det.addEventListener('toggle',function(){
      if(det.open){setTimeout(()=>renderPortAnalCharts(sym,pos.analysis||{},pos),50);}
      else{_portAnalDestroy(sym);}
    });
  });
}

// Bloque de charts historicos y sub-tabs de cartera removidos (redesign).


function fmtN(n){
  if(n==null)return'---';
  return Number(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
}

function badge(s,label){
  let text=label||s;
  if(s==="BUY"){
    let cls=text.includes('FUERTE')?'b-buy-strong':'b-buy';
    return'<span class="badge '+cls+'">'+text+'</span>';
  }
  if(s==="SELL"){
    let cls=text.includes('FUERTE')?'b-sell-strong':'b-sell';
    return'<span class="badge '+cls+'">'+text+'</span>';
  }
  if(!label||label==='HOLD')return'<span class="badge b-hold">NEUTRAL</span>';
  if(label.includes('INMINENTE')&&label.includes('COMPRA'))return'<span class="badge b-buy-near">'+text+'</span>';
  if(label.includes('INMINENTE')&&label.includes('VENTA'))return'<span class="badge b-sell-near">'+text+'</span>';
  if(label.includes('VIRANDO')&&label.includes('COMPRA'))return'<span class="badge b-turning-buy">'+text+'</span>';
  if(label.includes('VIRANDO')&&label.includes('VENTA'))return'<span class="badge b-turning-sell">'+text+'</span>';
  if(label.includes('SOBREVENTA'))return'<span class="badge b-oversold">'+text+'</span>';
  if(label.includes('SOBRECOMPRA'))return'<span class="badge b-overbought">'+text+'</span>';
  return'<span class="badge b-hold">'+text+'</span>';
}
function fp(p){return p!=null?"$"+p.toFixed(2):"---";}
function fv(val,ok){
  if(val==null||(typeof val==='number'&&isNaN(val)))return'<span class="iv v-na">---</span>';
  return'<span class="iv '+(ok?'v-ok':'v-no')+'">'+val.toFixed(1)+'</span>';
}
function cc(n){return"cond cond-"+n;}
function fconf(val){
  if(val==null||val===0)return'<span class="iv v-na">---</span>';
  let cls='v-no';if(val>=60)cls='v-ok';else if(val>=30)cls='v-warn';
  return'<span class="iv '+cls+'">'+val.toFixed(0)+'</span>';
}
function fret(val){
  if(val==null)return'<span class="iv v-na">---</span>';
  let cls=val>=0?'v-ok':'v-no';let s=val>=0?'+':'';
  return'<span class="iv '+cls+'">'+s+val.toFixed(1)+'%</span>';
}
function fstr(val,sig,r){
  if(val==null||val===0)return'<span class="iv v-na">---</span>';
  let cls='v-na';
  if(sig==='BUY')cls=val>=3?'v-ok':'v-warn';
  else if(sig==='SELL')cls=val>=3?'v-no':'v-warn';
  else cls=val>=3?'v-warn':'v-na';
  let t='FUERZA DE SENAL: '+val.toFixed(2)+' / 5.1\n';
  t+='Mide cuantos indicadores confirman y con que intensidad.\n\n';
  if(r){
    t+='MACD: '+(r.macd_ok?'SI':'NO')+' (+1 base'+(r.macd_detail&&r.macd_detail.includes('CONFIRMADO')?', +0.5 piso/techo confirmado':'')+')';
    t+='\n  '+((r.macd_detail||'').replace(/ /g,' '))+'\n';
    let rsi=r.values&&r.values.rsi?r.values.rsi:null;
    t+='RSI: '+(r.rsi_ok?'SI':'NO')+' (+1 base';
    if(rsi!=null&&rsi<20)t+=', +0.5 muy sobrevendido';
    else if(rsi!=null&&rsi>80)t+=', +0.5 muy sobrecomprado';
    if(r.rsi_detail&&(r.rsi_detail.includes('rebotando')||r.rsi_detail.includes('cayendo')))t+=', +0.3 girando';
    t+=')';
    t+='\n  '+((r.rsi_detail||'').replace(/ /g,' '))+'\n';
    t+='KONC: '+(r.konc_ok?'SI':'NO')+' (+1 base'+(r.konc_detail&&r.konc_detail.includes('CONFIRMADO')?', +0.5 piso/techo confirmado':'');
    if(r.konc_detail&&r.konc_detail.includes('institucional'))t+=', +0.3 institucional a favor';
    t+=')';
    t+='\n  '+((r.konc_detail||'').replace(/ /g,' '));
  }
  return'<span class="iv '+cls+'" title="'+t.replace(/"/g,'&quot;')+'">'+val.toFixed(1)+'</span>';
}
function sd(d){let s=String(d).replace(/-/g,"").replace(/ .*/,"");if(s.length>=8)return s.slice(6,8)+"/"+s.slice(4,6);return String(d).slice(-5);}
function fma(mas,key,price){
  if(!mas||price==null)return'<span class="iv v-na">---</span>';
  let v=mas[key];
  if(v==null)return'<span class="iv v-na">---</span>';
  let pct=(price-v)/v*100;
  let s=pct>=0?'+':'';
  let cls=pct>=0?'v-ok':'v-no';
  return'<span class="iv '+cls+'" title="$'+v.toFixed(2)+'">'+s+pct.toFixed(1)+'%</span>';
}
let _sortCol='vol'; // default sort by dollar volume
let _sortDir='desc';

function _getSortVal(r,col){
  if(!r)return null;
  let mas=r.chart?r.chart.mas:{};
  let vals=r.values||{};
  let konc=vals.koncorde||{};
  switch(col){
    case 'sym':return r.symbol||'';
    case 'price':return r.price||0;
    case 'signal':{let l=r.signal_label||'';if(r.signal==='BUY')return l.includes('FUERTE')?7:6;if(r.signal==='SELL')return l.includes('FUERTE')?5:4;if(l.includes('INMINENTE'))return 3;if(l.includes('VIRANDO'))return 2;return 1;}
    case 'strength':return r.strength||0;
    case 'sma200':return mas.sma200_val!=null&&r.price?((r.price-mas.sma200_val)/mas.sma200_val*100):null;
    case 'sma100':return mas.sma100_val!=null&&r.price?((r.price-mas.sma100_val)/mas.sma100_val*100):null;
    case 'sma50':return mas.sma50_val!=null&&r.price?((r.price-mas.sma50_val)/mas.sma50_val*100):null;
    case 'sma20':return mas.sma20_val!=null&&r.price?((r.price-mas.sma20_val)/mas.sma20_val*100):null;
    case 'ema9':return mas.ema9_val!=null&&r.price?((r.price-mas.ema9_val)/mas.ema9_val*100):null;
    case 'macd':return vals.macd?vals.macd.hist:null;
    case 'rsi':return vals.rsi!=null?vals.rsi:null;
    case 'konc':return konc.marron!=null?konc.marron:null;
    case 'cond':return r.conditions_met||0;
    case 'conf':return r.confidence!=null?r.confidence:null;
    case 'buy_ret':return r.buy_avg_return!=null?r.buy_avg_return:null;
    case 'sell_ret':return r.sell_avg_return!=null?r.sell_avg_return:null;
    case 'vol':return r.dollar_vol||0;
    default:return 0;
  }
}

function sortEntries(entries){
  return Object.keys(entries).sort((a,b)=>{
    let ra=entries[a],rb=entries[b];
    if(!ra&&!rb)return 0;if(!ra)return 1;if(!rb)return-1;
    let va=_getSortVal(ra,_sortCol);
    let vb=_getSortVal(rb,_sortCol);
    if(va==null&&vb==null)return 0;if(va==null)return 1;if(vb==null)return-1;
    let cmp;
    if(_sortCol==='sym')cmp=va.localeCompare(vb);
    else cmp=va-vb;
    return _sortDir==='asc'?cmp:-cmp;
  });
}

function sortListBy(col){
  if(_sortCol===col){_sortDir=_sortDir==='asc'?'desc':'asc';}
  else{_sortCol=col;_sortDir=(col==='sym'?'asc':'desc');}
  // Update header arrows
  document.querySelectorAll('#list-header .sort-arrow').forEach(e=>e.remove());
  let span=document.querySelector('#list-header [data-col="'+col+'"]');
  if(span){
    let arrow=document.createElement('span');
    arrow.className='sort-arrow';
    arrow.textContent=_sortDir==='asc'?'\u25B2':'\u25BC';
    span.appendChild(arrow);
  }
  if(_data)update();
}
function sl(arr,n){if(!arr||arr.length<=n)return arr;return arr.slice(-n);}

/* === WEEKLY AGGREGATION (daily -> weekly candles) === */
function toWeekly(ohlc){
  if(!ohlc||ohlc.length===0)return[];
  let weeks=[],cur=null;
  for(let b of ohlc){
    let d=new Date(b.time+'T00:00:00');
    let day=d.getDay();let mon=new Date(d);mon.setDate(d.getDate()-(day===0?6:day-1));
    let wk=mon.toISOString().slice(0,10);
    if(!cur||cur.wk!==wk){
      if(cur)weeks.push(cur.bar);
      cur={wk:wk,bar:{time:b.time,open:b.open,high:b.high,low:b.low,close:b.close}};
    }else{
      cur.bar.high=Math.max(cur.bar.high,b.high);
      cur.bar.low=Math.min(cur.bar.low,b.low);
      cur.bar.close=b.close;
    }
  }
  if(cur)weeks.push(cur.bar);
  return weeks;
}

/* === CHART MANAGEMENT === */
function destroyDetailCharts(idx){
  let c=_charts[idx];if(!c)return;
  if(c.lw)c.lw.remove();if(c.macd)c.macd.destroy();
  if(c.rsi)c.rsi.destroy();if(c.konc)c.konc.destroy();
  delete _charts[idx];
}

function _createLW(el,timeVis){
  return LightweightCharts.createChart(el,{
    width:el.clientWidth,height:310,
    layout:{background:{color:'#0d0d18'},textColor:'#94a3b8',fontSize:10,fontFamily:"'Inter',system-ui,sans-serif"},
    grid:{vertLines:{color:'#2a2a4a'},horzLines:{color:'#2a2a4a'}},
    crosshair:{mode:0,vertLine:{color:'#818cf877',labelBackgroundColor:'#818cf8'},horzLine:{color:'#818cf877',labelBackgroundColor:'#818cf8'}},
    timeScale:{borderColor:'#303055',timeVisible:!!timeVis},
    rightPriceScale:{borderColor:'#303055'},
  });
}
function _addMAs(chart,ohlc,mas,startIdx){
  if(!mas)return;
  let maColors={"sma200":"#f87171","sma100":"#fb923c","sma50":"#facc15","sma20":"#60a5fa","ema9":"#c084fc"};
  for(let name of["sma200","sma100","sma50","sma20","ema9"]){
    let vals=mas[name];if(!vals||vals.length===0)continue;
    let line=chart.addLineSeries({color:maColors[name],lineWidth:1,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false});
    let ld=[];
    for(let i=0;i<ohlc.length;i++){let gi=startIdx+i;if(gi>=vals.length||vals[gi]==null)continue;ld.push({time:ohlc[i].time,value:vals[gi]});}
    if(ld.length>0)line.setData(ld);
  }
}

function renderCandleDaily(containerId,allOhlc,mas,bars){
  let el=document.getElementById(containerId);if(!el)return null;
  let o=sl(allOhlc,bars);
  let chart=_createLW(el,false);
  let cs=chart.addCandlestickSeries({upColor:'#34d399',downColor:'#f87171',borderUpColor:'#34d399',borderDownColor:'#f87171',wickUpColor:'#34d39999',wickDownColor:'#f8717199'});
  cs.setData(o);
  _addMAs(chart,o,mas,allOhlc.length-o.length);
  chart.timeScale().fitContent();
  return chart;
}

function renderCandleWeekly(containerId,allOhlc,mas){
  let el=document.getElementById(containerId);if(!el)return null;
  let weekly=toWeekly(allOhlc);
  let chart=_createLW(el,false);
  let cs=chart.addCandlestickSeries({upColor:'#34d399',downColor:'#f87171',borderUpColor:'#34d399',borderDownColor:'#f87171',wickUpColor:'#34d39999',wickDownColor:'#f8717199'});
  cs.setData(weekly);
  // MAs on weekly: aggregate or skip (skip - weekly is for overview)
  chart.timeScale().fitContent();
  return chart;
}

function renderCandleIntraday(containerId,ohlc){
  let el=document.getElementById(containerId);if(!el)return null;
  let chart=_createLW(el,true);
  let cs=chart.addCandlestickSeries({upColor:'#34d399',downColor:'#f87171',borderUpColor:'#34d399',borderDownColor:'#f87171',wickUpColor:'#34d39999',wickDownColor:'#f8717199'});
  cs.setData(ohlc);
  chart.timeScale().fitContent();
  return chart;
}

function createMACDChart(id,dates,macd,bars){
  let ctx=document.getElementById(id);if(!ctx)return null;
  let d=sl(dates,bars),m={hist:sl(macd.hist,bars),macd:sl(macd.macd,bars),signal:sl(macd.signal,bars)};
  let labels=d.map(sd);
  let colors=m.hist.map(v=>v>=0?'#10b981':'#ef4444');
  return new Chart(ctx,{type:'bar',data:{labels:labels,datasets:[
    {type:'bar',data:m.hist,backgroundColor:colors,borderWidth:0,barPercentage:.8,order:2},
    {type:'line',data:m.macd,borderColor:'#7dd3fc',borderWidth:1.5,pointRadius:0,fill:false,order:1},
    {type:'line',data:m.signal,borderColor:'#fb923c',borderWidth:1.5,pointRadius:0,fill:false,order:1}
  ]},options:{responsive:true,maintainAspectRatio:false,animation:false,
    plugins:{legend:{display:false}},
    scales:{x:{ticks:{color:'#8896a8',font:{size:8},maxTicksLimit:8},grid:{color:'#2a2a4a'}},
            y:{ticks:{color:'#8896a8',font:{size:8}},grid:{color:'#2a2a4a'}}}}});
}

function createRSIChart(id,dates,rsi,bars){
  let ctx=document.getElementById(id);if(!ctx)return null;
  let d=sl(dates,bars),r=sl(rsi,bars);
  let labels=d.map(sd);
  return new Chart(ctx,{type:'line',data:{labels:labels,datasets:[
    {data:r,borderColor:'#c084fc',borderWidth:2,pointRadius:0,fill:false}
  ]},options:{responsive:true,maintainAspectRatio:false,animation:false,
    plugins:{legend:{display:false}},
    scales:{x:{ticks:{color:'#8896a8',font:{size:8},maxTicksLimit:8},grid:{color:'#2a2a4a'}},
            y:{min:0,max:100,ticks:{color:'#8896a8',font:{size:8},stepSize:10},
               grid:{color:function(c){return(c.tick.value===30||c.tick.value===70)?'#ffffff33':'#2a2a4a';}}}}},
  plugins:[{id:'rz',beforeDraw(ch){
    let{ctx,chartArea:a,scales}=ch;if(!a)return;let y=scales.y;
    ctx.save();
    ctx.fillStyle='rgba(52,211,153,0.08)';ctx.fillRect(a.left,y.getPixelForValue(30),a.width,y.getPixelForValue(0)-y.getPixelForValue(30));
    ctx.fillStyle='rgba(248,113,113,0.08)';ctx.fillRect(a.left,y.getPixelForValue(100),a.width,y.getPixelForValue(70)-y.getPixelForValue(100));
    ctx.strokeStyle='#34d39950';ctx.setLineDash([4,4]);ctx.beginPath();ctx.moveTo(a.left,y.getPixelForValue(30));ctx.lineTo(a.right,y.getPixelForValue(30));ctx.stroke();
    ctx.strokeStyle='#f8717150';ctx.beginPath();ctx.moveTo(a.left,y.getPixelForValue(70));ctx.lineTo(a.right,y.getPixelForValue(70));ctx.stroke();
    ctx.restore();
  }}]});
}

/* === KONCORDE - TradingView style (overlapping bars + lines) === */
function createKoncordeChart(id,dates,k,bars){
  let ctx=document.getElementById(id);if(!ctx)return null;
  let d=sl(dates,bars),kk={verde:sl(k.verde,bars),marron:sl(k.marron,bars),azul:sl(k.azul,bars),media:sl(k.media,bars)};
  let labels=d.map(sd);
  let marronBg=kk.marron.map(v=>v>=0?'rgba(251,191,36,0.55)':'rgba(251,191,36,0.38)');
  let verdeBg=kk.verde.map(v=>v>=0?'rgba(52,211,153,0.65)':'rgba(52,211,153,0.45)');
  return new Chart(ctx,{type:'bar',data:{labels:labels,datasets:[
    {type:'bar',label:'Marron',data:kk.marron,backgroundColor:marronBg,borderColor:'#fbbf24',borderWidth:0.5,barPercentage:0.95,categoryPercentage:0.95,stack:'s1',order:4},
    {type:'bar',label:'Verde',data:kk.verde,backgroundColor:verdeBg,borderColor:'#34d399',borderWidth:0.5,barPercentage:0.7,categoryPercentage:0.7,stack:'s2',order:3},
    {type:'line',label:'Azul',data:kk.azul,borderColor:'#60a5fa',borderWidth:2,pointRadius:0,fill:false,order:1},
    {type:'line',label:'Media',data:kk.media,borderColor:'#f87171',borderWidth:1.5,borderDash:[4,4],pointRadius:0,fill:false,order:0}
  ]},options:{responsive:true,maintainAspectRatio:false,animation:false,
    plugins:{legend:{display:true,position:'top',labels:{color:'#64748b',font:{size:9},boxWidth:10,padding:5}}},
    scales:{
      x:{stacked:true,ticks:{color:'#8896a8',font:{size:8},maxTicksLimit:8},grid:{color:'#2a2a4a'}},
      y:{stacked:false,ticks:{color:'#8896a8',font:{size:8}},grid:{color:'#2a2a4a'}}
    }},
  plugins:[{id:'zl',beforeDraw(ch){let{ctx,chartArea:a,scales}=ch;if(!a)return;let y0=scales.y.getPixelForValue(0);
    if(y0>=a.top&&y0<=a.bottom){ctx.save();ctx.strokeStyle='#ffffff28';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(a.left,y0);ctx.lineTo(a.right,y0);ctx.stroke();ctx.restore();}}}]});
}

/* === RENDER CHARTS FOR A DETAIL === */
function renderDetailCharts(idx,sym,period){
  if(!_data)return;let r=_data.results[sym];if(!r||!r.chart)return;
  destroyDetailCharts(idx);
  let ch=r.chart;
  let indBars=DAILY_BARS[period]||252;

  // Candlestick: depends on period
  if(period==='5Y'){
    _charts[idx]={lw:renderCandleWeekly('candle_'+idx,ch.ohlc,ch.mas)};
  }else if(period==='ALL'){
    _charts[idx]={lw:renderCandleDaily('candle_'+idx,ch.ohlc,ch.mas,ch.ohlc.length)};
  }else if(period==='1M'||period==='1D'){
    // Intraday: fetch on demand
    let apiP=period==='1M'?'4h':'15m';
    let cacheKey=sym+'_'+apiP;
    if(_intradayCache[cacheKey]){
      _charts[idx]={lw:renderCandleIntraday('candle_'+idx,_intradayCache[cacheKey])};
    }else{
      let el=document.getElementById('candle_'+idx);
      if(el)el.innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted);font-size:13px">Cargando barras '+apiP+'...</div>';
      fetch('/api/bars/'+sym+'/'+apiP).then(r=>r.json()).then(d=>{
        _intradayCache[cacheKey]=d.ohlc||[];
        if(_periods[idx]===period){
          let el2=document.getElementById('candle_'+idx);
          if(el2)el2.innerHTML='';
          if(!_charts[idx])_charts[idx]={};
          if(_charts[idx].lw)_charts[idx].lw.remove();
          _charts[idx].lw=renderCandleIntraday('candle_'+idx,_intradayCache[cacheKey]);
        }
      }).catch(e=>console.error('Intraday fetch error:',e));
      _charts[idx]={};
    }
  }else{
    // Daily (1Y, 3M, 1W)
    let bars=DAILY_BARS[period]||252;
    _charts[idx]={lw:renderCandleDaily('candle_'+idx,ch.ohlc,ch.mas,bars)};
  }

  // Indicator charts always use daily data
  let c=_charts[idx]||{};
  c.macd=createMACDChart('macd_'+idx,ch.dates,ch.macd,indBars);
  c.rsi=createRSIChart('rsi_'+idx,ch.dates,ch.rsi,indBars);
  c.konc=createKoncordeChart('konc_'+idx,ch.dates,ch.koncorde,indBars);
  _charts[idx]=c;
}

function setPeriod(idx,sym,period){
  _periods[idx]=period;
  let btns=document.querySelectorAll('#pb_'+idx+' .period-btn');
  btns.forEach(b=>{b.classList.toggle('active',b.dataset.p===period);});
  renderDetailCharts(idx,sym,period);
}

// ─── TOP 3 ACCORDION — Chart Management ───
let _recDetailCharts={};
let _recResizeObservers={};
let _top3Data=[];
let _recPeriods={};   // {idx: '3M'}
let _recIntradayCache={};  // {sym_period: ohlc}

function destroyRecDetailCharts(idx){
  let entry=_recDetailCharts[idx];
  if(!entry)return;
  try{if(entry.lw)entry.lw.remove();}catch(e){}
  try{if(entry.macd)entry.macd.destroy();}catch(e){}
  try{if(entry.rsi)entry.rsi.destroy();}catch(e){}
  try{if(entry.konc)entry.konc.destroy();}catch(e){}
  let ro=_recResizeObservers[idx];
  if(ro)try{ro.disconnect();}catch(e){}
  delete _recDetailCharts[idx];
  delete _recResizeObservers[idx];
}

function destroyAllRecCharts(){
  for(let idx in _recDetailCharts)destroyRecDetailCharts(idx);
  _recDetailCharts={};
  _recResizeObservers={};
}

function fmtMktCap(v){if(v==null)return'N/A';if(v>=1e12)return'$'+(v/1e12).toFixed(1)+'T';if(v>=1e9)return'$'+(v/1e9).toFixed(1)+'B';return'$'+(v/1e6).toFixed(0)+'M';}

function renderRecFundamentals(fund){
  if(!fund||Object.keys(fund).length===0)return'';
  let h='<div class="rec-fund"><div class="rec-lt">Datos Fundamentales</div><div class="rec-fund-grid">';
  let sector=fund.sector||'N/A';
  let industry=fund.industry||'N/A';
  h+='<div class="rec-fl"><span class="rec-fll">Sector</span><span class="rec-flv">'+sector+'</span></div>';
  h+='<div class="rec-fl"><span class="rec-fll">Industria</span><span class="rec-flv" style="font-size:10px">'+industry+'</span></div>';
  let pe=fund.trailing_pe;h+='<div class="rec-fl"><span class="rec-fll">P/E TTM</span><span class="rec-flv">'+(pe!=null?pe.toFixed(1):'N/A')+'</span></div>';
  let fpe=fund.forward_pe;h+='<div class="rec-fl"><span class="rec-fll">P/E Fwd</span><span class="rec-flv">'+(fpe!=null?fpe.toFixed(1):'N/A')+'</span></div>';
  let eps=fund.eps;h+='<div class="rec-fl"><span class="rec-fll">EPS</span><span class="rec-flv">'+(eps!=null?'$'+eps.toFixed(2):'N/A')+'</span></div>';
  h+='<div class="rec-fl"><span class="rec-fll">Mkt Cap</span><span class="rec-flv">'+fmtMktCap(fund.market_cap)+'</span></div>';
  let dy=fund.dividend_yield;h+='<div class="rec-fl"><span class="rec-fll">Div Yield</span><span class="rec-flv">'+(dy!=null?dy.toFixed(2)+'%':'N/A')+'</span></div>';
  let beta=fund.beta;h+='<div class="rec-fl"><span class="rec-fll">Beta</span><span class="rec-flv">'+(beta!=null?beta.toFixed(2):'N/A')+'</span></div>';
  let hi=fund.fifty_two_week_high;h+='<div class="rec-fl"><span class="rec-fll">52W Hi</span><span class="rec-flv" style="color:var(--buy)">'+(hi!=null?'$'+hi.toFixed(2):'N/A')+'</span></div>';
  let lo=fund.fifty_two_week_low;h+='<div class="rec-fl"><span class="rec-fll">52W Lo</span><span class="rec-flv" style="color:var(--sell)">'+(lo!=null?'$'+lo.toFixed(2):'N/A')+'</span></div>';
  // Additional ratios
  h+='</div><hr class="rec-ratio-divider"><div class="rec-fund-grid">';
  let roe=fund.roe;
  let roeCol=roe!=null?(roe>0.15?'var(--buy)':(roe>0?'var(--text)':'var(--sell)')):'';
  h+='<div class="rec-fl"><span class="rec-fll">ROE</span><span class="rec-flv"'+(roeCol?' style="color:'+roeCol+'"':'')+'>'+(roe!=null?(roe*100).toFixed(1)+'%':'N/A')+'</span></div>';
  let de=fund.debt_to_equity;
  let deCol=de!=null?(de<1?'var(--buy)':(de<2?'var(--text)':'var(--sell)')):'';
  h+='<div class="rec-fl"><span class="rec-fll">D/E</span><span class="rec-flv"'+(deCol?' style="color:'+deCol+'"':'')+'>'+(de!=null?de.toFixed(1):'N/A')+'</span></div>';
  let cr=fund.current_ratio;h+='<div class="rec-fl"><span class="rec-fll">Current R.</span><span class="rec-flv">'+(cr!=null?cr.toFixed(2):'N/A')+'</span></div>';
  let rg=fund.revenue_growth;
  let rgCol=rg!=null?(rg>0?'var(--buy)':'var(--sell)'):'';
  h+='<div class="rec-fl"><span class="rec-fll">Rev Growth</span><span class="rec-flv"'+(rgCol?' style="color:'+rgCol+'"':'')+'>'+(rg!=null?(rg*100).toFixed(1)+'%':'N/A')+'</span></div>';
  let pm=fund.profit_margin;
  let pmCol=pm!=null?(pm>0?'var(--buy)':'var(--sell)'):'';
  h+='<div class="rec-fl"><span class="rec-fll">Profit Mrg</span><span class="rec-flv"'+(pmCol?' style="color:'+pmCol+'"':'')+'>'+(pm!=null?(pm*100).toFixed(1)+'%':'N/A')+'</span></div>';
  let om=fund.operating_margin;
  let omCol=om!=null?(om>0?'var(--buy)':'var(--sell)'):'';
  h+='<div class="rec-fl"><span class="rec-fll">Op. Margin</span><span class="rec-flv"'+(omCol?' style="color:'+omCol+'"':'')+'>'+(om!=null?(om*100).toFixed(1)+'%':'N/A')+'</span></div>';
  h+='</div></div>';
  return h;
}

function renderRecAnalystTargets(fund,price){
  if(!fund||!fund.analyst_targets)return'';
  let at=fund.analyst_targets;
  if(!at.mean)return'';
  let upside=price?((at.mean-price)/price*100):0;
  let upCol=upside>=0?'var(--buy)':'var(--sell)';
  let upSign=upside>=0?'+':'';
  let h='<div class="rec-analyst"><div class="rec-lt">Analistas — Price Target</div>';
  h+='<div class="rec-upside" style="color:'+upCol+'">'+upSign+upside.toFixed(1)+'% vs target</div>';
  // Range bar: low...mean...high with current price marker
  let lo=at.low||at.mean;let hi=at.high||at.mean;
  let range=hi-lo;
  if(range>0){
    let meanPct=((at.mean-lo)/range*100);
    let pricePct=price?Math.max(0,Math.min(100,((price-lo)/range*100))):meanPct;
    h+='<div class="rec-analyst-bar"><div class="rec-analyst-fill" style="width:100%"></div>';
    h+='<div class="rec-analyst-marker" style="left:'+pricePct+'%" title="Precio actual $'+price.toFixed(2)+'"></div>';
    h+='</div>';
    h+='<div class="rec-analyst-labels"><span>$'+lo.toFixed(0)+'</span><span style="color:var(--accent)">Mean $'+at.mean.toFixed(0)+'</span><span>$'+hi.toFixed(0)+'</span></div>';
  }
  h+='</div>';
  return h;
}

function renderRecInsiderTrades(fund){
  if(!fund||!fund.insider_trades)return'';
  let ins=fund.insider_trades;
  let sentClass=ins.sentiment==='bullish'?'sent-bullish':(ins.sentiment==='bearish'?'sent-bearish':'sent-neutral');
  let sentLabel=ins.sentiment==='bullish'?'Alcista':(ins.sentiment==='bearish'?'Bajista':'Neutral');
  let h='<div class="rec-insider">';
  h+='<div class="rec-insider-header"><span class="rec-lt" style="margin:0">Insiders (90d)</span>';
  h+='<span class="rec-sent-badge '+sentClass+'">'+sentLabel+'</span></div>';
  h+='<div class="rec-ins-summary">';
  h+='<span style="color:var(--buy)">'+ins.buys+' compras</span>';
  h+='<span style="color:var(--sell)">'+ins.sells+' ventas</span>';
  h+='</div>';
  if(ins.transactions&&ins.transactions.length>0){
    for(let t of ins.transactions.slice(0,3)){
      let valStr=t.value>0?'$'+(t.value/1e6).toFixed(1)+'M':'';
      let sharesStr=t.shares>0?t.shares.toLocaleString()+' shares':'';
      h+='<div class="rec-ins-tx">'+t.insider.substring(0,25)+(t.text?' — '+t.text.substring(0,30):'')+' '+(sharesStr?sharesStr+' ':'')+valStr+'</div>';
    }
  }
  h+='</div>';
  return h;
}

function renderRecEarnings(fund){
  if(!fund||!fund.earnings)return'';
  let e=fund.earnings;
  if(e.days_until==null)return'';
  let warn=e.days_until<=14;
  let badgeCol=warn?'background:#f8717128;color:var(--sell)':'background:#818cf828;color:var(--accent)';
  let h='<div class="rec-earnings">';
  h+='<div class="rec-earn-badge" style="'+badgeCol+'">'+e.days_until+'d</div>';
  h+='<div class="rec-earn-info">';
  h+='<div style="font-weight:700;'+(warn?'color:var(--sell)':'color:var(--text)')+'">Earnings: '+e.next_date+'</div>';
  if(warn)h+='<div class="rec-earn-warn">Volatilidad alta esperada</div>';
  if(e.eps_estimate!=null)h+='<div style="color:var(--muted)">EPS Est: $'+e.eps_estimate.toFixed(2)+'</div>';
  h+='</div></div>';
  return h;
}

function _recAddPriceLines(cs,rec){
  cs.createPriceLine({price:rec.entry_low,color:'#93c5fd',lineWidth:1,lineStyle:LightweightCharts.LineStyle.Dashed,axisLabelVisible:true,title:'Entrada'});
  if(Math.abs(rec.entry_high-rec.entry_low)>0.01)
    cs.createPriceLine({price:rec.entry_high,color:'#93c5fd',lineWidth:1,lineStyle:LightweightCharts.LineStyle.Dashed,axisLabelVisible:false,title:''});
  cs.createPriceLine({price:rec.target,color:'#34d399',lineWidth:2,lineStyle:LightweightCharts.LineStyle.Solid,axisLabelVisible:true,title:'Target'});
  cs.createPriceLine({price:rec.stop_loss,color:'#f87171',lineWidth:2,lineStyle:LightweightCharts.LineStyle.Solid,axisLabelVisible:true,title:'Stop'});
  if(rec.chart_markers&&rec.chart_markers.length>0){
    cs.setMarkers(rec.chart_markers.sort((a,b)=>a.time<b.time?-1:1));
  }
}

function renderRecDetailCharts(idx,rec,period){
  destroyRecDetailCharts(idx);
  if(!rec)return;
  let sym=rec.symbol;
  if(!period)period=_recPeriods[idx]||'1Y';
  _recPeriods[idx]=period;

  // Update active button
  let bar=document.getElementById('rec_pb_'+idx);
  if(bar){bar.querySelectorAll('.rec-period-btn').forEach(b=>{b.classList.toggle('active',b.dataset.p===period);});}

  let entry={lw:null,macd:null,rsi:null,konc:null};

  // Get full chart data from main analysis cache (same source as main grid)
  let fullData=(_data&&_data.results)?_data.results[sym]:null;
  let ch=fullData?fullData.chart:null;
  let indBars=DAILY_BARS[period]||252;

  // 1. Candlestick chart
  let candleEl=document.getElementById('rec_candle_'+idx);
  if(candleEl){
    candleEl.innerHTML='';
    if(period==='1M'||period==='1D'){
      // Intraday
      let apiP=period==='1M'?'4h':'15m';
      let cacheKey=sym+'_'+apiP;
      if(_recIntradayCache[cacheKey]){
        let chart=_createLW(candleEl,true);chart.applyOptions({height:360});
        let cs=chart.addCandlestickSeries({upColor:'#34d399',downColor:'#f87171',borderUpColor:'#34d399',borderDownColor:'#f87171',wickUpColor:'#34d39988',wickDownColor:'#f8717188'});
        cs.setData(_recIntradayCache[cacheKey]);
        _recAddPriceLines(cs,rec);
        chart.timeScale().fitContent();
        entry.lw=chart;
        let ro=new ResizeObserver(()=>{chart.applyOptions({width:candleEl.clientWidth});});ro.observe(candleEl);_recResizeObservers[idx]=ro;
      }else{
        candleEl.innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted);font-size:13px">Cargando barras '+apiP+'...</div>';
        fetch('/api/bars/'+sym+'/'+apiP).then(r=>r.json()).then(d=>{
          _recIntradayCache[cacheKey]=d.ohlc||[];
          if(_recPeriods[idx]===period){renderRecDetailCharts(idx,rec,period);}
        }).catch(e=>console.error('Rec intraday error:',e));
      }
    }else if(ch&&ch.ohlc&&ch.ohlc.length>=5){
      // Daily / weekly
      let chart=_createLW(candleEl,false);chart.applyOptions({height:360});
      let cs=chart.addCandlestickSeries({upColor:'#34d399',downColor:'#f87171',borderUpColor:'#34d399',borderDownColor:'#f87171',wickUpColor:'#34d39988',wickDownColor:'#f8717188'});
      if(period==='5Y'){
        let weekly=toWeekly(ch.ohlc);cs.setData(weekly);
      }else if(period==='ALL'){
        cs.setData(ch.ohlc);_addMAs(chart,ch.ohlc,ch.mas,0);
      }else{
        let o=sl(ch.ohlc,indBars);cs.setData(o);
        _addMAs(chart,o,ch.mas,ch.ohlc.length-o.length);
      }
      _recAddPriceLines(cs,rec);
      chart.timeScale().fitContent();
      entry.lw=chart;
      let ro=new ResizeObserver(()=>{chart.applyOptions({width:candleEl.clientWidth});});ro.observe(candleEl);_recResizeObservers[idx]=ro;
    }else if(rec.chart_ohlc&&rec.chart_ohlc.length>=5){
      // Fallback: use pre-sliced rec data
      let chart=_createLW(candleEl,false);chart.applyOptions({height:360});
      let cs=chart.addCandlestickSeries({upColor:'#34d399',downColor:'#f87171',borderUpColor:'#34d399',borderDownColor:'#f87171',wickUpColor:'#34d39988',wickDownColor:'#f8717188'});
      cs.setData(rec.chart_ohlc);
      let mas=rec.chart_mas||{};
      let maColors={"sma200":"#f87171","sma100":"#fb923c","sma50":"#facc15","sma20":"#60a5fa","ema9":"#c084fc"};
      for(let name of["sma200","sma100","sma50","sma20","ema9"]){
        let vals=mas[name];if(!vals||vals.length===0)continue;
        let line=chart.addLineSeries({color:maColors[name],lineWidth:1,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false});
        let ld=[];for(let j=0;j<rec.chart_ohlc.length;j++){if(j>=vals.length||vals[j]==null)continue;ld.push({time:rec.chart_ohlc[j].time,value:vals[j]});}
        if(ld.length>0)line.setData(ld);
      }
      _recAddPriceLines(cs,rec);
      chart.timeScale().fitContent();
      entry.lw=chart;
      let ro=new ResizeObserver(()=>{chart.applyOptions({width:candleEl.clientWidth});});ro.observe(candleEl);_recResizeObservers[idx]=ro;
    }
  }

  // 2. Indicator charts — use full data from main cache if available
  let dates=ch?ch.dates:rec.chart_dates;
  let macdData=ch?ch.macd:rec.chart_macd;
  let rsiData=ch?ch.rsi:rec.chart_rsi;
  let koncData=ch?ch.koncorde:rec.chart_koncorde;
  if(dates&&dates.length>0){
    if(macdData)entry.macd=createMACDChart('rec_macd_'+idx,dates,macdData,indBars);
    if(rsiData&&rsiData.length>0)entry.rsi=createRSIChart('rec_rsi_'+idx,dates,rsiData,indBars);
    if(koncData)entry.konc=createKoncordeChart('rec_konc_'+idx,dates,koncData,indBars);
  }

  _recDetailCharts[idx]=entry;
}

function setRecPeriod(idx,period){
  _recPeriods[idx]=period;
  let rec=_top3Data[idx];
  if(!rec)return;
  renderRecDetailCharts(idx,rec,period);
}

let _recFirstRender=true;
function renderTop3(top3){
  // Save which rec accordions are open before destroying
  let recOpenSet=new Set();
  document.querySelectorAll('.rec-details[open]').forEach(d=>{let idx=d.dataset.idx;if(idx!=null)recOpenSet.add(parseInt(idx));});
  destroyAllRecCharts();
  _top3Data=top3||[];
  let sec=document.getElementById('top3-section');
  if(!sec)return;
  if(!top3||top3.length===0){sec.style.display='none';return;}
  sec.style.display='';

  let periods=['ALL','5Y','1Y','3M','1M','1W','1D'];
  let html='<div class="top3-title">Top Recomendaciones</div>';
  for(let i=0;i<top3.length;i++){
    let r=top3[i];
    let sc=r.signal==='BUY'?'rec-buy':(r.signal==='SELL'?'rec-sell':'rec-hold');
    let sl=r.signal_label||r.signal;
    let bc=r.signal==='BUY'?'rb-buy':(r.signal==='SELL'?'rb-sell':(sl.includes('INMINENTE')&&sl.includes('COMPRA')?'rb-buy-near':(sl.includes('INMINENTE')&&sl.includes('VENTA')?'rb-sell-near':'rb-hold')));
    let wr=((r.win_rate||0)*100).toFixed(0);
    let ar=r.avg_return!=null?(r.avg_return>=0?'+':'')+r.avg_return.toFixed(1)+'%':'N/A';
    let arCol=r.avg_return!=null&&r.avg_return>=0?'var(--buy)':'var(--sell)';
    let curP=_recPeriods[i]||'1Y';

    // On first render open #1; on subsequent renders preserve user state
    let shouldOpen=_recFirstRender?(i===0):recOpenSet.has(i);
    html+='<details class="rec-details '+sc+'"'+(shouldOpen?' open':'')+' data-idx="'+i+'">';
    // ── Summary row ──
    html+='<summary>';
    html+='<span class="rec-arrow">&#9654;</span>';
    html+='<span class="rec-rank-badge">#'+(i+1)+'</span>';
    html+='<span class="rec-sym">'+r.symbol+'</span>';
    html+='<span class="rec-price">$'+r.price.toFixed(2)+'</span>';
    html+='<span class="rec-badge '+bc+'">'+sl+'</span>';
    html+='<span class="rec-sum-metrics">';
    html+='<span class="rec-sm"><span class="lab">Score</span><span class="val" style="color:var(--accent)">'+r.score+'</span></span>';
    html+='<span class="rec-sm"><span class="lab">Fuerza</span><span class="val" style="color:'+(r.strength>=3?'var(--buy)':'var(--hold)')+'">'+r.strength.toFixed(1)+'</span></span>';
    html+='<span class="rec-sm"><span class="lab">WR</span><span class="val" style="color:'+(r.win_rate>=0.6?'var(--buy)':'var(--muted)')+'">'+wr+'%</span></span>';
    html+='<span class="rec-sm"><span class="lab">R/R</span><span class="val" style="color:var(--accent)">'+r.risk_reward.toFixed(1)+':1</span></span>';
    html+='</span>';
    html+='</summary>';

    // ── Body ──
    html+='<div class="rec-body">';

    // Thesis (prominent, first thing in body)
    if(r.thesis){
      html+='<div class="rec-thesis">';
      html+='<div class="rec-thesis-title">Tesis de Inversion</div>';
      html+='<div class="rec-thesis-text">'+r.thesis+'</div>';
      html+='<div class="rec-thesis-meta">';
      if(r.horizon){
        html+='<span class="rec-thesis-horizon">Horizonte: '+r.horizon+'</span>';
      }
      if(r.target_pct){
        let sign=r.signal==='SELL'?'-':'+';
        html+='<span class="rec-thesis-target">Objetivo: '+sign+Math.abs(r.target_pct).toFixed(0)+'%</span>';
      }
      html+='</div>';
      html+='</div>';
    }

    // Period buttons
    html+='<div class="rec-period-bar" id="rec_pb_'+i+'">';
    for(let p of periods){
      html+='<button class="rec-period-btn'+(p===curP?' active':'')+'" data-p="'+p+'" onclick="setRecPeriod('+i+',\''+p+'\')">'+p+'</button>';
    }
    html+='</div>';

    // Top row: candle chart (left) + right panel
    html+='<div class="rec-top-row">';

    // Left: candlestick
    html+='<div class="rec-candle-wrap"><div class="rec-candle-box" id="rec_candle_'+i+'"></div>';
    html+='<div class="rec-candle-legend">';
    html+='<span><i style="background:#93c5fd"></i>Entrada</span>';
    html+='<span><i style="background:#34d399"></i>Target</span>';
    html+='<span><i style="background:#f87171"></i>Stop</span>';
    html+='<span><i style="background:#f87171"></i>SMA200</span>';
    html+='<span><i style="background:#f97316"></i>SMA100</span>';
    html+='<span><i style="background:#eab308"></i>SMA50</span>';
    html+='<span><i style="background:#3b82f6"></i>SMA20</span>';
    html+='<span><i style="background:#a855f7"></i>EMA9</span>';
    html+='</div></div>';

    // Right: metrics + levels only (compact)
    html+='<div class="rec-right-panel">';
    html+='<div class="rec-metrics">';
    html+='<div class="rec-m"><span class="rec-ml">Fuerza</span><span class="rec-mv" style="color:'+(r.strength>=3?'var(--buy)':'var(--hold)')+'">'+r.strength.toFixed(1)+'/5.1</span></div>';
    html+='<div class="rec-m"><span class="rec-ml">Confianza</span><span class="rec-mv" style="color:'+(r.confidence>=60?'var(--buy)':(r.confidence>=30?'var(--hold)':'var(--sell)'))+'">'+r.confidence.toFixed(0)+'%</span></div>';
    html+='<div class="rec-m"><span class="rec-ml">Win Rate</span><span class="rec-mv" style="color:'+(r.win_rate>=0.6?'var(--buy)':'var(--muted)')+'">'+wr+'%</span></div>';
    html+='<div class="rec-m"><span class="rec-ml">Ret. Prom.</span><span class="rec-mv" style="color:'+arCol+'">'+ar+'</span></div>';
    html+='</div>';
    html+='<div class="rec-levels"><div class="rec-lt">Niveles de Precio</div>';
    html+='<div class="rec-lr"><span class="rec-ll">Entrada</span><span class="rec-lv lv-entry">$'+r.entry_low.toFixed(2)+' — $'+r.entry_high.toFixed(2)+'</span></div>';
    let tSign=r.signal==='SELL'?'-':'+';
    let tPct=r.target_pct?(' ('+tSign+Math.abs(r.target_pct).toFixed(0)+'%)'):'';
    html+='<div class="rec-lr"><span class="rec-ll">'+(r.signal==='SELL'?'Obj. (baja)':'Objetivo')+'</span><span class="rec-lv lv-target">$'+r.target.toFixed(2)+tPct+'</span></div>';
    html+='<div class="rec-lr"><span class="rec-ll">Stop Loss</span><span class="rec-lv lv-stop">$'+r.stop_loss.toFixed(2)+'</span></div>';
    html+='<div class="rec-lr"><span class="rec-ll">R/R Ratio</span><span class="rec-lv lv-rr">'+r.risk_reward.toFixed(1)+':1</span></div>';
    if(r.horizon){html+='<div class="rec-lr"><span class="rec-ll">Horizonte</span><span class="rec-lv" style="color:var(--accent)">'+r.horizon+'</span></div>';}
    html+='</div>';
    html+='</div>'; // end right panel
    html+='</div>'; // end top-row

    // Research row: Analyst+Earnings | Insiders | Fundamentals
    html+='<div class="rec-research-row">';
    // Col 1: Analyst targets + Earnings
    html+='<div class="rec-research-panel">';
    html+=renderRecAnalystTargets(r.fundamentals||{},r.price);
    html+=renderRecEarnings(r.fundamentals||{});
    html+='</div>';
    // Col 2: Insider trades
    html+='<div class="rec-research-panel">';
    html+=renderRecInsiderTrades(r.fundamentals||{});
    html+='</div>';
    // Col 3: Fundamentals
    html+=renderRecFundamentals(r.fundamentals||{});
    html+='</div>'; // end research row

    // Indicator row: MACD / RSI / Koncorde
    html+='<div class="rec-ind-grid">';
    html+='<div class="rec-ind-wrap"><div class="rec-ind-title">MACD (12,26,9)</div><div style="padding:4px"><canvas class="rec-ind-canvas" id="rec_macd_'+i+'"></canvas></div></div>';
    html+='<div class="rec-ind-wrap"><div class="rec-ind-title">RSI (14)</div><div style="padding:4px"><canvas class="rec-ind-canvas" id="rec_rsi_'+i+'"></canvas></div></div>';
    html+='<div class="rec-ind-wrap"><div class="rec-ind-title">Koncorde</div><div style="padding:4px"><canvas class="rec-ind-canvas" id="rec_konc_'+i+'"></canvas></div></div>';
    html+='</div>';

    // Rationale
    html+='<div class="rec-rat"><div class="rec-rt">Detalle del Analisis</div>';
    if(r.rationale&&r.rationale.length>0){for(let l of r.rationale)html+='<div class="rec-ri">'+l+'</div>';}
    html+='</div>';
    // Score bar
    html+='<div class="rec-sb"><div class="rec-sf" style="width:'+r.score+'%"></div></div>';

    html+='</div>'; // end rec-body
    html+='</details>';
  }

  sec.innerHTML=html;

  // Lazy rendering: create charts only when <details> opens
  sec.querySelectorAll('.rec-details').forEach(det=>{
    let idx=parseInt(det.dataset.idx);
    det.addEventListener('toggle',function(){
      if(det.open){
        setTimeout(()=>renderRecDetailCharts(idx,_top3Data[idx]),50);
      }else{
        destroyRecDetailCharts(idx);
      }
    });
    // Render first opened item
    if(det.open){
      setTimeout(()=>renderRecDetailCharts(idx,_top3Data[idx]),80);
    }
  });
  _recFirstRender=false;
}

function buildTechSummary(r){
  // Returns {text: "readable summary", alerts: [{label,color}], hasAlert: bool}
  if(!r)return {text:'',alerts:[],hasAlert:false};
  let parts=[];
  let alerts=[];
  let rsi=r.values?r.values.rsi:null;
  let mas=r.chart?r.chart.mas:null;
  let mh=r.values&&r.values.macd?r.values.macd.hist:null;
  let price=r.price||0;

  // 1. Señal principal (usa signal_label descriptivo)
  let sl=r.signal_label||r.signal;
  if(r.signal==='BUY'){
    let extra=sl.includes('FUERTE')?' con alta conviccion':'';
    parts.push('<span style="color:var(--buy);font-weight:700">'+sl+'</span> — Los 3 indicadores alineados al alza'+extra);
  }else if(r.signal==='SELL'){
    let extra=sl.includes('FUERTE')?' con alta conviccion':'';
    parts.push('<span style="color:var(--sell);font-weight:700">'+sl+'</span> — Los 3 indicadores alineados a la baja'+extra);
  }else{
    let c=r.conditions_met||0;
    if(sl.includes('INMINENTE')&&sl.includes('COMPRA')) parts.push('<span style="color:#6ee7b7;font-weight:700">'+sl+'</span> — 2/3 indicadores alineados al alza, falta 1 para confirmar');
    else if(sl.includes('INMINENTE')&&sl.includes('VENTA')) parts.push('<span style="color:#fca5a5;font-weight:700">'+sl+'</span> — 2/3 indicadores alineados a la baja, falta 1 para confirmar');
    else if(sl.includes('VIRANDO')&&sl.includes('COMPRA')) parts.push('<span style="color:#86efac;font-weight:700">'+sl+'</span> — Indicadores comenzando a girar al alza');
    else if(sl.includes('VIRANDO')&&sl.includes('VENTA')) parts.push('<span style="color:#fda4af;font-weight:700">'+sl+'</span> — Indicadores comenzando a girar a la baja');
    else if(sl.includes('SOBREVENTA')) parts.push('<span style="color:#7dd3fc;font-weight:700">'+sl+'</span> — Accion en zona de sobreventa, posible rebote');
    else if(sl.includes('SOBRECOMPRA')) parts.push('<span style="color:#d8b4fe;font-weight:700">'+sl+'</span> — Accion en zona de sobrecompra, posible correccion');
    else if(c===0) parts.push('<span style="color:var(--dim)">NEUTRAL</span> — Ningun indicador activo');
    else parts.push('<span style="color:var(--hold)">'+sl+'</span> — '+c+'/3 indicadores activos, sin tendencia clara');
  }

  // 2. RSI
  if(rsi!=null&&!isNaN(rsi)){
    if(rsi<25){parts.push('RSI '+rsi.toFixed(0)+' <span style="color:var(--buy)">sobreventa extrema</span>');alerts.push({label:'SOBREVENTA',color:'#34d399'});}
    else if(rsi<30){parts.push('RSI '+rsi.toFixed(0)+' <span style="color:var(--buy)">en sobreventa</span>');alerts.push({label:'SOBREVENTA',color:'#34d399'});}
    else if(rsi<35) parts.push('RSI '+rsi.toFixed(0)+' cerca de sobreventa');
    else if(rsi>80){parts.push('RSI '+rsi.toFixed(0)+' <span style="color:var(--sell)">sobrecompra extrema</span>');alerts.push({label:'SOBRECOMPRA',color:'#f87171'});}
    else if(rsi>70){parts.push('RSI '+rsi.toFixed(0)+' <span style="color:var(--sell)">en sobrecompra</span>');alerts.push({label:'SOBRECOMPRA',color:'#f87171'});}
    else if(rsi>65) parts.push('RSI '+rsi.toFixed(0)+' cerca de sobrecompra');
    else parts.push('RSI '+rsi.toFixed(0)+' neutral');
  }

  // 3. Posicion vs SMA200
  if(mas&&mas.sma200_val&&price>0){
    let pct=(price-mas.sma200_val)/mas.sma200_val*100;
    if(Math.abs(pct)<3){
      let txt=pct>=0?pct.toFixed(1)+'% sobre':Math.abs(pct).toFixed(1)+'% bajo';
      parts.push('<span style="color:var(--accent)">Cerca de SMA200</span> ('+txt+')');
      alerts.push({label:'SMA200',color:'#818cf8'});
    } else if(pct>0) parts.push(pct.toFixed(0)+'% sobre SMA200');
    else parts.push(Math.abs(pct).toFixed(0)+'% bajo SMA200');
  }

  // 4. Death/Golden Cross
  if(mas&&mas.sma50_val&&mas.sma200_val&&mas.sma200_val>0){
    let gap=Math.abs(mas.sma50_val-mas.sma200_val)/mas.sma200_val*100;
    if(gap<2){
      if(mas.sma50_val>mas.sma200_val){
        parts.push('<span style="color:var(--sell)">Death cross inminente</span> (gap '+gap.toFixed(1)+'%)');
        alerts.push({label:'DEATH CROSS',color:'#f87171'});
      } else {
        parts.push('<span style="color:var(--buy)">Golden cross inminente</span> (gap '+gap.toFixed(1)+'%)');
        alerts.push({label:'GOLDEN CROSS',color:'#34d399'});
      }
    }
  }

  // 5. MACD momentum
  if(mh!=null&&!isNaN(mh)){
    if(mh>0) parts.push('MACD positivo');
    else parts.push('MACD negativo');
  }

  return {text:parts.join(' &bull; '),alerts:alerts,hasAlert:alerts.length>0};
}

function drawRsiSpark(canvasId,rsiArr,currentRsi){
  let c=document.getElementById(canvasId);
  if(!c||!rsiArr||rsiArr.length<5)return;
  let ctx=c.getContext('2d');
  let w=c.width=60*2,h=c.height=18*2;
  c.style.width='60px';c.style.height='18px';
  ctx.clearRect(0,0,w,h);
  let vals=rsiArr.slice(-20);
  let n=vals.length;
  ctx.strokeStyle='#ffffff15';ctx.lineWidth=1;ctx.setLineDash([3,3]);
  let y30=h-(30/100)*h,y70=h-(70/100)*h;
  ctx.beginPath();ctx.moveTo(0,y30);ctx.lineTo(w,y30);ctx.stroke();
  ctx.beginPath();ctx.moveTo(0,y70);ctx.lineTo(w,y70);ctx.stroke();
  ctx.setLineDash([]);
  let color=currentRsi<30?'#34d399':(currentRsi>70?'#f87171':'#94a3b8');
  ctx.strokeStyle=color;ctx.lineWidth=2;
  ctx.beginPath();
  for(let i=0;i<n;i++){
    let x=(i/(n-1))*w;
    let y=h-(vals[i]/100)*h;
    if(i===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);
  }
  ctx.stroke();
  let lastY=h-(vals[n-1]/100)*h;
  ctx.fillStyle=color;ctx.beginPath();ctx.arc(w-1,lastY,3,0,Math.PI*2);ctx.fill();
}

function update(){
  // Save scroll position before update
  let scrollY=window.scrollY;
  fetch("/api/data").then(r=>r.json()).then(data=>{
    _data=data;
    document.getElementById("port-info").textContent="Puerto: "+data.port+" ("+(data.port===7497?"PAPER":"LIVE")+")";
    document.getElementById("footer-port").textContent="Puerto: "+data.port;
    document.getElementById("last-update").textContent=data.last_update||"--";
    let next=new Date(Date.now()+REFRESH_MS);
    document.getElementById("next-update").textContent=next.toLocaleTimeString("es-AR",{hour:"2-digit",minute:"2-digit",second:"2-digit"});

    let entries=data.results;
    let buy=0,sell=0,buyNear=0,sellNear=0,turnBuy=0,turnSell=0,zone=0,neutral=0,nodata=0,total=Object.keys(entries).length;
    for(let s in entries){
      let r=entries[s];
      if(!r){nodata++;continue;}
      if(r.signal==='BUY')buy++;
      else if(r.signal==='SELL')sell++;
      else{
        let l=r.signal_label||'';
        if(l.includes('INMINENTE')&&l.includes('COMPRA'))buyNear++;
        else if(l.includes('INMINENTE')&&l.includes('VENTA'))sellNear++;
        else if(l.includes('VIRANDO')&&l.includes('COMPRA'))turnBuy++;
        else if(l.includes('VIRANDO')&&l.includes('VENTA'))turnSell++;
        else if(l.includes('SOBREVENTA')||l.includes('SOBRECOMPRA'))zone++;
        else neutral++;
      }
    }
    let ch='<span class="counter c-total">Total '+total+'</span>';
    if(buy)ch+='<span class="counter c-buy">Compra '+buy+'</span>';
    if(sell)ch+='<span class="counter c-sell">Venta '+sell+'</span>';
    if(buyNear)ch+='<span class="counter c-buy-near">Compra Inminente '+buyNear+'</span>';
    if(sellNear)ch+='<span class="counter c-sell-near">Venta Inminente '+sellNear+'</span>';
    if(turnBuy)ch+='<span class="counter c-turning-buy">Virando a Compra '+turnBuy+'</span>';
    if(turnSell)ch+='<span class="counter c-turning-sell">Virando a Venta '+turnSell+'</span>';
    if(zone)ch+='<span class="counter c-zone">Zona Extrema '+zone+'</span>';
    if(neutral)ch+='<span class="counter c-neutral">Neutral '+neutral+'</span>';
    if(nodata)ch+='<span class="counter c-nodata">Sin datos '+nodata+'</span>';
    document.getElementById("counters").innerHTML=ch;

    renderTop3(data.top3);

    let sorted=sortEntries(entries);
    let openSet=new Set();
    document.querySelectorAll('details[open]').forEach(d=>{if(d.dataset.sym)openSet.add(d.dataset.sym);});
    for(let k in _charts)destroyDetailCharts(k);

    let html="";
    let idx=0;
    for(let sym of sorted){
      let r=entries[sym];
      if(!r){
        let na='<span class="iv v-na">---</span>';
        html+='<details data-sym="'+sym+'" data-idx="'+idx+'"><summary>'+
          '<div class="stock-row">'+
          '<span class="arrow">&#9654;</span><span class="sym">'+sym+'</span>'+
          '<span class="price" style="color:var(--dim)">---</span><span></span>'+na+
          na+na+na+na+na+na+na+na+
          '<span class="cond cond-0">--</span>'+na+na+na+
          '</div></summary>'+
          '<div class="detail-body" style="color:var(--dim)">Sin datos historicos</div></details>';
        idx++;continue;
      }

      let cond=r.conditions_met||0;
      let isOpen=openSet.has(sym);
      let mh=r.values&&r.values.macd?r.values.macd.hist:null;
      let rv=r.values?r.values.rsi:null;
      let km=r.values&&r.values.koncorde?r.values.koncorde.marron:null;
      let mas=r.chart?r.chart.mas:null;

      let ts=buildTechSummary(r);

      html+='<details data-sym="'+sym+'" data-idx="'+idx+'"'+(isOpen?' open':'')+'>';
      html+='<summary><div class="stock-row">'+
        '<span class="arrow">&#9654;</span>'+
        '<span class="sym">'+sym+'</span>'+
        '<span class="price">'+fp(r.price)+'</span>'+
        badge(r.signal,r.signal_label)+fstr(r.strength,r.signal,r)+
        fma(mas,'sma200_val',r.price)+fma(mas,'sma100_val',r.price)+
        fma(mas,'sma50_val',r.price)+fma(mas,'sma20_val',r.price)+
        fma(mas,'ema9_val',r.price)+
        fv(mh,r.macd_ok)+fv(rv,r.rsi_ok)+fv(km,r.konc_ok)+
        '<span class="'+cc(cond)+'">'+cond+'/3</span>'+
        fconf(r.confidence)+fret(r.buy_avg_return)+fret(r.sell_avg_return)+
        '</div>';
      html+='</summary>';

      let curPeriod=_periods[idx]||'1Y';
      html+='<div class="detail-body">';
      // Technical summary line (inside expanded detail only)
      if(ts.text){
        html+='<div class="obs-row" style="margin-bottom:10px">';
        for(let a of ts.alerts){
          html+='<span class="obs-tag" style="background:'+a.color+'18;border-color:'+a.color+'40;color:'+a.color+'">'+a.label;
          if(a.label==='SOBREVENTA'||a.label==='SOBRECOMPRA') html+=' <canvas class="obs-spark" id="spark_'+idx+'"></canvas>';
          html+='</span>';
        }
        html+='<span class="obs-text">'+ts.text+'</span>';
        html+='</div>';
      }
      html+=''+
        '<div class="cond-line"><span class="cond-label" style="color:#7dd3fc">MACD</span><span class="'+(r.macd_ok?'v-ok':'v-no')+'">'+(r.macd_detail||"")+'</span></div>'+
        '<div class="cond-line"><span class="cond-label" style="color:#c084fc">RSI</span><span class="'+(r.rsi_ok?'v-ok':'v-no')+'">'+(r.rsi_detail||"")+'</span></div>'+
        '<div class="cond-line"><span class="cond-label" style="color:#fbbf24">Koncorde</span><span class="'+(r.konc_ok?'v-ok':'v-no')+'">'+(r.konc_detail||"")+'</span></div>'+
        '<div class="bt-line">'+
        '<b>Backtest 5Y</b> &nbsp; Confianza: '+(r.confidence!=null?'<span class="'+(r.confidence>=60?'v-ok':r.confidence>=30?'v-warn':'v-no')+'">'+r.confidence.toFixed(0)+'%</span>':'N/A')+
        ' &nbsp;&bull;&nbsp; Senales: '+(r.buy_count||0)+'B / '+(r.sell_count||0)+'S'+
        ' &nbsp;&bull;&nbsp; Ret.Buy: '+(r.buy_avg_return!=null?'<span class="'+(r.buy_avg_return>=0?'v-ok':'v-no')+'">'+(r.buy_avg_return>=0?'+':'')+r.buy_avg_return.toFixed(1)+'%</span>':'N/A')+
        ' &nbsp;&bull;&nbsp; Ret.Sell: '+(r.sell_avg_return!=null?'<span class="'+(r.sell_avg_return>=0?'v-ok':'v-no')+'">'+(r.sell_avg_return>=0?'+':'')+r.sell_avg_return.toFixed(1)+'%</span>':'N/A')+
        '</div>'+
        '<div style="display:flex;justify-content:space-between;align-items:center;margin:10px 0 4px">'+
        '<div class="ma-legend">'+
        '<span><span class="dot" style="background:#f87171"></span>SMA200</span>'+
        '<span><span class="dot" style="background:#fb923c"></span>SMA100</span>'+
        '<span><span class="dot" style="background:#facc15"></span>SMA50</span>'+
        '<span><span class="dot" style="background:#60a5fa"></span>SMA20</span>'+
        '<span><span class="dot" style="background:#c084fc"></span>EMA9</span></div>'+
        '<div class="period-bar" id="pb_'+idx+'">'+
        '<button class="period-btn'+(curPeriod==='ALL'?' active':'')+'" data-p="ALL" onclick="setPeriod('+idx+',\''+sym+'\',\'ALL\')">ALL</button>'+
        '<button class="period-btn'+(curPeriod==='5Y'?' active':'')+'" data-p="5Y" onclick="setPeriod('+idx+',\''+sym+'\',\'5Y\')">5Y</button>'+
        '<button class="period-btn'+(curPeriod==='1Y'?' active':'')+'" data-p="1Y" onclick="setPeriod('+idx+',\''+sym+'\',\'1Y\')">1Y</button>'+
        '<button class="period-btn'+(curPeriod==='3M'?' active':'')+'" data-p="3M" onclick="setPeriod('+idx+',\''+sym+'\',\'3M\')">3M</button>'+
        '<button class="period-btn'+(curPeriod==='1M'?' active':'')+'" data-p="1M" onclick="setPeriod('+idx+',\''+sym+'\',\'1M\')">1M</button>'+
        '<button class="period-btn'+(curPeriod==='1W'?' active':'')+'" data-p="1W" onclick="setPeriod('+idx+',\''+sym+'\',\'1W\')">1W</button>'+
        '<button class="period-btn'+(curPeriod==='1D'?' active':'')+'" data-p="1D" onclick="setPeriod('+idx+',\''+sym+'\',\'1D\')">1D</button>'+
        '</div></div>'+
        '<div class="candle-box" id="candle_'+idx+'"></div>'+
        '<div class="charts-grid">'+
        '<div class="chart-box"><h4>MACD</h4><canvas id="macd_'+idx+'"></canvas></div>'+
        '<div class="chart-box"><h4>RSI</h4><canvas id="rsi_'+idx+'"></canvas></div>'+
        '<div class="chart-box"><h4>KONCORDE</h4><canvas id="konc_'+idx+'"></canvas></div>'+
        '</div>'+
        '<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--border)"><button onclick="event.stopPropagation();openInOptionsLab(\''+sym+'\')" style="background:rgba(99,102,241,.15);color:#818cf8;border:1px solid rgba(99,102,241,.3);padding:5px 14px;border-radius:5px;font-weight:700;font-size:11px;cursor:pointer;letter-spacing:.3px">&#x2697; OPTIONS LAB</button></div>'+
        '</div></details>';
      idx++;
    }
    document.getElementById("stock-list").innerHTML=html;

    // Draw RSI sparklines for stocks with observations
    let sparkIdx=0;
    for(let sym of sorted){
      let r=entries[sym];
      if(r&&r.chart&&r.chart.rsi){
        let rsi=r.values?r.values.rsi:null;
        if(rsi!=null)drawRsiSpark('spark_'+sparkIdx,r.chart.rsi,rsi);
      }
      sparkIdx++;
    }

    document.querySelectorAll('details[open]').forEach(d=>{
      let i=parseInt(d.dataset.idx);
      renderDetailCharts(i,d.dataset.sym,_periods[i]||'1Y');
    });
    document.querySelectorAll('details').forEach(d=>{
      d.addEventListener('toggle',function(){
        let i=parseInt(this.dataset.idx),s=this.dataset.sym;
        if(this.open)renderDetailCharts(i,s,_periods[i]||'1Y');else destroyDetailCharts(i);
      });
    });
    // Restore scroll position after DOM update
    requestAnimationFrame(()=>{window.scrollTo(0,scrollY);});
  }).catch(err=>console.error("Error:",err));
}

update();
setInterval(update,REFRESH_MS);
// Sub-tabs de cartera y sus helpers removidos (redesign).


// Auto-refresh portfolio every 5 min if active
setInterval(function(){if(_activeTab==='portfolio'&&_portLoaded){_portLoaded=false;loadPortfolio();}},REFRESH_MS);

// ══════════════════════════════════════════════════════════════
//  OPTIONS LAB
// ══════════════════════════════════════════════════════════════

let _olabData=null;

function loadOptionsLab(sym){
  sym=sym||(document.getElementById('olab-symbol-input').value||'').trim().toUpperCase();
  if(!sym){document.getElementById('olab-status').textContent='Ingresa un ticker';return;}
  document.getElementById('olab-symbol-input').value=sym;
  document.getElementById('olab-loading').style.display='';
  document.getElementById('olab-loading').textContent='Analizando opciones para '+sym+'...';
  document.getElementById('olab-status').textContent='';
  document.getElementById('olab-content').style.display='none';
  document.getElementById('olab-multi').style.display='none';

  fetch('/api/options-lab/'+sym)
    .then(r=>r.json())
    .then(d=>{
      document.getElementById('olab-loading').style.display='none';
      if(d.error){document.getElementById('olab-status').textContent=d.error;return;}
      _olabData=d;
      document.getElementById('olab-status').textContent='Analisis de '+sym;
      renderOptionsLab(d);
    })
    .catch(e=>{
      document.getElementById('olab-loading').style.display='';
      document.getElementById('olab-loading').textContent='Error: '+e;
    });
}

function loadOptionsLabTop(){
  document.getElementById('olab-loading').style.display='';
  document.getElementById('olab-loading').textContent='Analizando mejores oportunidades de opciones...';
  document.getElementById('olab-status').textContent='';
  document.getElementById('olab-content').style.display='none';
  document.getElementById('olab-multi').style.display='none';

  fetch('/api/options-lab-top')
    .then(r=>r.json())
    .then(d=>{
      document.getElementById('olab-loading').style.display='none';
      if(!d.opportunities||!d.opportunities.length){
        document.getElementById('olab-loading').style.display='';
        document.getElementById('olab-loading').textContent='No hay oportunidades disponibles aun. El scanner necesita completar al menos un ciclo de analisis.';
        return;
      }
      document.getElementById('olab-status').textContent=d.opportunities.length+' oportunidades encontradas';
      renderOptionsLabMulti(d.opportunities);
    })
    .catch(e=>{
      document.getElementById('olab-loading').style.display='';
      document.getElementById('olab-loading').textContent='Error cargando oportunidades: '+e;
    });
}

function renderOptionsLab(d){
  let el=document.getElementById('olab-content');
  el.style.display='block';

  // Summary
  let sigColor=d.signal==='BUY'?'#10b981':(d.signal==='SELL'?'#ef4444':'#818cf8');
  let sigIcon=d.signal==='BUY'?'&#x25B2;':(d.signal==='SELL'?'&#x25BC;':'&#x25C6;');
  document.getElementById('olab-summary').innerHTML=
    '<div class="olab-summary-icon" style="color:'+sigColor+'">'+sigIcon+'</div>'+
    '<div class="olab-summary-text">'+
      '<h3>'+d.symbol+' &mdash; '+d.signal_label+' (fuerza '+(d.strength||0).toFixed(1)+')</h3>'+
      '<p>'+d.summary+'</p>'+
    '</div>';

  // IV Analysis
  renderIVSection(d);

  // Backtest
  renderBacktest(d);

  // Strategies
  renderStrategies(d);
}

function renderIVSection(d){
  let iv=d.iv_analysis||{};
  let html='<div class="olab-section-title">Analisis de Volatilidad'+
    '<span class="olab-badge" style="background:'+(iv.iv_regime==='high'?'rgba(239,68,68,.15);color:#ef4444':(iv.iv_regime==='low'?'rgba(16,185,129,.15);color:#10b981':'rgba(99,102,241,.15);color:#818cf8'))+'">'+
    'IV '+(iv.iv_regime==='high'?'ALTA':(iv.iv_regime==='low'?'BAJA':'NORMAL'))+'</span></div>';

  html+='<div class="olab-iv-grid">';
  let cards=[
    {label:'IV Estimada',value:iv.estimated_iv?((iv.estimated_iv*100).toFixed(1)+'%'):'--',sub:'Volatilidad implicita'},
    {label:'HV 30d',value:iv.hv_30?((iv.hv_30*100).toFixed(1)+'%'):'--',sub:'Volatilidad historica'},
    {label:'HV 10d',value:iv.hv_10?((iv.hv_10*100).toFixed(1)+'%'):'--',sub:'Vol. corto plazo'},
    {label:'HV 60d',value:iv.hv_60?((iv.hv_60*100).toFixed(1)+'%'):'--',sub:'Vol. largo plazo'},
    {label:'HV Rank',value:iv.hv_rank!=null?(iv.hv_rank.toFixed(0)+'%'):'--',sub:'Percentil vs 1 ano'},
    {label:'IV/HV Ratio',value:iv.iv_vs_hv?(iv.iv_vs_hv.toFixed(2)+'x'):'--',sub:iv.iv_premium!=null?('Prima '+(iv.iv_premium>0?'+':'')+iv.iv_premium.toFixed(0)+'%'):''},
  ];
  for(let c of cards){
    html+='<div class="olab-iv-card"><div class="label">'+c.label+'</div><div class="value">'+c.value+'</div><div class="sub">'+c.sub+'</div></div>';
  }
  html+='</div>';

  document.getElementById('olab-iv-section').innerHTML=html;

  // IV Opportunities
  let opps=d.iv_opportunities||[];
  let oppHtml='';
  if(opps.length){
    oppHtml='<div class="olab-section-title">Oportunidades de Desalineacion IV <span class="olab-badge" style="background:rgba(245,158,11,.15);color:#f59e0b">ALERTA</span></div>';
    for(let o of opps){
      oppHtml+='<div class="olab-iv-alert"><strong>&#x26A0; Desalineacion detectada:</strong> '+o+'</div>';
    }
  }
  document.getElementById('olab-iv-opps').innerHTML=oppHtml;
}

function renderBacktest(d){
  let bt=d.backtest||{};
  let outcomes=bt.outcomes||{};
  let html='<div class="olab-section-title">Backtesting Historico <span class="olab-badge" style="background:rgba(99,102,241,.15);color:#818cf8">'+
    (bt.similar_count||0)+' situaciones similares</span></div>';

  if(bt.current_vs_history){
    html+='<div class="olab-bt-context">'+bt.current_vs_history+'</div>';
  }

  let days=Object.keys(outcomes).sort((a,b)=>+a-+b);
  if(days.length){
    html+='<div class="olab-bt-grid">';
    for(let day of days){
      let o=outcomes[day];
      let retColor=o.avg_return>=0?'#10b981':'#ef4444';
      let wrColor=o.win_rate>=60?'#10b981':(o.win_rate>=45?'#f59e0b':'#ef4444');

      html+='<div class="olab-bt-card">'+
        '<div class="days">'+day+' dias</div>'+
        '<div class="ret" style="color:'+retColor+'">'+(o.avg_return>=0?'+':'')+o.avg_return.toFixed(1)+'%</div>'+
        '<div class="wr" style="color:'+wrColor+'">Win rate: '+o.win_rate.toFixed(0)+'%</div>'+
        '<div class="range">Rango: '+o.worst.toFixed(1)+'% a +'+o.best.toFixed(1)+'%</div>';

      // Mini distribution histogram
      if(o.distribution&&o.distribution.counts){
        let counts=o.distribution.counts;
        let maxC=Math.max(...counts,1);
        html+='<div class="olab-bt-hist">';
        let edges=o.distribution.edges;
        for(let i=0;i<counts.length;i++){
          let h=Math.max(2,counts[i]/maxC*36);
          let midVal=(edges[i]+edges[i+1])/2;
          let barColor=midVal>=0?'#10b981':'#ef4444';
          html+='<div class="olab-bt-bar" style="height:'+h+'px;background:'+barColor+'"></div>';
        }
        html+='</div>';
      }

      // Percentiles
      if(o.percentiles){
        let p=o.percentiles;
        html+='<div style="font-size:9px;color:var(--muted);margin-top:6px">P10:'+p.p10+'% P50:'+p.p50+'% P90:'+p.p90+'%</div>';
      }

      html+='</div>';
    }
    html+='</div>';
  } else {
    html+='<div style="color:var(--muted);font-size:12px;padding:10px 0">No se encontraron suficientes situaciones historicas similares.</div>';
  }

  document.getElementById('olab-backtest').innerHTML=html;
}

function renderStrategies(d){
  let strats=d.strategies||[];
  let html='<div class="olab-section-title">Top 10 Estrategias Recomendadas</div>';
  html+='<div class="olab-strat-list">';

  for(let i=0;i<strats.length;i++){
    let s=strats[i];
    let rank=s.rank||i+1;
    let rankCls=rank===1?'gold':(rank===2?'silver':(rank===3?'bronze':'normal'));

    let scoreColor=s.score>=70?'#10b981':(s.score>=50?'#f59e0b':(s.score>=30?'#818cf8':'#ef4444'));
    let biasCls=s.bias==='bullish'?'bullish':(s.bias==='bearish'?'bearish':'neutral');
    let biasLabel=s.bias==='bullish'?'ALCISTA':(s.bias==='bearish'?'BAJISTA':'NEUTRAL');

    let mpColor=s.max_profit>=0?'#10b981':'#ef4444';
    let mlColor='#ef4444';

    html+='<div class="olab-strat-card" id="olab-strat-'+i+'">'+
      '<div class="olab-strat-header" onclick="toggleOlabStrat('+i+')">'+
        '<div class="olab-strat-rank '+rankCls+'">'+rank+'</div>'+
        '<div class="olab-strat-info">'+
          '<div class="olab-strat-name">'+s.name+' <span class="olab-bias '+biasCls+'">'+biasLabel+'</span>'+
            (s.dte?' <span style="font-size:10px;color:var(--muted);font-weight:400">'+s.dte+'d</span>':'')+
          '</div>'+
          '<div class="olab-strat-desc">'+s.description+'</div>'+
        '</div>'+
        '<div class="olab-strat-metrics">'+
          '<div class="olab-strat-metric"><div class="val" style="color:'+mpColor+'">$'+(s.max_profit>=0?'+':'')+s.max_profit.toFixed(0)+'</div><div class="lbl">Max Profit</div></div>'+
          '<div class="olab-strat-metric"><div class="val" style="color:'+mlColor+'">$'+s.max_loss.toFixed(0)+'</div><div class="lbl">Max Loss</div></div>'+
          '<div class="olab-strat-metric"><div class="val">'+s.prob_profit.toFixed(0)+'%</div><div class="lbl">Prob. Profit</div></div>'+
          '<div class="olab-strat-metric"><div class="val">'+s.risk_reward.toFixed(1)+'x</div><div class="lbl">R/R</div></div>'+
        '</div>'+
        '<div class="olab-strat-score" style="background:'+scoreColor+'22;color:'+scoreColor+';border:2px solid '+scoreColor+'">'+s.score.toFixed(0)+'</div>'+
        '<div class="olab-strat-expand">&#x25BC;</div>'+
      '</div>'+
      '<div class="olab-strat-body">'+renderStrategyDetail(s,i)+'</div>'+
    '</div>';
  }
  html+='</div>';
  document.getElementById('olab-strategies').innerHTML=html;
}

function renderStrategyDetail(s,idx){
  let html='<div class="olab-detail-grid">';

  // Left: Payoff chart
  html+='<div>'+
    '<div style="font-size:11px;font-weight:700;color:var(--text);margin-bottom:8px">Diagrama P&L al Vencimiento</div>'+
    '<div class="olab-payoff-chart"><canvas id="payoff-canvas-'+idx+'" class="olab-payoff-canvas"></canvas></div>'+
    '<div style="display:flex;gap:16px;margin-top:8px;flex-wrap:wrap">';
  if(s.breakevens&&s.breakevens.length){
    html+='<div style="font-size:10px;color:var(--muted)">Breakeven: '+s.breakevens.map(b=>'$'+b.toFixed(2)).join(', ')+'</div>';
  }
  html+='<div style="font-size:10px;color:var(--muted)">Capital: $'+s.capital_required.toFixed(0)+'</div>';
  html+='<div style="font-size:10px;color:var(--muted)">Prima neta: $'+(s.net_premium>=0?'+':'')+s.net_premium.toFixed(0)+'</div>';
  html+='</div>';

  if(s.iv_edge){
    html+='<div style="font-size:10px;color:#f59e0b;margin-top:6px">&#x26A0; '+s.iv_edge+'</div>';
  }
  html+='</div>';

  // Right: Greeks + Legs
  html+='<div>';
  html+='<div style="font-size:11px;font-weight:700;color:var(--text);margin-bottom:8px">Griegas Agregadas</div>';
  let g=s.greeks_agg||{};
  html+='<table class="olab-greeks-table"><thead><tr><th>Griega</th><th>Valor</th><th>Significado</th></tr></thead><tbody>';
  html+='<tr><td style="font-weight:700">Delta</td><td>'+(g.delta||0).toFixed(3)+'</td><td style="color:var(--muted)">'+deltaExplain(g.delta)+'</td></tr>';
  html+='<tr><td style="font-weight:700">Gamma</td><td>'+(g.gamma||0).toFixed(4)+'</td><td style="color:var(--muted)">Aceleracion del delta</td></tr>';
  html+='<tr><td style="font-weight:700">Theta</td><td style="color:'+(g.theta>=0?'#10b981':'#ef4444')+'">'+(g.theta||0).toFixed(3)+'</td><td style="color:var(--muted)">'+(g.theta>=0?'Gana':'Pierde')+' $'+Math.abs((g.theta||0)*100).toFixed(0)+'/dia</td></tr>';
  html+='<tr><td style="font-weight:700">Vega</td><td>'+(g.vega||0).toFixed(3)+'</td><td style="color:var(--muted)">'+(g.vega>=0?'Beneficia':'Perjudica')+' si IV sube</td></tr>';
  html+='<tr><td style="font-weight:700">Rho</td><td>'+(g.rho||0).toFixed(3)+'</td><td style="color:var(--muted)">Sensibilidad a tasa</td></tr>';
  html+='</tbody></table>';

  // Legs table
  if(s.legs&&s.legs.length){
    html+='<div style="font-size:11px;font-weight:700;color:var(--text);margin:12px 0 6px">Patas de la Estrategia</div>';
    html+='<table class="olab-legs-table"><thead><tr><th>Accion</th><th>Tipo</th><th>Strike</th><th>Prima</th><th>Delta</th><th>Qty</th></tr></thead><tbody>';
    for(let leg of s.legs){
      let ac=leg.action==='BUY'?'<span style="color:#10b981">COMPRA</span>':'<span style="color:#ef4444">VENTA</span>';
      let tipo=leg.right==='C'?'Call':'Put';
      html+='<tr><td>'+ac+'</td><td>'+tipo+'</td><td>$'+leg.strike.toFixed(0)+'</td><td>$'+leg.premium.toFixed(2)+'</td><td>'+(leg.greeks_data.delta||0).toFixed(3)+'</td><td>'+leg.qty+'</td></tr>';
    }
    html+='</tbody></table>';
  }
  html+='</div></div>';

  return html;
}

function deltaExplain(d){
  if(d==null) return '';
  let abs=Math.abs(d);
  if(abs>0.7) return 'Muy direccional';
  if(abs>0.3) return 'Direccional moderado';
  return 'Baja direccionalidad';
}

function toggleOlabStrat(idx){
  let card=document.getElementById('olab-strat-'+idx);
  let wasOpen=card.classList.contains('open');
  card.classList.toggle('open');
  if(!wasOpen){
    setTimeout(()=>drawPayoffChart(idx),50);
  }
}

function drawPayoffChart(idx){
  let s=(_olabData||{}).strategies||[];
  if(idx>=s.length) return;
  let strat=s[idx];
  let points=strat.payoff_points||[];
  if(!points.length) return;

  let canvas=document.getElementById('payoff-canvas-'+idx);
  if(!canvas) return;
  let ctx=canvas.getContext('2d');
  let W=canvas.parentElement.clientWidth-24;
  let H=200;
  canvas.width=W*2;canvas.height=H*2;
  canvas.style.width=W+'px';canvas.style.height=H+'px';
  ctx.scale(2,2);

  let prices=points.map(p=>p.price);
  let pnls=points.map(p=>p.pnl);
  let minP=Math.min(...pnls);
  let maxP=Math.max(...pnls);
  let range=Math.max(maxP-minP,1);
  let pad=range*0.1;
  minP-=pad;maxP+=pad;

  let minX=Math.min(...prices);
  let maxX=Math.max(...prices);

  function x(v){return (v-minX)/(maxX-minX)*W;}
  function y(v){return H-(v-minP)/(maxP-minP)*H;}

  // Grid
  ctx.strokeStyle='rgba(255,255,255,0.06)';
  ctx.lineWidth=0.5;
  for(let i=0;i<=4;i++){
    let yy=H*i/4;
    ctx.beginPath();ctx.moveTo(0,yy);ctx.lineTo(W,yy);ctx.stroke();
  }

  // Zero line
  let y0=y(0);
  ctx.strokeStyle='rgba(255,255,255,0.2)';
  ctx.setLineDash([4,4]);
  ctx.beginPath();ctx.moveTo(0,y0);ctx.lineTo(W,y0);ctx.stroke();
  ctx.setLineDash([]);

  // Current price line
  let cp=(_olabData||{}).price||0;
  if(cp>minX&&cp<maxX){
    ctx.strokeStyle='rgba(99,102,241,0.4)';
    ctx.setLineDash([3,3]);
    ctx.beginPath();ctx.moveTo(x(cp),0);ctx.lineTo(x(cp),H);ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle='rgba(99,102,241,0.6)';
    ctx.font='9px sans-serif';
    ctx.fillText('Precio actual',x(cp)+3,12);
  }

  // Profit area (green) and loss area (red)
  // Fill profit
  ctx.beginPath();
  ctx.moveTo(x(prices[0]),y0);
  for(let i=0;i<points.length;i++){
    let px=x(prices[i]),py=y(Math.max(pnls[i],0));
    if(i===0) ctx.lineTo(px,py);
    else ctx.lineTo(px,py);
  }
  ctx.lineTo(x(prices[prices.length-1]),y0);
  ctx.closePath();
  ctx.fillStyle='rgba(16,185,129,0.12)';
  ctx.fill();

  // Fill loss
  ctx.beginPath();
  ctx.moveTo(x(prices[0]),y0);
  for(let i=0;i<points.length;i++){
    let px=x(prices[i]),py=y(Math.min(pnls[i],0));
    if(i===0) ctx.lineTo(px,py);
    else ctx.lineTo(px,py);
  }
  ctx.lineTo(x(prices[prices.length-1]),y0);
  ctx.closePath();
  ctx.fillStyle='rgba(239,68,68,0.12)';
  ctx.fill();

  // P&L line
  ctx.beginPath();
  for(let i=0;i<points.length;i++){
    let px=x(prices[i]),py=y(pnls[i]);
    if(i===0) ctx.moveTo(px,py);
    else ctx.lineTo(px,py);
  }
  ctx.strokeStyle='#e2e8f0';
  ctx.lineWidth=2;
  ctx.stroke();

  // Breakevens
  if(strat.breakevens){
    for(let bk of strat.breakevens){
      if(bk>=minX&&bk<=maxX){
        ctx.fillStyle='#f59e0b';
        ctx.beginPath();ctx.arc(x(bk),y0,4,0,Math.PI*2);ctx.fill();
        ctx.font='9px sans-serif';
        ctx.fillText('BE $'+bk.toFixed(0),x(bk)-12,y0-8);
      }
    }
  }

  // Labels
  ctx.fillStyle='rgba(255,255,255,0.4)';
  ctx.font='9px sans-serif';
  ctx.textAlign='left';
  ctx.fillText('$'+minX.toFixed(0),2,H-4);
  ctx.textAlign='right';
  ctx.fillText('$'+maxX.toFixed(0),W-2,H-4);
  ctx.textAlign='left';

  // Max profit label
  ctx.fillStyle='#10b981';
  ctx.fillText('Max +$'+strat.max_profit.toFixed(0),4,y(maxP)+12);
  ctx.fillStyle='#ef4444';
  ctx.fillText('Max $'+strat.max_loss.toFixed(0),4,y(minP)-4);
}

// Multi-symbol rendering
function renderOptionsLabMulti(opportunities){
  let el=document.getElementById('olab-multi');
  el.style.display='block';
  document.getElementById('olab-content').style.display='none';

  let html='';
  for(let i=0;i<opportunities.length;i++){
    let d=opportunities[i];
    let sigColor=d.signal==='BUY'?'#10b981':(d.signal==='SELL'?'#ef4444':'#818cf8');
    let iv=d.iv_analysis||{};
    let topStrat=(d.strategies||[])[0];
    let biasCls=topStrat?(topStrat.bias==='bullish'?'bullish':(topStrat.bias==='bearish'?'bearish':'neutral')):'neutral';

    html+='<div class="olab-multi-card" id="olab-multi-'+i+'">'+
      '<div class="olab-multi-header" onclick="toggleOlabMulti('+i+',\''+d.symbol+'\')">'+
        '<span style="font-size:18px;font-weight:900;color:'+sigColor+'">'+d.symbol+'</span>'+
        '<span style="font-size:12px;color:var(--muted)">$'+(d.price||0).toFixed(2)+'</span>'+
        '<span class="olab-bias '+biasCls+'" style="font-size:9px">'+(d.signal_label||d.signal)+'</span>'+
        '<span style="font-size:11px;color:var(--muted);margin-left:auto">'+
          'IV: '+(iv.estimated_iv?((iv.estimated_iv*100).toFixed(0)+'%'):'--')+
          ' | Score: '+(d.stock_score||0).toFixed(0)+
          ' | Top: '+(topStrat?topStrat.name:'--')+
          ' ('+((topStrat||{}).prob_profit||0).toFixed(0)+'% prob)'+
        '</span>'+
        '<span style="color:var(--muted);font-size:16px">&#x25BC;</span>'+
      '</div>'+
      '<div class="olab-multi-body" id="olab-multi-body-'+i+'"></div>'+
    '</div>';
  }
  el.innerHTML=html;

  // Store for lazy rendering
  el._opportunities=opportunities;
}

function toggleOlabMulti(idx,sym){
  let card=document.getElementById('olab-multi-'+idx);
  let wasOpen=card.classList.contains('open');
  card.classList.toggle('open');
  if(!wasOpen){
    let body=document.getElementById('olab-multi-body-'+idx);
    let el=document.getElementById('olab-multi');
    let d=el._opportunities[idx];
    _olabData=d;

    // Build scoped content directly
    let html='';

    // IV section
    let iv=d.iv_analysis||{};
    html+='<div class="olab-section"><div class="olab-section-title">Volatilidad'+
      '<span class="olab-badge" style="background:'+(iv.iv_regime==='high'?'rgba(239,68,68,.15);color:#ef4444':(iv.iv_regime==='low'?'rgba(16,185,129,.15);color:#10b981':'rgba(99,102,241,.15);color:#818cf8'))+'">'+
      'IV '+(iv.iv_regime==='high'?'ALTA':(iv.iv_regime==='low'?'BAJA':'NORMAL'))+'</span></div>';
    html+='<div class="olab-iv-grid">';
    let cards=[
      {label:'IV Est.',value:iv.estimated_iv?((iv.estimated_iv*100).toFixed(1)+'%'):'--'},
      {label:'HV 30d',value:iv.hv_30?((iv.hv_30*100).toFixed(1)+'%'):'--'},
      {label:'HV Rank',value:iv.hv_rank!=null?(iv.hv_rank.toFixed(0)+'%'):'--'},
    ];
    for(let c of cards) html+='<div class="olab-iv-card"><div class="label">'+c.label+'</div><div class="value">'+c.value+'</div></div>';
    html+='</div></div>';

    // IV opps
    let opps=d.iv_opportunities||[];
    if(opps.length){
      html+='<div class="olab-section">';
      for(let o of opps) html+='<div class="olab-iv-alert"><strong>&#x26A0;</strong> '+o+'</div>';
      html+='</div>';
    }

    // Top 5 strategies inline
    let strats=(d.strategies||[]).slice(0,5);
    html+='<div class="olab-section"><div class="olab-section-title">Top 5 Estrategias</div>';
    html+='<table style="width:100%;border-collapse:collapse;font-size:11px"><thead><tr>'+
      '<th style="text-align:left;padding:6px 8px;color:var(--muted);border-bottom:1px solid var(--border)">#</th>'+
      '<th style="text-align:left;padding:6px 8px;color:var(--muted);border-bottom:1px solid var(--border)">Estrategia</th>'+
      '<th style="text-align:center;padding:6px 8px;color:var(--muted);border-bottom:1px solid var(--border)">Sesgo</th>'+
      '<th style="text-align:right;padding:6px 8px;color:var(--muted);border-bottom:1px solid var(--border)">Max Profit</th>'+
      '<th style="text-align:right;padding:6px 8px;color:var(--muted);border-bottom:1px solid var(--border)">Max Loss</th>'+
      '<th style="text-align:right;padding:6px 8px;color:var(--muted);border-bottom:1px solid var(--border)">Prob.</th>'+
      '<th style="text-align:right;padding:6px 8px;color:var(--muted);border-bottom:1px solid var(--border)">R/R</th>'+
      '<th style="text-align:right;padding:6px 8px;color:var(--muted);border-bottom:1px solid var(--border)">Score</th>'+
      '</tr></thead><tbody>';
    for(let s of strats){
      let biasCls=s.bias==='bullish'?'bullish':(s.bias==='bearish'?'bearish':'neutral');
      let biasLbl=s.bias==='bullish'?'ALC':(s.bias==='bearish'?'BAJ':'NEU');
      html+='<tr>'+
        '<td style="padding:5px 8px;border-bottom:1px solid var(--border)22;font-weight:700">'+(s.rank||'')+'</td>'+
        '<td style="padding:5px 8px;border-bottom:1px solid var(--border)22">'+s.name+' <span style="color:var(--muted)">'+s.dte+'d</span></td>'+
        '<td style="padding:5px 8px;border-bottom:1px solid var(--border)22;text-align:center"><span class="olab-bias '+biasCls+'">'+biasLbl+'</span></td>'+
        '<td style="padding:5px 8px;border-bottom:1px solid var(--border)22;text-align:right;color:#10b981">$'+s.max_profit.toFixed(0)+'</td>'+
        '<td style="padding:5px 8px;border-bottom:1px solid var(--border)22;text-align:right;color:#ef4444">$'+s.max_loss.toFixed(0)+'</td>'+
        '<td style="padding:5px 8px;border-bottom:1px solid var(--border)22;text-align:right">'+s.prob_profit.toFixed(0)+'%</td>'+
        '<td style="padding:5px 8px;border-bottom:1px solid var(--border)22;text-align:right">'+s.risk_reward.toFixed(1)+'x</td>'+
        '<td style="padding:5px 8px;border-bottom:1px solid var(--border)22;text-align:right;font-weight:700">'+s.score.toFixed(0)+'</td>'+
      '</tr>';
    }
    html+='</tbody></table>';
    html+='<div style="margin-top:8px"><button onclick="switchTab(\'optionslab\');loadOptionsLab(\''+d.symbol+'\')" style="background:var(--accent);color:#000;border:none;padding:6px 14px;border-radius:5px;font-weight:700;font-size:11px;cursor:pointer">Ver analisis completo</button></div>';
    html+='</div>';

    body.innerHTML=html;
  }
}

// Hook: clicking a symbol in the scanner opens it in Options Lab
function openInOptionsLab(sym){
  switchTab('optionslab');
  loadOptionsLab(sym);
}

/* ═══════════════════════════════════════════════════════════
   TRADES HISTORY
   ═══════════════════════════════════════════════════════════ */
function loadTradesHistory(){
  document.getElementById('th-loading').style.display='';
  document.getElementById('th-content').style.display='none';
  fetch('/api/trades-history').then(r=>r.json()).then(data=>{
    _thData=data;
    document.getElementById('th-loading').style.display='none';
    document.getElementById('th-content').style.display='';
    renderThSummary(data.summary);
    renderThList(data.trades);
  }).catch(e=>{
    document.getElementById('th-loading').innerHTML='<span style="color:var(--sell)">Error cargando trades: '+e.message+'</span>';
  });
}

function renderThSummary(s){
  if(!s)return;
  let el=document.getElementById('th-summary');
  let wrColor=s.win_rate>=60?'color:var(--buy)':s.win_rate>=40?'color:var(--hold)':'color:var(--sell)';
  let pnlColor=s.total_pnl>=0?'color:var(--buy)':'color:var(--sell)';
  let h='';
  h+='<div class="th-card"><div class="label">Total Trades</div><div class="value">'+s.total_trades+'</div><div class="sub">'+s.stocks_count+' acciones, '+s.options_count+' opciones</div></div>';
  h+='<div class="th-card"><div class="label">Win Rate</div><div class="value" style="'+wrColor+'">'+s.win_rate+'%</div><div class="sub">'+s.wins+'W / '+s.losses+'L</div></div>';
  h+='<div class="th-card"><div class="label">P&L Total</div><div class="value" style="'+pnlColor+'">$'+fmtN(s.total_pnl)+'</div><div class="sub">Comisiones: $'+fmtN(s.total_commissions)+'</div></div>';
  h+='<div class="th-card"><div class="label">Retorno Promedio</div><div class="value" style="'+(s.avg_return_pct>=0?'color:var(--buy)':'color:var(--sell)')+'">'+s.avg_return_pct.toFixed(1)+'%</div><div class="sub">Duracion prom: '+s.avg_duration_days+'d</div></div>';
  if(s.best_trade){
    h+='<div class="th-card"><div class="label">Mejor Trade</div><div class="value" style="color:var(--buy)">'+s.best_trade.symbol+'</div><div class="sub">+$'+fmtN(s.best_trade.pnl)+' (+'+s.best_trade.pnl_pct.toFixed(1)+'%)</div></div>';
  }
  if(s.worst_trade){
    h+='<div class="th-card"><div class="label">Peor Trade</div><div class="value" style="color:var(--sell)">'+s.worst_trade.symbol+'</div><div class="sub">$'+fmtN(s.worst_trade.pnl)+' ('+s.worst_trade.pnl_pct.toFixed(1)+'%)</div></div>';
  }
  el.innerHTML=h;
}

function filterTrades(f){
  _thFilter=f;
  document.querySelectorAll('.th-filter-btn').forEach(b=>b.classList.remove('active'));
  document.querySelector('.th-filter-btn[onclick*="\''+f+'\'"]').classList.add('active');
  if(_thData)renderThList(_thData.trades);
}

function _thFilterMatch(t,f){
  if(f==='all')return true;
  if(f==='stk')return t.type==='STK';
  if(f==='opt')return t.type==='OPT'||t.type==='SPREAD';
  if(f==='win')return t.result==='WIN';
  if(f==='loss')return t.result==='LOSS';
  return true;
}

function renderThList(trades){
  let el=document.getElementById('th-list');
  // Destroy old charts
  Object.keys(_thCharts).forEach(k=>{
    if(_thCharts[k].lw)_thCharts[k].lw.remove();
    if(_thCharts[k].macd)_thCharts[k].macd.destroy();
    if(_thCharts[k].rsi)_thCharts[k].rsi.destroy();
    if(_thCharts[k].konc)_thCharts[k].konc.destroy();
  });
  _thCharts={};

  let filtered=trades.filter(t=>_thFilterMatch(t,_thFilter));
  if(filtered.length===0){
    el.innerHTML='<div style="text-align:center;color:var(--muted);padding:40px">No hay trades que coincidan con el filtro.</div>';
    return;
  }

  let h='';
  filtered.forEach((t,i)=>{
    let pnlColor=t.pnl>=0?'var(--buy)':'var(--sell)';
    let pnlSign=t.pnl>=0?'+':'';
    let typeBadge=t.type==='STK'?'stk':t.type==='SPREAD'?'spread':'opt';
    let optDetail=t.option_detail?'<span class="th-trade-badge '+typeBadge+'" style="font-size:9px;padding:2px 7px">'+t.option_detail+'</span>':'';

    h+='<div class="th-trade" id="th-trade-'+i+'">';
    h+='<div class="th-trade-header" onclick="toggleThTrade('+i+')">';
    h+='<span class="th-trade-sym">'+t.symbol+'</span>';
    h+='<span class="th-trade-badge '+typeBadge+'">'+t.type+'</span>';
    if(t.estimated_entry)h+='<span class="th-trade-badge estimated">ENTRADA EST.</span>';
    h+=optDetail;
    h+='<span class="th-trade-badge '+(t.result==='WIN'?'win':'loss')+'">'+t.result+'</span>';
    h+='<span class="th-trade-dates">'+t.entry_date+' &rarr; '+t.exit_date+' ('+t.duration_days+'d)</span>';
    h+='<div class="th-trade-metrics">';
    h+='<div class="th-trade-metric"><div class="val" style="color:'+pnlColor+'">'+pnlSign+'$'+fmtN(Math.abs(t.pnl))+'</div><div class="lbl">P&L</div></div>';
    h+='<div class="th-trade-metric"><div class="val" style="color:'+pnlColor+'">'+pnlSign+t.pnl_pct.toFixed(1)+'%</div><div class="lbl">Retorno</div></div>';
    h+='</div>';
    h+='<span class="th-trade-expand">&#9662;</span>';
    h+='</div>';

    // Body (hidden until toggled)
    h+='<div class="th-trade-body" id="th-body-'+i+'">';

    // Trade summary row
    h+='<div class="th-detail-grid" style="margin-top:14px">';

    // Left: trade details
    h+='<div>';
    h+='<div class="th-detail-section">';
    h+='<div class="th-detail-title">Detalle del Trade</div>';
    h+='<table class="th-fills-table"><tbody>';
    h+='<tr><td style="color:var(--muted)">Precio Entrada</td><td style="font-weight:700">$'+t.entry_price.toFixed(2)+(t.estimated_entry?' (est.)':'')+'</td></tr>';
    h+='<tr><td style="color:var(--muted)">Precio Salida</td><td style="font-weight:700">$'+t.exit_price.toFixed(2)+'</td></tr>';
    h+='<tr><td style="color:var(--muted)">Cantidad</td><td>'+t.quantity+(t.type!=='STK'?' contratos':' acciones')+'</td></tr>';
    h+='<tr><td style="color:var(--muted)">Monto Invertido</td><td>$'+fmtN(t.invested)+'</td></tr>';
    h+='<tr><td style="color:var(--muted)">Comisiones</td><td>$'+t.commissions.toFixed(2)+'</td></tr>';
    h+='<tr><td style="color:var(--muted)">P&L Neto</td><td style="color:'+pnlColor+';font-weight:700">'+pnlSign+'$'+fmtN(Math.abs(t.pnl))+'</td></tr>';
    h+='</tbody></table>';
    h+='</div>';

    // Fills breakdown
    if(t.buy_fills&&t.buy_fills.length>0){
      h+='<div class="th-detail-section">';
      h+='<div class="th-detail-title">Compras</div>';
      h+='<table class="th-fills-table"><thead><tr><th>Fecha</th><th>Qty</th><th>Precio</th></tr></thead><tbody>';
      t.buy_fills.forEach(f=>{
        h+='<tr><td>'+f.date+'</td><td>'+f.qty+'</td><td>$'+f.price.toFixed(2)+'</td></tr>';
      });
      h+='</tbody></table></div>';
    }
    if(t.sell_fills&&t.sell_fills.length>0){
      h+='<div class="th-detail-section">';
      h+='<div class="th-detail-title">Ventas</div>';
      h+='<table class="th-fills-table"><thead><tr><th>Fecha</th><th>Qty</th><th>Precio</th></tr></thead><tbody>';
      t.sell_fills.forEach(f=>{
        h+='<tr><td>'+f.date+'</td><td>'+f.qty+'</td><td>$'+f.price.toFixed(2)+'</td></tr>';
      });
      h+='</tbody></table></div>';
    }
    h+='</div>';

    // Right: thesis + context (loaded on demand)
    h+='<div>';
    h+='<div class="th-detail-section" id="th-thesis-'+i+'">';
    h+='<div class="th-detail-title">Tesis de Entrada</div>';
    h+='<div class="th-detail-text" id="th-entry-thesis-'+i+'" style="color:var(--muted);font-style:italic">Se cargara al expandir...</div>';
    h+='</div>';
    h+='<div class="th-detail-section">';
    h+='<div class="th-detail-title">Tesis de Salida</div>';
    h+='<div class="th-detail-text" id="th-exit-thesis-'+i+'" style="color:var(--muted);font-style:italic">Se cargara al expandir...</div>';
    h+='</div>';
    h+='<div id="th-context-'+i+'"></div>';
    h+='<div id="th-lessons-'+i+'"></div>';
    h+='</div>';

    h+='</div>';

    // Chart (loaded on demand)
    h+='<div class="th-chart-row">';
    h+='<div class="th-chart-container" id="th-candle-'+i+'" style="min-height:310px"></div>';
    h+='</div>';
    h+='<div class="th-ind-charts">';
    h+='<div class="th-ind-chart"><div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px;font-weight:600">MACD</div><canvas id="th-macd-'+i+'"></canvas></div>';
    h+='<div class="th-ind-chart"><div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px;font-weight:600">RSI</div><canvas id="th-rsi-'+i+'"></canvas></div>';
    h+='<div class="th-ind-chart"><div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px;font-weight:600">Koncorde</div><canvas id="th-konc-'+i+'"></canvas></div>';
    h+='</div>';

    h+='</div>';  // th-trade-body
    h+='</div>';  // th-trade
  });
  el.innerHTML=h;
}

function toggleThTrade(idx){
  let card=document.getElementById('th-trade-'+idx);
  if(!card)return;
  let wasOpen=card.classList.contains('open');
  card.classList.toggle('open');

  if(!wasOpen&&!_thCharts[idx]){
    // Load chart data on first expand
    let filtered=(_thData?_thData.trades:[]).filter(t=>_thFilterMatch(t,_thFilter));
    let trade=filtered[idx];
    if(!trade)return;
    _thCharts[idx]={loading:true};
    let candleEl=document.getElementById('th-candle-'+idx);
    if(candleEl)candleEl.innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted);font-size:13px;padding:40px">Cargando grafico y analisis...</div>';

    fetch('/api/trades-history/chart/'+encodeURIComponent(trade.id)+'?symbol='+trade.symbol+'&entry='+trade.entry_date+'&exit='+trade.exit_date)
      .then(r=>r.json())
      .then(chart=>{
        if(chart.error){
          if(candleEl)candleEl.innerHTML='<div style="padding:20px;color:var(--muted);text-align:center">'+chart.error+'</div>';
          return;
        }
        _renderThTradeChart(idx,trade,chart);
      }).catch(e=>{
        if(candleEl)candleEl.innerHTML='<div style="padding:20px;color:var(--sell);text-align:center">Error: '+e.message+'</div>';
      });
  }
}

function _renderThTradeChart(idx,trade,chart){
  let entry={lw:null,macd:null,rsi:null,konc:null};

  // Candlestick
  let candleEl=document.getElementById('th-candle-'+idx);
  if(candleEl&&chart.ohlc&&chart.ohlc.length>0){
    candleEl.innerHTML='';
    let lwChart=_createLW(candleEl,false);
    lwChart.applyOptions({height:310});
    let cs=lwChart.addCandlestickSeries({upColor:'#34d399',downColor:'#f87171',borderUpColor:'#34d399',borderDownColor:'#f87171',wickUpColor:'#34d39988',wickDownColor:'#f8717188'});
    cs.setData(chart.ohlc);

    // Add MAs
    if(chart.mas){
      let maColors={"sma50":"#facc15","sma20":"#60a5fa"};
      for(let name of["sma50","sma20"]){
        let vals=chart.mas[name];if(!vals||vals.length===0)continue;
        let line=lwChart.addLineSeries({color:maColors[name],lineWidth:1,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false});
        let ld=[];
        for(let i=0;i<chart.ohlc.length;i++){
          if(i>=vals.length||vals[i]==null||isNaN(vals[i]))continue;
          ld.push({time:chart.ohlc[i].time,value:vals[i]});
        }
        if(ld.length>0)line.setData(ld);
      }
    }

    // Buy/Sell markers
    let markers=[];
    // Find entry price at entry_date
    if(trade.entry_date){
      markers.push({
        time:trade.entry_date,
        position:'belowBar',
        color:'#34d399',
        shape:'arrowUp',
        text:'BUY $'+trade.entry_price.toFixed(2)
      });
    }
    // Sell markers for each sell fill
    if(trade.sell_fills){
      trade.sell_fills.forEach(sf=>{
        markers.push({
          time:sf.date,
          position:'aboveBar',
          color:'#f87171',
          shape:'arrowDown',
          text:'SELL $'+sf.price.toFixed(2)
        });
      });
    }
    if(markers.length>0){
      markers.sort((a,b)=>a.time<b.time?-1:1);
      cs.setMarkers(markers);
    }

    // Entry price line
    cs.createPriceLine({price:trade.entry_price,color:'#93c5fd',lineWidth:1,lineStyle:LightweightCharts.LineStyle.Dashed,axisLabelVisible:true,title:'Entrada'});
    if(trade.sell_fills&&trade.sell_fills.length>0){
      let lastSell=trade.sell_fills[trade.sell_fills.length-1];
      cs.createPriceLine({price:lastSell.price,color:trade.pnl>=0?'#34d399':'#f87171',lineWidth:1,lineStyle:LightweightCharts.LineStyle.Dashed,axisLabelVisible:true,title:'Salida'});
    }

    lwChart.timeScale().fitContent();
    entry.lw=lwChart;

    let ro=new ResizeObserver(()=>{lwChart.applyOptions({width:candleEl.clientWidth});});
    ro.observe(candleEl);
    entry.ro=ro;
  }

  // Build entry/exit vertical-line plugin for indicator charts
  let _thMarkerIndices=[];
  if(chart.dates){
    let ed=trade.entry_date,sellDates=trade.sell_fills?trade.sell_fills.map(sf=>sf.date):[];
    chart.dates.forEach((d,i)=>{
      if(d===ed)_thMarkerIndices.push({idx:i,type:'BUY',color:'#34d399'});
      if(sellDates.indexOf(d)!==-1)_thMarkerIndices.push({idx:i,type:'SELL',color:'#f87171'});
    });
  }
  let _thVLinePlugin={id:'thVLines',afterDraw(ch){
    let{ctx:cx,chartArea:a,scales}=ch;if(!a)return;let x=scales.x;
    cx.save();
    _thMarkerIndices.forEach(m=>{
      let px=x.getPixelForValue(m.idx);
      cx.strokeStyle=m.color;cx.lineWidth=1.5;cx.setLineDash([5,3]);
      cx.beginPath();cx.moveTo(px,a.top);cx.lineTo(px,a.bottom);cx.stroke();
      cx.setLineDash([]);
      cx.fillStyle=m.color;cx.font='bold 9px sans-serif';cx.textAlign='center';
      cx.fillText(m.type,px,a.top-4);
    });
    cx.restore();
  }};

  // MACD chart
  if(chart.macd&&chart.dates){
    let ctx=document.getElementById('th-macd-'+idx);
    if(ctx){
      let labels=chart.dates.map(d=>d.length>7?d.substring(5):d);
      let colors=chart.macd.hist.map(v=>v>=0?'#10b981':'#ef4444');
      entry.macd=new Chart(ctx,{type:'bar',data:{labels:labels,datasets:[
        {type:'bar',data:chart.macd.hist,backgroundColor:colors,borderWidth:0,barPercentage:.8,order:2},
        {type:'line',data:chart.macd.macd,borderColor:'#7dd3fc',borderWidth:1.5,pointRadius:0,fill:false,order:1},
        {type:'line',data:chart.macd.signal,borderColor:'#fb923c',borderWidth:1.5,pointRadius:0,fill:false,order:1}
      ]},options:{responsive:true,maintainAspectRatio:false,animation:false,
        layout:{padding:{top:14}},
        plugins:{legend:{display:false}},
        scales:{x:{ticks:{color:'#8896a8',font:{size:7},maxTicksLimit:6},grid:{color:'#2a2a4a'}},
                y:{ticks:{color:'#8896a8',font:{size:7}},grid:{color:'#2a2a4a'}}}},
      plugins:[_thVLinePlugin]});
    }
  }

  // RSI chart
  if(chart.rsi&&chart.dates){
    let ctx=document.getElementById('th-rsi-'+idx);
    if(ctx){
      let labels=chart.dates.map(d=>d.length>7?d.substring(5):d);
      entry.rsi=new Chart(ctx,{type:'line',data:{labels:labels,datasets:[
        {data:chart.rsi,borderColor:'#c084fc',borderWidth:2,pointRadius:0,fill:false}
      ]},options:{responsive:true,maintainAspectRatio:false,animation:false,
        layout:{padding:{top:14}},
        plugins:{legend:{display:false}},
        scales:{x:{ticks:{color:'#8896a8',font:{size:7},maxTicksLimit:6},grid:{color:'#2a2a4a'}},
                y:{min:0,max:100,ticks:{color:'#8896a8',font:{size:7},stepSize:20},
                   grid:{color:function(c){return(c.tick.value===30||c.tick.value===70)?'#ffffff33':'#2a2a4a';}}}}},
      plugins:[{id:'rz2',beforeDraw(ch){
        let{ctx:cx,chartArea:a,scales}=ch;if(!a)return;let y=scales.y;
        cx.save();
        cx.fillStyle='rgba(52,211,153,0.08)';cx.fillRect(a.left,y.getPixelForValue(30),a.width,y.getPixelForValue(0)-y.getPixelForValue(30));
        cx.fillStyle='rgba(248,113,113,0.08)';cx.fillRect(a.left,y.getPixelForValue(100),a.width,y.getPixelForValue(70)-y.getPixelForValue(100));
        cx.strokeStyle='#34d39950';cx.setLineDash([4,4]);cx.beginPath();cx.moveTo(a.left,y.getPixelForValue(30));cx.lineTo(a.right,y.getPixelForValue(30));cx.stroke();
        cx.strokeStyle='#f8717150';cx.beginPath();cx.moveTo(a.left,y.getPixelForValue(70));cx.lineTo(a.right,y.getPixelForValue(70));cx.stroke();
        cx.restore();
      }},_thVLinePlugin]});
    }
  }

  // Koncorde chart
  if(chart.koncorde&&chart.dates){
    let ctx=document.getElementById('th-konc-'+idx);
    if(ctx){
      let labels=chart.dates.map(d=>d.length>7?d.substring(5):d);
      let kk=chart.koncorde;
      let marronBg=kk.marron.map(v=>v>=0?'rgba(251,191,36,0.55)':'rgba(251,191,36,0.38)');
      let verdeBg=kk.verde.map(v=>v>=0?'rgba(52,211,153,0.65)':'rgba(52,211,153,0.45)');
      entry.konc=new Chart(ctx,{type:'bar',data:{labels:labels,datasets:[
        {type:'bar',label:'Marron',data:kk.marron,backgroundColor:marronBg,borderColor:'#fbbf24',borderWidth:0.5,barPercentage:0.95,categoryPercentage:0.95,stack:'s1',order:4},
        {type:'bar',label:'Verde',data:kk.verde,backgroundColor:verdeBg,borderColor:'#34d399',borderWidth:0.5,barPercentage:0.7,categoryPercentage:0.7,stack:'s2',order:3},
        {type:'line',label:'Azul',data:kk.azul,borderColor:'#60a5fa',borderWidth:2,pointRadius:0,fill:false,order:1},
        {type:'line',label:'Media',data:kk.media,borderColor:'#f87171',borderWidth:1.5,borderDash:[4,4],pointRadius:0,fill:false,order:0}
      ]},options:{responsive:true,maintainAspectRatio:false,animation:false,
        layout:{padding:{top:14}},
        plugins:{legend:{display:true,position:'top',labels:{color:'#64748b',font:{size:8},boxWidth:8,padding:4}}},
        scales:{
          x:{stacked:true,ticks:{color:'#8896a8',font:{size:7},maxTicksLimit:6},grid:{color:'#2a2a4a'}},
          y:{stacked:false,ticks:{color:'#8896a8',font:{size:7}},grid:{color:'#2a2a4a'}}
        }},
      plugins:[_thVLinePlugin]});
    }
  }

  _thCharts[idx]=entry;

  // Fill in thesis text
  let entryThEl=document.getElementById('th-entry-thesis-'+idx);
  let exitThEl=document.getElementById('th-exit-thesis-'+idx);
  if(entryThEl&&chart.entry_thesis){
    entryThEl.style.fontStyle='normal';
    entryThEl.innerHTML=chart.entry_thesis;
  }
  if(exitThEl&&chart.exit_thesis){
    exitThEl.style.fontStyle='normal';
    exitThEl.innerHTML=chart.exit_thesis;
  }

  // Market context
  let ctxEl=document.getElementById('th-context-'+idx);
  if(ctxEl&&chart.market_context){
    ctxEl.innerHTML='<div class="th-context-box"><div class="th-ctx-label">Contexto de Mercado</div><div class="th-ctx-text">'+chart.market_context+'</div></div>';
  }

  // Lessons
  let lesEl=document.getElementById('th-lessons-'+idx);
  if(lesEl){
    let pnl_pct=trade.pnl_pct;
    let dur=trade.duration_days;
    let lessons=[];
    if(trade.result==='WIN'){
      if(pnl_pct>15)lessons.push('Excelente trade con +'+pnl_pct.toFixed(1)+'% de retorno.');
      else if(pnl_pct>5)lessons.push('Buen trade con +'+pnl_pct.toFixed(1)+'% de retorno.');
      else lessons.push('Trade positivo pero modesto (+'+pnl_pct.toFixed(1)+'%).');
      if(dur<5)lessons.push('Trade muy rapido — buen timing de entrada y salida.');
      else if(dur>60)lessons.push('Posicion mantenida '+dur+' dias — paciencia recompensada.');
    }else{
      if(pnl_pct<-20)lessons.push('Perdida significativa ('+pnl_pct.toFixed(1)+'%). Revisar si el stop loss fue respetado.');
      else if(pnl_pct<-10)lessons.push('Perdida considerable ('+pnl_pct.toFixed(1)+'%). Evaluar si las senales de salida se activaron a tiempo.');
      else lessons.push('Perdida controlada ('+pnl_pct.toFixed(1)+'%).');
      if(trade.type!=='STK'&&pnl_pct<=-90)lessons.push('Opcion expiro sin valor — riesgo inherente de opciones.');
    }
    if(lessons.length>0){
      lesEl.innerHTML='<div class="th-lessons-box"><div class="th-les-label">Analisis del Trade</div><div class="th-les-text">'+lessons.join(' ')+'</div></div>';
    }
  }
}
</script>
</body>
</html>"""


@flask_app.route("/")
def index():
    return Response(DASHBOARD_HTML, mimetype="text/html")


@flask_app.route("/api/data")
def api_data():
    with update_lock:
        results = {}
        for sym, sig in analysis_cache.items():
            if sig is None:
                results[sym] = None
                continue

            rt_price, mkt = get_rt_price(sym)
            price = rt_price if rt_price else sig.get("price", 0)

            entry = {
                "signal": sig["signal"],
                "signal_label": sig.get("signal_label", sig["signal"]),
                "strength": float(sig.get("strength", 0)),
                "conditions_met": int(sig.get("conditions_met", 0)),
                "macd_ok": bool(sig.get("macd_ok", False)),
                "rsi_ok": bool(sig.get("rsi_ok", False)),
                "konc_ok": bool(sig.get("konc_ok", False)),
                "macd_detail": sig.get("macd_detail", ""),
                "rsi_detail": sig.get("rsi_detail", ""),
                "konc_detail": sig.get("konc_detail", ""),
                "price": float(price),
                "dollar_vol": float(sig.get("dollar_vol", 0)),
                "values": sig.get("values", {}),
                "chart": sig.get("chart"),
            }

            # Backtest metrics
            bt = sig.get("backtest", {})
            entry["confidence"] = bt.get("confidence", 0)
            entry["buy_avg_return"] = bt.get("buy_avg_return")
            entry["sell_avg_return"] = bt.get("sell_avg_return")
            entry["buy_count"] = bt.get("buy_count", 0)
            entry["sell_count"] = bt.get("sell_count", 0)

            bid = mkt.get("delayed_bid") or mkt.get("bid")
            ask = mkt.get("delayed_ask") or mkt.get("ask")
            vol = mkt.get("delayed_volume") or mkt.get("volume")
            if bid: entry["bid"] = float(bid)
            if ask: entry["ask"] = float(ask)
            if vol: entry["volume"] = float(vol)

            results[sym] = entry

        top3 = compute_top3(analysis_cache)

        return Response(to_json({
            "results": results,
            "last_update": last_update_time,
            "port": config.IB_PORT,
            "top3": top3,
        }), mimetype="application/json")


intraday_lock = threading.Lock()
intraday_cache = {}  # {(symbol, period): {"data": ..., "ts": float}}


def _build_ohlc(df):
    """Convierte DataFrame de barras IB a lista OHLC para charts."""
    ohlc = []
    for _, row in df.iterrows():
        d = str(row["date"]).strip()
        # IB intraday: "20240315  14:30:00" → unix timestamp
        if ":" in d:
            parts = d.split()
            ds = parts[0].replace("-", "")
            ts_str = parts[1] if len(parts) > 1 else "00:00:00"
            try:
                dt_obj = datetime.strptime(ds + ts_str, "%Y%m%d%H:%M:%S")
                ts = int(dt_obj.timestamp())
            except ValueError:
                continue
            ohlc.append({
                "time": ts,
                "open": round(float(row["open"]), 2),
                "high": round(float(row["high"]), 2),
                "low": round(float(row["low"]), 2),
                "close": round(float(row["close"]), 2),
            })
        else:
            # Daily: "YYYY-MM-DD" o "YYYYMMDD"
            ds = d.replace("-", "").replace(" ", "")
            if len(ds) >= 8:
                ts = f"{ds[:4]}-{ds[4:6]}-{ds[6:8]}"
            else:
                ts = d
            ohlc.append({
                "time": ts,
                "open": round(float(row["open"]), 2),
                "high": round(float(row["high"]), 2),
                "low": round(float(row["low"]), 2),
                "close": round(float(row["close"]), 2),
            })
    return ohlc


@flask_app.route("/api/bars/<symbol>/<period>")
def api_bars(symbol, period):
    """Endpoint on-demand para barras intraday (4h, 15min)."""
    configs = {
        "4h": ("1 M", "4 hours"),
        "15m": ("2 D", "15 mins"),
    }
    if period not in configs:
        return Response('{"error":"invalid"}', status=400, mimetype="application/json")

    cache_key = (symbol, period)
    cached = intraday_cache.get(cache_key)
    if cached and time.time() - cached["ts"] < 300:
        return Response(to_json(cached["data"]), mimetype="application/json")

    dur, bar_size = configs[period]
    result = {"ohlc": []}

    try:
        with intraday_lock:
            req_id = 8000 + abs(hash(symbol + period)) % 999
            contract = make_contract(symbol)
            ib_app.historical_data[req_id] = []
            ib_app.hist_done[req_id] = False
            ib_app.reqHistoricalData(
                req_id, contract, "", dur, bar_size,
                config.HIST_WHAT_TO_SHOW, 1, 1, False, []
            )
            start = time.time()
            while not ib_app.hist_done.get(req_id, False) and time.time() - start < 30:
                time.sleep(0.2)

        data = ib_app.historical_data.get(req_id, [])
        if data:
            df = pd.DataFrame(data)
            result["ohlc"] = _build_ohlc(df)
    except Exception as e:
        print(f"  Error fetching {period} bars for {symbol}: {e}")

    intraday_cache[cache_key] = {"data": result, "ts": time.time()}
    return Response(to_json(result), mimetype="application/json")


# ══════════════════════════════════════════════════════════════
#  PORTFOLIO ENDPOINTS
# ══════════════════════════════════════════════════════════════

def _compute_position_trend(data):
    """Devuelve 'up' | 'down' | 'flat' segun momentum de MACD/RSI/Koncorde.
    Base para tendencia hacia BUY/SELL cuando la senal esta en HOLD."""
    try:
        chart = data.get("chart") or {}
        macd = chart.get("macd") or {}
        hist = macd.get("hist") or []
        rsi = chart.get("rsi") or []
        konc = chart.get("koncorde") or {}
        marron = konc.get("marron") or []

        up_votes = 0
        down_votes = 0
        # MACD histogram slope (last 3)
        if len(hist) >= 3:
            if hist[-1] > hist[-2] > hist[-3]:
                up_votes += 1
            elif hist[-1] < hist[-2] < hist[-3]:
                down_votes += 1
        # RSI slope
        if len(rsi) >= 3:
            if rsi[-1] > rsi[-2] > rsi[-3]:
                up_votes += 1
            elif rsi[-1] < rsi[-2] < rsi[-3]:
                down_votes += 1
        # Koncorde marron slope
        if len(marron) >= 3:
            if marron[-1] > marron[-2] > marron[-3]:
                up_votes += 1
            elif marron[-1] < marron[-2] < marron[-3]:
                down_votes += 1

        if up_votes >= 2 and up_votes > down_votes:
            return "up"
        if down_votes >= 2 and down_votes > up_votes:
            return "down"
        return "flat"
    except Exception:
        return "flat"


def _compute_position_verdict(data, position):
    """Combina la senal tecnica con el estado de la posicion (P&L, SL/TP hit)
    y devuelve un veredicto accionable para el tablero.

    Returns dict:
      verdict: 'BUY' | 'SELL' | 'HOLD' | 'REDUCE' | 'ADD'
      urgency: 'high' | 'medium' | 'low'
      headline: texto corto para el card
      reason: texto explicativo
      trend: 'up' | 'down' | 'flat'
    """
    sig = data.get("signal", "HOLD")
    strength = data.get("strength", 0) or 0
    conds = data.get("conditions_met", 0) or 0
    price = data.get("price") or 0
    pnl_pct = (position or {}).get("pnl_pct", 0) or 0
    trend = _compute_position_trend(data)

    # Senal activa de VENTA sobre una posicion abierta = accion urgente
    if sig == "SELL":
        return {
            "verdict": "SELL",
            "urgency": "high",
            "headline": "VENDER",
            "reason": f"Senal de VENTA activa (3/3 indicadores, fuerza {strength:.1f}). Considerar cerrar posicion.",
            "trend": "down",
        }

    # Senal activa de COMPRA sobre algo ya en cartera = agregar
    if sig == "BUY":
        return {
            "verdict": "ADD",
            "urgency": "high",
            "headline": "SUMAR",
            "reason": f"Senal de COMPRA activa (3/3 indicadores, fuerza {strength:.1f}). Oportunidad para ampliar posicion.",
            "trend": "up",
        }

    # HOLD con 2/3 y tendencia bajista = reducir
    if conds >= 2 and trend == "down":
        return {
            "verdict": "REDUCE",
            "urgency": "medium",
            "headline": "REDUCIR",
            "reason": f"{conds}/3 indicadores girando a VENTA. Momentum bajista, ajustar exposicion.",
            "trend": "down",
        }

    # HOLD con 2/3 y tendencia alcista = mantener con sesgo alcista
    if conds >= 2 and trend == "up":
        return {
            "verdict": "HOLD",
            "urgency": "low",
            "headline": "HOLD (alcista)",
            "reason": f"{conds}/3 indicadores alineados alcistas. Mantener, cerca de senal de compra.",
            "trend": "up",
        }

    # Perdida grande sin senal tecnica = revisar stop
    if pnl_pct < -12:
        return {
            "verdict": "REDUCE",
            "urgency": "medium",
            "headline": "REVISAR",
            "reason": f"Perdida acumulada {pnl_pct:.1f}%. Sin senal tecnica de compra. Revisar stop-loss.",
            "trend": trend,
        }

    return {
        "verdict": "HOLD",
        "urgency": "low",
        "headline": "HOLD",
        "reason": "Sin senales tecnicas relevantes. Mantener y monitorear.",
        "trend": trend,
    }


def _build_position_deep_analysis(sym, position, n_bars=90):
    """Enriquece una posicion abierta con el analisis completo estilo escaner
    (charts, MACD/RSI/Koncorde, tesis, targets, fundamentales, veredicto).

    Retorna el dict que el frontend renderiza como card expandible.
    """
    # 1. Fetch full analysis (cache first, fresh if needed)
    data = analysis_cache.get(sym)
    if data is None or (data.get("chart") or {}).get("ohlc") is None:
        try:
            df = fetch_historical(ib_app, sym, 8500 + (abs(hash(sym)) % 500),
                                   duration=config.BACKTEST_DURATION)
            data = analyze_symbol(df)
        except Exception as e:
            print(f"  [Portfolio deep] Error analizando {sym}: {e}")
            return None
    if data is None:
        return None

    # 2. Fetch fundamentals (cached, 1h TTL)
    try:
        _fetch_fundamentals([sym])
    except Exception as e:
        print(f"  [Portfolio deep] Fundamentals error para {sym}: {e}")
    fund_entry = fundamentals_cache.get(sym, {})
    fund = fund_entry.get("data", {}) if isinstance(fund_entry, dict) else {}

    # 3. Compute levels, rationale, thesis (misma logica que top3)
    try:
        levels = _compute_price_levels(data)
    except Exception:
        levels = {"entry_low": 0, "entry_high": 0, "target": 0, "stop_loss": 0,
                  "atr": 0, "risk_reward": 0, "target_pct": 0,
                  "target_basis": "", "horizon_weeks": ""}
    try:
        rationale = _generate_rationale(sym, data, levels)
    except Exception:
        rationale = []
    try:
        thesis = _generate_thesis(sym, data, levels, fund)
    except Exception:
        thesis = ""

    # 4. Slice chart data (idem top3)
    try:
        ohlc_slice, mas_sliced, _ = _extract_chart_data(data, n_bars)
    except Exception:
        ohlc_slice, mas_sliced = [], {}
    try:
        sig_markers = _compute_signal_markers(data, n_bars)
    except Exception:
        sig_markers = []

    chart = data.get("chart") or {}
    total = len(chart.get("ohlc", []))
    start = max(0, total - n_bars)
    all_dates = chart.get("dates", [])
    dates_slice = all_dates[start:] if len(all_dates) > start else all_dates

    macd_full = chart.get("macd", {})
    chart_macd = {
        "macd": (macd_full.get("macd") or [])[start:],
        "signal": (macd_full.get("signal") or [])[start:],
        "hist": (macd_full.get("hist") or [])[start:],
    }
    chart_rsi = (chart.get("rsi") or [])[start:]
    konc_full = chart.get("koncorde", {})
    chart_koncorde = {
        "verde": (konc_full.get("verde") or [])[start:],
        "marron": (konc_full.get("marron") or [])[start:],
        "azul": (konc_full.get("azul") or [])[start:],
        "media": (konc_full.get("media") or [])[start:],
    }

    # 5. Verdict (accion recomendada)
    verdict = _compute_position_verdict(data, position)

    bt = data.get("backtest", {}) or {}
    sig = data.get("signal", "HOLD")

    # 6. Score for consistency with top3 (no eligibility filter)
    score = 0
    try:
        s = _score_stock(sym, data)
        if s is not None:
            score = round(s, 1)
    except Exception:
        pass

    return {
        "signal": sig,
        "signal_label": data.get("signal_label", sig),
        "strength": data.get("strength", 0) or 0,
        "conditions_met": data.get("conditions_met", 0) or 0,
        "confidence": bt.get("confidence", 0) or 0,
        "score": score,
        "price": data.get("price", 0),
        "entry_low": levels.get("entry_low", 0),
        "entry_high": levels.get("entry_high", 0),
        "target": levels.get("target", 0),
        "stop_loss": levels.get("stop_loss", 0),
        "risk_reward": levels.get("risk_reward", 0),
        "atr": levels.get("atr", 0),
        "target_pct": levels.get("target_pct", 0),
        "target_basis": levels.get("target_basis", ""),
        "horizon": levels.get("horizon_weeks", ""),
        "thesis": thesis,
        "win_rate": (bt.get("sell_win_rate", 0) if _label_is_bearish(data.get("signal_label", sig))
                     else bt.get("buy_win_rate", 0)) or 0,
        "avg_return": (bt.get("sell_avg_return") if _label_is_bearish(data.get("signal_label", sig))
                       else bt.get("buy_avg_return")),
        "rationale": rationale,
        "chart_ohlc": ohlc_slice,
        "chart_mas": mas_sliced,
        "chart_markers": sig_markers,
        "chart_dates": dates_slice,
        "chart_macd": chart_macd,
        "chart_rsi": chart_rsi,
        "chart_koncorde": chart_koncorde,
        "fundamentals": fund,
        "verdict": verdict.get("verdict", "HOLD"),
        "urgency": verdict.get("urgency", "low"),
        "headline": verdict.get("headline", "HOLD"),
        "verdict_reason": verdict.get("reason", ""),
        "trend": verdict.get("trend", "flat"),
        "macd_ok": data.get("macd_ok", False),
        "rsi_ok": data.get("rsi_ok", False),
        "konc_ok": data.get("konc_ok", False),
        "macd_detail": data.get("macd_detail", ""),
        "rsi_detail": data.get("rsi_detail", ""),
        "konc_detail": data.get("konc_detail", ""),
        "values": data.get("values", {}),
    }


portfolio.register_portfolio_endpoint(
    flask_app,
    ib_app_ref=lambda: ib_app,
    analyze_symbol_fn=analyze_symbol,
    fetch_historical_fn=fetch_historical,
    to_json_fn=to_json,
    build_position_analysis_fn=_build_position_deep_analysis,
)


# ══════════════════════════════════════════════════════════════
#  OPTIONS LAB ENDPOINT
# ══════════════════════════════════════════════════════════════

options_lab_cache = {}  # {symbol: {"data": ..., "ts": float}}
options_lab_lock = threading.Lock()


@flask_app.route("/api/options-lab/<symbol>")
def api_options_lab(symbol):
    """Genera analisis completo del Options Lab para un simbolo."""
    symbol = symbol.upper()

    # Check cache (5 min TTL)
    with options_lab_lock:
        cached = options_lab_cache.get(symbol)
        if cached and time.time() - cached["ts"] < 300:
            return Response(to_json(cached["data"]), mimetype="application/json")

    # Get analysis data
    data = analysis_cache.get(symbol)
    if data is None:
        return Response(to_json({"error": f"No hay datos para {symbol}"}),
                        status=404, mimetype="application/json")

    price = data.get("price", 0)
    if price <= 0:
        return Response(to_json({"error": f"Precio no disponible para {symbol}"}),
                        status=400, mimetype="application/json")

    # Build signal_data for the lab
    vals = data.get("values", {}) or {}
    macd_vals = vals.get("macd", {})
    chart = data.get("chart", {}) or {}
    macd_chart = chart.get("macd", {}) or {}
    hist_arr = macd_chart.get("hist", [])

    signal_data = {
        "signal": data.get("signal", "HOLD"),
        "signal_label": data.get("signal_label", "NEUTRAL"),
        "strength": data.get("strength", 0),
        "rsi": vals.get("rsi"),
        "macd_hist": macd_vals.get("hist"),
        "macd_hist_prev": hist_arr[-2] if len(hist_arr) >= 2 else None,
        "conditions_met": data.get("conditions_met", 0),
    }

    # Extract closes, highs, lows from OHLC data
    ohlc = chart.get("ohlc", [])
    if len(ohlc) < 100:
        return Response(to_json({"error": f"Datos historicos insuficientes para {symbol}"}),
                        status=400, mimetype="application/json")

    closes = np.array([b["close"] for b in ohlc], dtype=float)
    highs = np.array([b["high"] for b in ohlc], dtype=float)
    lows = np.array([b["low"] for b in ohlc], dtype=float)

    try:
        result = options_lab.generate_options_lab(
            symbol=symbol,
            price=price,
            signal_data=signal_data,
            closes=closes,
            highs=highs,
            lows=lows,
            risk_free_rate=config.OPTIONS_RISK_FREE_RATE,
            dte_options=config.OPTIONS_DTE_TARGETS,
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        return Response(to_json({"error": f"Error generando Options Lab: {str(e)}"}),
                        status=500, mimetype="application/json")

    with options_lab_lock:
        options_lab_cache[symbol] = {"data": result, "ts": time.time()}

    return Response(to_json(result), mimetype="application/json")


@flask_app.route("/api/options-lab-top")
def api_options_lab_top():
    """Analiza TODAS las acciones del scanner para encontrar las mejores
    oportunidades de opciones. Rankea por una combinacion de:
    - Fuerza de senal tecnica (del scanner)
    - Desalineacion de IV (opciones caras o baratas)
    - Calidad del mejor strategy score
    Devuelve las top 10 oportunidades con analisis completo."""

    # 1. Pre-screen: score all stocks quickly for options potential
    candidates = []
    for sym, data in analysis_cache.items():
        if data is None:
            continue
        price = data.get("price", 0)
        if price <= 0:
            continue
        chart = data.get("chart", {}) or {}
        ohlc = chart.get("ohlc", [])
        if len(ohlc) < 100:
            continue

        # Quick options-opportunity score (no full lab yet)
        signal = data.get("signal", "HOLD")
        strength = data.get("strength", 0) or 0
        conditions = data.get("conditions_met", 0) or 0
        bt = data.get("backtest", {}) or {}
        confidence = bt.get("confidence", 0) or 0

        # IV pre-check (fast)
        closes = [b["close"] for b in ohlc]
        iv_data = options_lab.iv_analysis(closes)
        hv_rank = iv_data.get("hv_rank") or 50
        iv_regime = iv_data.get("iv_regime", "normal")

        # Options opportunity score (different from stock signal score)
        opt_score = 0.0

        # Active signals are the best candidates
        if signal in ("BUY", "SELL"):
            opt_score += 40 + strength * 5
        elif conditions >= 2:
            opt_score += 25 + strength * 3
        elif conditions >= 1:
            opt_score += 10

        # IV extremes create opportunities regardless of signal
        if iv_regime == "high":
            opt_score += 20  # sell premium
        elif iv_regime == "low":
            opt_score += 15  # buy premium cheap

        # HV rank extremes
        if hv_rank > 80 or hv_rank < 20:
            opt_score += 10

        # Backtest confidence
        opt_score += min(confidence / 100, 1.0) * 15

        # Liquidity bonus (more liquid = better fills)
        dv = data.get("dollar_vol", 0) or 0
        if dv > 500e6:
            opt_score += 5
        elif dv > 100e6:
            opt_score += 3

        candidates.append((sym, data, opt_score, iv_data))

    candidates.sort(key=lambda x: x[2], reverse=True)

    # 2. Full analysis on top candidates
    results = []
    for sym, data, opt_score, iv_pre in candidates[:10]:
        price = data.get("price", 0)
        vals = data.get("values", {}) or {}
        macd_vals = vals.get("macd", {})
        chart = data.get("chart", {}) or {}
        macd_chart = chart.get("macd", {}) or {}
        hist_arr = macd_chart.get("hist", [])

        signal_data = {
            "signal": data.get("signal", "HOLD"),
            "signal_label": data.get("signal_label", "NEUTRAL"),
            "strength": data.get("strength", 0),
            "rsi": vals.get("rsi"),
            "macd_hist": macd_vals.get("hist"),
            "macd_hist_prev": hist_arr[-2] if len(hist_arr) >= 2 else None,
            "conditions_met": data.get("conditions_met", 0),
        }

        ohlc = chart.get("ohlc", [])
        closes = np.array([b["close"] for b in ohlc], dtype=float)
        highs_arr = np.array([b["high"] for b in ohlc], dtype=float)
        lows_arr = np.array([b["low"] for b in ohlc], dtype=float)

        try:
            lab = options_lab.generate_options_lab(
                symbol=sym, price=price, signal_data=signal_data,
                closes=closes, highs=highs_arr, lows=lows_arr,
                risk_free_rate=config.OPTIONS_RISK_FREE_RATE,
                dte_options=config.OPTIONS_DTE_TARGETS,
            )
            lab["stock_score"] = round(opt_score, 1)
            results.append(lab)
        except Exception as e:
            print(f"  Options Lab error for {sym}: {e}")

    # 3. Re-sort by best strategy score within each result
    results.sort(key=lambda r: (
        r.get("strategies", [{}])[0].get("score", 0) if r.get("strategies") else 0
    ) + r.get("stock_score", 0), reverse=True)

    return Response(to_json({"opportunities": results}), mimetype="application/json")


# ══════════════════════════════════════════════════════════════
#  TRADES HISTORY ENDPOINT
# ══════════════════════════════════════════════════════════════

trades_history_cache = {"data": None, "ts": 0}
trades_history_lock = threading.Lock()
TRADES_HISTORY_TTL = 3600  # 1 hour


def _parse_option_symbol(raw_sym):
    """Parse 'AAPL  260417C00305000' → (ticker, expiry, type, strike)."""
    parts = raw_sym.split()
    ticker = parts[0]
    if len(parts) < 2:
        return ticker, None, None, None
    detail = parts[-1]
    try:
        exp_raw = detail[:6]
        opt_type = detail[6]  # C or P
        strike_raw = detail[7:]
        strike = int(strike_raw) / 1000.0
        expiry = f"20{exp_raw[:2]}-{exp_raw[2:4]}-{exp_raw[4:6]}"
        return ticker, expiry, opt_type, strike
    except (ValueError, IndexError):
        return ticker, None, None, None


def _is_option_symbol(sym):
    return len(sym.strip()) > 10 and ("C0" in sym or "P0" in sym)


def _group_spread_legs(trades_list):
    """Group option trades executed on the same date into spreads."""
    from collections import defaultdict
    by_date = defaultdict(list)
    for t in trades_list:
        by_date[t["date"]].append(t)

    spreads = []
    standalone = []
    for date, day_trades in by_date.items():
        buys = [t for t in day_trades if t["action"] == "BUY"]
        sells = [t for t in day_trades if t["action"] == "SELL"]
        if buys and sells:
            spreads.append({"date": date, "legs": day_trades})
        else:
            standalone.extend(day_trades)
    return spreads, standalone


def _generate_trade_thesis(sym, entry_date, exit_date, indicators_at_entry, indicators_at_exit):
    """Generate entry/exit thesis from indicator values."""
    entry_parts = []
    exit_parts = []

    if indicators_at_entry:
        rsi_e = indicators_at_entry.get("rsi")
        macd_h_e = indicators_at_entry.get("macd_hist")
        konc_e = indicators_at_entry.get("koncorde", {})

        if rsi_e is not None:
            if rsi_e < 30:
                entry_parts.append(f"RSI en sobreventa ({rsi_e:.0f})")
            elif rsi_e < 40:
                entry_parts.append(f"RSI bajo ({rsi_e:.0f}), acercandose a sobreventa")
            elif rsi_e > 70:
                entry_parts.append(f"RSI en sobrecompra ({rsi_e:.0f})")
            else:
                entry_parts.append(f"RSI en {rsi_e:.0f}")

        if macd_h_e is not None:
            if macd_h_e < 0:
                entry_parts.append(f"MACD histograma negativo ({macd_h_e:.2f}), posible giro al alza")
            else:
                entry_parts.append(f"MACD histograma positivo ({macd_h_e:.2f})")

        marron_e = konc_e.get("marron")
        media_e = konc_e.get("media")
        if marron_e is not None and media_e is not None:
            if marron_e < media_e:
                entry_parts.append("Koncorde marron por debajo de media (presion vendedora)")
            else:
                entry_parts.append("Koncorde marron por encima de media (presion compradora)")

    if indicators_at_exit:
        rsi_x = indicators_at_exit.get("rsi")
        macd_h_x = indicators_at_exit.get("macd_hist")
        konc_x = indicators_at_exit.get("koncorde", {})

        if rsi_x is not None:
            if rsi_x > 70:
                exit_parts.append(f"RSI alcanzo sobrecompra ({rsi_x:.0f})")
            elif rsi_x > 60:
                exit_parts.append(f"RSI elevado ({rsi_x:.0f})")
            elif rsi_x < 30:
                exit_parts.append(f"RSI cayo a sobreventa ({rsi_x:.0f})")
            else:
                exit_parts.append(f"RSI en {rsi_x:.0f}")

        if macd_h_x is not None:
            if macd_h_x > 0:
                exit_parts.append(f"MACD histograma positivo ({macd_h_x:.2f}), girando a la baja")
            else:
                exit_parts.append(f"MACD histograma negativo ({macd_h_x:.2f})")

        marron_x = konc_x.get("marron")
        media_x = konc_x.get("media")
        if marron_x is not None and media_x is not None:
            if marron_x > media_x:
                exit_parts.append("Koncorde marron por encima de media")
            else:
                exit_parts.append("Koncorde marron cayo por debajo de media")

    entry_thesis = ". ".join(entry_parts) + "." if entry_parts else "Sin datos de indicadores al momento de la compra."
    exit_thesis = ". ".join(exit_parts) + "." if exit_parts else "Sin datos de indicadores al momento de la venta."
    return entry_thesis, exit_thesis


def _get_indicator_values_at_date(df_indicators, target_date, dates):
    """Get indicator values at or near a specific date."""
    target_str = str(target_date).replace("-", "")[:8]
    best_idx = None
    for i, d in enumerate(dates):
        d_str = str(d).replace("-", "").replace(" ", "")[:8]
        if d_str <= target_str:
            best_idx = i
    if best_idx is None:
        return None

    result = {}
    macd_data = df_indicators.get("macd")
    rsi_data = df_indicators.get("rsi")
    konc_data = df_indicators.get("koncorde")

    if macd_data is not None and best_idx < len(macd_data):
        try:
            result["macd_hist"] = float(macd_data.iloc[best_idx]["hist"])
            result["macd_line"] = float(macd_data.iloc[best_idx]["macd"])
            result["macd_signal"] = float(macd_data.iloc[best_idx]["signal"])
        except (KeyError, IndexError):
            pass

    if rsi_data is not None and best_idx < len(rsi_data):
        try:
            result["rsi"] = float(rsi_data.iloc[best_idx]["rsi"])
        except (KeyError, IndexError):
            pass

    if konc_data is not None and best_idx < len(konc_data):
        try:
            def _safe_float(v):
                fv = float(v)
                return fv if not (math.isnan(fv) or math.isinf(fv)) else None
            result["koncorde"] = {
                "marron": _safe_float(konc_data.iloc[best_idx]["marron"]),
                "verde": _safe_float(konc_data.iloc[best_idx]["verde"]),
                "azul": _safe_float(konc_data.iloc[best_idx]["azul"]),
                "media": _safe_float(konc_data.iloc[best_idx]["media"]),
            }
        except (KeyError, IndexError):
            pass

    return result if result else None


def _generate_post_trade_analysis(trade):
    """Generate lessons learned for a trade."""
    lessons = []
    pnl_pct = trade.get("pnl_pct", 0)
    duration = trade.get("duration_days", 0)

    if trade["result"] == "WIN":
        if pnl_pct > 15:
            lessons.append(f"Excelente trade con {pnl_pct:.1f}% de retorno.")
        elif pnl_pct > 5:
            lessons.append(f"Buen trade con {pnl_pct:.1f}% de retorno.")
        else:
            lessons.append(f"Trade positivo pero modesto ({pnl_pct:.1f}%).")

        if duration < 5:
            lessons.append("Trade muy rapido — buen timing de entrada y salida.")
        elif duration > 60:
            lessons.append(f"Posicion mantenida {duration} dias — paciencia recompensada.")
    else:
        if pnl_pct < -20:
            lessons.append(f"Perdida significativa ({pnl_pct:.1f}%). Revisar si el stop loss fue respetado.")
        elif pnl_pct < -10:
            lessons.append(f"Perdida considerable ({pnl_pct:.1f}%). Evaluar si las senales de salida se activaron a tiempo.")
        else:
            lessons.append(f"Perdida controlada ({pnl_pct:.1f}%).")

        if trade.get("type") == "OPT":
            if pnl_pct <= -90:
                lessons.append("Opcion expiro sin valor o con perdida casi total — riesgo inherente de opciones.")

    return " ".join(lessons)


def build_trades_history(trades_file=None):
    """Build full trades history from trades_imported.json."""
    import os
    from collections import defaultdict
    from datetime import datetime as dt, timedelta

    if trades_file is None:
        trades_file = os.path.join(os.path.dirname(__file__), "trades_imported.json")
    if not os.path.exists(trades_file):
        return {"trades": [], "summary": {}}

    with open(trades_file) as f:
        raw = json.load(f)
    all_trades = raw.get("trades", [])
    if not all_trades:
        return {"trades": [], "summary": {}}

    # Get currently open positions to exclude them
    history_file = os.path.join(os.path.dirname(__file__), "portfolio_history.json")
    open_symbols = set()
    if os.path.exists(history_file):
        try:
            with open(history_file) as f:
                hist = json.load(f)
            snapshots = hist.get("snapshots", [])
            if snapshots:
                latest = snapshots[-1]
                for p in latest.get("positions", []):
                    open_symbols.add(p.get("symbol", ""))
        except Exception:
            pass

    # Separate stock trades from option trades
    stock_trades = defaultdict(list)
    option_trades = defaultdict(list)

    for t in all_trades:
        sym = t["symbol"]
        if _is_option_symbol(sym):
            ticker = _parse_option_symbol(sym)[0]
            option_trades[sym].append(t)
        else:
            stock_trades[sym].append(t)

    completed_trades = []

    # --- Process STOCK trades ---
    for sym, trades in stock_trades.items():
        if sym in open_symbols:
            buys = [t for t in trades if t["action"] == "BUY"]
            sells = [t for t in trades if t["action"] == "SELL"]
            if not sells:
                continue
        else:
            buys = [t for t in trades if t["action"] == "BUY"]
            sells = [t for t in trades if t["action"] == "SELL"]

        if not sells:
            continue

        buys.sort(key=lambda x: x["date"])
        sells.sort(key=lambda x: x["date"])

        total_buy_qty = sum(b["filled_qty"] for b in buys)
        total_buy_cost = sum(b["filled_qty"] * b["avg_fill_price"] for b in buys)
        total_buy_comm = sum(b.get("commission", 0) for b in buys)

        total_sell_qty = sum(s["filled_qty"] for s in sells)
        total_sell_proceeds = sum(s["filled_qty"] * s["avg_fill_price"] for s in sells)
        total_sell_comm = sum(s.get("commission", 0) for s in sells)
        total_realized_pnl = sum(s.get("realized_pnl", 0) for s in sells)

        # Estimate entry price if no buys
        estimated_entry = False
        if not buys and total_sell_qty > 0:
            avg_sell_price = total_sell_proceeds / total_sell_qty
            avg_entry_price = avg_sell_price - (total_realized_pnl / total_sell_qty)
            entry_date = sells[0]["date"]
            estimated_entry = True
        elif buys:
            avg_entry_price = total_buy_cost / total_buy_qty if total_buy_qty else 0
            entry_date = buys[0]["date"]
        else:
            continue

        # Use the quantity that was actually closed
        closed_qty = min(total_buy_qty, total_sell_qty) if buys else total_sell_qty
        avg_exit_price = total_sell_proceeds / total_sell_qty if total_sell_qty else 0
        exit_date = sells[-1]["date"]
        commissions = total_buy_comm + total_sell_comm

        invested = avg_entry_price * closed_qty
        pnl = total_realized_pnl
        pnl_pct = (pnl / invested * 100) if invested > 0 else 0

        try:
            d1 = dt.strptime(entry_date, "%Y-%m-%d")
            d2 = dt.strptime(exit_date, "%Y-%m-%d")
            duration = (d2 - d1).days
        except ValueError:
            duration = 0

        completed_trades.append({
            "id": f"{sym}_{entry_date}",
            "symbol": sym,
            "type": "STK",
            "option_detail": None,
            "entry_date": entry_date,
            "exit_date": exit_date,
            "entry_price": round(avg_entry_price, 2),
            "exit_price": round(avg_exit_price, 2),
            "quantity": closed_qty,
            "invested": round(invested, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "duration_days": max(duration, 0),
            "commissions": round(commissions, 2),
            "result": "WIN" if pnl > 0 else "LOSS",
            "estimated_entry": estimated_entry,
            "buy_fills": [{"date": b["date"], "qty": b["filled_qty"], "price": b["avg_fill_price"]} for b in buys],
            "sell_fills": [{"date": s["date"], "qty": s["filled_qty"], "price": s["avg_fill_price"]} for s in sells],
        })

    # --- Process OPTION trades ---
    # Group by underlying + expiry to detect spreads
    opt_by_underlying = defaultdict(list)
    for sym, trades in option_trades.items():
        ticker, expiry, opt_type, strike = _parse_option_symbol(sym)
        for t in trades:
            t["_ticker"] = ticker
            t["_expiry"] = expiry
            t["_opt_type"] = opt_type
            t["_strike"] = strike
            t["_raw_sym"] = sym
            opt_by_underlying[ticker].append(t)

    # Group option trades by underlying + date range to form round trips
    for ticker, opts in opt_by_underlying.items():
        # Group by expiry date to identify related trades
        by_expiry = defaultdict(list)
        for o in opts:
            by_expiry[o["_expiry"]].append(o)

        for expiry, exp_trades in by_expiry.items():
            buys = [t for t in exp_trades if t["action"] == "BUY"]
            sells = [t for t in exp_trades if t["action"] == "SELL"]

            if not buys and not sells:
                continue

            # Check if this is a spread (multiple strikes same expiry)
            strikes_involved = set()
            for t in exp_trades:
                strikes_involved.add(t["_strike"])

            is_spread = len(strikes_involved) > 1

            total_buy_cost = sum(b["filled_qty"] * b["avg_fill_price"] * 100 for b in buys)
            total_sell_proceeds = sum(s["filled_qty"] * s["avg_fill_price"] * 100 for s in sells)
            total_pnl = sum(s.get("realized_pnl", 0) for s in sells)
            total_comm = sum(t.get("commission", 0) for t in exp_trades)

            all_dates = [t["date"] for t in exp_trades]
            all_dates.sort()
            entry_date = all_dates[0]
            exit_date = all_dates[-1]

            strikes_str = "/".join(f"${s:.0f}" for s in sorted(strikes_involved))
            opt_types = set(t["_opt_type"] for t in exp_trades)
            opt_type_str = "/".join(sorted(opt_types))

            if is_spread:
                type_label = "SPREAD"
                detail = f"{opt_type_str} {strikes_str} exp {expiry}"
            else:
                type_label = "OPT"
                detail = f"{opt_type_str} {strikes_str} exp {expiry}"

            invested = abs(total_buy_cost - total_sell_proceeds) if is_spread else total_buy_cost
            if invested == 0:
                invested = abs(total_pnl) + total_comm if total_pnl else 1

            pnl = total_pnl
            pnl_pct = (pnl / invested * 100) if invested > 0 else 0

            try:
                d1 = dt.strptime(entry_date, "%Y-%m-%d")
                d2 = dt.strptime(exit_date, "%Y-%m-%d")
                duration = (d2 - d1).days
            except ValueError:
                duration = 0

            completed_trades.append({
                "id": f"{ticker}_{type_label}_{entry_date}_{expiry}",
                "symbol": ticker,
                "type": type_label,
                "option_detail": detail,
                "entry_date": entry_date,
                "exit_date": exit_date,
                "entry_price": round(total_buy_cost / 100, 2) if buys else 0,
                "exit_price": round(total_sell_proceeds / 100, 2) if sells else 0,
                "quantity": sum(b["filled_qty"] for b in buys) if buys else sum(s["filled_qty"] for s in sells),
                "invested": round(invested, 2),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
                "duration_days": max(duration, 0),
                "commissions": round(total_comm, 2),
                "result": "WIN" if pnl > 0 else "LOSS",
                "estimated_entry": False,
                "buy_fills": [{"date": b["date"], "qty": b["filled_qty"], "price": b["avg_fill_price"]} for b in buys],
                "sell_fills": [{"date": s["date"], "qty": s["filled_qty"], "price": s["avg_fill_price"]} for s in sells],
            })

    # Sort by exit_date descending (most recent first)
    completed_trades.sort(key=lambda x: x["exit_date"], reverse=True)

    # Summary
    wins = [t for t in completed_trades if t["result"] == "WIN"]
    losses = [t for t in completed_trades if t["result"] == "LOSS"]
    total_pnl = sum(t["pnl"] for t in completed_trades)
    best = max(completed_trades, key=lambda x: x["pnl"]) if completed_trades else None
    worst = min(completed_trades, key=lambda x: x["pnl"]) if completed_trades else None
    durations = [t["duration_days"] for t in completed_trades if t["duration_days"] > 0]

    summary = {
        "total_trades": len(completed_trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(completed_trades) * 100, 1) if completed_trades else 0,
        "total_pnl": round(total_pnl, 2),
        "best_trade": {"symbol": best["symbol"], "pnl": best["pnl"], "pnl_pct": best["pnl_pct"]} if best else None,
        "worst_trade": {"symbol": worst["symbol"], "pnl": worst["pnl"], "pnl_pct": worst["pnl_pct"]} if worst else None,
        "avg_duration_days": round(sum(durations) / len(durations), 0) if durations else 0,
        "avg_return_pct": round(sum(t["pnl_pct"] for t in completed_trades) / len(completed_trades), 2) if completed_trades else 0,
        "total_commissions": round(sum(t["commissions"] for t in completed_trades), 2),
        "stocks_count": len([t for t in completed_trades if t["type"] == "STK"]),
        "options_count": len([t for t in completed_trades if t["type"] in ("OPT", "SPREAD")]),
    }

    return {"trades": completed_trades, "summary": summary}


def _fetch_trade_chart_data(symbol, entry_date, exit_date):
    """Fetch OHLC + indicators for a trade's time period via yfinance."""
    from datetime import datetime as dt, timedelta

    try:
        d1 = dt.strptime(entry_date, "%Y-%m-%d") - timedelta(days=60)
        d2 = dt.strptime(exit_date, "%Y-%m-%d") + timedelta(days=30)
        today = dt.now()
        if d2 > today:
            d2 = today
    except ValueError:
        return None

    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(start=d1.strftime("%Y-%m-%d"), end=d2.strftime("%Y-%m-%d"), interval="1d")
        if df is None or len(df) < 20:
            return None

        df = df.reset_index()
        df.columns = [c.lower() for c in df.columns]
        if "date" not in df.columns and "datetime" in df.columns:
            df["date"] = df["datetime"]
        df["date"] = df["date"].dt.strftime("%Y-%m-%d")

        # Calculate indicators
        ind = indicators.calculate_all(df)

        # Build OHLC for lightweight charts
        ohlc = []
        for _, row in df.iterrows():
            ohlc.append({
                "time": str(row["date"]),
                "open": round(float(row["open"]), 2),
                "high": round(float(row["high"]), 2),
                "low": round(float(row["low"]), 2),
                "close": round(float(row["close"]), 2),
            })

        dates = df["date"].tolist()

        # MACD, RSI, Koncorde
        macd_df = ind["macd"]
        rsi_df = ind["rsi"]
        konc_df = ind["koncorde"]

        def _clean_list(vals, decimals=2):
            out = []
            for x in vals:
                v = float(x)
                out.append(round(v, decimals) if not (math.isnan(v) or math.isinf(v)) else 0)
            return out

        chart = {
            "ohlc": ohlc,
            "dates": dates,
            "macd": {
                "macd": _clean_list(macd_df["macd"].tolist()),
                "signal": _clean_list(macd_df["signal"].tolist()),
                "hist": _clean_list(macd_df["hist"].tolist()),
            },
            "rsi": _clean_list(rsi_df["rsi"].tolist(), 1),
            "koncorde": {
                "verde": _clean_list(konc_df["verde"].tolist(), 1),
                "marron": _clean_list(konc_df["marron"].tolist(), 1),
                "azul": _clean_list(konc_df["azul"].tolist(), 1),
                "media": _clean_list(konc_df["media"].tolist(), 1),
            },
        }

        # Moving averages
        close = df["close"]
        mas = {}
        for p in [20, 50]:
            if len(close) >= p:
                ma_series = indicators.sma(close, p)
                mas[f"sma{p}"] = [round(float(x), 2) for x in ma_series.tolist()]
        chart["mas"] = mas

        # Indicator values at entry/exit
        ind_at_entry = _get_indicator_values_at_date(ind, entry_date, dates)
        ind_at_exit = _get_indicator_values_at_date(ind, exit_date, dates)

        chart["indicators_at_entry"] = ind_at_entry
        chart["indicators_at_exit"] = ind_at_exit

        # SPY context
        try:
            spy = yf.Ticker("SPY")
            spy_df = spy.history(start=d1.strftime("%Y-%m-%d"), end=d2.strftime("%Y-%m-%d"), interval="1d")
            if spy_df is not None and len(spy_df) >= 2:
                spy_start = float(spy_df["Close"].iloc[0])
                spy_entry_idx = None
                spy_exit_idx = None
                spy_dates = spy_df.index.strftime("%Y-%m-%d").tolist()
                for i, sd in enumerate(spy_dates):
                    if sd <= entry_date:
                        spy_entry_idx = i
                    if sd <= exit_date:
                        spy_exit_idx = i
                spy_at_entry = float(spy_df["Close"].iloc[spy_entry_idx]) if spy_entry_idx is not None else spy_start
                spy_at_exit = float(spy_df["Close"].iloc[spy_exit_idx]) if spy_exit_idx is not None else float(spy_df["Close"].iloc[-1])
                spy_change = (spy_at_exit - spy_at_entry) / spy_at_entry * 100
                if spy_change > 3:
                    chart["market_context"] = f"Mercado alcista durante el trade (S&P500 +{spy_change:.1f}%)."
                elif spy_change < -3:
                    chart["market_context"] = f"Mercado bajista durante el trade (S&P500 {spy_change:.1f}%)."
                else:
                    chart["market_context"] = f"Mercado lateral durante el trade (S&P500 {spy_change:+.1f}%)."
        except Exception:
            chart["market_context"] = ""

        return chart
    except Exception as e:
        print(f"  [TradesHistory] Error fetching chart for {symbol}: {e}")
        return None


@flask_app.route("/api/trades-history")
def api_trades_history():
    """Returns all completed trades with summary stats."""
    with trades_history_lock:
        if trades_history_cache["data"] and time.time() - trades_history_cache["ts"] < TRADES_HISTORY_TTL:
            return Response(to_json(trades_history_cache["data"]), mimetype="application/json")

    result = build_trades_history()

    with trades_history_lock:
        trades_history_cache["data"] = result
        trades_history_cache["ts"] = time.time()

    return Response(to_json(result), mimetype="application/json")


@flask_app.route("/api/trades-history/chart/<trade_id>")
def api_trade_chart(trade_id):
    """Fetch chart data for a specific trade on demand."""
    from flask import request as flask_req

    symbol = flask_req.args.get("symbol", "")
    entry_date = flask_req.args.get("entry", "")
    exit_date = flask_req.args.get("exit", "")

    if not symbol or not entry_date or not exit_date:
        return Response(to_json({"error": "Faltan parametros"}), status=400, mimetype="application/json")

    chart = _fetch_trade_chart_data(symbol.upper(), entry_date, exit_date)
    if chart is None:
        return Response(to_json({"error": f"No se pudieron obtener datos para {symbol}"}),
                        status=404, mimetype="application/json")

    # Generate thesis
    ind_entry = chart.get("indicators_at_entry")
    ind_exit = chart.get("indicators_at_exit")
    entry_thesis, exit_thesis = _generate_trade_thesis(symbol, entry_date, exit_date, ind_entry, ind_exit)
    chart["entry_thesis"] = entry_thesis
    chart["exit_thesis"] = exit_thesis

    return Response(to_json(chart), mimetype="application/json")


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

def main():
    global ib_app, stock_list

    print("VISTA WEB - TOP 100 VOLUMEN - MACD + RSI + KONCORDE")
    print(f"Conectando a TWS ({config.IB_HOST}:{config.IB_PORT})...\n")

    # 1. Get top volume stocks via scanner
    print(f"Escaneando top {config.SCAN_COUNT} acciones por volumen...")
    stocks = get_top_volume_stocks()
    if not stocks:
        print("ERROR: No se obtuvieron acciones del scanner.")

    stock_list = [s["symbol"] for s in stocks] if stocks else []
    ib_app = None

    if stock_list:
        print(f"Obtenidas {len(stock_list)} acciones: {', '.join(stock_list[:10])}...\n")

        # 2. Connect IB for historical data + market data
        ib_app = VistaIB()
        ib_app.connect(config.IB_HOST, config.IB_PORT, config.VISTA_CLIENT_ID)

        ib_thread = threading.Thread(target=ib_app.run, daemon=True)
        ib_thread.start()

        if not ib_app.connected_event.wait(timeout=10):
            print("ERROR: No se pudo conectar a TWS. Dashboard arranca sin datos en vivo.")
            ib_app = None
        else:
            print("Conectado a TWS!\n")
            ib_app.reqMarketDataType(3)
            time.sleep(0.5)

            print(f"Suscribiendo market data para {len(stock_list)} simbolos...")
            for i, symbol in enumerate(stock_list):
                ib_app.reqMktData(5000 + i, make_contract(symbol), "", False, False, [])
                time.sleep(0.2)

            analysis_thread = threading.Thread(target=analysis_loop, daemon=True)
            analysis_thread.start()
    else:
        print("Iniciando dashboard sin conexion a TWS (solo trades importados)...\n")

    print(f"\nDashboard en: http://localhost:5050")
    print("Ctrl+C para detener\n")

    try:
        flask_app.run(host="0.0.0.0", port=5050, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        print("\nDeteniendo...")
        if ib_app:
            for i in range(len(stock_list)):
                ib_app.cancelMktData(5000 + i)
            ib_app.disconnect()
        print("Desconectado.")


if __name__ == "__main__":
    main()
