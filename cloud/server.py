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
    for symbol, result in data.get("results", {}).items():
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
    rows = []
    for symbol in store.get("stocks", []):
        result = store.get("analysis", {}).get(symbol)
        if result:
            rows.append(result)
    return Response(
        to_json({
            "stocks": rows,
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
    return Response(
        to_json({
            "positions": store.get("portfolio_positions", []),
            "account_values": store.get("account_values", {}),
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
    return jsonify({
        "bridge_connected": store.get("connected", False),
        "stocks_count": len(store.get("stocks", [])),
        "last_update": store.get("last_update", ""),
    })


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
    return """<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>IB Trading Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0a0e17;color:#e0e0e0}
.header{background:#141924;border-bottom:1px solid #1e2a3a;padding:12px 24px;display:flex;align-items:center;justify-content:space-between}
.header h1{font-size:18px;color:#fff}
.status{display:flex;align-items:center;gap:8px;font-size:13px}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.dot.on{background:#3fb950}.dot.off{background:#f85149}
.tabs{display:flex;gap:0;background:#141924;border-bottom:1px solid #1e2a3a;padding:0 24px}
.tab{padding:10px 20px;cursor:pointer;color:#8899aa;font-size:13px;border-bottom:2px solid transparent}
.tab.active{color:#58a6ff;border-color:#58a6ff}
.tab:hover{color:#c9d1d9}
.content{padding:24px}
.setup-card{background:#141924;border:1px solid #1e2a3a;border-radius:8px;padding:24px;max-width:700px;margin:0 auto}
.setup-card h2{font-size:18px;margin-bottom:16px;color:#fff}
.step{margin-bottom:20px;padding-left:16px;border-left:2px solid #238636}
.step h3{font-size:14px;color:#58a6ff;margin-bottom:4px}
.step p{font-size:13px;color:#8899aa;line-height:1.6}
code{background:#161b22;padding:2px 6px;border-radius:4px;font-size:12px;color:#f0883e}
pre{background:#161b22;padding:12px;border-radius:6px;font-size:12px;color:#c9d1d9;overflow-x:auto;
margin:8px 0;cursor:pointer;position:relative}
pre:hover::after{content:'Copiar';position:absolute;top:4px;right:8px;font-size:11px;color:#58a6ff}
.token-box{background:#0d1117;border:1px solid #2a3a4a;border-radius:6px;padding:10px 14px;
font-family:monospace;font-size:13px;color:#f0883e;word-break:break-all;margin:8px 0;position:relative}
.btn{padding:6px 14px;background:#238636;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:12px}
.btn:hover{background:#2ea043}
.btn-danger{background:#da3633}.btn-danger:hover{background:#f85149}
.btn-sm{padding:4px 10px;font-size:11px}

/* Scanner table */
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:8px 12px;background:#141924;color:#8899aa;border-bottom:1px solid #1e2a3a;
font-weight:500;position:sticky;top:0}
td{padding:8px 12px;border-bottom:1px solid #1e2a3a}
tr:hover{background:#161b22}
.buy{color:#3fb950;font-weight:600}.sell{color:#f85149;font-weight:600}
.score{font-weight:600}
.empty{text-align:center;padding:60px;color:#484f58}
.user-menu{display:flex;align-items:center;gap:12px}
.user-email{color:#8899aa;font-size:12px}
</style></head><body>
<div class="header">
  <h1>IB Trading Dashboard</h1>
  <div class="user-menu">
    <div class="status"><span class="dot off" id="dot"></span><span id="status-text">Desconectado</span></div>
    <span class="user-email" id="user-email"></span>
    <a href="/logout" class="btn btn-sm" style="background:#2a3a4a">Salir</a>
  </div>
</div>
<div class="tabs">
  <div class="tab active" data-tab="scanner">Escáner</div>
  <div class="tab" data-tab="portfolio">Mi Cartera</div>
  <div class="tab" data-tab="setup">Conectar TWS</div>
</div>
<div class="content" id="content"></div>

<script>
let currentTab='scanner', bridgeConnected=false, stocksData=[], bridgeToken='';

// Tab switching
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  t.classList.add('active');
  currentTab=t.dataset.tab;
  render();
});

async function fetchStatus(){
  try{
    const r=await fetch('/api/status');
    if(r.status===401){window.location='/login';return}
    const d=await r.json();
    bridgeConnected=d.bridge_connected;
    document.getElementById('dot').className='dot '+(bridgeConnected?'on':'off');
    document.getElementById('status-text').textContent=bridgeConnected
      ?`Conectado — ${d.stocks_count} acciones (${d.last_update})`:'TWS Desconectado';
  }catch(e){}
}

async function fetchData(){
  try{
    const r=await fetch('/api/data');
    if(!r.ok)return;
    const d=await r.json();
    stocksData=d.stocks||[];
    if(currentTab==='scanner')renderScanner();
  }catch(e){}
}

async function fetchBridgeToken(){
  try{
    const r=await fetch('/api/bridge-token');
    const d=await r.json();
    bridgeToken=d.bridge_token;
  }catch(e){}
}

function render(){
  const c=document.getElementById('content');
  if(currentTab==='scanner')renderScanner();
  else if(currentTab==='portfolio')renderPortfolio();
  else if(currentTab==='setup')renderSetup();
}

function renderScanner(){
  const c=document.getElementById('content');
  if(!bridgeConnected){
    c.innerHTML='<div class="empty"><p style="font-size:16px;margin-bottom:8px">TWS no conectado</p><p style="color:#484f58">Conecta tu TWS usando la pestaña "Conectar TWS"</p></div>';
    return;
  }
  if(!stocksData.length){
    c.innerHTML='<div class="empty"><p>Escaneando mercado...</p></div>';
    return;
  }
  let html='<table><thead><tr><th>Símbolo</th><th>Precio</th><th>Señal</th><th>Score</th><th>MACD</th><th>RSI</th><th>Koncorde</th></tr></thead><tbody>';
  stocksData.sort((a,b)=>(b.score||0)-(a.score||0));
  for(const s of stocksData){
    const sig=s.signal||'—';
    const cls=sig==='BUY'?'buy':sig==='SELL'?'sell':'';
    html+=`<tr>
      <td><strong>${s.symbol||''}</strong></td>
      <td>$${(s.price||0).toFixed(2)}</td>
      <td class="${cls}">${sig}</td>
      <td class="score">${(s.score||0).toFixed(1)}</td>
      <td>${s.macd_status||'—'}</td>
      <td>${(s.rsi||0).toFixed(1)}</td>
      <td>${s.koncorde_status||'—'}</td>
    </tr>`;
  }
  html+='</tbody></table>';
  c.innerHTML=html;
}

function renderPortfolio(){
  const c=document.getElementById('content');
  if(!bridgeConnected){
    c.innerHTML='<div class="empty"><p>Conecta TWS para ver tu cartera</p></div>';
    return;
  }
  c.innerHTML='<div class="empty"><p>Cargando cartera...</p></div>';
  fetch('/api/portfolio').then(r=>r.json()).then(d=>{
    if(!d.positions||!d.positions.length){
      c.innerHTML='<div class="empty"><p>Sin posiciones abiertas</p></div>';
      return;
    }
    let html='<table><thead><tr><th>Símbolo</th><th>Cantidad</th><th>Precio Mkt</th><th>Valor</th><th>P&L</th></tr></thead><tbody>';
    for(const p of d.positions){
      const pnl=p.unrealizedPNL||0;
      const cls=pnl>=0?'buy':'sell';
      html+=`<tr><td><strong>${p.symbol||''}</strong></td><td>${p.position||0}</td>
        <td>$${(p.marketPrice||0).toFixed(2)}</td><td>$${(p.marketValue||0).toFixed(2)}</td>
        <td class="${cls}">$${pnl.toFixed(2)}</td></tr>`;
    }
    html+='</tbody></table>';
    c.innerHTML=html;
  });
}

function renderSetup(){
  const c=document.getElementById('content');
  const serverUrl=window.location.origin;
  const installCmd=`curl -sL ${serverUrl}/install.sh | bash`;
  const runCmd=`~/.ib-bridge/run-bridge.sh ${serverUrl} ${bridgeToken||'TOKEN'}`;

  c.innerHTML=`<div class="setup-card" style="text-align:center;max-width:620px">
    <h2 style="font-size:22px;margin-bottom:8px">Conectar tu TWS</h2>
    <p style="color:#8899aa;margin-bottom:28px;font-size:14px">
      Solo necesitas TWS abierta y seguir estos 3 pasos.
    </p>

    <div style="background:#141924;border:1px solid #1e2a3a;border-radius:8px;padding:20px;text-align:left;margin-bottom:20px">

      <div style="display:flex;align-items:flex-start;gap:12px;margin-bottom:20px">
        <span style="background:#238636;color:#fff;width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:700;flex-shrink:0">1</span>
        <div style="flex:1">
          <p style="font-size:15px;font-weight:600;color:#fff;margin-bottom:4px">Abri TWS</p>
          <p style="font-size:13px;color:#8899aa;line-height:1.5">Abre Trader Workstation y habilita la API:<br>
          <code>Edit → Global Configuration → API → Settings</code><br>
          ✓ Enable ActiveX and Socket Clients &nbsp; ✓ Puerto: <code>7497</code></p>
        </div>
      </div>

      <div style="display:flex;align-items:flex-start;gap:12px;margin-bottom:20px">
        <span style="background:#238636;color:#fff;width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:700;flex-shrink:0">2</span>
        <div style="flex:1">
          <p style="font-size:15px;font-weight:600;color:#fff;margin-bottom:4px">Instalar el Bridge <span style="font-size:11px;color:#8899aa;font-weight:400">(solo la primera vez)</span></p>
          <p style="font-size:13px;color:#8899aa;margin-bottom:8px">Abri la Terminal y pega este comando:</p>
          <div style="position:relative">
            <pre id="install-cmd" style="padding-right:70px;font-size:12px;margin:0">${installCmd}</pre>
            <button onclick="copyCmd('install-cmd','install-btn')" id="install-btn"
              style="position:absolute;top:6px;right:6px;padding:4px 12px;background:#238636;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;font-weight:600">
              Copiar
            </button>
          </div>
          <p style="font-size:11px;color:#8899aa;margin-top:6px">Requiere Python 3.10+ &nbsp;|&nbsp; Se instala en <code>~/.ib-bridge/</code></p>
        </div>
      </div>

      <div style="display:flex;align-items:flex-start;gap:12px">
        <span style="background:#238636;color:#fff;width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:700;flex-shrink:0">3</span>
        <div style="flex:1">
          <p style="font-size:15px;font-weight:600;color:#fff;margin-bottom:4px">Conectar</p>
          <p style="font-size:13px;color:#8899aa;margin-bottom:8px">Cada vez que quieras conectar, pega esto en la Terminal:</p>
          <div style="position:relative">
            <pre id="run-cmd" style="padding-right:70px;font-size:12px;margin:0">${runCmd}</pre>
            <button onclick="copyCmd('run-cmd','run-btn')" id="run-btn"
              style="position:absolute;top:6px;right:6px;padding:4px 12px;background:#238636;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;font-weight:600">
              Copiar
            </button>
          </div>
          <p style="font-size:11px;color:#8899aa;margin-top:6px">El indicador arriba cambiara a <span style="color:#3fb950">● Conectado</span></p>
        </div>
      </div>

    </div>

    <div style="background:#0d1117;border:1px solid #1e2a3a;border-radius:8px;padding:16px;text-align:left;margin-bottom:20px">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div>
          <p style="font-size:12px;color:#8899aa">Estado de conexion</p>
          <p id="bridge-live-status" style="font-size:14px;margin-top:4px">Verificando...</p>
        </div>
        <div id="status-dot" style="width:12px;height:12px;border-radius:50%;background:#484f58"></div>
      </div>
    </div>

    <details style="text-align:left">
      <summary style="color:#58a6ff;cursor:pointer;font-size:13px">Opciones avanzadas</summary>
      <div style="margin-top:12px;padding:12px;background:#0d1117;border-radius:6px">
        <p style="font-size:12px;color:#8899aa;margin-bottom:4px">Tu bridge token (no lo compartas):</p>
        <div class="token-box" id="token-display" style="font-size:11px">${bridgeToken||'Cargando...'}</div>
        <button class="btn btn-sm" onclick="regenerateToken()" style="margin-top:8px;font-size:11px">Regenerar Token</button>
        <p style="font-size:11px;color:#8899aa;margin-top:12px">Puerto 7497 = paper trading &nbsp;|&nbsp; Agrega <code>--ib-port 7496</code> para live</p>
      </div>
    </details>
  </div>`;

  fetch('/api/status').then(r=>r.json()).then(d=>{
    const el=document.getElementById('bridge-live-status');
    const dot=document.getElementById('status-dot');
    if(d.bridge_connected){
      el.innerHTML='<span style="color:#3fb950;font-weight:600">Conectado</span> — recibiendo datos de TWS';
      dot.style.background='#3fb950';
    } else {
      el.innerHTML='<span style="color:#484f58">Desconectado</span> — segui los pasos de arriba para conectar';
      dot.style.background='#484f58';
    }
  }).catch(()=>{});
}

function copyCmd(preId,btnId){
  const text=document.getElementById(preId).textContent;
  navigator.clipboard.writeText(text);
  const btn=document.getElementById(btnId);
  btn.textContent='Copiado!';
  btn.style.background='#3fb950';
  setTimeout(()=>{btn.textContent='Copiar';btn.style.background='#238636'},2000);
}

async function regenerateToken(){
  if(!confirm('¿Regenerar token? El bridge actual se desconectará.'))return;
  const r=await fetch('/api/bridge-token/regenerate',{method:'POST'});
  const d=await r.json();
  bridgeToken=d.bridge_token;
  renderSetup();
}

// Init
fetchBridgeToken();
fetchStatus();
fetchData();
render();
setInterval(fetchStatus,5000);
setInterval(fetchData,15000);
</script></body></html>"""


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
