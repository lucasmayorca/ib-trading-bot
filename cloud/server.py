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

import gevent
import numpy as np

from flask import Flask, request, jsonify, Response, redirect
from flask_socketio import SocketIO, emit, disconnect
from dotenv import load_dotenv

load_dotenv()

from cloud import db, auth, flex
import config
import options_lab
import calibration
import enrichment

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
    # Defaults ping_interval=25s + ping_timeout=20s mean that between scans
    # (5 min sleep on the bridge), the first missed ping/pong terminates the
    # session. On flaky ARG residential links that easily runs every ~30s,
    # and every reconnect triggers a full re-emit (cart + 100 stocks + 100
    # ETFs — several MB) that saturates the TCP window and cascades into
    # another write-timeout. Ping every 15s keeps NATs/proxies from timing
    # out the socket, and a 40s ping_timeout tolerates one slow-link hiccup
    # without dropping the whole connection.
    ping_interval=15,
    ping_timeout=40,
)

# Owner account — sees the collected-feedback review panel in the "Tu Opinion"
# tab. Override with ADMIN_EMAIL env var if the owner account changes.
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "lucas.mayorca@gmail.com").strip().lower()

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
                "portfolio_received": False,
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
#  STORE PERSISTENCE  (survives restarts / redeploys)
# ══════════════════════════════════════════════════════════════
# The in-memory user_data store is wiped on every container restart. We
# snapshot it to Postgres (debounced, off the hot path) and restore it on
# boot, so a restarted server serves last-known data instead of blank tabs.
_persist_timers = {}       # user_id -> pending gevent greenlet
_PERSIST_DEBOUNCE = 20     # seconds; trailing write after the last mutation


def schedule_persist(user_id):
    """Debounced snapshot of a user's store. Rapid batches during a scan keep
    rescheduling, so only ONE write lands ~20s after the last mutation —
    capturing the complete cycle instead of hammering the DB per batch. Runs
    in a gevent greenlet and never raises into the caller."""
    try:
        old = _persist_timers.get(user_id)
        if old is not None:
            old.kill(block=False)
        _persist_timers[user_id] = gevent.spawn_later(
            _PERSIST_DEBOUNCE, _do_persist, user_id
        )
    except Exception as e:
        print(f"[PERSIST] schedule failed for user {user_id}: {e}", flush=True)


def _do_persist(user_id):
    _persist_timers.pop(user_id, None)
    try:
        store = user_data.get(user_id)
        if not store:
            return
        # Drop the per-symbol on-demand chart cache (bars_*): large and
        # re-fetchable. Keep analysis / etf / portfolio — the tab content.
        # json.dumps doesn't yield greenlets, so this is atomic vs. the
        # socket handlers under gevent's cooperative scheduler.
        snapshot = {k: v for k, v in store.items() if not str(k).startswith("bars_")}
        payload = to_json(snapshot)
        db.save_user_store(user_id, payload)
        print(f"[PERSIST] Saved store for user {user_id} ({len(payload) // 1024} KB)", flush=True)
    except Exception as e:
        print(f"[PERSIST] save failed for user {user_id}: {e}", flush=True)


def _restore_stores():
    """Load persisted snapshots into user_data on boot. Best-effort: any
    failure just leaves the server starting empty (previous behaviour)."""
    try:
        rows = db.load_all_user_stores()
    except Exception as e:
        print(f"[RESTORE] load failed: {e}", flush=True)
        return
    restored = 0
    for user_id, data in rows:
        if not isinstance(data, dict):
            continue
        store = get_user_store(user_id)   # seeds defaults, then overlay
        data["connected"] = False         # bridge isn't connected yet post-restart
        store.update(data)
        restored += 1
    print(f"[RESTORE] Restored {restored} user store(s) from DB", flush=True)


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


@app.route("/api/feedback", methods=["POST"])
@login_required
def submit_feedback():
    data = request.get_json(force=True, silent=True) or {}
    try:
        rating = int(data.get("rating") or 0)
    except (TypeError, ValueError):
        rating = 0
    category = (data.get("category") or "").strip()[:40]
    message = (data.get("message") or "").strip()[:4000]
    if rating < 1 or rating > 5:
        return jsonify({"error": "Elegi una valoracion de 1 a 5 estrellas"}), 400
    if not message:
        return jsonify({"error": "Escribi un comentario"}), 400
    db.save_feedback(request.user_id, request.user_email, rating, category, message)
    return jsonify({"ok": True})


@app.route("/api/feedback", methods=["GET"])
@login_required
def list_feedback():
    # Owner-only review panel. Regular users get 403 and the front-end simply
    # keeps the review section hidden.
    if (request.user_email or "").strip().lower() != ADMIN_EMAIL:
        return jsonify({"error": "No autorizado"}), 403
    rows = db.get_all_feedback()
    items, total = [], 0
    for r in rows:
        total += r["rating"] or 0
        items.append({
            "email": r["email"],
            "rating": r["rating"],
            "category": r["category"],
            "message": r["message"],
            "created_at": r["created_at"].strftime("%Y-%m-%d %H:%M") if r["created_at"] else "",
        })
    avg = round(total / len(items), 2) if items else 0
    return jsonify({"items": items, "count": len(items), "avg_rating": avg})


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
    schedule_persist(user_id)


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
    schedule_persist(user_id)


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
    store["portfolio_received"] = True
    store["last_update"] = datetime.now().strftime("%H:%M:%S")
    schedule_persist(user_id)


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
            # Enriquecimiento server-side (beta/RS/RVOL + analistas/insiders/
            # short) — calculado aca, no en el bridge (cero reinstalls).
            "ext": enrichment.get_ext(symbol),
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
            # Enriquecimiento server-side (beta/RS/RVOL + analistas/insiders/
            # short) — calculado aca, no en el bridge (cero reinstalls).
            "ext": enrichment.get_ext(symbol),
        }

    try:
        etf_top3 = compute_top3(etf_analysis,
                                min_target_pct=getattr(config, "MIN_OPPORTUNITY_TARGET_PCT_ETF", None))
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


@app.route("/api/market-pulse")
@login_required
def api_market_pulse():
    # Espejo del endpoint local: cotizaciones/sentimiento son globales (cache
    # compartido en vista_web), la amplitud sale del scan del usuario.
    from vista_web import _pulse_base, _pulse_sentiment, _breadth_from_results

    store = get_user_store(request.user_id)
    base = _pulse_base()
    breadth = _breadth_from_results(store.get("analysis", {}), store.get("etf_analysis", {}))
    score, label = _pulse_sentiment(base, breadth)
    return to_json({
        "quotes": base.get("quotes", []),
        "sentiment": score, "sentiment_label": label, "breadth": breadth,
    })


_bars_cache = {}   # (symbol, period) -> {"data": ..., "ts": float}


