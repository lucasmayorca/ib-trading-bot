"""
Backtesting del sistema MACD + RSI + Koncorde.

Recorre datos historicos dia a dia, replica las mismas condiciones
de signals.py, simula trades con stop-loss/take-profit, y calcula
metricas de rendimiento y confianza.

Funcion pura: no depende de IB, solo recibe un DataFrame OHLCV.
"""

import math
import numpy as np
import indicators
import config


def run_backtest(df, indicators_dict=None, stop_loss_pct=None,
                 take_profit_pct=None, max_hold_days=None,
                 warmup_bars=None, cost_pct=None, cooldown=None):
    """
    Backtest de la estrategia sobre un DataFrame completo.

    Args:
        df: DataFrame con columnas date, open, high, low, close, volume
        indicators_dict: indicadores pre-computados (opcional, evita recalculo)
        stop_loss_pct: % stop loss (default config.STOP_LOSS_PCT)
        take_profit_pct: % take profit (default config.TAKE_PROFIT_PCT)
        max_hold_days: dias maximo por trade (default config.BACKTEST_MAX_HOLD_DAYS)
        warmup_bars: barras iniciales a saltar (default config.BACKTEST_WARMUP_BARS)
        cost_pct: coste round-trip por trade en % (comision + slippage)
        cooldown: si True, no abre un nuevo trade hasta cerrar el anterior (evita solapes)

    Returns:
        dict con metricas de backtesting
    """
    if stop_loss_pct is None:
        stop_loss_pct = config.STOP_LOSS_PCT
    if take_profit_pct is None:
        take_profit_pct = config.TAKE_PROFIT_PCT
    if max_hold_days is None:
        max_hold_days = getattr(config, "BACKTEST_MAX_HOLD_DAYS", 20)
    if warmup_bars is None:
        warmup_bars = getattr(config, "BACKTEST_WARMUP_BARS", 260)
    if cost_pct is None:
        cost_pct = getattr(config, "BACKTEST_COST_PCT", 0.10)
    if cooldown is None:
        cooldown = getattr(config, "BACKTEST_COOLDOWN", True)
    trend_win = getattr(config, "BACKTEST_TREND_SMA", 200)

    # Datos insuficientes
    if df is None or len(df) < warmup_bars + max_hold_days:
        return _empty_result()

    # 1. Calcular indicadores una sola vez
    if indicators_dict is None:
        indicators_dict = indicators.calculate_all(df)

    koncorde_df = indicators_dict["koncorde"]
    macd_df = indicators_dict["macd"]
    rsi_df = indicators_dict["rsi"]

    # 2. Extraer arrays numpy para acceso rapido (evita overhead pandas)
    closes = df["close"].values.astype(float)
    hist_vals = macd_df["hist"].values.astype(float)
    rsi_vals = rsi_df["rsi"].values.astype(float)
    marron_vals = koncorde_df["marron"].values.astype(float)
    media_vals = koncorde_df["media"].values.astype(float)
    trend_sma = _sma(closes, trend_win)   # para clasificar regimen con/contra tendencia

    n = len(df)
    buy_trades = []
    sell_trades = []
    blocked_until = -1   # cooldown: indice hasta el que no se puede abrir otro trade

    # 3. Recorrer dia a dia desde warmup
    for i in range(warmup_bars, n):
        # Necesita al menos 2 barras previas para hist_prev, hist_prev2
        if i < 2:
            continue
        # Cooldown: ya hay una posicion abierta que no cerro
        if cooldown and i <= blocked_until:
            continue

        # Valores actuales y previos
        h = hist_vals[i]
        h1 = hist_vals[i - 1]
        r = rsi_vals[i]
        m = marron_vals[i]
        m1 = marron_vals[i - 1]
        med = media_vals[i]

        # Saltar si hay NaN
        if (math.isnan(h) or math.isnan(h1) or math.isnan(r) or
                math.isnan(m) or math.isnan(m1) or math.isnan(med)):
            continue

        sma_i = trend_sma[i]

        # --- Condiciones BUY (misma logica que signals.py) ---
        macd_buy = h < 0 and h > h1           # hist negativo pero subiendo
        rsi_buy = r < 30                        # RSI en sobreventa
        konc_buy = m < med and m > m1           # marron bajo media pero subiendo

        if macd_buy and rsi_buy and konc_buy:
            trade = _simulate_long(closes, i, stop_loss_pct,
                                   take_profit_pct, max_hold_days, cost_pct)
            if trade is not None:
                # Con-tendencia si el precio esta por encima de su SMA larga
                trade["with_trend"] = (not math.isnan(sma_i)) and closes[i] > sma_i
                buy_trades.append(trade)
                if cooldown:
                    blocked_until = trade["exit_idx"]

        # --- Condiciones SELL (misma logica que signals.py) ---
        macd_sell = h > 0 and h < h1           # hist positivo pero cayendo
        rsi_sell = r > 70                       # RSI en sobrecompra
        konc_sell = m > med and m < m1          # marron sobre media pero cayendo

        if macd_sell and rsi_sell and konc_sell:
            trade = _simulate_short(closes, i, stop_loss_pct,
                                    take_profit_pct, max_hold_days, cost_pct)
            if trade is not None:
                # Con-tendencia (bajista) si el precio esta por debajo de su SMA larga
                trade["with_trend"] = (not math.isnan(sma_i)) and closes[i] < sma_i
                sell_trades.append(trade)
                if cooldown:
                    blocked_until = trade["exit_idx"]

    # 4. Calcular metricas
    return _compute_metrics(buy_trades, sell_trades)


