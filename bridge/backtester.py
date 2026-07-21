"""
Backtesting del sistema MACD + RSI + Koncorde (copia standalone para el bridge).

Recorre datos historicos dia a dia, replica las mismas condiciones
de bridge/signals.py, simula trades con stop-loss/take-profit, y calcula
metricas de rendimiento y confianza.

Funcion pura: no depende de IB, solo recibe un DataFrame OHLCV.
Duplicado deliberadamente de backtester.py (raiz) para que el paquete
"bridge" siga siendo autonomo y instalable via pip sin depender de
modulos sueltos del repo (mismo patron que bridge/indicators.py y
bridge/signals.py).
"""

import math
import numpy as np

STOP_LOSS_PCT = 3.0
TAKE_PROFIT_PCT = 8.0
BACKTEST_MAX_HOLD_DAYS = 20
BACKTEST_WARMUP_BARS = 260
BACKTEST_COST_PCT = 0.10      # coste round-trip por trade (comision + slippage), en %
BACKTEST_ROBUST_TRADES = 12   # nº de trades no-solapados para peso de confianza pleno
BACKTEST_COOLDOWN = True      # no abrir un nuevo trade hasta cerrar el anterior
BACKTEST_TREND_SMA = 200      # SMA para clasificar regimen (con/contra tendencia)


def run_backtest(df, indicators_dict, stop_loss_pct=None,
                 take_profit_pct=None, max_hold_days=None,
                 warmup_bars=None, cost_pct=None, cooldown=None):
    """
    Backtest de la estrategia sobre un DataFrame completo.

    Args:
        df: DataFrame con columnas date, open, high, low, close, volume
        indicators_dict: dict con "macd", "rsi", "koncorde" (DataFrames ya calculados)
        stop_loss_pct/take_profit_pct/max_hold_days/warmup_bars: overrides opcionales
        cost_pct: coste round-trip por trade en % (comision + slippage)
        cooldown: si True, no abre un nuevo trade hasta cerrar el anterior (evita solapes)

    Returns:
        dict con metricas de backtesting
    """
    if stop_loss_pct is None:
        stop_loss_pct = STOP_LOSS_PCT
    if take_profit_pct is None:
        take_profit_pct = TAKE_PROFIT_PCT
    if max_hold_days is None:
        max_hold_days = BACKTEST_MAX_HOLD_DAYS
    if warmup_bars is None:
        warmup_bars = BACKTEST_WARMUP_BARS
    if cost_pct is None:
        cost_pct = BACKTEST_COST_PCT
    if cooldown is None:
        cooldown = BACKTEST_COOLDOWN

    if df is None or len(df) < warmup_bars + max_hold_days:
        return _empty_result()

    koncorde_df = indicators_dict["koncorde"]
    macd_df = indicators_dict["macd"]
    rsi_df = indicators_dict["rsi"]

    closes = df["close"].values.astype(float)
    hist_vals = macd_df["hist"].values.astype(float)
    rsi_vals = rsi_df["rsi"].values.astype(float)
    marron_vals = koncorde_df["marron"].values.astype(float)
    media_vals = koncorde_df["media"].values.astype(float)
    trend_sma = _sma(closes, BACKTEST_TREND_SMA)

    n = len(df)
    buy_trades = []
    sell_trades = []
    blocked_until = -1

    for i in range(warmup_bars, n):
        if i < 2:
            continue
        if cooldown and i <= blocked_until:
            continue

        h = hist_vals[i]
        h1 = hist_vals[i - 1]
        r = rsi_vals[i]
        m = marron_vals[i]
        m1 = marron_vals[i - 1]
        med = media_vals[i]

        if (math.isnan(h) or math.isnan(h1) or math.isnan(r) or
                math.isnan(m) or math.isnan(m1) or math.isnan(med)):
            continue

        sma_i = trend_sma[i]

        macd_buy = h < 0 and h > h1
        rsi_buy = r < 30
        konc_buy = m < med and m > m1

        if macd_buy and rsi_buy and konc_buy:
            trade = _simulate_long(closes, i, stop_loss_pct,
                                   take_profit_pct, max_hold_days, cost_pct)
            if trade is not None:
                trade["with_trend"] = (not math.isnan(sma_i)) and closes[i] > sma_i
                buy_trades.append(trade)
                if cooldown:
                    blocked_until = trade["exit_idx"]

        macd_sell = h > 0 and h < h1
        rsi_sell = r > 70
        konc_sell = m > med and m < m1

        if macd_sell and rsi_sell and konc_sell:
            trade = _simulate_short(closes, i, stop_loss_pct,
                                    take_profit_pct, max_hold_days, cost_pct)
            if trade is not None:
                trade["with_trend"] = (not math.isnan(sma_i)) and closes[i] < sma_i
                sell_trades.append(trade)
                if cooldown:
                    blocked_until = trade["exit_idx"]

    return _compute_metrics(buy_trades, sell_trades)


def _sma(closes, window):
    n = len(closes)
    out = np.full(n, np.nan)
    if window <= 0 or n < window:
        return out
    csum = np.cumsum(np.insert(closes, 0, 0.0))
    out[window - 1:] = (csum[window:] - csum[:-window]) / window
    return out


def _simulate_long(closes, entry_idx, sl_pct, tp_pct, max_days, cost_pct=0.0):
    entry = closes[entry_idx]
    if entry <= 0 or math.isnan(entry):
        return None

    sl = entry * (1 - sl_pct / 100)
    tp = entry * (1 + tp_pct / 100)
    n = len(closes)

    for j in range(1, max_days + 1):
        idx = entry_idx + j
        if idx >= n:
            return _trade(entry, closes[n - 1], n - 1, long=True, cost_pct=cost_pct)

        px = closes[idx]
        if math.isnan(px):
            continue

        if px <= sl:
            return _trade(entry, px, idx, long=True, cost_pct=cost_pct)
        if px >= tp:
            return _trade(entry, px, idx, long=True, cost_pct=cost_pct)

    exit_idx = min(entry_idx + max_days, n - 1)
    return _trade(entry, closes[exit_idx], exit_idx, long=True, cost_pct=cost_pct)


