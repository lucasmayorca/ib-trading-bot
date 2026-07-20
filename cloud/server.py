"""
Cloud server — Flask + SocketIO.

Serves the multi-tenant dashboard and receives real-time data from
IB Bridge clients running on each user's machine.
"""

from gevent import monkey
monkey.patch_all()

import os
import json
import math
import time
import functools
import threading
from datetime import datetime

import numpy as np

from flask import Flask, request, jsonify, Response, redirect
from flask_socketio import SocketIO, emit, disconnect
from dotenv import load_dotenv

load_dotenv()

from cloud import db, auth, flex
import config
import options_lab

# ══════════════════════════════════════════════════════════════
#  APP SETUP
# ══════════════════════════════════════════════════════════════

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("JWT_SECRET", "change-me-in-production")
socketio = SocketIO(
    app, cors_allowed_origins="*", async_mode="gevent",
    # Default max_http_buffer_size is 1MB, meant for typical chat-app-sized
    # payloads. Full parity with vista_web.py means each stock's analysis
    # batch carries 5 years of daily OHLC + MACD + RSI + Koncorde series
    # (~1260 bars x 8 arrays), so a batch of 10 symbols easily runs several
    # MB. Without raising this, engineio silently kills the connection with
    # "packet is too large" the moment the first real batch goes out.
    max_http_buffer_size=25 * 1024 * 1024,
)

# Per-user live data store: { user_id: { ... } }
user_data = {}
user_data_lock = threading.Lock()

# Bridge SID → user_id mapping
bridge_sessions = {}


def get_user_store(user_id):
    with user_data_lock:
        if user_id not in user_data:
            user_data[user_id] = {
                "connected": False,
                "stocks": [],
                "analysis": {},
                "etf_stocks": [],
                "etf_analysis": {},
                "portfolio_positions": [],
                "account_values": {},
                "open_orders": [],
                "executions": [],
                "live_trades": [],
                "last_update": None,
            }
        return user_data[user_id]


# ══════════════════════════════════════════════════════════════
#  JSON HELPER
# ══════════════════════════════════════════════════════════════

def _clean(obj):
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    return obj


def to_json(obj):
    return json.dumps(_clean(obj))


# ══════════════════════════════════════════════════════════════
#  AUTH DECORATOR
# ══════════════════════════════════════════════════════════════

def login_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        token = None
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
        if not token:
            token = request.cookies.get("token")
        if not token:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Not authenticated"}), 401
            return redirect("/login")
        payload = auth.decode_jwt(token)
        if not payload:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Invalid or expired token"}), 401
            return redirect("/login")
        request.user_id = payload["user_id"]
        request.user_email = payload["email"]
        return f(*args, **kwargs)
    return wrapper


# ══════════════════════════════════════════════════════════════
#  AUTH ROUTES
# ══════════════════════════════════════════════════════════════

@app.route("/register", methods=["GET"])
def register_page():
    return _auth_page("register")


@app.route("/login", methods=["GET"])
def login_page():
    return _auth_page("login")


@app.route("/api/register", methods=["POST"])
def api_register():
    data = request.get_json()
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    if not email or not password:
        return jsonify({"error": "Email and password required"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    if db.get_user_by_email(email):
        return jsonify({"error": "Email already registered"}), 409
    hashed = auth.hash_password(password)
    result = db.create_user(email, hashed)
    token = auth.create_jwt(result["id"], email)
    resp = jsonify({"token": token, "bridge_token": result["bridge_token"]})
    resp.set_cookie("token", token, httponly=True, samesite="Lax", max_age=86400)
    return resp, 201


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json()
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    user = db.get_user_by_email(email)
    if not user or not auth.check_password(password, user["password"]):
        return jsonify({"error": "Invalid email or password"}), 401
    token = auth.create_jwt(user["id"], email)
    resp = jsonify({"token": token, "bridge_token": user["bridge_token"]})
    resp.set_cookie("token", token, httponly=True, samesite="Lax", max_age=86400)
    return resp


@app.route("/api/bridge-token", methods=["GET"])
@login_required
def get_bridge_token():
    user = db.get_user_by_email(request.user_email)
    return jsonify({"bridge_token": user["bridge_token"]})


@app.route("/api/bridge-token/regenerate", methods=["POST"])
@login_required
def regenerate_bridge_token():
    new_token = db.regenerate_token(request.user_id)
    return jsonify({"bridge_token": new_token})


@app.route("/api/flex-config", methods=["GET"])
@login_required
def get_flex_config_route():
    flex_token, flex_query_id = db.get_flex_config(request.user_id)
    return jsonify({
        # Never echo the full token back once saved — only whether it's set.
        "configured": bool(flex_token and flex_query_id),
        "flex_query_id": flex_query_id or "",
    })


@app.route("/api/flex-config", methods=["POST"])
@login_required
def save_flex_config_route():
    data = request.get_json(force=True, silent=True) or {}
    flex_token = (data.get("flex_token") or "").strip()
    flex_query_id = (data.get("flex_query_id") or "").strip()
    if not flex_token or not flex_query_id:
        return jsonify({"error": "Flex Token y Query ID son requeridos"}), 400
    db.save_flex_config(request.user_id, flex_token, flex_query_id)
    flex_cache.pop(request.user_id, None)
    return jsonify({"ok": True})


@app.route("/api/flex-config/test", methods=["POST"])
@login_required
def test_flex_config_route():
    """Fetch immediately (bypassing cache) so the setup UI can confirm the
    token/query id actually work, instead of the user finding out only
    when they later open Trades Historicos."""
    flex_cache.pop(request.user_id, None)
    trades, error = _get_flex_trades(request.user_id)
    if error:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True, "trades_found": len(trades)})


@app.route("/logout")
def logout():
    resp = redirect("/login")
    resp.delete_cookie("token")
    return resp


# ══════════════════════════════════════════════════════════════
#  BRIDGE WEBSOCKET (data from user's local TWS)
# ══════════════════════════════════════════════════════════════

@socketio.on("bridge_auth")
def handle_bridge_auth(data):
    print(f"[WS] bridge_auth received: {data}", flush=True)
    token = data.get("bridge_token", "")
    user = db.get_user_by_token(token)
    if not user:
        print(f"[WS] Auth failed - invalid token", flush=True)
        emit("auth_result", {"ok": False, "error": "Invalid bridge token"})
        disconnect()
        return
    user_id = user["id"]
    bridge_sessions[request.sid] = user_id
    store = get_user_store(user_id)
    store["connected"] = True
    emit("auth_result", {"ok": True, "user_id": user_id})
    socketio.emit(f"bridge_status_{user_id}", {"connected": True})
    print(f"[BRIDGE] User {user['email']} connected (sid={request.sid})")


@socketio.on("disconnect")
def handle_disconnect():
    user_id = bridge_sessions.pop(request.sid, None)
    if user_id:
        remaining = [uid for uid in bridge_sessions.values() if uid == user_id]
        if not remaining:
            store = get_user_store(user_id)
            store["connected"] = False
            socketio.emit(f"bridge_status_{user_id}", {"connected": False})
        print(f"[BRIDGE] User {user_id} disconnected (sid={request.sid})")


@socketio.on("stock_list")
def handle_stock_list(data):
    user_id = bridge_sessions.get(request.sid)
    if not user_id:
        return
    store = get_user_store(user_id)
    store["stocks"] = data.get("symbols", [])
    store["last_update"] = datetime.now().strftime("%H:%M:%S")


@socketio.on("analysis_data")
def handle_analysis_data(data):
    user_id = bridge_sessions.get(request.sid)
    if not user_id:
        return
    store = get_user_store(user_id)
    symbol = data.get("symbol")
    if symbol:
        store["analysis"][symbol] = data.get("result", {})
        store["last_update"] = datetime.now().strftime("%H:%M:%S")


