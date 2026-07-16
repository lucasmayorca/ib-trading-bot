"""
Indicators for the bridge (standalone, no dependency on root config.py).
Port of MACD+RSI+KONCORDE YAMIL Pine Script.
"""

import numpy as np
import pandas as pd

# Default params (same as Pine Script)
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
RSI_PERIOD, RSI_MA_PERIOD = 14, 21
KONCORDE_EMA_LENGTH = 255
KONCORDE_PVI_NVI_PERIOD, KONCORDE_PVI_NVI_RANGE = 15, 90
KONCORDE_MFI_PERIOD = 14
KONCORDE_BB_PERIOD, KONCORDE_BB_MULT = 25, 2.0
KONCORDE_RSI_PERIOD = 14
KONCORDE_STOCH_PERIOD, KONCORDE_STOCH_SMOOTH = 21, 3
KONCORDE_MEDIA_PERIOD = 21


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


def calc_pvi_nvi(close, volume):
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
    change = hlc3.diff()
    pos_flow = (volume * hlc3).where(change > 0, 0.0)
    neg_flow = (volume * hlc3).where(change < 0, 0.0)
    pos_sum = pos_flow.rolling(window=period).sum()
    neg_sum = neg_flow.rolling(window=period).sum()
    return 100.0 - (100.0 / (1.0 + pos_sum / neg_sum.replace(0, np.nan)))


def calculate_koncorde(df):
    open_, high, low, close, volume = df["open"], df["high"], df["low"], df["close"], df["volume"]
    tprice = (open_ + high + low + close) / 4.0
    hlc3 = (high + low + close) / 3.0

    pvi, nvi = calc_pvi_nvi(close, volume)
    m, rng = KONCORDE_PVI_NVI_PERIOD, KONCORDE_PVI_NVI_RANGE

    pvim = ema(pvi, m)
    pvimax = pvim.rolling(window=rng).max()
    pvimin = pvim.rolling(window=rng).min()
    oscp = (pvi - pvim) * 100.0 / (pvimax - pvimin).replace(0, np.nan)

    nvim = ema(nvi, m)
    nvimax = nvim.rolling(window=rng).max()
    nvimin = nvim.rolling(window=rng).min()
    azul = (nvi - nvim) * 100.0 / (nvimax - nvimin).replace(0, np.nan)

    xmf = calc_mfi(hlc3, volume, KONCORDE_MFI_PERIOD)

    basis = sma(tprice, KONCORDE_BB_PERIOD)
    dev = tprice.rolling(window=KONCORDE_BB_PERIOD).std() * KONCORDE_BB_MULT
    upper, lower = basis + dev, basis - dev
    ob1 = (upper + lower) / 2.0
    ob2 = upper - lower
    boll_osc = ((tprice - ob1) / ob2.replace(0, np.nan)) * 100.0

    xrsi = rsi(tprice, KONCORDE_RSI_PERIOD)

    lowest_low = low.rolling(window=KONCORDE_STOCH_PERIOD).min()
    highest_high = high.rolling(window=KONCORDE_STOCH_PERIOD).max()
    raw_stoch = 100.0 * (tprice - lowest_low) / (highest_high - lowest_low).replace(0, np.nan)
    stoc = sma(raw_stoch, KONCORDE_STOCH_SMOOTH)

    marron = (xrsi + xmf + boll_osc + (stoc / 3.0)) / 2.0
    verde = marron + oscp
    media = ema(marron, KONCORDE_MEDIA_PERIOD)

    return pd.DataFrame({"verde": verde, "marron": marron, "azul": azul, "media": media}, index=df.index)


def calculate_macd(df):
    src = df["close"]
    fast_ma = ema(src, MACD_FAST)
    slow_ma = ema(src, MACD_SLOW)
    macd_val = (fast_ma - slow_ma) / slow_ma * 1000.0
    signal_val = ema(macd_val, MACD_SIGNAL)
    hist_val = macd_val - signal_val
    return pd.DataFrame({"macd": macd_val, "signal": signal_val, "hist": hist_val}, index=df.index)


def calculate_rsi(df):
    src = df["close"]
    rsi_val = rsi(src, RSI_PERIOD)
    rsi_centered = rsi_val - 50.0
    rsi_ma = wma(rsi_centered, RSI_MA_PERIOD)
    return pd.DataFrame({"rsi": rsi_val, "rsi_centered": rsi_centered, "rsi_ma": rsi_ma}, index=df.index)