def _simulate_short(closes, entry_idx, sl_pct, tp_pct, max_days, cost_pct=0.0):
    entry = closes[entry_idx]
    if entry <= 0 or math.isnan(entry):
        return None

    sl = entry * (1 + sl_pct / 100)
    tp = entry * (1 - tp_pct / 100)
    n = len(closes)

    for j in range(1, max_days + 1):
        idx = entry_idx + j
        if idx >= n:
            return _trade(entry, closes[n - 1], n - 1, long=False, cost_pct=cost_pct)

        px = closes[idx]
        if math.isnan(px):
            continue

        if px >= sl:
            return _trade(entry, px, idx, long=False, cost_pct=cost_pct)
        if px <= tp:
            return _trade(entry, px, idx, long=False, cost_pct=cost_pct)

    exit_idx = min(entry_idx + max_days, n - 1)
    return _trade(entry, closes[exit_idx], exit_idx, long=False, cost_pct=cost_pct)


def _trade(entry, exit_px, exit_idx, long=True, cost_pct=0.0):
    if math.isnan(exit_px) or entry <= 0:
        return None
    if long:
        ret = (exit_px - entry) / entry * 100
    else:
        ret = (entry - exit_px) / entry * 100
    ret -= cost_pct
    return {"entry": entry, "exit": exit_px, "return_pct": ret, "exit_idx": exit_idx}


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _side_stats(trades):
    count = len(trades)
    if count == 0:
        return {
            "count": 0, "win_rate": 0.0, "avg_return": None,
            "avg_win": None, "avg_loss": None, "profit_factor": None,
            "expectancy": None, "edge_conf": 0.0,
            "count_trend": 0, "win_rate_trend": None,
        }

    rets = [t["return_pct"] for t in trades]
    mean = sum(rets) / count
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    win_rate = len(wins) / count
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (
        float("inf") if gross_win > 0 else 0.0)

    if count >= 2:
        std = float(np.std(rets, ddof=1))
    else:
        std = abs(mean) if mean != 0 else 1.0
    if std <= 1e-9:
        std = abs(mean) if mean != 0 else 1e-9
    t_stat = mean / (std / math.sqrt(count))
    prob_positive = _norm_cdf(t_stat)
    sample_w = min(1.0, count / BACKTEST_ROBUST_TRADES)
    edge_conf = 0.5 + (prob_positive - 0.5) * sample_w

    trend_trades = [t for t in trades if t.get("with_trend")]
    count_trend = len(trend_trades)
    if count_trend > 0:
        win_rate_trend = sum(1 for t in trend_trades if t["return_pct"] > 0) / count_trend
    else:
        win_rate_trend = None

    pf_out = round(profit_factor, 2) if profit_factor != float("inf") else 999.0
    return {
        "count": count,
        "win_rate": round(win_rate, 3),
        "avg_return": round(mean, 2),
        "avg_win": round(avg_win, 2) if avg_win is not None else None,
        "avg_loss": round(avg_loss, 2) if avg_loss is not None else None,
        "profit_factor": pf_out,
        "expectancy": round(mean, 2),
        "edge_conf": edge_conf,
        "count_trend": count_trend,
        "win_rate_trend": round(win_rate_trend, 3) if win_rate_trend is not None else None,
    }


def _compute_metrics(buy_trades, sell_trades):
    b = _side_stats(buy_trades)
    s = _side_stats(sell_trades)
    total = b["count"] + s["count"]

    if total == 0:
        confidence = 0.0
    else:
        blended = (b["edge_conf"] * b["count"] + s["edge_conf"] * s["count"]) / total
        confidence = max(0.0, (blended - 0.5) / 0.5) * 100

    return {
        "confidence": round(confidence, 1),
        "buy_avg_return": b["avg_return"],
        "sell_avg_return": s["avg_return"],
        "buy_count": b["count"],
        "sell_count": s["count"],
        "buy_win_rate": b["win_rate"],
        "sell_win_rate": s["win_rate"],
        "total_signals": total,
        "buy_expectancy": b["expectancy"],
        "sell_expectancy": s["expectancy"],
        "buy_avg_win": b["avg_win"],
        "sell_avg_win": s["avg_win"],
        "buy_avg_loss": b["avg_loss"],
        "sell_avg_loss": s["avg_loss"],
        "buy_profit_factor": b["profit_factor"],
        "sell_profit_factor": s["profit_factor"],
        "buy_win_rate_trend": b["win_rate_trend"],
        "sell_win_rate_trend": s["win_rate_trend"],
        "buy_count_trend": b["count_trend"],
        "sell_count_trend": s["count_trend"],
    }


def _empty_result():
    return {
        "confidence": 0.0,
        "buy_avg_return": None,
        "sell_avg_return": None,
        "buy_count": 0,
        "sell_count": 0,
        "buy_win_rate": 0.0,
        "sell_win_rate": 0.0,
        "total_signals": 0,
        "buy_expectancy": None,
        "sell_expectancy": None,
        "buy_avg_win": None,
        "sell_avg_win": None,
        "buy_avg_loss": None,
        "sell_avg_loss": None,
        "buy_profit_factor": None,
        "sell_profit_factor": None,
        "buy_win_rate_trend": None,
        "sell_win_rate_trend": None,
        "buy_count_trend": 0,
        "sell_count_trend": 0,
    }