@app.route("/api/bars/<symbol>/<period>")
@login_required
def api_bars(symbol, period):
    """Barras intradiarias (4h/1h/15m) via yfinance, mismo contrato {"ohlc": [...]}
    que el dashboard local espera. (Antes hacia un round-trip al bridge y devolvia
    202/otro shape -> los graficos 1M/1W/1D nunca cargaban en el cloud.)"""
    if period not in ("4h", "1h", "30m", "15m"):
        return Response('{"error":"invalid"}', status=400, mimetype="application/json")
    cache_key = (symbol, period)
    cached = _bars_cache.get(cache_key)
    if cached and time.time() - cached["ts"] < 300:
        return Response(to_json(cached["data"]), mimetype="application/json")
    result = {"ohlc": []}
    try:
        from vista_web import _fetch_bars_yf
        result["ohlc"] = _fetch_bars_yf(symbol, period)
    except Exception as e:
        print(f"[BARS] Error {period} {symbol}: {e}", flush=True)
    _bars_cache[cache_key] = {"data": result, "ts": time.time()}
    return Response(to_json(result), mimetype="application/json")


def _build_cloud_position_analysis(sym, position, data, live_trades=None, n_bars=90):
    """Cloud equivalent of vista_web._build_position_deep_analysis(), but takes
    the analysis dict explicitly (from this user's bridge-fed store) instead of
    reading vista_web's own module-global analysis_cache."""
    from vista_web import (
        _compute_price_levels, _generate_rationale, _generate_thesis,
        _score_stock, _extract_chart_data, _compute_signal_markers,
        _compute_position_verdict, _generate_position_recommendation,
        _fetch_fundamentals, fundamentals_cache,
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

    # Entry fills (BUY) del bridge/flex/seed — para el chart Y la narrativa
    entry_fills = []
    if live_trades:
        for t in live_trades:
            if (t.get("symbol") or "").upper() != sym.upper():
                continue
            if (t.get("action") or "").upper() != "BUY":
                continue
            date = t.get("date") or ""
            price_f = t.get("avg_fill_price") or t.get("lmt_price")
            qty = t.get("filled_qty") or 0
            if date and price_f:
                entry_fills.append({
                    "time": date,
                    "price": round(float(price_f), 2),
                    "qty": float(qty or 0),
                })
        entry_fills.sort(key=lambda x: x["time"])

    verdict = _compute_position_verdict(data, position, levels)
    try:
        narrative = _generate_position_recommendation(
            sym, data, position, levels, entry_fills, verdict)
    except Exception as e:
        print(f"[PORTFOLIO_DEEP] Narrative error for {sym}: {e}", flush=True)
        narrative = verdict.get("reason", "")

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
        "entry_fills": entry_fills,
        "fundamentals": fund,
        "verdict": verdict.get("verdict", "HOLD"),
        "urgency": verdict.get("urgency", "low"),
        "headline": verdict.get("headline", "HOLD"),
        "verdict_reason": narrative,
        "verdict_summary": (narrative.split("\n\n")[-1] if narrative else verdict.get("reason", "")),
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
    # Fills en vivo del bridge (para marcar en el chart las compras)
    live_trades = store.get("live_trades", [])

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
            deep = _build_cloud_position_analysis(sym, p, analysis.get(sym), live_trades)
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
            "portfolio_received": store.get("portfolio_received", False),
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

        option_market = options_lab.get_option_market(
            sym, config.OPTIONS_DTE_TARGETS, spot=price)
        try:
            lab = options_lab.generate_options_lab(
                symbol=sym, price=price, signal_data=signal_data,
                closes=closes, highs=highs_arr, lows=lows_arr,
                risk_free_rate=config.OPTIONS_RISK_FREE_RATE,
                dte_options=config.OPTIONS_DTE_TARGETS,
                option_market=option_market,
            )
            lab["stock_score"] = round(opt_score, 1)
            try:
                from vista_web import _compute_price_levels
                lab["price_levels"] = _compute_price_levels(data)
            except Exception:
                pass
            results.append(lab)
        except Exception as e:
            print(f"[OPTIONS_LAB] Error for {sym}: {e}", flush=True)

    results.sort(key=lambda r: (
        r.get("strategies", [{}])[0].get("score", 0) if r.get("strategies") else 0
    ) + r.get("stock_score", 0), reverse=True)

    return Response(to_json({"opportunities": results}), mimetype="application/json")


_calibration_cache = {"data": None, "ts": 0}
_CALIBRATION_TTL = 3600
_CALIBRATION_MAX_SYMBOLS = 20


def _fetch_5y_yf(symbol):
    """5Y OHLCV via yfinance con el shape que espera calibration/backtester."""
    try:
        import yfinance as yf
        import pandas as pd
        h = yf.Ticker(symbol.replace(" ", "-")).history(period="5y", interval="1d",
                                                        auto_adjust=False)
        if h is None or h.empty:
            return None
        return pd.DataFrame({
            "date": [d.strftime("%Y%m%d") for d in h.index],
            "open": h["Open"].values, "high": h["High"].values,
            "low": h["Low"].values, "close": h["Close"].values,
            "volume": h["Volume"].astype(float).values,
        })
    except Exception:
        return None


@app.route("/api/calibration")
@login_required
def api_calibration():
    """Diagrama de calibracion (universo-nivel, cacheado global 1h): fuerza de
    senal predicha vs win-rate y retorno realmente observados en 5A."""
    if _calibration_cache["data"] and time.time() - _calibration_cache["ts"] < _CALIBRATION_TTL:
        return Response(to_json(_calibration_cache["data"]), mimetype="application/json")

    store = get_user_store(request.user_id)
    syms = list(dict.fromkeys(list(getattr(config, "WATCHLIST", [])) +
                              list(store.get("analysis", {}).keys())))[:_CALIBRATION_MAX_SYMBOLS]
    try:
        ohlc = {}
        for s in syms:
            df = _fetch_5y_yf(s)
            if df is not None and len(df) > 300:
                ohlc[s] = df
        if not ohlc:
            result = {"error": "Sin datos historicos suficientes para calibrar",
                      "overall": {"n": 0}}
        else:
            result = calibration.calibrate_universe(ohlc)
            result["symbols_requested"] = len(syms)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return Response(to_json({"error": f"Error de calibracion: {e}", "overall": {"n": 0}}),
                        mimetype="application/json", status=500)

    _calibration_cache["data"] = result
    _calibration_cache["ts"] = time.time()
    return Response(to_json(result), mimetype="application/json")


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

    option_market = options_lab.get_option_market(
        symbol, config.OPTIONS_DTE_TARGETS, spot=price)
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
            option_market=option_market,
        )
    except Exception as e:
        print(f"[OPTIONS_LAB] Error for {symbol}: {e}", flush=True)
        return Response(
            to_json({"error": f"Error generando Options Lab: {str(e)}"}),
            mimetype="application/json",
            status=500,
        )

    try:
        from vista_web import _compute_price_levels
        result["price_levels"] = _compute_price_levels(data)
    except Exception:
        pass

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
    from cloud import db as _db
    try:
        trades_file = _merged_trades_file_path(request.user_id)
        result = build_trades_history(trades_file=trades_file)
        flex_token, flex_query_id = _db.get_flex_config(request.user_id)
        result["flex_configured"] = bool(flex_token and flex_query_id)
        cached = flex_cache.get(request.user_id)
        if cached:
            import datetime
            result["flex_last_update"] = datetime.datetime.fromtimestamp(cached["ts"]).strftime("%Y-%m-%d %H:%M")
            if cached.get("error"):
                result["flex_error"] = cached["error"]
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