@socketio.on("analysis_batch")
def handle_analysis_batch(data):
    user_id = bridge_sessions.get(request.sid)
    if not user_id:
        return
    store = get_user_store(user_id)
    results = data.get("results", {})
    print(f"[ANALYSIS_BATCH] User {user_id}: Received {len(results)} symbols", flush=True)
    if results:
        sample_sym = list(results.keys())[0]
        sample = results[sample_sym]
        print(f"[ANALYSIS_BATCH] Sample {sample_sym}: keys={list(sample.keys())}", flush=True)
    for symbol, result in results.items():
        store["analysis"][symbol] = result
    store["last_update"] = datetime.now().strftime("%H:%M:%S")


@socketio.on("etf_stock_list")
def handle_etf_stock_list(data):
    user_id = bridge_sessions.get(request.sid)
    if not user_id:
        return
    store = get_user_store(user_id)
    store["etf_stocks"] = data.get("symbols", [])


@socketio.on("etf_analysis_batch")
def handle_etf_analysis_batch(data):
    user_id = bridge_sessions.get(request.sid)
    if not user_id:
        return
    store = get_user_store(user_id)
    results = data.get("results", {})
    print(f"[ETF_BATCH] User {user_id}: Received {len(results)} ETFs", flush=True)
    for symbol, result in results.items():
        store["etf_analysis"][symbol] = result
    store["last_update"] = datetime.now().strftime("%H:%M:%S")


@socketio.on("portfolio_data")
def handle_portfolio_data(data):
    user_id = bridge_sessions.get(request.sid)
    if not user_id:
        return
    store = get_user_store(user_id)
    store["portfolio_positions"] = data.get("positions", [])
    store["account_values"] = data.get("account_values", {})
    store["open_orders"] = data.get("open_orders", [])
    store["executions"] = data.get("executions", [])
    store["last_update"] = datetime.now().strftime("%H:%M:%S")


@socketio.on("bars_data")
def handle_bars_data(data):
    user_id = bridge_sessions.get(request.sid)
    if not user_id:
        return
    store = get_user_store(user_id)
    symbol = data.get("symbol")
    period = data.get("period", "1Y")
    if symbol:
        key = f"bars_{symbol}_{period}"
        store[key] = data.get("bars", [])


@socketio.on("trades_data")
def handle_trades_data(data):
    """New fills reported by the bridge since it last connected (from
    reqExecutions). Appended to the user's live trade log, deduped against
    the seed history in _merged_trades_file_path()."""
    user_id = bridge_sessions.get(request.sid)
    if not user_id:
        return
    store = get_user_store(user_id)
    new_fills = data.get("fills", [])
    if not new_fills:
        return
    existing_keys = {t.get("order_id") for t in store["live_trades"] if t.get("order_id")}
    for fill in new_fills:
        if fill.get("order_id") and fill["order_id"] in existing_keys:
            continue
        store["live_trades"].append(fill)
    print(f"[TRADES_DATA] User {user_id}: +{len(new_fills)} fills reported (total live: {len(store['live_trades'])})", flush=True)


# ══════════════════════════════════════════════════════════════
#  DASHBOARD API (serves data to the web frontend)
# ══════════════════════════════════════════════════════════════

@app.route("/")
@login_required
def index():
    return _dashboard_page()


@app.route("/api/data")
@login_required
def api_data():
    from vista_web import compute_top3

    store = get_user_store(request.user_id)
    analysis = store.get("analysis", {})

    results = {}
    for symbol in store.get("stocks", []):
        sig = analysis.get(symbol)
        if not sig:
            results[symbol] = None
            continue

        bt = sig.get("backtest", {}) or {}
        results[symbol] = {
            "symbol": symbol,
            "signal": sig.get("signal", "HOLD"),
            "signal_label": sig.get("signal_label", sig.get("signal", "HOLD")),
            "strength": float(sig.get("strength", 0)),
            "conditions_met": int(sig.get("conditions_met", 0)),
            "macd_ok": bool(sig.get("macd_ok", False)),
            "rsi_ok": bool(sig.get("rsi_ok", False)),
            "konc_ok": bool(sig.get("konc_ok", False)),
            "macd_detail": sig.get("macd_detail", ""),
            "rsi_detail": sig.get("rsi_detail", ""),
            "konc_detail": sig.get("konc_detail", ""),
            "price": float(sig.get("price", 0)),
            "dollar_vol": float(sig.get("dollar_vol", 0)),
            "values": sig.get("values", {}),
            "chart": sig.get("chart"),
            "confidence": bt.get("confidence", 0),
            "buy_avg_return": bt.get("buy_avg_return"),
            "sell_avg_return": bt.get("sell_avg_return"),
            "buy_count": bt.get("buy_count", 0),
            "sell_count": bt.get("sell_count", 0),
        }

    try:
        top3 = compute_top3(analysis)
    except Exception as e:
        print(f"[TOP3] Error: {e}", flush=True)
        top3 = []

    return Response(
        to_json({
            "results": results,
            "top3": top3,
            "last_update": store.get("last_update", ""),
            "bridge_connected": store.get("connected", False),
        }),
        mimetype="application/json",
    )


@app.route("/api/etf-data")
@login_required
def api_etf_data():
    from vista_web import compute_top3

    store = get_user_store(request.user_id)
    etf_analysis = store.get("etf_analysis", {})

    results = {}
    for symbol in store.get("etf_stocks", []):
        sig = etf_analysis.get(symbol)
        if not sig:
            results[symbol] = None
            continue

        bt = sig.get("backtest", {}) or {}
        results[symbol] = {
            "symbol": symbol,
            "signal": sig.get("signal", "HOLD"),
            "signal_label": sig.get("signal_label", sig.get("signal", "HOLD")),
            "strength": float(sig.get("strength", 0)),
            "conditions_met": int(sig.get("conditions_met", 0)),
            "macd_ok": bool(sig.get("macd_ok", False)),
            "rsi_ok": bool(sig.get("rsi_ok", False)),
            "konc_ok": bool(sig.get("konc_ok", False)),
            "macd_detail": sig.get("macd_detail", ""),
            "rsi_detail": sig.get("rsi_detail", ""),
            "konc_detail": sig.get("konc_detail", ""),
            "price": float(sig.get("price", 0)),
            "dollar_vol": float(sig.get("dollar_vol", 0)),
            "values": sig.get("values", {}),
            "chart": sig.get("chart"),
            "confidence": bt.get("confidence", 0),
            "buy_avg_return": bt.get("buy_avg_return"),
            "sell_avg_return": bt.get("sell_avg_return"),
            "buy_count": bt.get("buy_count", 0),
            "sell_count": bt.get("sell_count", 0),
        }

    try:
        etf_top3 = compute_top3(etf_analysis)
    except Exception as e:
        print(f"[ETF TOP3] Error: {e}", flush=True)
        etf_top3 = []

    return Response(
        to_json({
            "results": results,
            "top3": etf_top3,
            "last_update": store.get("last_update", ""),
            "bridge_connected": store.get("connected", False),
        }),
        mimetype="application/json",
    )


@app.route("/api/bars/<symbol>/<period>")
@login_required
def api_bars(symbol, period):
    store = get_user_store(request.user_id)
    key = f"bars_{symbol}_{period}"
    bars = store.get(key)
    if bars:
        return Response(to_json({"bars": bars, "cached": True}), mimetype="application/json")
    user_id = request.user_id
    bridge_sid = None
    for sid, uid in bridge_sessions.items():
        if uid == user_id:
            bridge_sid = sid
            break
    if not bridge_sid:
        return jsonify({"error": "Bridge not connected"}), 503
    socketio.emit("request_bars", {"symbol": symbol, "period": period}, to=bridge_sid)
    return jsonify({"status": "requested", "message": "Data is being fetched, retry in a few seconds"}), 202


