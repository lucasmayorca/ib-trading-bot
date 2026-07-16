"""
Signal generation for the bridge (standalone).
BUY/SELL when all 3 indicators align.
"""


def check_buy_conditions(koncorde_df, macd_df, rsi_df):
    if len(koncorde_df) < 5 or len(macd_df) < 5 or len(rsi_df) < 5:
        return False, {"razon": "Datos insuficientes"}

    hist = macd_df["hist"].iloc[-1]
    hist_prev = macd_df["hist"].iloc[-2]
    hist_prev2 = macd_df["hist"].iloc[-3]

    macd_oversold = hist < 0
    macd_turning = hist > hist_prev
    macd_had_bottom = hist_prev <= hist_prev2
    macd_ok = macd_oversold and macd_turning
    macd_detail = f"hist={hist:.2f} prev={hist_prev:.2f}"
    if macd_ok and macd_had_bottom:
        macd_detail += " PISO CONFIRMADO"
    elif macd_ok:
        macd_detail += " girando"

    rsi_val = rsi_df["rsi"].iloc[-1]
    rsi_prev = rsi_df["rsi"].iloc[-2]
    rsi_ok = rsi_val < 30
    rsi_detail = f"RSI={rsi_val:.1f}"
    if rsi_ok:
        rsi_detail += " SOBREVENTA"
        if rsi_val > rsi_prev:
            rsi_detail += " rebotando"

    marron = koncorde_df["marron"].iloc[-1]
    marron_prev = koncorde_df["marron"].iloc[-2]
    marron_prev2 = koncorde_df["marron"].iloc[-3]
    azul = koncorde_df["azul"].iloc[-1]
    media = koncorde_df["media"].iloc[-1]

    konc_negative = marron < media
    konc_turning = marron > marron_prev
    konc_had_bottom = marron_prev <= marron_prev2
    konc_ok = konc_negative and konc_turning
    konc_detail = f"marron={marron:.1f} media={media:.1f}"
    if konc_ok and konc_had_bottom:
        konc_detail += " PISO CONFIRMADO"
    elif konc_ok:
        konc_detail += " girando"
    if azul > 0:
        konc_detail += f" institucional+(azul={azul:.1f})"

    is_signal = macd_ok and rsi_ok and konc_ok

    strength = 0
    if macd_ok:
        strength += 1
        if macd_had_bottom:
            strength += 0.5
    if rsi_ok:
        strength += 1
        if rsi_val < 20:
            strength += 0.5
        if rsi_val > rsi_prev:
            strength += 0.3
    if konc_ok:
        strength += 1
        if konc_had_bottom:
            strength += 0.5
        if azul > 0:
            strength += 0.3

    return is_signal, {
        "macd_ok": macd_ok, "rsi_ok": rsi_ok, "konc_ok": konc_ok,
        "macd_detail": macd_detail, "rsi_detail": rsi_detail, "konc_detail": konc_detail,
        "strength": strength, "conditions_met": sum([macd_ok, rsi_ok, konc_ok]),
    }


def check_sell_conditions(koncorde_df, macd_df, rsi_df):
    if len(koncorde_df) < 5 or len(macd_df) < 5 or len(rsi_df) < 5:
        return False, {"razon": "Datos insuficientes"}

    hist = macd_df["hist"].iloc[-1]
    hist_prev = macd_df["hist"].iloc[-2]
    hist_prev2 = macd_df["hist"].iloc[-3]

    macd_overbought = hist > 0
    macd_turning = hist < hist_prev
    macd_had_peak = hist_prev >= hist_prev2
    macd_ok = macd_overbought and macd_turning
    macd_detail = f"hist={hist:.2f} prev={hist_prev:.2f}"
    if macd_ok and macd_had_peak:
        macd_detail += " TECHO CONFIRMADO"
    elif macd_ok:
        macd_detail += " girando"

    rsi_val = rsi_df["rsi"].iloc[-1]
    rsi_prev = rsi_df["rsi"].iloc[-2]
    rsi_ok = rsi_val > 70
    rsi_detail = f"RSI={rsi_val:.1f}"
    if rsi_ok:
        rsi_detail += " SOBRECOMPRA"
        if rsi_val < rsi_prev:
            rsi_detail += " cayendo"

    marron = koncorde_df["marron"].iloc[-1]
    marron_prev = koncorde_df["marron"].iloc[-2]
    marron_prev2 = koncorde_df["marron"].iloc[-3]
    azul = koncorde_df["azul"].iloc[-1]
    media = koncorde_df["media"].iloc[-1]

    konc_positive = marron > media
    konc_turning = marron < marron_prev
    konc_had_peak = marron_prev >= marron_prev2
    konc_ok = konc_positive and konc_turning
    konc_detail = f"marron={marron:.1f} media={media:.1f}"
    if konc_ok and konc_had_peak:
        konc_detail += " TECHO CONFIRMADO"
    elif konc_ok:
        konc_detail += " girando"
    if azul < 0:
        konc_detail += f" institucional-(azul={azul:.1f})"

    is_signal = macd_ok and rsi_ok and konc_ok

    strength = 0
    if macd_ok:
        strength += 1
        if macd_had_peak:
            strength += 0.5
    if rsi_ok:
        strength += 1
        if rsi_val > 80:
            strength += 0.5
        if rsi_val < rsi_prev:
            strength += 0.3
    if konc_ok:
        strength += 1
        if konc_had_peak:
            strength += 0.5
        if azul < 0:
            strength += 0.3

    return is_signal, {
        "macd_ok": macd_ok, "rsi_ok": rsi_ok, "konc_ok": konc_ok,
        "macd_detail": macd_detail, "rsi_detail": rsi_detail, "konc_detail": konc_detail,
        "strength": strength, "conditions_met": sum([macd_ok, rsi_ok, konc_ok]),
    }


def generate_signal(indicators):
    koncorde = indicators["koncorde"]
    macd = indicators["macd"]
    rsi_data = indicators["rsi"]

    is_buy, buy_details = check_buy_conditions(koncorde, macd, rsi_data)
    is_sell, sell_details = check_sell_conditions(koncorde, macd, rsi_data)

    if is_buy:
        signal, details = "BUY", buy_details
    elif is_sell:
        signal, details = "SELL", sell_details
    else:
        buy_met = buy_details.get("conditions_met", 0)
        sell_met = sell_details.get("conditions_met", 0)
        details = buy_details if buy_met >= sell_met else sell_details
        signal = "HOLD"

    last_vals = {}
    if len(koncorde) > 0:
        k = koncorde.iloc[-1]
        last_vals["koncorde"] = {"verde": k["verde"], "marron": k["marron"], "azul": k["azul"], "media": k["media"]}
    if len(macd) > 0:
        m = macd.iloc[-1]
        last_vals["macd"] = {"macd": m["macd"], "signal": m["signal"], "hist": m["hist"]}
    if len(rsi_data) > 0:
        last_vals["rsi"] = rsi_data.iloc[-1]["rsi"]

    return {
        "signal": signal,
        "score": details.get("strength", 0),
        "conditions_met": details.get("conditions_met", 0),
        "macd_detail": details.get("macd_detail", ""),
        "rsi_detail": details.get("rsi_detail", ""),
        "koncorde_detail": details.get("konc_detail", ""),
        "values": last_vals,
    }