@app.route("/api/trades-history/refresh", methods=["POST"])
@login_required
def api_trades_history_refresh():
    from cloud import flex as _flex, db as _db
    flex_token, flex_query_id = _db.get_flex_config(request.user_id)
    if not flex_token or not flex_query_id:
        return Response(
            to_json({"error": "Flex no configurado. Configuralo en la pestana Conectar TWS."}),
            mimetype="application/json", status=400,
        )
    try:
        trades = _flex.fetch_flex_trades(flex_token, flex_query_id)
        flex_cache[request.user_id] = {"trades": trades, "error": None, "ts": time.time()}
        return Response(
            to_json({"ok": True, "trades_count": len(trades)}),
            mimetype="application/json",
        )
    except _flex.FlexError as e:
        return Response(
            to_json({"error": str(e)}),
            mimetype="application/json", status=502,
        )


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
    cta_hint = ("Crea tu cuenta gratis y conecta tu TWS en 3 pasos."
                if mode == "register" else
                "Entra a tu dashboard. ¿Primera vez? Crear la cuenta toma 30 segundos.")
    endpoint = f"/api/{mode}"
    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — IB Trading Dashboard</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
:root{{--bg:#f4f4f1;--surface:#fff;--border:#e3e2dc;--border-subtle:#ecebe6;--text:#16181d;
--muted:#6d7480;--dim:#9aa0aa;--accent:#2456e6;--buy:#0b7a4b;--sell:#c22436;--hold:#b45309}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:var(--bg);color:var(--text);min-height:100vh}}
.wrap{{max-width:1120px;margin:0 auto;padding:0 24px}}
/* ── top bar ── */
.topbar{{display:flex;justify-content:space-between;align-items:center;padding:22px 0}}
.logo{{font-size:15px;font-weight:800;letter-spacing:.5px}}
.logo em{{font-style:normal;color:var(--accent)}}
.logo small{{display:block;font-weight:500;font-size:10px;color:var(--muted);letter-spacing:1.5px;margin-top:2px}}
/* ── hero: pitch + login card ── */
.hero{{display:grid;grid-template-columns:1fr 400px;gap:56px;align-items:start;padding:36px 0 20px}}
.eyebrow{{display:inline-block;font-size:11px;font-weight:700;letter-spacing:1.5px;color:var(--accent);
background:rgba(36,86,230,.08);border:1px solid rgba(36,86,230,.18);border-radius:999px;padding:5px 14px;margin-bottom:18px}}
h1.head{{font-size:38px;line-height:1.15;font-weight:800;letter-spacing:-.5px;margin-bottom:16px}}
h1.head em{{font-style:normal;color:var(--accent)}}
.lead{{font-size:16px;color:var(--muted);line-height:1.65;margin-bottom:26px;max-width:540px}}
.chips{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:30px}}
.chip{{font-size:11px;font-weight:700;letter-spacing:.5px;padding:5px 12px;border-radius:999px;border:1px solid}}
.chip.b{{color:var(--buy);background:#effaf4;border-color:#bfe5d2}}
.chip.s{{color:var(--sell);background:#fdf1f2;border-color:#f2c8cd}}
.chip.h{{color:var(--hold);background:#fdf6ec;border-color:#eed9b8}}
.feats{{display:flex;flex-direction:column;gap:14px}}
.feat{{display:flex;gap:14px;align-items:flex-start}}
.feat .ic{{flex:none;width:34px;height:34px;border-radius:9px;background:var(--surface);border:1px solid var(--border);
display:flex;align-items:center;justify-content:center;box-shadow:0 1px 3px rgba(30,33,38,.05)}}
.feat .ic svg{{width:17px;height:17px;stroke:var(--accent);fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}}
.feat b{{display:block;font-size:14px;margin-bottom:2px}}
.feat p{{font-size:13px;color:var(--muted);line-height:1.55}}
/* ── login card ── */
.card{{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:34px;
box-shadow:0 10px 30px rgba(30,33,38,.09);position:sticky;top:24px;
animation:slideIn .8s .3s cubic-bezier(.22,1,.36,1) backwards}}
.card h2{{font-size:21px;margin-bottom:6px}}
.subtitle{{color:var(--muted);margin-bottom:20px;font-size:13px;line-height:1.55}}
label{{display:block;font-size:13px;color:var(--muted);margin-bottom:4px;margin-top:16px}}
input{{width:100%;padding:10px 12px;background:#fbfbf9;border:1px solid var(--border);border-radius:6px;
color:var(--text);font-size:14px;outline:none}}
input:focus{{border-color:var(--accent);box-shadow:0 0 0 3px rgba(36,86,230,.1)}}
button{{width:100%;padding:12px;background:var(--accent);color:#fff;border:none;border-radius:6px;
font-size:15px;font-weight:600;cursor:pointer;margin-top:24px;
transition:background .2s,transform .15s,box-shadow .2s}}
button:hover{{background:#1d47c4;transform:translateY(-1px);box-shadow:0 6px 16px rgba(36,86,230,.32)}}
.alt{{text-align:center;margin-top:16px}}
.alt a{{color:var(--accent);text-decoration:none;font-size:13px}}
.error{{background:#fdf1f2;border:1px solid #f2c8cd;color:var(--sell);padding:8px 12px;border-radius:6px;
margin-top:12px;font-size:13px;display:none}}
.success{{background:#effaf4;border:1px solid #bfe5d2;color:var(--buy);padding:12px;border-radius:6px;
margin-top:12px;font-size:13px;display:none}}
.priv{{margin-top:18px;padding-top:14px;border-top:1px solid var(--border-subtle);font-size:11px;
color:var(--dim);line-height:1.6}}
.priv b{{color:var(--muted)}}
/* ── tabs grid ── */
.sect{{padding:34px 0 8px}}
.sect h3{{font-size:12px;font-weight:700;letter-spacing:1.5px;color:var(--muted);text-transform:uppercase;margin-bottom:16px}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}}
.tile{{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:18px 18px 16px;
box-shadow:0 1px 3px rgba(30,33,38,.05)}}
.tile .tag{{font-size:10px;font-weight:800;letter-spacing:1px;color:var(--accent);margin-bottom:8px}}
.tile b{{display:block;font-size:14px;margin-bottom:5px}}
.tile p{{font-size:12px;color:var(--muted);line-height:1.55}}
/* ── how it works ── */
.steps{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;counter-reset:st}}
.step{{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:18px;position:relative}}
.step .n{{width:26px;height:26px;border-radius:50%;background:var(--accent);color:#fff;font-size:12px;font-weight:800;
display:flex;align-items:center;justify-content:center;margin-bottom:10px}}
.step b{{display:block;font-size:14px;margin-bottom:4px}}
.step p{{font-size:12px;color:var(--muted);line-height:1.55}}
footer{{padding:36px 0 28px;font-size:11px;color:var(--dim);text-align:center;line-height:1.7}}
/* ── motion ── */
@keyframes fadeUp{{from{{opacity:0;transform:translateY(18px)}}to{{opacity:1;transform:none}}}}
@keyframes popIn{{from{{opacity:0;transform:scale(.8)}}to{{opacity:1;transform:scale(1)}}}}
@keyframes slideIn{{from{{opacity:0;transform:translateX(28px)}}to{{opacity:1;transform:none}}}}
@keyframes pulse{{0%{{box-shadow:0 0 0 0 rgba(11,122,75,.45)}}70%{{box-shadow:0 0 0 7px rgba(11,122,75,0)}}100%{{box-shadow:0 0 0 0 rgba(11,122,75,0)}}}}
@keyframes marquee{{from{{transform:translateX(0)}}to{{transform:translateX(-50%)}}}}
@keyframes draw{{to{{stroke-dashoffset:0}}}}
.au{{opacity:0;animation:fadeUp .7s cubic-bezier(.22,1,.36,1) forwards}}
.chips .chip{{opacity:0;animation:popIn .45s cubic-bezier(.34,1.56,.64,1) forwards}}
.chips .chip:nth-child(1){{animation-delay:.30s}} .chips .chip:nth-child(2){{animation-delay:.38s}}
.chips .chip:nth-child(3){{animation-delay:.46s}} .chips .chip:nth-child(4){{animation-delay:.54s}}
.chips .chip:nth-child(5){{animation-delay:.62s}} .chips .chip:nth-child(6){{animation-delay:.70s}}
/* ── ticker ── */
.ticker{{overflow:hidden;background:var(--surface);border-bottom:1px solid var(--border);
white-space:nowrap;padding:8px 0;font-size:12px;color:var(--muted)}}
.tk{{display:inline-flex;gap:30px;padding-right:30px;animation:marquee 38s linear infinite;will-change:transform}}
.tk span b{{color:var(--text);font-weight:700;margin-right:6px}}
.up{{color:var(--buy);font-weight:600}} .dn{{color:var(--sell);font-weight:600}}
/* ── live mock panel ── */
.livedot{{width:7px;height:7px;border-radius:50%;background:var(--buy);display:inline-block;
margin-right:8px;animation:pulse 1.8s infinite;vertical-align:1px}}
.mock{{display:flex;align-items:center;gap:18px;background:var(--surface);border:1px solid var(--border);
border-radius:12px;padding:14px 18px;margin-bottom:28px;box-shadow:0 4px 14px rgba(30,33,38,.06);max-width:540px}}
.mock svg{{flex:none}}
.mock .sym{{font-weight:800;font-size:15px;margin-right:10px}}
.mock .lbl{{font-size:10px;font-weight:800;letter-spacing:.6px;padding:3px 10px;border-radius:999px;border:1px solid}}
.mock .meta{{font-size:11px;color:var(--muted);margin-top:5px}}
.pb{{color:var(--buy);background:#effaf4;border-color:#bfe5d2}}
.ps{{color:var(--sell);background:#fdf1f2;border-color:#f2c8cd}}
.ph{{color:var(--hold);background:#fdf6ec;border-color:#eed9b8}}
.spark{{stroke-dasharray:280;stroke-dashoffset:280;animation:draw 2s ease forwards}}
/* ── scroll reveal + hover ── */
.rv{{opacity:0;transform:translateY(22px);transition:opacity .6s ease,transform .6s cubic-bezier(.22,1,.36,1)}}
.rv.in{{opacity:1;transform:none}}
.grid .rv:nth-child(2),.steps .rv:nth-child(2){{transition-delay:.08s}}
.grid .rv:nth-child(3),.steps .rv:nth-child(3){{transition-delay:.16s}}
.grid .rv:nth-child(4){{transition-delay:.24s}}
.tile,.step{{transition:transform .25s ease,box-shadow .25s ease}}
.tile:hover,.step:hover{{transform:translateY(-4px);box-shadow:0 10px 24px rgba(30,33,38,.10)}}
@media(prefers-reduced-motion:reduce){{
  *{{animation:none!important;transition:none!important}}
  .au,.chips .chip,.rv,.card{{opacity:1;transform:none}}
  .spark{{stroke-dashoffset:0}}
}}
@media(max-width:900px){{
  .hero{{grid-template-columns:1fr;gap:32px}}
  .card{{position:static;max-width:440px}}
  h1.head{{font-size:30px}}
  .grid,.steps{{grid-template-columns:1fr 1fr}}
}}
@media(max-width:540px){{ .grid,.steps{{grid-template-columns:1fr}} }}
</style></head><body>
<div class="ticker"><div class="tk" id="tk">
<span><b>AAPL</b><i class="up">+0.8%</i></span><span><b>NVDA</b><i class="up">+1.6%</i></span>
<span><b>MSFT</b><i class="dn">-0.4%</i></span><span><b>TSLA</b><i class="up">+2.1%</i></span>
<span><b>AMZN</b><i class="up">+0.5%</i></span><span><b>SPY</b><i class="up">+0.3%</i></span>
<span><b>QQQ</b><i class="up">+0.6%</i></span><span><b>META</b><i class="dn">-1.1%</i></span>
<span><b>GOOGL</b><i class="up">+0.9%</i></span><span><b>AMD</b><i class="up">+1.8%</i></span>
<span><b>JPM</b><i class="dn">-0.2%</i></span><span><b>XOM</b><i class="up">+0.4%</i></span>
</div></div>
<div class="wrap">

<div class="topbar">
  <div class="logo">IB TRADING <em>DASHBOARD</em><small>ESC&Aacute;NER &middot; SE&Ntilde;ALES &middot; CARTERA &middot; OPCIONES</small></div>
</div>

<div class="hero">
  <div>
    <span class="eyebrow au"><span class="livedot"></span>CONECTADO A TU INTERACTIVE BROKERS</span>
    <h1 class="head au" style="animation-delay:.08s">Escaneá el mercado y decidí con datos: <em>qué comprar, cuándo y a qué precio</em></h1>
    <p class="lead au" style="animation-delay:.16s">La plataforma escanea 100 acciones y ETFs cada 5 minutos y convierte el análisis
    técnico en decisiones concretas: señal de compra o venta, precio de entrada, objetivo y stop —
    con backtest de 5 años detrás de cada recomendación. Y no es solo el escáner: también analiza
    tu cartera posición por posición, evalúa estrategias de opciones y repasa tus trades cerrados
    para que aprendas de cada operación.</p>
    <div class="chips">
      <span class="chip b">COMPRA</span><span class="chip b">COMPRA INMINENTE</span>
      <span class="chip h">VIRANDO A COMPRA</span><span class="chip h">NEUTRAL</span>
      <span class="chip s">VENTA INMINENTE</span><span class="chip s">VENTA</span>
    </div>
    <div class="mock au" style="animation-delay:.4s">
      <svg width="120" height="38" viewBox="0 0 120 38">
        <path id="mock-spark" class="spark" d="M2,30 L14,26 L26,28 L38,20 L50,23 L62,15 L74,18 L86,10 L98,13 L110,6 L118,8"
          fill="none" stroke="#0b7a4b" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      <div>
        <span class="sym" id="mock-sym">NVDA</span><span class="lbl pb" id="mock-lbl">COMPRA INMINENTE</span>
        <div class="meta" id="mock-meta">RSI 33 &middot; MACD girando al alza &middot; 2/3 condiciones</div>
      </div>
    </div>
    <div class="feats">
      <div class="feat au" style="animation-delay:.5s"><div class="ic"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/></svg></div><div>
        <b>Top recomendaciones con objetivo real</b>
        <p>Solo señales completas o inminentes con zonas coherentes. Cada una trae entrada, objetivo anclado a techos/pisos técnicos, stop y relación riesgo/beneficio.</p>
      </div></div>
      <div class="feat au" style="animation-delay:.58s"><div class="ic"><svg viewBox="0 0 24 24"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg></div><div>
        <b>Backtest de 5 años por activo</b>
        <p>Win-rate, expectancy y confianza estadística calibrada de cada setup — sabés si el edge es real o ruido antes de operar.</p>
      </div></div>
      <div class="feat au" style="animation-delay:.66s"><div class="ic"><svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg></div><div>
        <b>Tesis en español, sin jerga</b>
        <p>Cada activo explica qué condiciones ya cumple, qué le falta para confirmar señal y por qué el objetivo es ese.</p>
      </div></div>
      <div class="feat au" style="animation-delay:.74s"><div class="ic"><svg viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg></div><div>
        <b>Tus datos nunca salen de tu maquina</b>
        <p>TWS corre en tu computadora; un pequeño bridge le manda los datos al dashboard. Sin claves de IB en la nube.</p>
      </div></div>
    </div>
  </div>

  <div class="card">
    <h2>{title}</h2>
    <p class="subtitle">{cta_hint}</p>
    <form id="form">
    <label>Email</label><input type="email" id="email" required>
    <label>Contraseña</label><input type="password" id="password" required minlength="8">
    <div class="error" id="error"></div>
    <div class="success" id="success"></div>
    <button type="submit">{title}</button>
    </form>
    <div class="alt"><a href="{alt_link}">{alt_text}</a></div>
    <div class="priv"><b>Sin riesgo:</b> el dashboard solo lee datos de mercado y tu cartera.
    No ejecuta órdenes ni pide tus credenciales de Interactive Brokers.</div>
  </div>
</div>

<div class="sect">
  <h3>Lo que vas a ver adentro</h3>
  <div class="grid">
    <div class="tile rv"><div class="tag">TAB 1</div><b>Escáner</b>
      <p>100 acciones y ETFs con señal, fuerza, RSI, condiciones de giro y confianza de backtest. Gráficos de velas de 1D a 5 años.</p></div>
    <div class="tile rv"><div class="tag">TAB 2</div><b>Mi Cartera</b>
      <p>Tus posiciones reales de IB con veredicto por posición: mantener, tomar ganancia o salir, con el mismo análisis del escáner.</p></div>
    <div class="tile rv"><div class="tag">TAB 3</div><b>Options Lab</b>
      <p>15 estrategias de opciones evaluadas con IV real de mercado, Monte Carlo y valor esperado — ranking de las mejores para cada señal.</p></div>
    <div class="tile rv"><div class="tag">TAB 4</div><b>Trades Históricos</b>
      <p>Tus trades cerrados con gráfico e indicadores al momento de entrada y salida: qué hiciste bien y qué no, trade por trade.</p></div>
  </div>
</div>

<div class="sect">
  <h3>Cómo funciona</h3>
  <div class="steps">
    <div class="step rv"><div class="n">1</div><b>Crea tu cuenta</b>
      <p>Email y contraseña. El escáner y las recomendaciones funcionan desde el primer minuto, sin conectar nada.</p></div>
    <div class="step rv"><div class="n">2</div><b>Conecta tu TWS</b>
      <p>Un comando en la terminal instala el bridge que enlaza tu Trader Workstation con el dashboard. Tus claves de IB no salen de tu máquina.</p></div>
    <div class="step rv"><div class="n">3</div><b>Mira tu cartera en vivo</b>
      <p>Posiciones, señales sobre lo que tenés y análisis de cada oportunidad, actualizado cada 5 minutos.</p></div>
  </div>
</div>

<footer>IB Trading Dashboard &middot; Herramienta de análisis técnico — no es asesoramiento financiero.<br>
Requiere cuenta de Interactive Brokers y TWS para datos de cartera en vivo.</footer>
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

// duplicar contenido del ticker para loop continuo
const tk=document.getElementById('tk');
tk.innerHTML+=tk.innerHTML;

// reveal on scroll
const io=new IntersectionObserver(es=>es.forEach(en=>{{
  if(en.isIntersecting){{ en.target.classList.add('in'); io.unobserve(en.target); }}
}}),{{threshold:.15}});
document.querySelectorAll('.rv').forEach(el=>io.observe(el));

// panel "señal en vivo": rota entre ejemplos y re-dibuja el sparkline
const MOCKS=[
 {{sym:'NVDA',lbl:'COMPRA INMINENTE',cls:'pb',color:'#0b7a4b',
   meta:'RSI 33 · MACD girando al alza · 2/3 condiciones',
   path:'M2,30 L14,26 L26,28 L38,20 L50,23 L62,15 L74,18 L86,10 L98,13 L110,6 L118,8'}},
 {{sym:'MSFT',lbl:'VIRANDO A COMPRA',cls:'ph',color:'#b45309',
   meta:'RSI 41 · Koncorde girando desde piso · 1/3 condiciones',
   path:'M2,26 L14,18 L26,24 L38,14 L50,20 L62,10 L74,16 L86,20 L98,14 L110,17 L118,12'}},
 {{sym:'XLE',lbl:'VENTA',cls:'ps',color:'#c22436',
   meta:'RSI 74 · MACD cayendo · 3/3 condiciones',
   path:'M2,8 L14,12 L26,10 L38,17 L50,14 L62,22 L74,19 L86,27 L98,24 L110,30 L118,28'}}
];
let _mi=0;
const _reduced=window.matchMedia('(prefers-reduced-motion: reduce)').matches;
function setMock(i){{
  const m=MOCKS[i], sp=document.getElementById('mock-spark');
  document.getElementById('mock-sym').textContent=m.sym;
  const lbl=document.getElementById('mock-lbl');
  lbl.textContent=m.lbl; lbl.className='lbl '+m.cls;
  document.getElementById('mock-meta').innerHTML=m.meta.replace(/·/g,'&middot;');
  sp.setAttribute('d',m.path); sp.setAttribute('stroke',m.color);
  sp.classList.remove('spark'); void sp.getBoundingClientRect(); sp.classList.add('spark');
}}
if(!_reduced) setInterval(()=>{{ _mi=(_mi+1)%MOCKS.length; setMock(_mi); }},4200);
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
        '  <button class="nav-tab" onclick="switchTab(\'setup\')">Conectar TWS</button>\n'
        '  <button class="nav-tab" onclick="switchTab(\'feedback\')">Tu Opinion</button>\n</div>',
    )

    # 2. Header: bridge status + user email + logout (right side, stacked under the sub line)
    html = html.replace(
        '<div class="sub">Esc&aacute;ner &middot; Se&ntilde;ales &middot; Cartera &middot; Opciones &nbsp;&bull;&nbsp; <span id="port-info"></span></div>\n</div>',
        '<div style="display:flex;flex-direction:column;align-items:flex-end;gap:4px">\n'
        '    <div class="sub">Esc&aacute;ner &middot; Se&ntilde;ales &middot; Cartera &middot; Opciones &nbsp;&bull;&nbsp; <span id="port-info"></span></div>\n'
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
<div class="setup-section" style="max-width:760px;margin:28px auto 48px;padding:0 20px">
  <div class="port-title" style="margin-bottom:2px">CONECTAR <em>TWS</em></div>
  <p style="margin:0 0 18px;color:var(--muted);font-size:13px">Tu TWS corre en tu maquina; el bridge le manda los datos a este dashboard. Solo 3 pasos.</p>
  <div class="setup-card" style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);padding:26px 28px;box-shadow:var(--shadow-sm)">
    <div class="setup-steps" style="display:flex;flex-direction:column;gap:22px">
      <div style="display:flex;gap:14px">
        <span style="background:var(--accent);color:#fff;border-radius:50%;width:26px;height:26px;flex:none;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:12px">1</span>
        <div>
          <h3 style="margin:0 0 4px;font-size:14px;font-weight:700">Abri TWS</h3>
          <p style="margin:0;font-size:13px;color:var(--muted);line-height:1.7">Abre Trader Workstation y habilita la API:<br>
          <code style="background:var(--bg);border:1px solid var(--border-subtle);border-radius:5px;padding:1px 7px;font-size:12px">Edit &rarr; Global Configuration &rarr; API &rarr; Settings</code><br>
          <span style="color:var(--buy)">&check;</span> Enable ActiveX and Socket Clients &nbsp; <span style="color:var(--buy)">&check;</span> Puerto: <code style="background:var(--bg);border:1px solid var(--border-subtle);border-radius:5px;padding:1px 7px;font-size:12px">7497</code></p>
        </div>
      </div>
      <div style="display:flex;gap:14px">
        <span style="background:var(--accent);color:#fff;border-radius:50%;width:26px;height:26px;flex:none;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:12px">2</span>
        <div style="flex:1;min-width:0">
          <h3 style="margin:0 0 4px;font-size:14px;font-weight:700">Instalar el Bridge <span style="font-size:11px;color:var(--muted);font-weight:500">(solo la primera vez)</span></h3>
          <p style="margin:0 0 8px;font-size:13px;color:var(--muted)">Abri la Terminal y pega este comando:</p>
          <div style="display:flex;gap:8px;align-items:stretch">
            <div id="install-cmd" style="flex:1;min-width:0;background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:10px 12px;font-family:'JetBrains Mono',monospace;font-size:12px;overflow-x:auto;white-space:nowrap;color:var(--text)"></div>
            <button id="install-btn" onclick="copyCmd('install-cmd','install-btn')" style="flex:none;background:var(--accent);color:#fff;border:none;padding:0 18px;border-radius:8px;cursor:pointer;font-size:11px;font-weight:700;letter-spacing:.3px">Copiar</button>
          </div>
          <p style="font-size:11px;color:var(--dim);margin-top:6px">Requiere Python 3.10+ &nbsp;&middot;&nbsp; Se instala en <code>~/.ib-bridge/</code></p>
        </div>
      </div>
      <div style="display:flex;gap:14px">
        <span style="background:var(--accent);color:#fff;border-radius:50%;width:26px;height:26px;flex:none;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:12px">3</span>
        <div style="flex:1;min-width:0">
          <h3 style="margin:0 0 4px;font-size:14px;font-weight:700">Conectar</h3>
          <p style="margin:0 0 8px;font-size:13px;color:var(--muted)">Cada vez que quieras conectar, pega esto en la Terminal:</p>
          <div style="display:flex;gap:8px;align-items:stretch">
            <div id="run-cmd" style="flex:1;min-width:0;background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:10px 12px;font-family:'JetBrains Mono',monospace;font-size:12px;overflow-x:auto;white-space:nowrap;color:var(--text)"></div>
            <button id="run-btn" onclick="copyCmd('run-cmd','run-btn')" style="flex:none;background:var(--accent);color:#fff;border:none;padding:0 18px;border-radius:8px;cursor:pointer;font-size:11px;font-weight:700;letter-spacing:.3px">Copiar</button>
          </div>
          <p style="font-size:11px;color:var(--dim);margin-top:6px">El indicador de arriba cambiara a <span style="color:var(--buy);font-weight:600">&#9679; Conectado</span></p>
        </div>
      </div>
    </div>
    <div style="margin-top:24px;padding:14px 18px;background:var(--bg);border:1px solid var(--border-subtle);border-radius:10px;display:flex;justify-content:space-between;align-items:center">
      <div>
        <p style="font-size:10px;color:var(--muted);margin:0;text-transform:uppercase;letter-spacing:.7px;font-weight:600">Estado de conexion</p>
        <p id="setup-live-status" style="font-size:14px;margin:4px 0 0;font-weight:600">Verificando...</p>
      </div>
      <div id="setup-status-dot" style="width:12px;height:12px;border-radius:50%;background:var(--dim)"></div>
    </div>
  </div>
    <details style="text-align:left;margin-top:14px;background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden">
      <summary style="color:var(--accent);cursor:pointer;font-size:13px;font-weight:600;padding:13px 18px">Opciones avanzadas</summary>
      <div style="padding:4px 18px 16px">
        <p style="font-size:12px;color:var(--muted);margin-bottom:6px">Tu bridge token (no lo compartas):</p>
        <div id="token-display" style="font-family:'JetBrains Mono',monospace;font-size:12px;background:var(--bg);border:1px solid var(--border);padding:9px 12px;border-radius:8px;word-break:break-all">Cargando...</div>
        <button onclick="regenerateToken()" style="background:var(--accent);color:#fff;border:none;padding:7px 16px;border-radius:8px;cursor:pointer;font-size:11px;font-weight:700;margin-top:10px">Regenerar Token</button>
        <p style="font-size:11px;color:var(--dim);margin-top:12px">Puerto 7497 = paper trading &nbsp;&middot;&nbsp; Agrega <code>--ib-port 7496</code> para live</p>
      </div>
    </details>
    <details style="text-align:left;margin-top:12px;background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden">
      <summary style="color:var(--accent);cursor:pointer;font-size:13px;font-weight:600;padding:13px 18px">Ver historial completo de trades (opcional)</summary>
      <div style="padding:4px 18px 18px">
        <p style="font-size:12px;color:var(--muted);margin-bottom:14px;line-height:1.6">
          Por defecto, "Trades Historicos" solo muestra las operaciones de <b>hoy</b>
          (asi funciona la conexion normal con TWS). Para ver tu historial completo,
          IB pide un paso extra de configuracion que se hace <b>una sola vez</b> en tu cuenta.
          Son 3 partes.
        </p>

        <div style="background:var(--bg);border:1px solid var(--border-subtle);border-radius:8px;padding:12px 14px;margin-bottom:10px">
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

        <div style="background:var(--bg);border:1px solid var(--border-subtle);border-radius:8px;padding:12px 14px;margin-bottom:14px">
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
        <input id="flex-token-input" type="password" placeholder="Pega el token aqui" style="width:100%;box-sizing:border-box;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:8px;border-radius:6px;font-family:monospace;font-size:12px;margin-bottom:8px">
        <p style="font-size:11px;color:var(--muted);margin-bottom:4px">Query ID (el numero del paso 1):</p>
        <input id="flex-query-input" type="text" placeholder="Ej: 123456" style="width:100%;box-sizing:border-box;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:8px;border-radius:6px;font-family:monospace;font-size:12px;margin-bottom:8px">
        <button onclick="saveFlexConfig()" style="background:var(--accent);color:#fff;border:none;padding:8px 16px;border-radius:6px;cursor:pointer;font-size:12px;font-weight:600">Guardar y Probar</button>
        <span id="flex-config-status" style="font-size:11px;margin-left:8px"></span>
      </div>
    </details>
</div>
</div>

'''
    # Feedback / rating tab — collect satisfaction + improvement ideas.
    feedback_tab_html = '''
<!-- TAB: TU OPINION -->
<div id="tab-feedback" class="tab-content">
<div class="setup-section" style="max-width:680px;margin:28px auto 48px;padding:0 20px">
  <div class="port-title" style="margin-bottom:2px">TU <em>OPINION</em></div>
  <p style="margin:0 0 18px;color:var(--muted);font-size:13px">Cuentanos que te parece la plataforma y que te gustaria mejorar. Cada comentario nos ayuda a priorizar.</p>

  <div id="fb-form" class="setup-card" style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);padding:26px 28px;box-shadow:var(--shadow-sm)">
    <label style="display:block;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);margin-bottom:8px">Como valorarias la plataforma?</label>
    <div id="fb-stars" style="display:flex;gap:6px;margin-bottom:22px;font-size:34px;line-height:1;user-select:none">
      <span class="fb-star" data-val="1" onclick="setRating(1)" onmouseover="hoverRating(1)" onmouseout="hoverRating(0)" style="cursor:pointer;color:var(--border)">&#9733;</span>
      <span class="fb-star" data-val="2" onclick="setRating(2)" onmouseover="hoverRating(2)" onmouseout="hoverRating(0)" style="cursor:pointer;color:var(--border)">&#9733;</span>
      <span class="fb-star" data-val="3" onclick="setRating(3)" onmouseover="hoverRating(3)" onmouseout="hoverRating(0)" style="cursor:pointer;color:var(--border)">&#9733;</span>
      <span class="fb-star" data-val="4" onclick="setRating(4)" onmouseover="hoverRating(4)" onmouseout="hoverRating(0)" style="cursor:pointer;color:var(--border)">&#9733;</span>
      <span class="fb-star" data-val="5" onclick="setRating(5)" onmouseover="hoverRating(5)" onmouseout="hoverRating(0)" style="cursor:pointer;color:var(--border)">&#9733;</span>
      <span id="fb-rating-label" style="align-self:center;margin-left:10px;font-size:13px;color:var(--muted)"></span>
    </div>

    <label style="display:block;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);margin-bottom:8px">Tipo de comentario</label>
    <select id="fb-category" style="width:100%;box-sizing:border-box;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:10px;border-radius:8px;font-size:13px;margin-bottom:18px">
      <option value="Mejora">Mejora sobre algo existente</option>
      <option value="Idea">Idea o funcionalidad nueva</option>
      <option value="Problema">Problema / algo no funciona</option>
      <option value="Elogio">Me gusta / elogio</option>
      <option value="Otro">Otro</option>
    </select>

    <label style="display:block;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);margin-bottom:8px">Tu comentario</label>
    <textarea id="fb-message" rows="5" maxlength="4000" placeholder="Que te gustaria que mejoremos, agreguemos o cambiemos?" style="width:100%;box-sizing:border-box;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:11px 12px;border-radius:8px;font-size:13px;line-height:1.6;resize:vertical;font-family:inherit"></textarea>

    <div style="display:flex;align-items:center;gap:12px;margin-top:18px">
      <button id="fb-submit-btn" onclick="submitFeedback()" style="background:var(--accent);color:#fff;border:none;padding:10px 22px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:700">Enviar comentario</button>
      <span id="fb-status" style="font-size:12px"></span>
    </div>
  </div>

  <div id="fb-thanks" style="display:none;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);padding:32px 28px;box-shadow:var(--shadow-sm);text-align:center">
    <div style="font-size:38px;margin-bottom:8px">&#128075;</div>
    <p style="font-size:16px;font-weight:700;margin:0 0 6px;color:var(--text)">Gracias por tu opinion</p>
    <p style="font-size:13px;color:var(--muted);margin:0 0 18px">La tuvimos en cuenta. Podes dejar otro comentario cuando quieras.</p>
    <button onclick="resetFeedback()" style="background:var(--bg);color:var(--accent);border:1px solid var(--border);padding:9px 18px;border-radius:8px;cursor:pointer;font-size:12px;font-weight:600">Dejar otro comentario</button>
  </div>

  <!-- Owner-only: collected feedback review. Hidden unless /api/feedback (GET) returns 200. -->
  <div id="fb-admin" style="display:none;margin-top:26px">
    <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:12px">
      <div class="port-title" style="font-size:15px">COMENTARIOS <em>RECIBIDOS</em></div>
      <span id="fb-admin-stats" style="font-size:12px;color:var(--muted)"></span>
    </div>
    <div id="fb-admin-list" style="display:flex;flex-direction:column;gap:10px"></div>
  </div>
</div>
</div>

'''
    html = html.replace('\n<div class="footer">', setup_tab_html + feedback_tab_html + '<div class="footer">')

    # 4. switchTab(): load the setup tab's dynamic content when opened
    html = html.replace(
        "if(tab==='etf'&&!_etfLoaded){_etfLoaded=true;updateEtf();}\n}",
        "if(tab==='etf'&&!_etfLoaded){_etfLoaded=true;updateEtf();}\n"
        "  if(tab==='setup')renderSetup();\n"
        "  if(tab==='feedback')onFeedbackTab();\n}",
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
// ---- Tu Opinion (feedback) tab ----
let _fbRating=0;
let _fbAdminLoaded=false;
const _fbLabels={1:'Muy mala',2:'Mala',3:'Regular',4:'Buena',5:'Excelente'};
function paintStars(n){
  document.querySelectorAll('#fb-stars .fb-star').forEach(s=>{
    s.style.color=(parseInt(s.dataset.val)<=n)?'#f5b301':'var(--border)';
  });
}
function hoverRating(n){paintStars(n||_fbRating);}
function setRating(n){
  _fbRating=n;
  paintStars(n);
  let lbl=document.getElementById('fb-rating-label');
  if(lbl)lbl.textContent=_fbLabels[n]||'';
}
async function submitFeedback(){
  let statusEl=document.getElementById('fb-status');
  let msg=document.getElementById('fb-message').value.trim();
  let cat=document.getElementById('fb-category').value;
  if(_fbRating<1){statusEl.innerHTML='<span style="color:var(--sell)">Elegi cuantas estrellas</span>';return;}
  if(!msg){statusEl.innerHTML='<span style="color:var(--sell)">Escribi un comentario</span>';return;}
  let btn=document.getElementById('fb-submit-btn');
  btn.disabled=true;statusEl.textContent='Enviando...';
  try{
    let r=await fetch('/api/feedback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rating:_fbRating,category:cat,message:msg})});
    let d=await r.json();
    if(!r.ok){statusEl.innerHTML='<span style="color:var(--sell)">'+(d.error||'Error')+'</span>';btn.disabled=false;return;}
    document.getElementById('fb-form').style.display='none';
    document.getElementById('fb-thanks').style.display='';
    statusEl.textContent='';
    _fbAdminLoaded=false;
    loadFeedbackAdmin();
  }catch(e){
    statusEl.innerHTML='<span style="color:var(--sell)">Error de conexion</span>';
    btn.disabled=false;
  }
}
function resetFeedback(){
  _fbRating=0;
  paintStars(0);
  document.getElementById('fb-rating-label').textContent='';
  document.getElementById('fb-message').value='';
  document.getElementById('fb-category').selectedIndex=0;
  document.getElementById('fb-status').textContent='';
  document.getElementById('fb-submit-btn').disabled=false;
  document.getElementById('fb-thanks').style.display='none';
  document.getElementById('fb-form').style.display='';
}
function _fbEsc(s){let d=document.createElement('div');d.textContent=s==null?'':s;return d.innerHTML;}
async function loadFeedbackAdmin(){
  try{
    let r=await fetch('/api/feedback');
    if(r.status!==200){return;}  // non-owner: keep panel hidden
    let d=await r.json();
    let box=document.getElementById('fb-admin');
    let list=document.getElementById('fb-admin-list');
    let stats=document.getElementById('fb-admin-stats');
    box.style.display='';
    stats.textContent=d.count+' comentario'+(d.count===1?'':'s')+(d.count?' \\u00b7 promedio '+d.avg_rating+' \\u2605':'');
    if(!d.items.length){list.innerHTML='<p style="font-size:13px;color:var(--muted)">Todavia no hay comentarios.</p>';return;}
    list.innerHTML=d.items.map(function(it){
      let stars='\\u2605'.repeat(it.rating||0)+'\\u2606'.repeat(5-(it.rating||0));
      return '<div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px 14px">'
        +'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">'
        +'<span style="color:#f5b301;font-size:13px;letter-spacing:1px">'+stars+'</span>'
        +'<span style="font-size:11px;color:var(--dim)">'+_fbEsc(it.created_at)+'</span></div>'
        +'<p style="margin:0 0 6px;font-size:13px;color:var(--text);line-height:1.55">'+_fbEsc(it.message)+'</p>'
        +'<div style="font-size:11px;color:var(--muted)"><span style="background:var(--bg);border:1px solid var(--border-subtle);border-radius:5px;padding:1px 7px">'+_fbEsc(it.category||'-')+'</span> &middot; '+_fbEsc(it.email||'')+'</div>'
        +'</div>';
    }).join('');
    _fbAdminLoaded=true;
  }catch(e){}
}
function onFeedbackTab(){
  if(!_fbAdminLoaded)loadFeedbackAdmin();
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
    _restore_stores()
except Exception as e:
    print(f"[SERVER] WARNING: Database init failed: {e}")
    print("[SERVER] Server will start but registration/login won't work until DB is available")


def _enrichment_symbols():
    """Universo para el enriquecimiento: union de acciones + ETFs de todos los
    stores de usuarios (en la practica todos comparten el mismo universo)."""
    syms = []
    with user_data_lock:
        stores = list(user_data.values())
    for store in stores:
        syms += list(store.get("stocks", []) or [])
        syms += list(store.get("etf_stocks", []) or [])
    return syms


try:
    # Sin persist_path: el filesystem de Railway es efimero, se rellena solo.
    enrichment.start_background(_enrichment_symbols)
    print("[SERVER] Enrichment threads started")
except Exception as e:
    print(f"[SERVER] WARNING: enrichment no arranco: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[SERVER] Starting on port {port} (gevent)")
    socketio.run(app, host="0.0.0.0", port=port, debug=False)