def _build_cloud_position_analysis(sym, position, data, n_bars=90):
    """Cloud equivalent of vista_web._build_position_deep_analysis(), but takes
    the analysis dict explicitly (from this user's bridge-fed store) instead of
    reading vista_web's own module-global analysis_cache."""
    from vista_web import (
        _compute_price_levels, _generate_rationale, _generate_thesis,
        _score_stock, _extract_chart_data, _compute_signal_markers,
        _compute_position_verdict, _fetch_fundamentals, fundamentals_cache,
    )

    if data is None or not (data.get("chart") or {}).get("ohlc"):
        return None

    try:
        _fetch_fundamentals([sym])
    except Exception as e:
        print(f"[PORTFOLIO_DEEP] Fundamentals error for {sym}: {e}", flush=True)
    fund_entry = fundamentals_cache.get(sym, {})
    fund = fund_entry.get("data", {}) if isinstance(fund_entry, dict) else {}

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

    verdict = _compute_position_verdict(data, position)
    bt = data.get("backtest", {}) or {}
    sig = data.get("signal", "HOLD")

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
        "win_rate": (bt.get("sell_win_rate", 0) if sig == "SELL"
                     else bt.get("buy_win_rate", 0)) or 0,
        "avg_return": (bt.get("sell_avg_return") if sig == "SELL"
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


@app.route("/api/portfolio")
@login_required
def api_portfolio():
    from portfolio import (
        extract_sl_tp_by_symbol, _classify_position,
        _generate_portfolio_alerts, _compute_portfolio_metrics,
    )

    store = get_user_store(request.user_id)
    raw_positions = store.get("portfolio_positions", [])
    analysis = store.get("analysis", {})
    acct_vals = store.get("account_values", {})
    open_orders = store.get("open_orders", [])

    active_positions = [p for p in raw_positions if p.get("position", 0) != 0]

    sl_tp_map = extract_sl_tp_by_symbol(open_orders)

    positions_enriched = []
    total_value = 0.0
    total_cost = 0.0
    total_pnl_realizado = 0.0

    for p in active_positions:
        sym = p.get("symbol", "")
        cantidad = p.get("position", 0)
        costo_prom = p.get("averageCost", 0) or 0
        precio_actual = p.get("marketPrice", 0) or 0
        valor_mercado = abs(p.get("marketValue", 0) or 0)
        pnl = p.get("unrealizedPNL", 0) or 0
        pnl_realizado = p.get("realizedPNL", 0) or 0

        if not precio_actual or precio_actual <= 0:
            precio_actual = costo_prom
            valor_mercado = abs(cantidad) * precio_actual

        costo_total = abs(cantidad) * costo_prom
        pnl_pct = (pnl / costo_total * 100) if costo_total > 0 else 0.0

        total_value += valor_mercado
        total_cost += costo_total
        total_pnl_realizado += pnl_realizado

        es_etf, sector = _classify_position(sym, p.get("secType", "STK"))
        order_info = sl_tp_map.get(sym, {})
        sl_price = order_info.get("stop_loss")
        tp_price = order_info.get("take_profit")

        positions_enriched.append({
            "symbol": sym,
            "tipo": p.get("secType", "STK"),
            "cuenta": "",
            "moneda": "USD",
            "cantidad": cantidad,
            "costo_promedio": round(costo_prom, 4),
            "precio_actual": round(precio_actual, 2) if precio_actual else None,
            "costo_total": round(costo_total, 2),
            "valor_mercado": round(valor_mercado, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "pnl_realizado": round(pnl_realizado, 2),
            "es_etf": es_etf,
            "sector": sector,
            "peso_portafolio": 0,
            "stop_loss": round(sl_price, 2) if sl_price else None,
            "take_profit": round(tp_price, 2) if tp_price else None,
        })

    for p in positions_enriched:
        if total_value > 0:
            p["peso_portafolio"] = round(p["valor_mercado"] / total_value, 4)
    positions_enriched.sort(key=lambda x: x["valor_mercado"], reverse=True)

    total_pnl = sum(p["pnl"] for p in positions_enriched)
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0.0

    indicators_data = {}
    for p in positions_enriched:
        sym = p["symbol"]
        data = analysis.get(sym)
        if data:
            indicators_data[sym] = {
                "signal": data.get("signal", "HOLD"),
                "signal_label": data.get("signal_label", data.get("signal", "HOLD")),
                "strength": data.get("strength", 0),
                "conditions_met": data.get("conditions_met", 0),
                "macd_ok": data.get("macd_ok", False),
                "rsi_ok": data.get("rsi_ok", False),
                "konc_ok": data.get("konc_ok", False),
                "macd_detail": data.get("macd_detail", ""),
                "rsi_detail": data.get("rsi_detail", ""),
                "konc_detail": data.get("konc_detail", ""),
                "price": data.get("price", 0),
                "values": data.get("values", {}),
            }

    for p in positions_enriched:
        sym = p["symbol"]
        if sym in indicators_data:
            p["indicadores"] = indicators_data[sym]
        try:
            deep = _build_cloud_position_analysis(sym, p, analysis.get(sym))
            if deep:
                p["analysis"] = deep
                ind = p.get("indicadores") or {}
                ind.setdefault("signal", deep.get("signal", "HOLD"))
                ind.setdefault("signal_label", deep.get("signal_label", deep.get("signal", "HOLD")))
                ind.setdefault("strength", deep.get("strength", 0))
                ind.setdefault("conditions_met", deep.get("conditions_met", 0))
                p["indicadores"] = ind
        except Exception as e:
            print(f"[PORTFOLIO_DEEP] Error for {sym}: {e}", flush=True)

    try:
        alerts = _generate_portfolio_alerts(positions_enriched, {}, indicators_data)
    except Exception as e:
        print(f"[PORTFOLIO_ALERTS] Error: {e}", flush=True)
        alerts = []

    try:
        metrics = _compute_portfolio_metrics(positions_enriched, total_value)
    except Exception as e:
        print(f"[PORTFOLIO_METRICS] Error: {e}", flush=True)
        metrics = {}

    acct = {}
    for key, val in acct_vals.items():
        try:
            acct[key] = {"value": float(val), "currency": "USD"}
        except (ValueError, TypeError):
            acct[key] = {"value": val, "currency": "USD"}

    return Response(
        to_json({
            "positions": positions_enriched,
            "total_value": round(total_value, 2),
            "total_cost": round(total_cost, 2),
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "total_pnl_realizado": round(total_pnl_realizado, 2),
            "num_positions": len(positions_enriched),
            "composition": {},
            "account": acct,
            "indicators": indicators_data,
            "alerts": alerts,
            "metrics": metrics,
            "bridge_connected": store.get("connected", False),
            "warnings": [],
        }),
        mimetype="application/json",
    )


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "async_mode": socketio.async_mode,
        "server_mode": socketio.server.async_mode if hasattr(socketio, 'server') else "unknown",
    })


@app.route("/api/status")
@login_required
def api_status():
    store = get_user_store(request.user_id)
    email = ""
    try:
        user = db.get_user_by_id(request.user_id)
        if user:
            email = user.get("email", "")
    except Exception:
        pass
    return jsonify({
        "bridge_connected": store.get("connected", False),
        "stocks_count": len(store.get("stocks", [])),
        "analysis_count": len(store.get("analysis", {})),
        "last_update": store.get("last_update", ""),
        "email": email,
    })


@app.route("/api/debug")
@login_required
def api_debug():
    store = get_user_store(request.user_id)
    stocks = store.get("stocks", [])
    analysis = store.get("analysis", {})

    all_connected_users = []
    for sid, uid in bridge_sessions.items():
        try:
            u = db.get_user_by_id(uid)
            all_connected_users.append({"user_id": uid, "email": u.get("email", "") if u else "?"})
        except Exception:
            all_connected_users.append({"user_id": uid, "email": "?"})

    return jsonify({
        "current_user_id": request.user_id,
        "current_user_email": request.user_email,
        "bridge_connected": store.get("connected", False),
        "stocks_sent_by_bridge": len(stocks),
        "stocks_list": stocks[:10],  # First 10
        "analysis_received": len(analysis),
        "analysis_symbols": list(analysis.keys())[:10],  # First 10
        "analysis_sample": analysis.get(stocks[0], {}) if stocks else None,
        "last_update": store.get("last_update", ""),
        "bridge_sessions_total": len(bridge_sessions),
        "all_connected_bridge_users": all_connected_users,
        "portfolio_positions_count": len(store.get("portfolio_positions", [])),
        "portfolio_positions_sample": store.get("portfolio_positions", [])[:2],
        "account_values_count": len(store.get("account_values", {})),
    })


