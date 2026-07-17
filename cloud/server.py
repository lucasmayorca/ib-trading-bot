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
import functools
import threading
from datetime import datetime

from flask import Flask, request, jsonify, Response, redirect
from flask_socketio import SocketIO, emit, disconnect
from dotenv import load_dotenv

load_dotenv()

from cloud import db, auth

# ══════════════════════════════════════════════════════════════
#  APP SETUP
# ══════════════════════════════════════════════════════════════

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("JWT_SECRET", "change-me-in-production")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent")

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
                "portfolio_positions": [],
                "account_values": {},
                "open_orders": [],
                "executions": [],
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
    store = get_user_store(request.user_id)
    results = {}
    for symbol in store.get("stocks", []):
        result = store.get("analysis", {}).get(symbol)
        if result:
            # Ensure symbol is included in each result
            result["symbol"] = symbol
            results[symbol] = result

    sorted_results = sorted(results.values(), key=lambda x: x.get("strength", 0), reverse=True)
    top3 = []
    for r in sorted_results[:3]:
        if r.get("conditions_met", 0) >= 2 or r.get("signal") in ("BUY", "SELL"):
            top3.append(r)

    return Response(
        to_json({
            "results": results,
            "top3": top3,
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


@app.route("/api/portfolio")
@login_required
def api_portfolio():
    store = get_user_store(request.user_id)
    positions = store.get("portfolio_positions", [])
    analysis = store.get("analysis", {})
    acct_vals = store.get("account_values", {})

    total_value = sum(p.get("marketValue", 0) for p in positions)
    total_cost = sum(p.get("averageCost", 0) * p.get("position", 0) for p in positions)
    total_pnl = sum(p.get("unrealizedPNL", 0) for p in positions)
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0

    acct = {}
    for key, val in acct_vals.items():
        try:
            acct[key] = {"value": float(val)}
        except:
            acct[key] = {"value": val}

    positions_with_analysis = []
    for pos in positions:
        sym = pos.get("symbol", "")
        scan_data = analysis.get(sym, {})

        verdict = "HOLD"
        if scan_data.get("signal") == "SELL":
            verdict = "SELL"
        elif scan_data.get("signal") == "BUY":
            verdict = "ADD"

        positions_with_analysis.append({
            **pos,
            "verdict": verdict,
            "signal_label": scan_data.get("signal_label", ""),
            "strength": scan_data.get("strength", 0),
            "conditions_met": scan_data.get("conditions_met", 0),
            "macd_ok": scan_data.get("macd_ok", False),
            "rsi_ok": scan_data.get("rsi_ok", False),
            "konc_ok": scan_data.get("konc_ok", False),
        })

    return Response(
        to_json({
            "positions": positions_with_analysis,
            "total_value": total_value,
            "total_cost": total_cost,
            "total_pnl": total_pnl,
            "total_pnl_pct": total_pnl_pct,
            "num_positions": len(positions),
            "account": acct,
            "account_values": acct_vals,
            "open_orders": store.get("open_orders", []),
            "executions": store.get("executions", []),
            "bridge_connected": store.get("connected", False),
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

    return jsonify({
        "bridge_connected": store.get("connected", False),
        "stocks_sent_by_bridge": len(stocks),
        "stocks_list": stocks[:10],  # First 10
        "analysis_received": len(analysis),
        "analysis_symbols": list(analysis.keys())[:10],  # First 10
        "analysis_sample": analysis.get(stocks[0], {}) if stocks else None,
        "last_update": store.get("last_update", ""),
        "bridge_sessions_total": len(bridge_sessions),
    })


@app.route("/api/options-lab-top")
@login_required
def api_options_lab_top():
    store = get_user_store(request.user_id)
    results = store.get("analysis", {})
    if not results:
        return Response(to_json({"strategies": []}), mimetype="application/json")

    candidates = sorted(
        [r for r in results.values() if r],
        key=lambda x: x.get("strength", 0),
        reverse=True
    )[:10]

    return Response(
        to_json({
            "strategies": [
                {
                    "symbol": c.get("symbol"),
                    "price": c.get("price"),
                    "signal": c.get("signal"),
                    "strength": c.get("strength"),
                    "message": "Análisis de opciones disponible próximamente"
                }
                for c in candidates
            ]
        }),
        mimetype="application/json",
    )


@app.route("/api/options-lab/<symbol>")
@login_required
def api_options_lab(symbol):
    store = get_user_store(request.user_id)
    result = store.get("analysis", {}).get(symbol)

    if not result:
        return Response(
            to_json({"error": "Symbol not found", "symbol": symbol}),
            mimetype="application/json",
            status=404
        )

    return Response(
        to_json({
            "symbol": symbol,
            "price": result.get("price"),
            "signal": result.get("signal"),
            "strength": result.get("strength"),
            "message": "Análisis de opciones disponible próximamente",
            "coming_soon": True
        }),
        mimetype="application/json",
    )


@app.route("/api/trades-history")
@login_required
def api_trades_history():
    try:
        trades_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "trades_imported.json")
        with open(trades_file, "r") as f:
            trades_data = json.load(f)

        trades = trades_data.get("trades", [])

        total_trades = len(trades)
        stocks_count = len(set(t.get("symbol") for t in trades if t.get("sec_type") == "STK"))
        options_count = len(set(t.get("symbol") for t in trades if t.get("sec_type") != "STK"))

        wins = sum(1 for t in trades if t.get("realized_pnl", 0) > 0)
        losses = sum(1 for t in trades if t.get("realized_pnl", 0) < 0)
        total_pnl = sum(t.get("realized_pnl", 0) for t in trades)
        total_commissions = sum(t.get("commission", 0) for t in trades)
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

        pnls = [t.get("realized_pnl", 0) for t in trades]
        avg_return_pct = (sum(pnls) / len(pnls) / 1000 * 100) if pnls else 0

        best_trade = None
        worst_trade = None
        if trades:
            best_t = max(trades, key=lambda x: x.get("realized_pnl", 0))
            worst_t = min(trades, key=lambda x: x.get("realized_pnl", 0))
            if best_t.get("realized_pnl", 0) > 0:
                best_trade = {
                    "symbol": best_t.get("symbol"),
                    "pnl": best_t.get("realized_pnl", 0),
                    "pnl_pct": (best_t.get("realized_pnl", 0) / 1000 * 100),
                }
            if worst_t.get("realized_pnl", 0) < 0:
                worst_trade = {
                    "symbol": worst_t.get("symbol"),
                    "pnl": worst_t.get("realized_pnl", 0),
                    "pnl_pct": (worst_t.get("realized_pnl", 0) / 1000 * 100),
                }

        return Response(
            to_json({
                "trades": trades,
                "summary": {
                    "total_trades": total_trades,
                    "stocks_count": stocks_count,
                    "options_count": options_count,
                    "wins": wins,
                    "losses": losses,
                    "total_pnl": round(total_pnl, 2),
                    "total_commissions": round(total_commissions, 2),
                    "win_rate": round(win_rate, 1),
                    "avg_return_pct": avg_return_pct,
                    "avg_duration_days": 30,
                    "best_trade": best_trade,
                    "worst_trade": worst_trade,
                },
            }),
            mimetype="application/json",
        )
    except Exception as e:
        return Response(
            to_json({"error": str(e), "trades": [], "summary": {}}),
            mimetype="application/json",
            status=500
        )


@app.route("/api/trades-history/chart/<trade_id>")
@login_required
def api_trades_history_chart(trade_id):
    try:
        trades_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "trades_imported.json")
        with open(trades_file, "r") as f:
            trades_data = json.load(f)

        trades = trades_data.get("trades", [])
        trade = trades[int(trade_id)] if int(trade_id) < len(trades) else None

        if not trade:
            return Response(
                to_json({"error": "Trade not found"}),
                mimetype="application/json",
                status=404
            )

        return Response(
            to_json({
                "trade": trade,
                "symbol": trade.get("symbol"),
                "date": trade.get("date"),
                "pnl": trade.get("realized_pnl"),
                "message": "Gráfico disponible próximamente"
            }),
            mimetype="application/json",
        )
    except Exception as e:
        return Response(
            to_json({"error": str(e)}),
            mimetype="application/json",
            status=400
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
    allowed = {"main.py", "indicators.py", "signals.py", "__init__.py"}
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
background:#0a0e17;color:#e0e0e0;display:flex;justify-content:center;align-items:center;min-height:100vh}}
.card{{background:#141924;border:1px solid #1e2a3a;border-radius:12px;padding:40px;width:400px;max-width:90vw}}
h1{{font-size:24px;margin-bottom:8px;color:#fff}}
.subtitle{{color:#8899aa;margin-bottom:24px;font-size:14px}}
label{{display:block;font-size:13px;color:#8899aa;margin-bottom:4px;margin-top:16px}}
input{{width:100%;padding:10px 12px;background:#0d1117;border:1px solid #2a3a4a;border-radius:6px;
color:#fff;font-size:14px;outline:none}}
input:focus{{border-color:#58a6ff}}
button{{width:100%;padding:12px;background:#238636;color:#fff;border:none;border-radius:6px;
font-size:15px;font-weight:600;cursor:pointer;margin-top:24px}}
button:hover{{background:#2ea043}}
.alt{{text-align:center;margin-top:16px}}
.alt a{{color:#58a6ff;text-decoration:none;font-size:13px}}
.error{{background:#3d1f1f;border:1px solid #f85149;color:#f85149;padding:8px 12px;border-radius:6px;
margin-top:12px;font-size:13px;display:none}}
.success{{background:#1f3d2f;border:1px solid #3fb950;color:#3fb950;padding:12px;border-radius:6px;
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


def _dashboard_page():
    from vista_web import DASHBOARD_HTML
    return DASHBOARD_HTML


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
