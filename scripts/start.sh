#!/usr/bin/env bash
# start.sh — Khởi động toàn bộ hệ thống AI Agent + Observability stack
set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
AGENT_ENV_FILE="${AGENT_ENV_FILE:-$ROOT/.env}"
AGENT_ENV_EXAMPLE="$ROOT/.env.example"
AGENT_COMPOSE_FILE="$ROOT/deploy/docker/docker-compose.yml"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
section() { echo -e "\n${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; echo -e "${YELLOW}  $*${NC}"; echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; }

# ── 1. Check prerequisites ─────────────────────────────────────────────────
section "1. Checking prerequisites"

command -v docker >/dev/null 2>&1 || error "Docker not found. Install Docker Desktop first."
docker info >/dev/null 2>&1 || error "Docker daemon is not running. Please start Docker Desktop."
info "Docker OK"

# ── Resolve Python: prefer conda env → PATH python3.11/3.10 → python3 ──
CONDA_ENV_NAME="agent-class-agentops"

# Find and source conda.sh from common WSL install locations
_activate_conda() {
    local init_scripts=(
        "$HOME/miniconda3/etc/profile.d/conda.sh"
        "$HOME/anaconda3/etc/profile.d/conda.sh"
        "$HOME/miniforge3/etc/profile.d/conda.sh"
        "/opt/conda/etc/profile.d/conda.sh"
        "/usr/local/conda/etc/profile.d/conda.sh"
    )
    for f in "${init_scripts[@]}"; do
        if [ -f "$f" ]; then
            source "$f"
            return 0
        fi
    done
    return 1
}

if [ -x "$ROOT/.venv/bin/python" ]; then
    PYTHON="$ROOT/.venv/bin/python"
    info "Using project virtual environment: $PYTHON"
elif [ -n "$CONDA_PREFIX" ]; then
    PYTHON="$CONDA_PREFIX/bin/python"
    info "Using active conda env: $CONDA_PREFIX"
else
    if _activate_conda; then
        conda activate "$CONDA_ENV_NAME" 2>/dev/null
        PYTHON="$CONDA_PREFIX/bin/python"
        info "Activated conda env: $CONDA_ENV_NAME"
    fi
fi

# Fallback to system Python if conda not resolved
if [ -z "$PYTHON" ] || [ ! -x "$PYTHON" ]; then
    PYTHON=$(command -v python3.11 2>/dev/null || \
             command -v python3.10 2>/dev/null || \
             command -v python3.9  2>/dev/null || \
             command -v python3    2>/dev/null || \
             command -v python     2>/dev/null) || error "Python not found."
fi

PY_VERSION=$("$PYTHON" -c "import sys; print(sys.version_info[:2])")
info "Python: $("$PYTHON" --version) ($PYTHON)"

# Warn if still on 3.8
if "$PYTHON" -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" 2>/dev/null; then
    : # >= 3.9, OK
else
    warn "Python < 3.9 detected! This project requires Python 3.9+."
    warn "Activate your conda env first:  conda activate $CONDA_ENV_NAME"
    warn "Then re-run:  bash scripts/start.sh"
    error "Aborted — wrong Python version."
fi

# ── 2. Check .env ──────────────────────────────────────────────────────────
section "2. Checking .env"

if [ ! -f "$AGENT_ENV_FILE" ]; then
    warn ".env not found — copying from .env.example"
    cp "$AGENT_ENV_EXAMPLE" "$AGENT_ENV_FILE"
    warn "Please fill in your API keys in .env before proceeding!"
    warn "  ANTHROPIC_API_KEY=..."
    warn "  OPENAI_API_KEY=..."
    echo ""
    read -rp "Press Enter to continue after editing .env (or Ctrl+C to abort)..."
fi

# Make sure OTLP endpoint is set for local stack
if ! grep -q "^OTEL_EXPORTER_OTLP_ENDPOINT=http" "$AGENT_ENV_FILE" 2>/dev/null; then
    echo "" >> "$AGENT_ENV_FILE"
    echo "OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318" >> "$AGENT_ENV_FILE"
    info "Added OTEL_EXPORTER_OTLP_ENDPOINT to .env"
fi

info ".env OK"

# ── 3. Install Python dependencies ────────────────────────────────────────
section "3. Installing Python dependencies"

"$PYTHON" -m pip install --quiet --upgrade pip
"$PYTHON" -m pip install --quiet -r requirements.txt
info "Python packages installed"

# ── 4. Start observability stack ──────────────────────────────────────────
section "4. Starting observability stack (Docker Compose)"

docker compose -f "$AGENT_COMPOSE_FILE" up -d
info "Containers started"

# Wait for otel-collector to be ready
echo ""
info "Waiting for OTel Collector to be ready..."
MAX_WAIT=60
ELAPSED=0
until curl -sf http://localhost:4318 >/dev/null 2>&1 || [ "$ELAPSED" -ge "$MAX_WAIT" ]; do
    sleep 2
    ELAPSED=$((ELAPSED + 2))
    printf "."
done
echo ""

if [ "$ELAPSED" -ge "$MAX_WAIT" ]; then
    warn "OTel Collector did not respond in ${MAX_WAIT}s — traces may not export yet."
    warn "Check: docker compose -f deploy/docker/docker-compose.yml logs otel-collector"
else
    info "OTel Collector ready (${ELAPSED}s)"
fi

# Wait for Grafana
info "Waiting for Grafana to be ready..."
ELAPSED=0
until curl -sf http://localhost:3000/api/health >/dev/null 2>&1 || [ "$ELAPSED" -ge "$MAX_WAIT" ]; do
    sleep 2
    ELAPSED=$((ELAPSED + 2))
    printf "."
done
echo ""

if [ "$ELAPSED" -ge "$MAX_WAIT" ]; then
    warn "Grafana did not respond in ${MAX_WAIT}s — try opening it manually."
else
    info "Grafana ready (${ELAPSED}s)"
fi

# ── 5. Print service URLs ──────────────────────────────────────────────────
section "5. Service URLs"

echo -e "  ${GREEN}Grafana Dashboard${NC}   →  http://localhost:3000"
echo -e "  ${GREEN}Prometheus${NC}          →  http://localhost:9090"
echo -e "  ${GREEN}Tempo (traces)${NC}      →  http://localhost:3200"
echo -e "  ${GREEN}OTel Collector${NC}      →  http://localhost:4318  (OTLP receiver)"
echo ""
echo -e "  ${GREEN}Agent HTTP API${NC}      →  http://localhost:9004  (starting next...)"
echo ""

# ── 6. Start the Agent server ─────────────────────────────────────────────
section "6. Starting AI Agent server (port 9004)"

info "Loading .env and starting server..."
set -a && source "$AGENT_ENV_FILE" && set +a
exec "$PYTHON" -m src.api.server
