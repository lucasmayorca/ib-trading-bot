"""
Port del indicador Pine Script "MACD+RSI+KONCORDE YAMIL" a Python.
Replica exacta de la logica del script de TradingView.
"""

import numpy as np
import pandas as pd
import config


def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def sma(series, period):
    return series.rolling(window=period).mean()


def wma(series, period):
    weights = np.arange(1, period + 1, dtype=float)
    return series.rolling(window=period).apply(
        lambda x: np.dot(x, weights) / weights.sum(), raw=True
    )


def rsi(series, period):
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def stochastic(src, high, low, period, smooth_k, smooth_d):
    lowest_low = low.rolling(window=period).min()
    highest_high = high.rolling(window=period).max()
    raw_k = 100.0 * (src - lowest_low) / (highest_high - lowest_low)
    k = sma(raw_k, smooth_k)
    d = sma(k, smooth_d)
    return k, d


def calc_pvi_nvi(close, volume):
    """Calcula Positive Volume Index y Negative Volume Index."""
    pvi = pd.Series(1000.0, index=close.index, dtype=float)
    nvi = pd.Series(1000.0, index=close.index, dtype=float)

    for i in range(1, len(close)):
        price_change = (close.iloc[i] - close.iloc[i - 1]) / close.iloc[i - 1]
        if volume.iloc[i] > volume.iloc[i - 1]:
            pvi.iloc[i] = pvi.iloc[i - 1] * (1 + price_change)
            nvi.iloc[i] = nvi.iloc[i - 1]
        else:
            pvi.iloc[i] = pvi.iloc[i - 1]
            nvi.iloc[i] = nvi.iloc[i - 1] * (1 + price_change)

    return pvi, nvi


def calc_mfi(hlc3, volume, period):
    """Money Flow Index - replica de la funcion calc_mfi del Pine Script."""
    change = hlc3.diff()
    pos_flow = (volume * hlc3).where(change > 0, 0.0)
    neg_flow = (volume * hlc3).where(change < 0, 0.0)
    pos_sum = pos_flow.rolling(window=period).sum()
    neg_sum = neg_flow.rolling(window=period).sum()
    mfi = 100.0 - (100.0 / (1.0 + pos_sum / neg_sum.replace(0, np.nan)))
    return mfi


def calculate_koncorde(df):
    """
    Calcula el indicador Koncorde.
    Retorna DataFrame con columnas: verde, marron, azul, media
    """
    c = config
    open_ = df["open"]
    high = df["high"]
    low = df["low"]
    close = df["close"]
    volume = df["volume"]

    tprice = (open_ + high + low + close) / 4.0  # ohlc4
    hlc3 = (high + low + close) / 3.0

    # PVI / NVI
    pvi, nvi = calc_pvi_nvi(close, volume)

    m = c.KONCORDE_PVI_NVI_PERIOD
    rng = c.KONCORDE_PVI_NVI_RANGE

    pvim = ema(pvi, m)
    pvimax = pvim.rolling(window=rng).max()
    pvimin = pvim.rolling(window=rng).min()
    oscp = (pvi - pvim) * 100.0 / (pvimax - pvimin).replace(0, np.nan)

    nvim = ema(nvi, m)
    nvimax = nvim.rolling(window=rng).max()
    nvimin = nvim.rolling(window=rng).min()
    azul = (nvi - nvim) * 100.0 / (nvimax - nvimin).replace(0, np.nan)

    # MFI
    xmf = calc_mfi(hlc3, volume, c.KONCORDE_MFI_PERIOD)

    # Bollinger Bands Oscillator
    basis = sma(tprice, c.KONCORDE_BB_PERIOD)
    dev = tprice.rolling(window=c.KONCORDE_BB_PERIOD).std() * c.KONCORDE_BB_MULT
    upper = basis + dev
    lower = basis - dev
    ob1 = (upper + lower) / 2.0
    ob2 = upper - lower
    boll_osc = ((tprice - ob1) / ob2.replace(0, np.nan)) * 100.0

    # RSI sobre ohlc4
    xrsi = rsi(tprice, c.KONCORDE_RSI_PERIOD)

    # Estocastico
    lowest_low = low.rolling(window=c.KONCORDE_STOCH_PERIOD).min()
    highest_high = high.rolling(window=c.KONCORDE_STOCH_PERIOD).max()
    raw_stoch = 100.0 * (tprice - lowest_low) / (highest_high - lowest_low).replace(0, np.nan)
    stoc = sma(raw_stoch, c.KONCORDE_STOCH_SMOOTH)

    # Componentes finales
    marron = (xrsi + xmf + boll_osc + (stoc / 3.0)) / 2.0
    verde = marron + oscp
    media = ema(marron, c.KONCORDE_MEDIA_PERIOD)

    return pd.DataFrame({
        "verde": verde,
        "marron": marron,
        "azul": azul,
        "media": media,
    }, index=df.index)


def calculate_macd(df):
    """
    Calcula MACD normalizado (igual que el Pine Script).
    Retorna DataFrame con columnas: macd, signal, hist
    """
    c = config
    src = df["close"]

    fast_ma = ema(src, c.MACD_FAST)
    slow_ma = ema(src, c.MACD_SLOW)
    macd_val = (fast_ma - slow_ma) / slow_ma * 1000.0
    signal_val = ema(macd_val, c.MACD_SIGNAL)
    hist_val = macd_val - signal_val

    return pd.DataFrame({
        "macd": macd_val,
        "signal": signal_val,
        "hist": hist_val,
    }, index=df.index)


def calculate_rsi(df):
    """
    Calcula RSI con media movil WMA.
    Retorna DataFrame con columnas: rsi, rsi_ma
    """
    c = config
    src = df["close"]

    rsi_val = rsi(src, c.RSI_PERIOD)
    rsi_centered = rsi_val - 50.0  # Centrado en 0 como en el Pine Script
    rsi_ma = wma(rsi_centered, c.RSI_MA_PERIOD)

    return pd.DataFrame({
        "rsi": rsi_val,
        "rsi_centered": rsi_centered,
        "rsi_ma": rsi_ma,
    }, index=df.index)


def calculate_stochastic(df):
    """
    Calcula Estocastico (14, 1, 3).
    Retorna DataFrame con columnas: k, d
    """
    c = config
    k, d = stochastic(
        df["close"], df["high"], df["low"],
        c.STOCH_PERIOD, c.STOCH_SMOOTH_K, c.STOCH_SMOOTH_D
    )
    return pd.DataFrame({"k": k, "d": d}, index=df.index)


def calculate_all(df):
    """
    Calcula todos los indicadores sobre un DataFrame OHLCV.
    df debe tener columnas: open, high, low, close, volume
    Retorna dict con todos los indicadores.
    """
    koncorde = calculate_koncorde(df)
    macd = calculate_macd(df)
    rsi_data = calculate_rsi(df)
    stoch = calculate_stochastic(df)

    return {
        "koncorde": koncorde,
        "macd": macd,
        "rsi": rsi_data,
        "stoch": stoch,
    }
