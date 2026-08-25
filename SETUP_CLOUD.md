# Deploying IB Trading Dashboard to Railway

Guía paso a paso para deployar el dashboard en la nube y conectar tu TWS.

---

## Parte 1 — Deploy en Railway (una sola vez)

### 1. Crear cuenta en Railway

1. Ve a [railway.app](https://railway.app) y crea una cuenta (puedes usar GitHub).

### 2. Crear un nuevo proyecto

1. En el dashboard de Railway, click **"New Project"**
2. Selecciona **"Deploy from GitHub repo"**
3. Conecta tu repo de GitHub donde está este código
4. Railway detectará el `Dockerfile` automáticamente

### 3. Agregar PostgreSQL

1. En tu proyecto de Railway, click **"+ New"** → **"Database"** → **"PostgreSQL"**
2. Railway creará la base de datos automáticamente
3. La variable `DATABASE_URL` se conecta sola

### 4. Configurar variables de entorno

En tu servicio de Railway, ve a **"Variables"** y agrega:

| Variable | Valor |
|----------|-------|
| `JWT_SECRET` | Un string largo y aleatorio (ej: `openssl rand -hex 32` en tu terminal) |
| `DATABASE_URL` | *(se agrega automáticamente al conectar PostgreSQL)* |

### 5. Deploy

Railway hace deploy automáticamente al detectar cambios. Espera ~2 minutos.

Tu dashboard estará disponible en una URL tipo:
```
https://tu-proyecto.up.railway.app
```

---

## Parte 2 — Registrarse en el Dashboard

1. Abre la URL de tu dashboard en el navegador
2. Click en **"Regístrate"**
3. Ingresa tu email y una contraseña (mínimo 8 caracteres)
4. Serás redirigido al dashboard

---

## Parte 3 — Conectar TWS (cada usuario)

### Paso 1 — Abrir TWS

1. Abre **Trader Workstation (TWS)** de Interactive Brokers
2. Ve a **Edit → Global Configuration → API → Settings**
3. Verifica que esté habilitado:
   - ✅ **Enable ActiveX and Socket Clients**
   - ✅ **Socket port**: `7497` (paper trading) o `7496` (live)
   - ✅ Desmarca "Read-Only API"

### Paso 2 — Instalar el Bridge

El instalador crea su propio entorno en `~/.ib-bridge` y deja el comando
`ib-bridge` listo. Una sola línea:

```bash
curl -sL https://raw.githubusercontent.com/lucasmayorca/ib-trading-bot/main/install-bridge.sh | bash
```

> El dashboard, en la pestaña **"Conectar TWS"**, te muestra este mismo comando
> ya con tu URL y tu token prellenados — es más cómodo copiarlo de ahí.

> El repo debe ser público para que `curl` y el `pip install git+…` funcionen sin
> credenciales.

### Paso 3 — Obtener tu Token

1. En el dashboard web, ve a la pestaña **"Conectar TWS"**
2. Copia tu **Bridge Token** (es único por usuario)

### Paso 4 — Ejecutar el Bridge

Pega este comando en tu terminal (reemplaza los valores):

```bash
ib-bridge --server https://tu-proyecto.up.railway.app --token TU_TOKEN_AQUI
```

**Para live trading** (puerto 7496):
```bash
ib-bridge --server https://tu-proyecto.up.railway.app --token TU_TOKEN_AQUI --ib-port 7496
```

También queda un launcher: `~/.ib-bridge/run-bridge.sh URL TOKEN [PUERTO]`.

### Paso 5 — Verificar conexión

Deberías ver algo así en la terminal:

```
╔══════════════════════════════════════════╗
║       IB Trading Bridge v1.0.0          ║
║  Conecta tu TWS al dashboard cloud      ║
╚══════════════════════════════════════════╝

[14:30:22] Conectando al servidor: https://tu-proyecto.up.railway.app
[14:30:23] Autenticado con el servidor cloud
[14:30:23] Conectando a TWS en 127.0.0.1:7497...
[14:30:24] Conectado a TWS ✓
[14:30:24] Bridge activo — escaneando mercado cada 5 minutos
```

En el dashboard web, el indicador cambiará a **🟢 Conectado**.

---

## Parte 4 — Actualizar el Bridge

`run-bridge.sh` **solo lanza** el bridge ya instalado: no baja código nuevo. Si el
bridge cambió (lo dicen las notas del release o te lo indicamos), reinstalá:

```bash
rm -rf ~/.ib-bridge
curl -sL https://raw.githubusercontent.com/lucasmayorca/ib-trading-bot/main/install-bridge.sh | bash
```

Las mejoras que corren **server-side** — figuras técnicas, Fibonacci, velas
japonesas, enriquecimiento (beta/RS/RVOL, analistas, insiders), órdenes pendientes
— llegan solas con el deploy de Railway y **no** requieren reinstalar nada.

---

## Parte 5 — Historial completo de trades (opcional)

`reqExecutions` de IB solo devuelve los fills de la **sesión actual** de TWS: no
existe llamada que traiga meses de historia. Para ver tus trades cerrados
históricos hace falta tu propio **IB Flex Web Service** (una sola vez):

1. En IBKR: **Account Management → Performance & Reports → Flex Queries**
2. Creá una query de la sección **"Trades"**, formato **XML**
3. Generá un **Flex token**
4. Pegá el token y el Query ID en el dashboard: **Conectar TWS → "Ver historial
   completo de trades"**

Si la pestaña *Trades Históricos* aparece vacía, casi siempre es esto: falta
conectar Flex (vacío ≠ "no tenés historia").

---

## Troubleshooting

### "No se pudo conectar a TWS"
- ¿TWS está abierta? Debe estar ejecutándose
- ¿El puerto es correcto? Paper = 7497, Live = 7496
- ¿La API está habilitada? Ve a Edit → Global Configuration → API → Settings

### "No se pudo conectar al servidor"
- Verifica la URL del servidor (debe incluir `https://`)
- ¿El deploy en Railway está funcionando? Revisa los logs en Railway

### "Auth failed: Invalid bridge token"
- Ve al dashboard → "Conectar TWS" y copia el token de nuevo
- Si regeneraste el token, el anterior ya no funciona

### "No veo las funciones nuevas en el dashboard"
- El navegador cachea el JavaScript del dashboard: recargá con **Cmd/Ctrl + Shift + R**
- Verificá qué commit está desplegado: `curl https://tu-proyecto.up.railway.app/health`
- Si el cambio era del bridge, reinstalalo (ver Parte 4)

### "Mi Cartera muestra $0 / el escáner muestra Total 0"
- Suele ser que el bridge no está corriendo o perdió la conexión: revisá su terminal
- Tras un redeploy el servidor pierde su memoria y la repuebla desde el bridge o
  desde su snapshot en PostgreSQL — puede tardar un ciclo (~5 min)

### El bridge se desconecta
- El bridge se reconecta automáticamente si pierde la conexión al servidor
- Si TWS se cierra, el bridge se detendrá — vuelve a abrir TWS y ejecuta el bridge de nuevo

---

## Arquitectura

```
Tu PC                              Railway (Cloud)
┌────────────────┐                ┌─────────────────┐
│ TWS / IB       │                │ Flask + SocketIO │
│ Gateway        │◄──TCP──►       │                 │
│ (port 7497)    │        │       │ PostgreSQL      │
└────────────────┘        │       └────────┬────────┘
       ▲                  │                │
       │ localhost        │                │ HTTPS
       │                  │                │
┌──────┴─────────┐        │       ┌────────┴────────┐
│ IB Bridge      │──WebSocket────►│ Dashboard Web   │
│ (Python CLI)   │                │ (Navegador)     │
└────────────────┘                └─────────────────┘
```

- **IB Bridge** corre en tu máquina, se conecta a TWS por TCP local
- Envía datos al servidor Railway por WebSocket (encriptado HTTPS)
- El dashboard web muestra los datos en tiempo real
- Cada usuario tiene su propio bridge y token — los datos son aislados