def _build_options_signal_data(data):
    vals = data.get("values", {}) or {}
    macd_vals = vals.get("macd", {})
    chart = data.get("chart", {}) or {}
    macd_chart = chart.get("macd", {}) or {}
    hist_arr = macd_chart.get("hist", [])

    return {
        "signal": data.get("signal", "HOLD"),
        "signal_label": data.get("signal_label", "NEUTRAL"),
        "strength": data.get("strength", 0),
        "rsi": vals.get("rsi"),
        "macd_hist": macd_vals.get("hist"),
        "macd_hist_prev": hist_arr[-2] if len(hist_arr) >= 2 else None,
        "conditions_met": data.get("conditions_met", 0),
    }


@app.route("/api/options-lab-top")
@login_required
def api_options_lab_top():
    store = get_user_store(request.user_id)
    analysis = store.get("analysis", {})

    # 1. Pre-screen: score all stocks quickly for options potential
    candidates = []
    for sym, data in analysis.items():
        if not data:
            continue
        price = data.get("price", 0)
        if price <= 0:
            continue
        chart = data.get("chart", {}) or {}
        ohlc = chart.get("ohlc", [])
        if len(ohlc) < 100:
            continue

        signal = data.get("signal", "HOLD")
        strength = data.get("strength", 0) or 0
        conditions = data.get("conditions_met", 0) or 0

        closes = [b["close"] for b in ohlc]
        iv_data = options_lab.iv_analysis(closes)
        hv_rank = iv_data.get("hv_rank") or 50
        iv_regime = iv_data.get("iv_regime", "normal")

        opt_score = 0.0
        if signal in ("BUY", "SELL"):
            opt_score += 40 + strength * 5
        elif conditions >= 2:
            opt_score += 25 + strength * 3
        elif conditions >= 1:
            opt_score += 10

        if iv_regime == "high":
            opt_score += 20
        elif iv_regime == "low":
            opt_score += 15

        if hv_rank > 80 or hv_rank < 20:
            opt_score += 10

        dv = data.get("dollar_vol", 0) or 0
        if dv > 500e6:
            opt_score += 5
        elif dv > 100e6:
            opt_score += 3

        candidates.append((sym, data, opt_score))

    candidates.sort(key=lambda x: x[2], reverse=True)

    # 2. Full analysis on top candidates
    results = []
    for sym, data, opt_score in candidates[:10]:
        price = data.get("price", 0)
        signal_data = _build_options_signal_data(data)
        chart = data.get("chart", {}) or {}
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
            print(f"[OPTIONS_LAB] Error for {sym}: {e}", flush=True)

    results.sort(key=lambda r: (
        r.get("strategies", [{}])[0].get("score", 0) if r.get("strategies") else 0
    ) + r.get("stock_score", 0), reverse=True)

    return Response(to_json({"opportunities": results}), mimetype="application/json")


@app.route("/api/options-lab/<symbol>")
@login_required
def api_options_lab(symbol):
    symbol = symbol.upper()
    store = get_user_store(request.user_id)
    data = store.get("analysis", {}).get(symbol)

    if not data:
        return Response(
            to_json({"error": f"No hay datos para {symbol}"}),
            mimetype="application/json",
            status=404,
        )

    price = data.get("price", 0)
    if price <= 0:
        return Response(
            to_json({"error": f"Precio no disponible para {symbol}"}),
            mimetype="application/json",
            status=400,
        )

    signal_data = _build_options_signal_data(data)
    chart = data.get("chart", {}) or {}
    ohlc = chart.get("ohlc", [])
    if len(ohlc) < 100:
        return Response(
            to_json({"error": f"Datos historicos insuficientes para {symbol}"}),
            mimetype="application/json",
            status=400,
        )

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
        print(f"[OPTIONS_LAB] Error for {symbol}: {e}", flush=True)
        return Response(
            to_json({"error": f"Error generando Options Lab: {str(e)}"}),
            mimetype="application/json",
            status=500,
        )

    return Response(to_json(result), mimetype="application/json")


SEED_TRADES_DIR = os.path.join(os.path.dirname(__file__), "seed_trades")

# Flex Web Service is rate-limited by IB and each call takes several
# seconds (send + poll), so cache per-user results instead of hitting it
# on every dashboard load.
flex_cache = {}
FLEX_CACHE_TTL = 1800  # 30 min


def _get_flex_trades(user_id):
    """Fetch the user's full trade history via IB's Flex Web Service, if
    they've configured a token/query id. Cached; returns (trades, error) —
    error is a user-facing string when the fetch failed, trades is [] in
    that case (falls back to seed+live_trades transparently)."""
    cached = flex_cache.get(user_id)
    now = time.time()
    if cached and now - cached["ts"] < FLEX_CACHE_TTL:
        return cached["trades"], cached["error"]

    flex_token, flex_query_id = db.get_flex_config(user_id)
    if not flex_token or not flex_query_id:
        flex_cache[user_id] = {"trades": [], "error": None, "ts": now}
        return [], None

    try:
        trades = flex.fetch_flex_trades(flex_token, flex_query_id)
        flex_cache[user_id] = {"trades": trades, "error": None, "ts": now}
        return trades, None
    except flex.FlexError as e:
        print(f"[FLEX] User {user_id}: {e}", flush=True)
        # Keep serving the previous good result (if any) rather than a
        # transient IB hiccup wiping the user's trade history from view.
        prev_trades = cached["trades"] if cached else []
        flex_cache[user_id] = {"trades": prev_trades, "error": str(e), "ts": now}
        return prev_trades, str(e)


def _merged_trades_file_path(user_id):
    """Combine IB Flex Web Service history (if configured), the one-time
    seed (historical fills manually exported), and fills the bridge has
    reported live since connecting, into a temp file build_trades_history()
    can read."""
    seed_path = os.path.join(SEED_TRADES_DIR, f"user_{user_id}.json")
    seed_trades = []
    if os.path.exists(seed_path):
        with open(seed_path) as f:
            seed_trades = json.load(f).get("trades", [])

    flex_trades, _flex_error = _get_flex_trades(user_id)

    store = get_user_store(user_id)
    live_trades = store.get("live_trades", [])

    seen_keys = set()
    combined = []
    for t in flex_trades + seed_trades + live_trades:
        key = t.get("order_id") or t.get("perm_id") or (t.get("symbol"), t.get("date"), t.get("action"), t.get("filled_qty"), t.get("avg_fill_price"))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        combined.append(t)

    tmp_dir = os.path.join(os.path.dirname(__file__), "_tmp_trades")
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_path = os.path.join(tmp_dir, f"user_{user_id}.json")
    with open(tmp_path, "w") as f:
        json.dump({"trades": combined}, f)
    return tmp_path


@app.route("/api/trades-history")
@login_required
def api_trades_history():
    from vista_web import build_trades_history
    try:
        trades_file = _merged_trades_file_path(request.user_id)
        result = build_trades_history(trades_file=trades_file)
        return Response(to_json(result), mimetype="application/json")
    except Exception as e:
        import traceback
        traceback.print_exc()
        return Response(
            to_json({"error": str(e), "trades": [], "summary": {}}),
            mimetype="application/json",
            status=500
        )


@app.route("/api/trades-history/chart/<trade_id>")
@login_required
def api_trades_history_chart(trade_id):
    from vista_web import _fetch_trade_chart_data, _generate_trade_thesis

    symbol = request.args.get("symbol", "")
    entry_date = request.args.get("entry", "")
    exit_date = request.args.get("exit", "")

    if not symbol or not entry_date or not exit_date:
        return Response(to_json({"error": "Faltan parametros"}), status=400, mimetype="application/json")

    chart = _fetch_trade_chart_data(symbol.upper(), entry_date, exit_date)
    if chart is None:
        return Response(to_json({"error": f"No se pudieron obtener datos para {symbol}"}),
                        status=404, mimetype="application/json")

    ind_entry = chart.get("indicators_at_entry")
    ind_exit = chart.get("indicators_at_exit")
    entry_thesis, exit_thesis = _generate_trade_thesis(symbol, entry_date, exit_date, ind_entry, ind_exit)
    chart["entry_thesis"] = entry_thesis
    chart["exit_thesis"] = exit_thesis

    return Response(to_json(chart), mimetype="application/json")