def _entry_strength(side, h, h1, h2, r, r1, m, m1, m2):
    """Fuerza de la senal en la barra de entrada (misma escala que signals.py,
    sin la componente azul de Koncorde que no esta en los arrays del backtest)."""
    strength = 1.0  # macd_ok siempre se cumple si hay senal
    if side == "buy":
        if h1 <= h2:
            strength += 0.5           # piso de MACD confirmado
        strength += 1.0               # rsi_ok
        if r < 20:
            strength += 0.5           # muy sobrevendido
        if r > r1:
            strength += 0.3           # ya rebotando
        strength += 1.0               # konc_ok
        if m1 <= m2:
            strength += 0.5           # piso de Koncorde confirmado
    else:
        if h1 >= h2:
            strength += 0.5
        strength += 1.0
        if r > 80:
            strength += 0.5
        if r < r1:
            strength += 0.3
        strength += 1.0
        if m1 >= m2:
            strength += 0.5
    return strength


def run_calibration_trades(df, indicators_dict=None, warmup_bars=None,
                           stop_loss_pct=None, take_profit_pct=None,
                           max_hold_days=None, cost_pct=None):
    """Como run_backtest pero devuelve la lista de trades individuales con la
    fuerza de senal calculada en la entrada. Alimenta el diagrama de calibracion:
    permite agrupar por fuerza/condiciones y medir win-rate y retorno realizados.

    Returns: lista de dicts {side, strength, conditions_met, with_trend, return_pct, win}.
    """
    if stop_loss_pct is None:
        stop_loss_pct = config.STOP_LOSS_PCT
    if take_profit_pct is None:
        take_profit_pct = config.TAKE_PROFIT_PCT
    if max_hold_days is None:
        max_hold_days = getattr(config, "BACKTEST_MAX_HOLD_DAYS", 20)
    if warmup_bars is None:
        warmup_bars = getattr(config, "BACKTEST_WARMUP_BARS", 260)
    if cost_pct is None:
        cost_pct = getattr(config, "BACKTEST_COST_PCT", 0.10)
    cooldown = getattr(config, "BACKTEST_COOLDOWN", True)
    trend_win = getattr(config, "BACKTEST_TREND_SMA", 200)

    if df is None or len(df) < warmup_bars + max_hold_days:
        return []
    if indicators_dict is None:
        indicators_dict = indicators.calculate_all(df)

    closes = df["close"].values.astype(float)
    hist_vals = indicators_dict["macd"]["hist"].values.astype(float)
    rsi_vals = indicators_dict["rsi"]["rsi"].values.astype(float)
    marron_vals = indicators_dict["koncorde"]["marron"].values.astype(float)
    media_vals = indicators_dict["koncorde"]["media"].values.astype(float)
    trend_sma = _sma(closes, trend_win)

    n = len(df)
    trades = []
    blocked_until = -1

    for i in range(warmup_bars, n):
        if i < 3:
            continue
        if cooldown and i <= blocked_until:
            continue
        h, h1, h2 = hist_vals[i], hist_vals[i - 1], hist_vals[i - 2]
        r, r1 = rsi_vals[i], rsi_vals[i - 1]
        m, m1, m2 = marron_vals[i], marron_vals[i - 1], marron_vals[i - 2]
        med = media_vals[i]
        if (math.isnan(h) or math.isnan(h1) or math.isnan(h2) or math.isnan(r) or
                math.isnan(r1) or math.isnan(m) or math.isnan(m1) or
                math.isnan(m2) or math.isnan(med)):
            continue
        sma_i = trend_sma[i]

        side = None
        if h < 0 and h > h1 and r < 30 and m < med and m > m1:
            side = "buy"
            trade = _simulate_long(closes, i, stop_loss_pct, take_profit_pct,
                                   max_hold_days, cost_pct)
            with_trend = (not math.isnan(sma_i)) and closes[i] > sma_i
        elif h > 0 and h < h1 and r > 70 and m > med and m < m1:
            side = "sell"
            trade = _simulate_short(closes, i, stop_loss_pct, take_profit_pct,
                                    max_hold_days, cost_pct)
            with_trend = (not math.isnan(sma_i)) and closes[i] < sma_i
        else:
            continue

        if trade is None:
            continue
        if cooldown:
            blocked_until = trade["exit_idx"]
        strength = _entry_strength(side, h, h1, h2, r, r1, m, m1, m2)
        trades.append({
            "side": side,
            "strength": round(strength, 2),
            "conditions_met": 3,     # las 3 condiciones se cumplen en toda senal
            "with_trend": bool(with_trend),
            "return_pct": round(trade["return_pct"], 3),
            "win": trade["return_pct"] > 0,
        })

    return trades


