"""
IB Flex Web Service client.

reqExecutions() on the live TWS API connection only ever returns fills from
the current session -- there is no way to pull years of trade history
through the bridge's live connection, by IB's own design. The only
programmatic route to full history is the Flex Web Service: a separate
HTTP API, gated behind a token + query ID the account owner generates once
in IB Account Management (Reports > Flex Queries > Trade Confirmation Flex
Query, or a custom Activity Flex Query with the Trades section enabled).

This module turns that token+query_id into the same trade-dict shape
vista_web.py's build_trades_history() expects (symbol/action/date/
filled_qty/avg_fill_price/commission/realized_pnl/order_id), so it can be
merged with live_trades in cloud/server.py exactly like the old
trades_imported.json seed file was.
"""

import time
import ssl
import xml.etree.ElementTree as ET
import urllib.request
import urllib.parse

try:
    import certifi
    _ssl_ctx = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _ssl_ctx = None

SEND_REQUEST_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
GET_STATEMENT_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"


class FlexError(Exception):
    pass


def _http_get(url, params, timeout=20):
    qs = urllib.parse.urlencode(params)
    with urllib.request.urlopen(f"{url}?{qs}", timeout=timeout, context=_ssl_ctx) as resp:
        return resp.read()


def _fmt_date(raw):
    """Flex dates come as YYYYMMDD; normalize to YYYY-MM-DD like the rest
    of the app expects."""
    raw = (raw or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


def _option_symbol(underlying, expiry_raw, put_call, strike):
    """Build the 'AAPL 260417C00305000' style symbol _parse_option_symbol()
    in vista_web.py expects, from Flex's separate underlying/expiry/
    putCall/strike fields."""
    expiry_raw = (expiry_raw or "").strip()
    if len(expiry_raw) == 8 and expiry_raw.isdigit():
        yymmdd = expiry_raw[2:]
    else:
        yymmdd = expiry_raw
    try:
        strike_int = int(round(float(strike) * 1000))
    except (TypeError, ValueError):
        strike_int = 0
    pc = (put_call or "").strip()[:1].upper()
    return f"{underlying} {yymmdd}{pc}{strike_int:08d}"


def fetch_flex_trades(flex_token, flex_query_id, max_attempts=5, poll_delay=3):
    """Fetch and parse the Flex Query's Trades section into the app's
    normalized trade-dict list. Raises FlexError with a message safe to
    show the user on any failure (bad token, query not ready, etc.)."""
    if not flex_token or not flex_query_id:
        raise FlexError("Flex Token o Query ID no configurados")

    send_xml = _http_get(SEND_REQUEST_URL, {"t": flex_token, "q": flex_query_id, "v": "3"})
    send_root = ET.fromstring(send_xml)
    status = (send_root.findtext("Status") or "").strip()
    if status != "Success":
        error_msg = send_root.findtext("ErrorMessage") or "Error desconocido"
        raise FlexError(f"IB rechazo la solicitud: {error_msg}")

    reference_code = send_root.findtext("ReferenceCode")
    statement_url = send_root.findtext("Url") or GET_STATEMENT_URL

    statement_xml = None
    last_error = None
    for attempt in range(max_attempts):
        raw = _http_get(statement_url, {"q": reference_code, "t": flex_token, "v": "3"})
        # A statement still generating comes back as another
        # FlexStatementResponse wrapper (with Status Warn/Fail), not the
        # actual FlexQueryResponse report -- distinguish by root tag.
        root = ET.fromstring(raw)
        if root.tag == "FlexQueryResponse":
            statement_xml = root
            break
        err_status = (root.findtext("Status") or "").strip()
        last_error = root.findtext("ErrorMessage") or f"Status: {err_status}"
        time.sleep(poll_delay)

    if statement_xml is None:
        raise FlexError(f"El reporte no estuvo listo a tiempo: {last_error}")

    trades = []
    for trade_el in statement_xml.iter("Trade"):
        a = trade_el.attrib
        asset_category = a.get("assetCategory", "STK")
        buy_sell = (a.get("buySell") or "").upper()
        if buy_sell not in ("BUY", "SELL"):
            # Older reports sometimes use BOT/SLD
            buy_sell = "BUY" if buy_sell == "BOT" else "SELL" if buy_sell == "SLD" else buy_sell

        qty = abs(float(a.get("quantity", 0) or 0))
        price = float(a.get("tradePrice", 0) or 0)
        commission = abs(float(a.get("ibCommission", 0) or 0))
        realized_pnl = float(a.get("fifoPnlRealized", 0) or 0)
        order_id = a.get("ibOrderID") or a.get("orderID") or a.get("ibExecID") or ""
        date = _fmt_date(a.get("tradeDate") or a.get("dateTime", "")[:8])

        if asset_category == "OPT":
            symbol = _option_symbol(
                a.get("underlyingSymbol") or a.get("symbol", ""),
                a.get("expiry", ""),
                a.get("putCall", ""),
                a.get("strike", 0),
            )
        else:
            symbol = a.get("symbol", "")

        if not symbol or buy_sell not in ("BUY", "SELL"):
            continue

        trades.append({
            "symbol": symbol,
            "action": buy_sell,
            "date": date,
            "filled_qty": qty,
            "avg_fill_price": price,
            "commission": commission,
            "realized_pnl": realized_pnl,
            "order_id": str(order_id),
        })

    return trades