@app.route("/install.sh")
def install_script():
    server_url = request.host_url.rstrip("/")
    script = f'''#!/bin/bash
set -e
GREEN='\\033[0;32m'; YELLOW='\\033[1;33m'; RED='\\033[0;31m'; CYAN='\\033[0;36m'; NC='\\033[0m'

echo ""
echo "+==========================================+"
echo "|    IB Trading Bridge — Installer          |"
echo "+==========================================+"
echo ""

PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" -c "import sys; print(f'{{sys.version_info.major}}.{{sys.version_info.minor}}')" 2>/dev/null)
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [ "$major" -ge 3 ] 2>/dev/null && [ "$minor" -ge 10 ] 2>/dev/null; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo -e "${{RED}}Python 3.10+ no encontrado.${{NC}}"
    echo "Instala Python desde: https://www.python.org/downloads/"
    exit 1
fi
echo -e "${{GREEN}}Python encontrado:${{NC}} $($PYTHON --version)"

INSTALL_DIR="$HOME/.ib-bridge"
echo -e "${{CYAN}}Instalando en:${{NC}} $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

if [ ! -d "venv" ]; then
    echo "Creando entorno virtual..."
    $PYTHON -m venv venv
fi

source venv/bin/activate
pip install --upgrade pip -q 2>/dev/null

echo "Instalando IB Bridge..."
pip install "ibapi>=9.81.1" "python-socketio[client]>=5.12.0" "pandas>=2.0" "numpy>=1.24" -q 2>/dev/null

# Download bridge files
mkdir -p bridge
curl -sL {server_url}/bridge-files/main.py -o bridge/main.py
curl -sL {server_url}/bridge-files/indicators.py -o bridge/indicators.py
curl -sL {server_url}/bridge-files/signals.py -o bridge/signals.py
curl -sL {server_url}/bridge-files/backtester.py -o bridge/backtester.py
curl -sL {server_url}/bridge-files/__init__.py -o bridge/__init__.py

# Create launcher
cat > run-bridge.sh << 'LAUNCHER'
#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
source "$DIR/venv/bin/activate"
if [ -z "$1" ] || [ -z "$2" ]; then
    echo "Uso: ./run-bridge.sh SERVER_URL BRIDGE_TOKEN [IB_PORT]"
    exit 1
fi
python -m bridge.main --server "$1" --token "$2" --ib-port "${{3:-7497}}"
LAUNCHER
chmod +x run-bridge.sh

echo ""
echo -e "${{GREEN}}+==========================================+${{NC}}"
echo -e "${{GREEN}}|    Instalacion completa!                  |${{NC}}"
echo -e "${{GREEN}}+==========================================+${{NC}}"
echo ""
echo -e "Para conectar tu TWS, ejecuta:"
echo ""
echo -e "  ${{CYAN}}cd ~/.ib-bridge && source venv/bin/activate${{NC}}"
echo -e "  ${{CYAN}}python -m bridge.main --server {server_url} --token TU_TOKEN${{NC}}"
echo ""
echo -e "O usa el launcher:"
echo -e "  ${{CYAN}}~/.ib-bridge/run-bridge.sh {server_url} TU_TOKEN${{NC}}"
echo ""
echo -e "${{YELLOW}}Requisitos:${{NC}} TWS o IB Gateway abierto con API habilitada (puerto 7497 o 7496)"
echo ""
'''
    return Response(script, mimetype="text/plain")


@app.route("/bridge-files/<filename>")
def bridge_files(filename):
    import os
    allowed = {"main.py", "indicators.py", "signals.py", "backtester.py", "__init__.py"}
    if filename not in allowed:
        return "Not found", 404
    filepath = os.path.join(os.path.dirname(__file__), "..", "bridge", filename)
    if not os.path.exists(filepath):
        return "Not found", 404
    with open(filepath) as f:
        return Response(f.read(), mimetype="text/plain")


@app.route("/download-bridge")
@login_required
def download_bridge():
    user = db.get_user_by_email(request.user_email) if hasattr(request, 'user_email') else None
    token = request.args.get("token", "")
    server_url = request.host_url.rstrip("/")
    platform = request.args.get("platform", "mac")

    if platform == "windows":
        filename = "Conectar-TWS.bat"
        script = f'''@echo off
chcp 65001 >nul 2>&1
title IB Trading Bridge
echo.
echo  ============================================
echo    IB Trading Bridge - Instalador Automatico
echo  ============================================
echo.

where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python no encontrado.
    echo.
    echo Descarga Python desde: https://www.python.org/downloads/
    echo IMPORTANTE: Marca "Add Python to PATH" durante la instalacion.
    echo.
    pause
    exit /b 1
)

echo [OK] Python encontrado
python --version
echo.

set INSTALL_DIR=%USERPROFILE%\\.ib-bridge
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
cd /d "%INSTALL_DIR%"

if not exist "venv" (
    echo Creando entorno virtual...
    python -m venv venv
)

call venv\\Scripts\\activate.bat

echo Instalando dependencias...
pip install --upgrade pip -q 2>nul
pip install "ibapi>=9.81.1" "python-socketio[client]>=5.12.0" "pandas>=2.0" "numpy>=1.24" -q 2>nul

if not exist "bridge" mkdir "bridge"
echo Descargando bridge...
curl -sL {server_url}/bridge-files/main.py -o bridge\\main.py
curl -sL {server_url}/bridge-files/indicators.py -o bridge\\indicators.py
curl -sL {server_url}/bridge-files/signals.py -o bridge\\signals.py
curl -sL {server_url}/bridge-files/backtester.py -o bridge\\backtester.py
curl -sL {server_url}/bridge-files/__init__.py -o bridge\\__init__.py

echo.
echo  ============================================
echo    Conectando a TWS...
echo  ============================================
echo.
echo  Asegurate de tener TWS abierta con la API habilitada.
echo  Puerto 7497 = paper trading, 7496 = live
echo  Presiona Ctrl+C para detener.
echo.

python -m bridge.main --server {server_url} --token {token}

pause
'''
        return Response(script, mimetype="application/octet-stream",
                       headers={{"Content-Disposition": f"attachment; filename={filename}"}})

    # macOS / Linux .command file
    filename = "Conectar-TWS.command"
    script = f'''#!/bin/bash
# IB Trading Bridge — doble-click para conectar tu TWS
# Token personalizado — no compartas este archivo.

clear
GREEN='\\033[0;32m'; RED='\\033[0;31m'; CYAN='\\033[0;36m'; YELLOW='\\033[1;33m'; NC='\\033[0m'
SERVER="{server_url}"
TOKEN="{token}"

echo ""
echo -e "${{CYAN}}  ============================================${{NC}}"
echo -e "${{CYAN}}    IB Trading Bridge${{NC}}"
echo -e "${{CYAN}}  ============================================${{NC}}"
echo ""

# --- Check Python ---
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" -c "import sys; print(f'{{sys.version_info.major}}.{{sys.version_info.minor}}')" 2>/dev/null)
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [ "$major" -ge 3 ] 2>/dev/null && [ "$minor" -ge 10 ] 2>/dev/null; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo -e "${{RED}}  Python 3.10+ no encontrado.${{NC}}"
    echo ""
    echo "  Descarga Python desde: https://www.python.org/downloads/"
    echo ""
    echo "  Presiona Enter para cerrar..."
    read
    exit 1
fi
echo -e "${{GREEN}}  Python:${{NC}} $($PYTHON --version)"

# --- Install/Update ---
DIR="$HOME/.ib-bridge"
mkdir -p "$DIR"
cd "$DIR"

if [ ! -d "venv" ]; then
    echo -e "${{CYAN}}  Creando entorno virtual (solo la primera vez)...${{NC}}"
    $PYTHON -m venv venv
fi

source venv/bin/activate
pip install --upgrade pip -q 2>/dev/null

NEEDS_INSTALL=false
python -c "import socketio, pandas, numpy, ibapi" 2>/dev/null || NEEDS_INSTALL=true

if [ "$NEEDS_INSTALL" = true ]; then
    echo -e "${{CYAN}}  Instalando dependencias (solo la primera vez)...${{NC}}"
    pip install "ibapi>=9.81.1" "python-socketio[client]>=5.12.0" "pandas>=2.0" "numpy>=1.24" -q 2>/dev/null
fi

# --- Download latest bridge ---
mkdir -p bridge
curl -sL $SERVER/bridge-files/main.py -o bridge/main.py
curl -sL $SERVER/bridge-files/indicators.py -o bridge/indicators.py
curl -sL $SERVER/bridge-files/signals.py -o bridge/signals.py
curl -sL $SERVER/bridge-files/backtester.py -o bridge/backtester.py
curl -sL $SERVER/bridge-files/__init__.py -o bridge/__init__.py

echo ""
echo -e "${{GREEN}}  ============================================${{NC}}"
echo -e "${{GREEN}}    Conectando a TWS...${{NC}}"
echo -e "${{GREEN}}  ============================================${{NC}}"
echo ""
echo -e "  Asegurate de tener TWS abierta con la API habilitada."
echo -e "  Puerto 7497 = paper trading"
echo -e "  Presiona Ctrl+C para detener."
echo ""

python -m bridge.main --server "$SERVER" --token "$TOKEN"

echo ""
echo "  Bridge detenido. Presiona Enter para cerrar..."
read
'''
    resp = Response(script, mimetype="application/octet-stream",
                   headers={"Content-Disposition": f"attachment; filename={filename}"})
    return resp