def _sma(closes, window):
    """SMA simple con NaN en el warmup (vectorizado)."""
    n = len(closes)
    out = np.full(n, np.nan)
    if window <= 0 or n < window:
        return out
    csum = np.cumsum(np.insert(closes, 0, 0.0))
    out[window - 1:] = (csum[window:] - csum[:-window]) / window
    return out


def _simulate_long(closes, entry_idx, sl_pct, tp_pct, max_days, cost_pct=0.0):
    """
    Simula trade long:
      Entry: close[entry_idx]
      Exit: primero de stop-loss, take-profit, o max_days
    """
    entry = closes[entry_idx]
    if entry <= 0 or math.isnan(entry):
        return None

    sl = entry * (1 - sl_pct / 100)
    tp = entry * (1 + tp_pct / 100)
    n = len(closes)

    for j in range(1, max_days + 1):
        idx = entry_idx + j
        if idx >= n:
            # Fin de datos
            return _trade(entry, closes[n - 1], n - 1, long=True, cost_pct=cost_pct)

        px = closes[idx]
        if math.isnan(px):
            continue

        if px <= sl:
            return _trade(entry, px, idx, long=True, cost_pct=cost_pct)
        if px >= tp:
            return _trade(entry, px, idx, long=True, cost_pct=cost_pct)

    # Max hold alcanzado
    exit_idx = min(entry_idx + max_days, n - 1)
    return _trade(entry, closes[exit_idx], exit_idx, long=True, cost_pct=cost_pct)


def _simulate_short(closes, entry_idx, sl_pct, tp_pct, max_days, cost_pct=0.0):
    """
    Simula trade short:
      Entry short: close[entry_idx]
      SL: precio sube sl_pct% (perdida)
      TP: precio baja tp_pct% (ganancia)
    """
    entry = closes[entry_idx]
    if entry <= 0 or math.isnan(entry):
        return None

    sl = entry * (1 + sl_pct / 100)    # precio sube = perdida
    tp = entry * (1 - tp_pct / 100)    # precio baja = ganancia
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
    """Calcula retorno neto de un trade (restando el coste round-trip)."""
    if math.isnan(exit_px) or entry <= 0:
        return None
    if long:
        ret = (exit_px - entry) / entry * 100
    else:
        ret = (entry - exit_px) / entry * 100
    ret -= cost_pct   # comision + slippage de entrada y salida
    return {"entry": entry, "exit": exit_px, "return_pct": ret, "exit_idx": exit_idx}


def _norm_cdf(x):
    """CDF de la normal estandar (probabilidad una cola)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _side_stats(trades):
    """
    Estadisticas de una direccion (buy o sell) a partir de sus retornos netos.

    En vez de reportar solo win rate, calcula:
      - expectancy: retorno medio por trade (ya neto de costes)
      - avg_win / avg_loss / profit_factor
      - t-stat de que la expectancy sea > 0 y una confianza calibrada:
        Phi(t) = prob (una cola) de que el edge real sea positivo, atenuada
        por el tamano de muestra (shrinkage hacia 0.5 cuando hay pocos trades).
      - win rate con-tendencia (regimen alineado)
    """
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

    # Confianza calibrada: significancia de expectancy>0 con shrinkage por muestra
    robust_n = getattr(config, "BACKTEST_ROBUST_TRADES", 12)
    if count >= 2:
        std = float(np.std(rets, ddof=1))
    else:
        std = abs(mean) if mean != 0 else 1.0
    if std <= 1e-9:
        std = abs(mean) if mean != 0 else 1e-9
    t_stat = mean / (std / math.sqrt(count))
    prob_positive = _norm_cdf(t_stat)          # 0.5 si no hay edge, ->1 si edge robusto y positivo
    sample_w = min(1.0, count / robust_n)      # shrinkage: pocos trades => menos peso
    edge_conf = 0.5 + (prob_positive - 0.5) * sample_w   # atenua hacia 0.5

    # Regimen: win rate solo con trades con-tendencia
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
    """Calcula confianza calibrada, expectancy, retornos y win rates por direccion."""
    b = _side_stats(buy_trades)
    s = _side_stats(sell_trades)
    total = b["count"] + s["count"]

    # Confianza global (0-100): mezcla ponderada por muestra de la confianza
    # calibrada de cada direccion. Una direccion sin trades no aporta.
    if total == 0:
        confidence = 0.0
    else:
        blended = (b["edge_conf"] * b["count"] + s["edge_conf"] * s["count"]) / total
        # Reescala 0.5..1.0 (sin edge..edge fuerte) a 0..100 para conservar el rango
        # historico de los umbrales de la UI (30/60).
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
        # --- Nuevas metricas de calidad del edge ---
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
    """Resultado vacio para stocks con datos insuficientes."""
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
