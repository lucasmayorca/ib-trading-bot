"""
Cloud server — Flask + SocketIO.

Serves the multi-tenant dashboard and receives real-time data from
IB Bridge clients running on each user's machine.

Run locally:  python -m cloud.server
Deploy:       gunicorn --worker-class eventlet -w 1 cloud.server:app
"""

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
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

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

@socketio.on("connect")
def handle_connect():
    try:
        sid = getattr(request, 'sid', 'unknown')
        print(f"[WS] New connection: {sid}", flush=True)
    except Exception as e:
        print(f"[WS] New connection (sid error: {e})", flush=True)

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
    return jsonify({"status": "ok"})


@app.route("/api/status")
@login_required
def api_status():
    store = get_user_store(request.user_id)
    return jsonify({
        "bridge_connected": store.get("connected", False),
        "stocks_count": len(store.get("stocks", [])),
        "last_update": store.get("last_update", ""),
    })


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
  c.innerHTML=`<div class="setup-card">
    <h2>Conectar tu TWS</h2>
    <p style="color:#8899aa;margin-bottom:20px;font-size:13px">
      Sigue estos pasos para conectar tu Trader Workstation al dashboard.
    </p>

    <div class="step"><h3>Paso 1 — Abrir TWS</h3>
      <p>Abre Trader Workstation (TWS) o IB Gateway y habilita la API:<br>
      <code>Edit → Global Configuration → API → Settings</code><br>
      ✓ Enable ActiveX and Socket Clients<br>
      ✓ Socket port: <code>7497</code> (paper) o <code>7496</code> (live)</p>
    </div>

    <div class="step"><h3>Paso 2 — Instalar el Bridge</h3>
      <p>En tu terminal:</p>
      <pre onclick="navigator.clipboard.writeText('pip install ib-trading-bridge')">pip install ib-trading-bridge</pre>
    </div>

    <div class="step"><h3>Paso 3 — Tu Token</h3>
      <p>Este es tu token personal (no lo compartas):</p>
      <div class="token-box" id="token-display">${bridgeToken||'Cargando...'}</div>
      <button class="btn btn-sm" onclick="regenerateToken()">Regenerar Token</button>
    </div>

    <div class="step"><h3>Paso 4 — Ejecutar</h3>
      <p>Copia y pega este comando:</p>
      <pre id="bridge-cmd" onclick="navigator.clipboard.writeText(this.textContent)">ib-bridge --server ${serverUrl} --token ${bridgeToken||'TU_TOKEN'}</pre>
      <p style="margin-top:8px">O si TWS está en un puerto diferente:</p>
      <pre onclick="navigator.clipboard.writeText(this.textContent)">ib-bridge --server ${serverUrl} --token ${bridgeToken||'TU_TOKEN'} --ib-port 7496</pre>
    </div>

    <div class="step"><h3>Paso 5 — Listo</h3>
      <p>El indicador arriba cambiará a <span style="color:#3fb950">● Conectado</span> cuando el bridge se conecte a TWS.</p>
    </div>
  </div>`;
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
    print(f"[SERVER] Starting on port {port} (async_mode=threading)")
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)