# ══════════════════════════════════════════════════════════════
#  HTML PAGES
# ══════════════════════════════════════════════════════════════

def _auth_page(mode):
    title = "Crear Cuenta" if mode == "register" else "Iniciar Sesión"
    alt_link = "/login" if mode == "register" else "/register"
    alt_text = "¿Ya tienes cuenta? Inicia sesión" if mode == "register" else "¿No tienes cuenta? Regístrate"
    endpoint = f"/api/{mode}"
    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — IB Trading Dashboard</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:#f4f4f1;color:#16181d;display:flex;justify-content:center;align-items:center;min-height:100vh}}
.card{{background:#ffffff;border:1px solid #e3e2dc;border-radius:12px;padding:40px;width:400px;max-width:90vw;box-shadow:0 4px 12px rgba(30,33,38,.07)}}
h1{{font-size:24px;margin-bottom:8px;color:#16181d}}
.subtitle{{color:#6d7480;margin-bottom:24px;font-size:14px}}
label{{display:block;font-size:13px;color:#6d7480;margin-bottom:4px;margin-top:16px}}
input{{width:100%;padding:10px 12px;background:#fbfbf9;border:1px solid #e3e2dc;border-radius:6px;
color:#16181d;font-size:14px;outline:none}}
input:focus{{border-color:#2456e6;box-shadow:0 0 0 3px rgba(36,86,230,.1)}}
button{{width:100%;padding:12px;background:#2456e6;color:#fff;border:none;border-radius:6px;
font-size:15px;font-weight:600;cursor:pointer;margin-top:24px}}
button:hover{{background:#1d47c4}}
.alt{{text-align:center;margin-top:16px}}
.alt a{{color:#2456e6;text-decoration:none;font-size:13px}}
.error{{background:#fdf1f2;border:1px solid #f2c8cd;color:#c22436;padding:8px 12px;border-radius:6px;
margin-top:12px;font-size:13px;display:none}}
.success{{background:#effaf4;border:1px solid #bfe5d2;color:#0b7a4b;padding:12px;border-radius:6px;
margin-top:12px;font-size:13px;display:none}}
</style></head><body>
<div class="card">
<h1>{title}</h1>
<p class="subtitle">IB Trading Dashboard — Multi-tenant</p>
<form id="form">
<label>Email</label><input type="email" id="email" required>
<label>Contraseña</label><input type="password" id="password" required minlength="8">
<div class="error" id="error"></div>
<div class="success" id="success"></div>
<button type="submit">{title}</button>
</form>
<div class="alt"><a href="{alt_link}">{alt_text}</a></div>
</div>
<script>
document.getElementById('form').onsubmit=async e=>{{
  e.preventDefault();
  const err=document.getElementById('error'), suc=document.getElementById('success');
  err.style.display='none'; suc.style.display='none';
  try{{
    const r=await fetch('{endpoint}',{{method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{email:document.getElementById('email').value,
        password:document.getElementById('password').value}})}});
    const d=await r.json();
    if(!r.ok){{ err.textContent=d.error; err.style.display='block'; return; }}
    if(d.bridge_token && '{mode}'==='register'){{
      suc.innerHTML='✓ Cuenta creada. Redirigiendo...';
      suc.style.display='block';
    }}
    setTimeout(()=>window.location='/',500);
  }}catch(ex){{ err.textContent='Error de conexión'; err.style.display='block'; }}
}};
</script></body></html>"""


def _inject_cloud_setup_tab(html):
    """vista_web.py's DASHBOARD_HTML is the local (single-user, always-connected)
    template. The cloud version reuses it verbatim for parity, but needs one
    extra thing the local bot doesn't: a way to connect a per-user IB Bridge.
    This splices in a "Conectar TWS" tab + bridge status header, leaving the
    rest of the template completely untouched."""

    # 1. Nav tab button
    html = html.replace(
        '<button class="nav-tab" onclick="switchTab(\'trades\')">Trades Historicos</button>\n</div>',
        '<button class="nav-tab" onclick="switchTab(\'trades\')">Trades Historicos</button>\n'
        '  <button class="nav-tab" onclick="switchTab(\'setup\')">Conectar TWS</button>\n</div>',
    )

    # 2. Header: bridge status + user email + logout (right side, stacked under the sub line)
    html = html.replace(
        '<div class="sub">MACD + RSI + KONCORDE &nbsp;&bull;&nbsp; <span id="port-info"></span></div>\n</div>',
        '<div style="display:flex;flex-direction:column;align-items:flex-end;gap:4px">\n'
        '    <div class="sub">MACD + RSI + KONCORDE &nbsp;&bull;&nbsp; <span id="port-info"></span></div>\n'
        '    <div style="display:flex;align-items:center;gap:10px">\n'
        '      <span id="bridge-dot" style="width:8px;height:8px;border-radius:50%;background:var(--dim);display:inline-block"></span>\n'
        '      <span id="bridge-status-text" style="font-size:12px;color:var(--muted)">Verificando...</span>\n'
        '      <span id="user-email" style="color:var(--muted);font-size:11px"></span>\n'
        '      <a href="/logout" style="color:var(--accent);font-size:11px;text-decoration:none;border:1px solid var(--border);padding:4px 10px;border-radius:6px">Salir</a>\n'
        '    </div>\n  </div>\n</div>',
    )

    # 3. Tab content, inserted right before the footer
    setup_tab_html = '''
<!-- TAB: CONECTAR TWS -->
<div id="tab-setup" class="tab-content">
<div class="setup-section" style="max-width:640px;margin:32px auto;padding:0 16px">
  <div class="setup-card" style="background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:28px">
    <h2 style="margin:0 0 4px">Conectar tu TWS</h2>
    <p class="sub" style="margin:0 0 20px;color:var(--muted)">Solo necesitas TWS abierta y seguir estos 3 pasos.</p>
    <div class="setup-steps" style="display:flex;flex-direction:column;gap:18px">
      <div style="display:flex;gap:12px">
        <span style="background:var(--accent);color:#fff;border-radius:50%;width:24px;height:24px;flex:none;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:12px">1</span>
        <div>
          <h3 style="margin:0 0 4px;font-size:14px">Abri TWS</h3>
          <p style="margin:0;font-size:13px;color:var(--muted)">Abre Trader Workstation y habilita la API:<br>
          <code>Edit &rarr; Global Configuration &rarr; API &rarr; Settings</code><br>
          &check; Enable ActiveX and Socket Clients &nbsp; &check; Puerto: <code>7497</code></p>
        </div>
      </div>
      <div style="display:flex;gap:12px">
        <span style="background:var(--accent);color:#fff;border-radius:50%;width:24px;height:24px;flex:none;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:12px">2</span>
        <div style="flex:1">
          <h3 style="margin:0 0 4px;font-size:14px">Instalar el Bridge <span style="font-size:11px;color:var(--muted);font-weight:400">(solo la primera vez)</span></h3>
          <p style="margin:0 0 8px;font-size:13px;color:var(--muted)">Abri la Terminal y pega este comando:</p>
          <div style="position:relative">
            <div id="install-cmd" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:10px 90px 10px 12px;font-family:monospace;font-size:12px;overflow-x:auto;white-space:nowrap"></div>
            <button id="install-btn" onclick="copyCmd('install-cmd','install-btn')" style="position:absolute;right:6px;top:6px;background:var(--accent);color:#fff;border:none;padding:5px 12px;border-radius:5px;cursor:pointer;font-size:11px">Copiar</button>
          </div>
          <p style="font-size:11px;color:var(--muted);margin-top:6px">Requiere Python 3.10+ &nbsp;|&nbsp; Se instala en <code>~/.ib-bridge/</code></p>
        </div>
      </div>
      <div style="display:flex;gap:12px">
        <span style="background:var(--accent);color:#fff;border-radius:50%;width:24px;height:24px;flex:none;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:12px">3</span>
        <div style="flex:1">
          <h3 style="margin:0 0 4px;font-size:14px">Conectar</h3>
          <p style="margin:0 0 8px;font-size:13px;color:var(--muted)">Cada vez que quieras conectar, pega esto en la Terminal:</p>
          <div style="position:relative">
            <div id="run-cmd" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:10px 90px 10px 12px;font-family:monospace;font-size:12px;overflow-x:auto;white-space:nowrap"></div>
            <button id="run-btn" onclick="copyCmd('run-cmd','run-btn')" style="position:absolute;right:6px;top:6px;background:var(--accent);color:#fff;border:none;padding:5px 12px;border-radius:5px;cursor:pointer;font-size:11px">Copiar</button>
          </div>
          <p style="font-size:11px;color:var(--muted);margin-top:6px">El indicador de arriba cambiara a <span style="color:var(--buy)">&#9679; Conectado</span></p>
        </div>
      </div>
    </div>
    <div style="margin-top:20px;padding:14px;background:var(--bg);border-radius:8px;display:flex;justify-content:space-between;align-items:center">
      <div>
        <p style="font-size:12px;color:var(--muted);margin:0">Estado de conexion</p>
        <p id="setup-live-status" style="font-size:14px;margin:4px 0 0">Verificando...</p>
      </div>
      <div id="setup-status-dot" style="width:12px;height:12px;border-radius:50%;background:var(--dim)"></div>
    </div>
    <details style="text-align:left;margin-top:16px">
      <summary style="color:var(--accent);cursor:pointer;font-size:13px">Opciones avanzadas</summary>
      <div style="margin-top:12px;padding:12px;background:var(--bg);border-radius:6px">
        <p style="font-size:12px;color:var(--muted);margin-bottom:4px">Tu bridge token (no lo compartas):</p>
        <div id="token-display" style="font-family:monospace;font-size:12px;background:var(--surface);border:1px solid var(--border);padding:8px;border-radius:6px;word-break:break-all">Cargando...</div>
        <button onclick="regenerateToken()" style="background:var(--accent);color:#fff;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:11px;font-weight:600;margin-top:8px">Regenerar Token</button>
        <p style="font-size:11px;color:var(--muted);margin-top:12px">Puerto 7497 = paper trading &nbsp;|&nbsp; Agrega <code>--ib-port 7496</code> para live</p>
      </div>
    </details>
    <details style="text-align:left;margin-top:12px">
      <summary style="color:var(--accent);cursor:pointer;font-size:13px">Ver historial completo de trades (opcional)</summary>
      <div style="margin-top:12px;padding:16px;background:var(--bg);border-radius:6px">
        <p style="font-size:12px;color:var(--muted);margin-bottom:14px;line-height:1.6">
          Por defecto, "Trades Historicos" solo muestra las operaciones de <b>hoy</b>
          (asi funciona la conexion normal con TWS). Para ver tu historial completo,
          IB pide un paso extra de configuracion que se hace <b>una sola vez</b> en tu cuenta.
          Son 3 partes.
        </p>

        <div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:10px">
          <p style="font-size:12px;font-weight:700;color:var(--text);margin-bottom:8px">1&#65039;&#8419; Crear la consulta (Flex Query)</p>
          <ol style="font-size:11.5px;color:var(--muted);line-height:1.9;margin:0;padding-left:18px">
            <li>Entra a <a href="https://www.interactivebrokers.com" target="_blank" style="color:var(--accent)">Client Portal de IB</a> (login normal, no TWS)</li>
            <li>Menu &rarr; <b>Performance &amp; Reports</b> &rarr; <b>Flex Queries</b></li>
            <li>En "Activity Flex Query", toca el boton <b>+</b></li>
            <li>Ponele un nombre (ej: "Historial Completo")</li>
            <li>En "Sections" marca <b>Trades</b> y tilda todos los campos que te muestre</li>
            <li>Guardar &rarr; Format: <b>XML</b> &rarr; Period: <b>Year to Date</b> &rarr; Continue &rarr; Create</li>
          </ol>
          <p style="font-size:11px;color:var(--accent);margin-top:8px">&#128161; Anota el numero que aparece al lado del nombre de tu query — ese es el <b>Query ID</b>.</p>
        </div>

        <div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:14px">
          <p style="font-size:12px;font-weight:700;color:var(--text);margin-bottom:8px">2&#65039;&#8419; Generar el Token</p>
          <ol style="font-size:11.5px;color:var(--muted);line-height:1.9;margin:0;padding-left:18px">
            <li>En la misma pagina de Flex Queries, busca <b>"Flex Web Service Configuration"</b></li>
            <li>Activa el interruptor</li>
            <li>Te va a mostrar un <b>Token</b> (codigo largo) — copialo</li>
          </ol>
          <p style="font-size:11px;color:var(--accent);margin-top:8px">&#9888;&#65039; El token vence — al generarlo, elegi la duracion maxima disponible.</p>
        </div>

        <p style="font-size:12px;font-weight:700;color:var(--text);margin-bottom:8px">3&#65039;&#8419; Pegar aca abajo</p>
        <p style="font-size:11px;color:var(--muted);margin-bottom:4px">Flex Token (el codigo largo del paso 2):</p>
        <input id="flex-token-input" type="password" placeholder="Pega el token aqui" style="width:100%;box-sizing:border-box;background:var(--surface);border:1px solid var(--border);color:var(--text);padding:8px;border-radius:6px;font-family:monospace;font-size:12px;margin-bottom:8px">
        <p style="font-size:11px;color:var(--muted);margin-bottom:4px">Query ID (el numero del paso 1):</p>
        <input id="flex-query-input" type="text" placeholder="Ej: 123456" style="width:100%;box-sizing:border-box;background:var(--surface);border:1px solid var(--border);color:var(--text);padding:8px;border-radius:6px;font-family:monospace;font-size:12px;margin-bottom:8px">
        <button onclick="saveFlexConfig()" style="background:var(--accent);color:#fff;border:none;padding:8px 16px;border-radius:6px;cursor:pointer;font-size:12px;font-weight:600">Guardar y Probar</button>
        <span id="flex-config-status" style="font-size:11px;margin-left:8px"></span>
      </div>
    </details>
  </div>
</div>
</div>

'''
    html = html.replace('\n<div class="footer">', setup_tab_html + '<div class="footer">')

    # 4. switchTab(): load the setup tab's dynamic content when opened
    html = html.replace(
        "if(tab==='optionslab'&&!_olabLoaded){_olabLoaded=true;loadOptionsLabTop();}\n}",
        "if(tab==='optionslab'&&!_olabLoaded){_olabLoaded=true;loadOptionsLabTop();}\n"
        "  if(tab==='setup')renderSetup();\n}",
    )

    # 5. New script block: bridge status polling + setup tab rendering.
    #    Kept separate from vista_web.py's own <script> to avoid touching it.
    cloud_script = '''
<script>
let _bridgeConnected=false;
let _bridgeToken='';
async function fetchStatus(){
  try{
    let r=await fetch('/api/status');
    if(r.status===401){window.location='/login';return;}
    let d=await r.json();
    _bridgeConnected=d.bridge_connected;
    document.getElementById('bridge-dot').style.background=_bridgeConnected?'var(--buy)':'var(--sell)';
    document.getElementById('bridge-status-text').textContent=_bridgeConnected
      ?'Conectado — '+d.stocks_count+' acciones'+(d.last_update?' ('+d.last_update+')':'')
      :'TWS Desconectado';
    if(d.email)document.getElementById('user-email').textContent=d.email;
    if(document.getElementById('tab-setup').classList.contains('active'))renderSetup();
  }catch(e){}
}
async function fetchBridgeToken(){
  try{
    let r=await fetch('/api/bridge-token');
    if(r.status===401)return;
    let d=await r.json();
    _bridgeToken=d.bridge_token||'';
  }catch(e){}
}
async function regenerateToken(){
  if(!confirm('Regenerar token? El bridge actual se desconectara.'))return;
  try{
    let r=await fetch('/api/bridge-token/regenerate',{method:'POST'});
    let d=await r.json();
    _bridgeToken=d.bridge_token||'';
    renderSetup();
  }catch(e){}
}
function renderSetup(){
  let serverUrl=window.location.origin;
  let installCmd=document.getElementById('install-cmd');
  let runCmd=document.getElementById('run-cmd');
  if(installCmd)installCmd.textContent='curl -sL https://raw.githubusercontent.com/lucasmayorca/ib-trading-bot/main/install-bridge.sh | bash';
  if(runCmd)runCmd.textContent='~/.ib-bridge/run-bridge.sh '+serverUrl+' '+(_bridgeToken||'TOKEN');
  let tokenEl=document.getElementById('token-display');
  if(tokenEl)tokenEl.textContent=_bridgeToken||'Cargando...';
  let statusEl=document.getElementById('setup-live-status');
  let dotEl=document.getElementById('setup-status-dot');
  if(statusEl&&dotEl){
    if(_bridgeConnected){
      statusEl.innerHTML='<span style="color:var(--buy);font-weight:600">Conectado</span> — recibiendo datos de TWS';
      dotEl.style.background='var(--buy)';
    }else{
      statusEl.innerHTML='<span style="color:var(--dim)">Desconectado</span> — segui los pasos de arriba para conectar';
      dotEl.style.background='var(--dim)';
    }
  }
}
async function fetchFlexConfig(){
  try{
    let r=await fetch('/api/flex-config');
    if(r.status===401)return;
    let d=await r.json();
    if(d.configured){
      let statusEl=document.getElementById('flex-config-status');
      if(statusEl)statusEl.innerHTML='<span style="color:var(--buy)">Configurado (Query ID: '+d.flex_query_id+')</span>';
    }
  }catch(e){}
}
async function saveFlexConfig(){
  let tokenEl=document.getElementById('flex-token-input');
  let queryEl=document.getElementById('flex-query-input');
  let statusEl=document.getElementById('flex-config-status');
  let token=tokenEl.value.trim();
  let queryId=queryEl.value.trim();
  if(!token||!queryId){
    statusEl.innerHTML='<span style="color:var(--sell)">Completa ambos campos</span>';
    return;
  }
  statusEl.textContent='Guardando...';
  try{
    let r=await fetch('/api/flex-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({flex_token:token,flex_query_id:queryId})});
    let d=await r.json();
    if(!r.ok){statusEl.innerHTML='<span style="color:var(--sell)">'+(d.error||'Error')+'</span>';return;}
    statusEl.textContent='Probando conexion con IB...';
    let r2=await fetch('/api/flex-config/test',{method:'POST'});
    let d2=await r2.json();
    if(d2.ok){
      statusEl.innerHTML='<span style="color:var(--buy)">Listo — '+d2.trades_found+' operaciones encontradas</span>';
      tokenEl.value='';
    }else{
      statusEl.innerHTML='<span style="color:var(--sell)">'+(d2.error||'No se pudo verificar')+'</span>';
    }
  }catch(e){
    statusEl.innerHTML='<span style="color:var(--sell)">Error de conexion</span>';
  }
}
function copyCmd(preId,btnId){
  let text=document.getElementById(preId).textContent;
  navigator.clipboard.writeText(text);
  let btn=document.getElementById(btnId);
  btn.textContent='Copiado!';
  setTimeout(()=>{btn.textContent='Copiar'},2000);
}
function goToFlexSetup(){
  switchTab('setup');
  // The Flex fields live inside a <details> the user hasn't necessarily
  // opened yet -- open both so they land right on the inputs, not just
  // somewhere on the page.
  document.querySelectorAll('#tab-setup details').forEach(d=>d.open=true);
  let box=document.getElementById('flex-token-input');
  if(box)box.scrollIntoView({behavior:'smooth',block:'center'});
}
// Override vista_web.py's loadTradesHistory(): on the cloud version, an
// empty trade list almost always means the user hasn't connected a Flex
// Query yet (reqExecutions only ever returns today's fills), not that
// they genuinely have zero trades. Point them at the fix instead of
// leaving them looking at a bare "no trades" placeholder with no context.
function loadTradesHistory(){
  document.getElementById('th-loading').style.display='';
  document.getElementById('th-content').style.display='none';
  fetch('/api/trades-history').then(r=>r.json()).then(data=>{
    _thData=data;
    document.getElementById('th-loading').style.display='none';
    if(!data.trades||data.trades.length===0){
      document.getElementById('th-content').style.display='none';
      document.getElementById('th-loading').style.display='';
      document.getElementById('th-loading').innerHTML=
        '<div style="max-width:480px;margin:0 auto;text-align:center">'+
        '<p style="font-size:15px;color:var(--text);margin-bottom:10px">Todavia no hay operaciones para mostrar</p>'+
        '<p style="font-size:13px;color:var(--muted);line-height:1.6;margin-bottom:16px">'+
        'El bridge solo ve las operaciones ejecutadas <b>hoy</b> (asi funciona la conexion con TWS). '+
        'Para ver tu historial completo, conecta un Flex Query de IB — se hace una sola vez.'+
        '</p>'+
        '<button onclick="goToFlexSetup()" style="background:var(--accent);color:#fff;border:none;padding:10px 20px;border-radius:6px;cursor:pointer;font-size:13px;font-weight:600">Conectar historial completo</button>'+
        '</div>';
      return;
    }
    document.getElementById('th-content').style.display='';
    renderThSummary(data.summary);
    renderThList(data.trades);
  }).catch(e=>{
    document.getElementById('th-loading').style.display='';
    document.getElementById('th-loading').innerHTML='<span style="color:var(--sell)">Error cargando trades: '+e.message+'</span>';
  });
}
fetchStatus();
fetchBridgeToken();
fetchFlexConfig();
setInterval(fetchStatus,10000);
</script>
'''
    html = html.replace("</body>\n</html>", cloud_script + "</body>\n</html>")
    return html


def _dashboard_page():
    from vista_web import DASHBOARD_HTML
    return _inject_cloud_setup_tab(DASHBOARD_HTML)


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

try:
    db.init_db()
    print("[SERVER] Database initialized")
except Exception as e:
    print(f"[SERVER] WARNING: Database init failed: {e}")
    print("[SERVER] Server will start but registration/login won't work until DB is available")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[SERVER] Starting on port {port} (gevent)")
    socketio.run(app, host="0.0.0.0", port=port, debug=False)
