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

STOP_LOSS_PCT = 3.0
TAKE_PROFIT_PCT = 8.0
BACKTEST_MAX_HOLD_DAYS = 20
BACKTEST_WARMUP_BARS = 260


def run_backtest(df, indicators_dict, stop_loss_pct=None,
                 take_profit_pct=None, max_hold_days=None,
                 warmup_bars=None):
    """
    Backtest de la estrategia sobre un DataFrame completo.

    Args:
        df: DataFrame con columnas date, open, high, low, close, volume
        indicators_dict: dict con "macd", "rsi", "koncorde" (DataFrames ya calculados)
        stop_loss_pct/take_profit_pct/max_hold_days/warmup_bars: overrides opcionales

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

    n = len(df)
    buy_trades = []
    sell_trades = []

    for i in range(warmup_bars, n):
        if i < 2:
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

        macd_buy = h < 0 and h > h1
        rsi_buy = r < 30
        konc_buy = m < med and m > m1

        if macd_buy and rsi_buy and konc_buy:
            trade = _simulate_long(closes, i, stop_loss_pct,
                                   take_profit_pct, max_hold_days)
            if trade is not None:
                buy_trades.append(trade)

        macd_sell = h > 0 and h < h1
        rsi_sell = r > 70
        konc_sell = m > med and m < m1

        if macd_sell and rsi_sell and konc_sell:
            trade = _simulate_short(closes, i, stop_loss_pct,
                                    take_profit_pct, max_hold_days)
            if trade is not None:
                sell_trades.append(trade)

    return _compute_metrics(buy_trades, sell_trades)


def _simulate_long(closes, entry_idx, sl_pct, tp_pct, max_days):
    entry = closes[entry_idx]
    if entry <= 0 or math.isnan(entry):
        return None

    sl = entry * (1 - sl_pct / 100)
    tp = entry * (1 + tp_pct / 100)
    n = len(closes)

    for j in range(1, max_days + 1):
        idx = entry_idx + j
        if idx >= n:
            px = closes[n - 1]
            return _trade(entry, px, long=True)

        px = closes[idx]
        if math.isnan(px):
            continue

        if px <= sl:
            return _trade(entry, px, long=True)
        if px >= tp:
            return _trade(entry, px, long=True)

    exit_idx = min(entry_idx + max_days, n - 1)
    return _trade(entry, closes[exit_idx], long=True)


def _simulate_short(closes, entry_idx, sl_pct, tp_pct, max_days):
    entry = closes[entry_idx]
    if entry <= 0 or math.isnan(entry):
        return None

    sl = entry * (1 + sl_pct / 100)
    tp = entry * (1 - tp_pct / 100)
    n = len(closes)

    for j in range(1, max_days + 1):
        idx = entry_idx + j
        if idx >= n:
            px = closes[n - 1]
            return _trade(entry, px, long=False)

        px = closes[idx]
        if math.isnan(px):
            continue

        if px >= sl:
            return _trade(entry, px, long=False)
        if px <= tp:
            return _trade(entry, px, long=False)

    exit_idx = min(entry_idx + max_days, n - 1)
    return _trade(entry, closes[exit_idx], long=False)


def _trade(entry, exit_px, long=True):
    if math.isnan(exit_px) or entry <= 0:
        return None
    if long:
        ret = (exit_px - entry) / entry * 100
    else:
        ret = (entry - exit_px) / entry * 100
    return {"entry": entry, "exit": exit_px, "return_pct": ret}


def _compute_metrics(buy_trades, sell_trades):
    buy_count = len(buy_trades)
    sell_count = len(sell_trades)
    total = buy_count + sell_count

    if buy_count > 0:
        buy_rets = [t["return_pct"] for t in buy_trades]
        buy_avg = sum(buy_rets) / buy_count
        buy_wins = sum(1 for r in buy_rets if r > 0)
        buy_wr = buy_wins / buy_count
    else:
        buy_avg = None
        buy_wr = 0.0

    if sell_count > 0:
        sell_rets = [t["return_pct"] for t in sell_trades]
        sell_avg = sum(sell_rets) / sell_count
        sell_wins = sum(1 for r in sell_rets if r > 0)
        sell_wr = sell_wins / sell_count
    else:
        sell_avg = None
        sell_wr = 0.0

    if total == 0:
        confidence = 0.0
    else:
        buy_score = buy_wr * min(1.0, buy_count / 5)
        sell_score = sell_wr * min(1.0, sell_count / 5)
        raw = (buy_score + sell_score) / 2.0
        vol_factor = min(1.0, total / 10)
        confidence = raw * vol_factor * 100

    return {
        "confidence": round(confidence, 1),
        "buy_avg_return": round(buy_avg, 2) if buy_avg is not None else None,
        "sell_avg_return": round(sell_avg, 2) if sell_avg is not None else None,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "buy_win_rate": round(buy_wr, 3),
        "sell_win_rate": round(sell_wr, 3),
        "total_signals": total,
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
    }
