#!/bin/bash
# ══════════════════════════════════════════════════════════════
#  IB Trading Bridge — One-line installer
#
#  Usage:
#    curl -sL https://raw.githubusercontent.com/lucasmayorca/ib-trading-bot/main/install-bridge.sh | bash
#
#  Or download and run:
#    chmod +x install-bridge.sh && ./install-bridge.sh
# ══════════════════════════════════════════════════════════════

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

echo ""
echo "+==========================================+"
echo "|    IB Trading Bridge — Installer          |"
echo "+==========================================+"
echo ""

# --- Check Python ---
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        version=$("$cmd" --version 2>&1 | sed 's/[^0-9.]//g' | cut -d. -f1-2)
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo -e "${RED}Python 3.10+ no encontrado.${NC}"
    echo ""
    echo "Instala Python desde: https://www.python.org/downloads/"
    echo ""
    exit 1
fi

echo -e "${GREEN}Python encontrado:${NC} $($PYTHON --version)"

# --- Create virtual environment ---
INSTALL_DIR="$HOME/.ib-bridge"
echo -e "${CYAN}Instalando en:${NC} $INSTALL_DIR"

if [ -d "$INSTALL_DIR" ]; then
    echo -e "${YELLOW}Directorio existente encontrado, actualizando...${NC}"
fi

mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

if [ ! -d "venv" ]; then
    echo "Creando entorno virtual..."
    $PYTHON -m venv venv
fi

# --- Activate and install ---
source venv/bin/activate

echo "Instalando dependencias..."
pip install --upgrade pip -q 2>/dev/null
pip install "git+https://github.com/lucasmayorca/ib-trading-bot.git" --upgrade -q 2>&1 | grep -v "already satisfied" || true

echo ""
echo -e "${GREEN}+==========================================+${NC}"
echo -e "${GREEN}|    Instalacion completa!                  |${NC}"
echo -e "${GREEN}+==========================================+${NC}"
echo ""
echo -e "Para ejecutar el bridge:"
echo ""
echo -e "  ${CYAN}source $INSTALL_DIR/venv/bin/activate${NC}"
echo -e "  ${CYAN}ib-bridge --server URL --token TOKEN${NC}"
echo ""
echo -e "O en una sola linea:"
echo ""
echo -e "  ${CYAN}$INSTALL_DIR/venv/bin/ib-bridge --server URL --token TOKEN${NC}"
echo ""

# --- Create launcher script ---
cat > "$INSTALL_DIR/run-bridge.sh" << 'LAUNCHER'
#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/venv/bin/activate"

if [ -z "$1" ] || [ -z "$2" ]; then
    echo "Uso: ./run-bridge.sh SERVER_URL BRIDGE_TOKEN [IB_PORT]"
    echo ""
    echo "Ejemplo:"
    echo "  ./run-bridge.sh https://my-app.railway.app abc123"
    echo "  ./run-bridge.sh https://my-app.railway.app abc123 7496"
    exit 1
fi

SERVER="$1"
TOKEN="$2"
PORT="${3:-7497}"

exec ib-bridge --server "$SERVER" --token "$TOKEN" --ib-port "$PORT"
LAUNCHER
chmod +x "$INSTALL_DIR/run-bridge.sh"

echo -e "Tambien podes usar el launcher:"
echo -e "  ${CYAN}$INSTALL_DIR/run-bridge.sh URL TOKEN${NC}"
echo ""
echo -e "${YELLOW}Requisitos:${NC}"
echo -e "  - TWS o IB Gateway abierto con API habilitada"
echo -e "  - Puerto 7497 (paper) o 7496 (live)"
echo ""
