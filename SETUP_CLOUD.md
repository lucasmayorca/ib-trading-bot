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

Abre una terminal y ejecuta:

```bash
pip install ib-trading-bridge
```

> Si usas un entorno virtual:
> ```bash
> python -m venv bridge-env
> source bridge-env/bin/activate    # Mac/Linux
> bridge-env\Scripts\activate       # Windows
> pip install ib-trading-bridge
> ```

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
