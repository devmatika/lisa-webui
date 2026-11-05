#!/usr/bin/env bash
# ==============================================================================
# Lisa WebUI - Unified Setup & Run Script (Local Dev & Production Server)
# ==============================================================================
# Usage:
#   ./setup_and_run.sh             # Auto-detect local vs server and start
#   ./setup_and_run.sh --local     # Force local mode (bind 127.0.0.1:8787)
#   ./setup_and_run.sh --server    # Force server mode (bind 0.0.0.0:8787)
#   ./setup_and_run.sh --port 8788 # Custom port
#   ./setup_and_run.sh --rebuild   # Force Docker rebuild (--no-cache)
#   ./setup_and_run.sh --stop      # Stop running container
#   ./setup_and_run.sh --restart   # Restart running container
#   ./setup_and_run.sh --logs      # Follow container logs
#   ./setup_and_run.sh --status    # Check container and health status
# ==============================================================================

set -euo pipefail

# Text colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# Determine script & repository directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PARENT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Default configurations
MODE="auto"
CUSTOM_PORT=""
REBUILD=0
CUSTOMER=""
ACTION="up"

# Parse CLI options
while [[ $# -gt 0 ]]; do
  case "$1" in
    --customer)
      CUSTOMER="$2"
      shift 2
      ;;
    --local)
      MODE="local"
      shift
      ;;
    --server|--public)
      MODE="server"
      shift
      ;;
    --port)
      CUSTOM_PORT="$2"
      shift 2
      ;;
    --rebuild|--build)
      REBUILD=1
      shift
      ;;
    --stop|--down)
      ACTION="down"
      shift
      ;;
    --restart)
      ACTION="restart"
      shift
      ;;
    --logs)
      ACTION="logs"
      shift
      ;;
    --status)
      ACTION="status"
      shift
      ;;
    -h|--help)
      echo -e "${BOLD}Lisa WebUI - Unified Setup & Run Script${NC}"
      echo "Usage: $0 [OPTIONS]"
      echo ""
      echo "Options:"
      echo "  --customer <ID> Create isolated data dir (~/.hermes-<customer>) per runbook"
      echo "  --local        Run in local mode (binds to 127.0.0.1)"
      echo "  --server       Run in server mode (binds to 0.0.0.0 for external access)"
      echo "  --port <PORT>  Set custom host port (default: 8787)"
      echo "  --rebuild      Force Docker rebuild before starting"
      echo "  --stop         Stop the WebUI container"
      echo "  --restart      Restart the WebUI container"
      echo "  --logs         View container logs"
      echo "  --status       Show current container and health status"
      echo "  -h, --help     Show this help message"
      exit 0
      ;;
    *)
      echo -e "${RED}Unknown option: $1${NC}"
      echo "Run '$0 --help' for usage."
      exit 1
      ;;
  esac
done

# Docker compose command detector
if docker compose version >/dev/null 2>&1; then
  DOCKER_COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DOCKER_COMPOSE="docker-compose"
else
  echo -e "${RED}Error: Neither 'docker compose' nor 'docker-compose' was found.${NC}"
  echo "Please install Docker and the Docker Compose plugin first:"
  echo "  curl -fsSL https://get.docker.com | sh"
  exit 1
fi

# Quick actions
if [[ "$ACTION" == "down" ]]; then
  echo -e "${YELLOW}Stopping Lisa WebUI container...${NC}"
  $DOCKER_COMPOSE down
  echo -e "${GREEN}Container stopped.${NC}"
  exit 0
fi

if [[ "$ACTION" == "logs" ]]; then
  $DOCKER_COMPOSE logs -f hermes-webui
  exit 0
fi

if [[ "$ACTION" == "status" ]]; then
  echo -e "${BOLD}Container Status:${NC}"
  $DOCKER_COMPOSE ps
  echo ""
  echo -e "${BOLD}Health Check:${NC}"
  PORT="${CUSTOM_PORT:-8787}"
  if [ -f .env ]; then
    PORT_IN_ENV=$(grep -E '^HERMES_WEBUI_PORT=' .env | cut -d= -f2 || echo "")
    if [ -n "$PORT_IN_ENV" ]; then PORT="$PORT_IN_ENV"; fi
  fi
  if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    echo -e "${GREEN}WebUI is HEALTHY and responding on port ${PORT}.${NC}"
  else
    echo -e "${RED}WebUI is NOT responding on http://127.0.0.1:${PORT}/health.${NC}"
  fi
  exit 0
fi

echo -e "${CYAN}======================================================${NC}"
echo -e "${BOLD}${CYAN}      Lisa WebUI & Customer Wizard - Startup          ${NC}"
echo -e "${CYAN}======================================================${NC}"

# Detect execution context (Root/Server vs Non-root/Local)
CURRENT_UID="$(id -u)"
CURRENT_GID="$(id -g)"
CURRENT_USER="$(whoami 2>/dev/null || echo "user")"

if [[ "$MODE" == "auto" ]]; then
  if [[ "$CURRENT_UID" -eq 0 ]] || [[ "$HOME" == "/root" ]]; then
    MODE="server"
    echo -e "${BLUE}[*] Auto-detected environment: ${BOLD}SERVER / ROOT${NC}"
  else
    MODE="local"
    echo -e "${BLUE}[*] Auto-detected environment: ${BOLD}LOCAL DEV (${CURRENT_USER})${NC}"
  fi
else
  echo -e "${BLUE}[*] Environment explicitly set to: ${BOLD}${MODE^^}${NC}"
fi

# Determine Bind Address
if [[ "$MODE" == "server" ]]; then
  BIND_ADDR="0.0.0.0"
else
  BIND_ADDR="127.0.0.1"
fi

# Determine Port
PORT="${CUSTOM_PORT:-8787}"

# Resolve hermes-agent location
HERMES_AGENT_PATH=""
if [[ -d "${PARENT_DIR}/hermes-agent" ]]; then
  HERMES_AGENT_PATH="${PARENT_DIR}/hermes-agent"
elif [[ -d "/root/hermes-agent" ]]; then
  HERMES_AGENT_PATH="/root/hermes-agent"
elif [[ -d "${HOME}/.hermes/hermes-agent" ]]; then
  HERMES_AGENT_PATH="${HOME}/.hermes/hermes-agent"
elif [[ -d "/opt/hermes/hermes-agent" ]]; then
  HERMES_AGENT_PATH="/opt/hermes/hermes-agent"
fi

if [[ -z "$HERMES_AGENT_PATH" ]]; then
  echo -e "${YELLOW}[!] Warning: 'hermes-agent' directory not found in sibling or standard paths.${NC}"
  echo "    Creating placeholder at ${HOME}/.hermes/hermes-agent..."
  mkdir -p "${HOME}/.hermes/hermes-agent"
  HERMES_AGENT_PATH="${HOME}/.hermes/hermes-agent"
else
  echo -e "${GREEN}[+] Found hermes-agent at:${NC} ${HERMES_AGENT_PATH}"
fi

# Resolve HERMES_HOME (Section 1.1 Customer Isolation from Deployment Runbook)
if [[ -n "$CUSTOMER" ]]; then
  HERMES_HOME_DIR="${HOME}/.hermes-${CUSTOMER}"
elif [ -f .env ] && grep -q "^HERMES_HOME=" .env; then
  HERMES_HOME_DIR=$(grep -E '^HERMES_HOME=' .env | cut -d= -f2)
else
  HERMES_HOME_DIR="${HOME}/.hermes"
fi
mkdir -p "${HERMES_HOME_DIR}/webui" "${HERMES_HOME_DIR}/plugins" "${HERMES_HOME_DIR}/cron"
echo -e "${GREEN}[+] Using HERMES_HOME at:${NC} ${HERMES_HOME_DIR}"

# Resolve HERMES_WORKSPACE
WORKSPACE_PATH=""
if [[ -d "${PARENT_DIR}/2nd_brain_matika" ]]; then
  WORKSPACE_PATH="${PARENT_DIR}/2nd_brain_matika"
elif [[ -d "${PARENT_DIR}/workspace" ]]; then
  WORKSPACE_PATH="${PARENT_DIR}/workspace"
else
  WORKSPACE_PATH="${HOME}/workspace"
  mkdir -p "$WORKSPACE_PATH"
fi
echo -e "${GREEN}[+] Using HERMES_WORKSPACE at:${NC} ${WORKSPACE_PATH}"

# Check or Create .env
if [[ ! -f .env ]]; then
  echo -e "${YELLOW}[*] Generating initial .env configuration...${NC}"
  cat <<EOF > .env
# Auto-generated configuration by setup_and_run.sh
UID=${CURRENT_UID}
GID=${CURRENT_GID}
HERMES_HOME=${HERMES_HOME_DIR}
HERMES_AGENT_DIR=${HERMES_AGENT_PATH}
HERMES_WORKSPACE=${WORKSPACE_PATH}
HERMES_WEBUI_BIND_ADDR=${BIND_ADDR}
HERMES_WEBUI_PORT=${PORT}

# White-label Branding (Lisa Assistant)
NEXT_PUBLIC_APP_NAME=Lisa AI Assistant
NEXT_PUBLIC_COMPANY_NAME=Matika
NEXT_PUBLIC_LOGO=static/matika-logo.png
NEXT_PUBLIC_FAVICON=static/favicon-matika.png
NEXT_PUBLIC_PRIMARY_COLOR=#006eb3
NEXT_PUBLIC_PRIMARY_COLOR_DARK=#00908d
NEXT_PUBLIC_TAGLINE=Your Guided AI Assistant
MATIKA_VOICE_UPSTREAM=https://matika-voice.fusiontrade.at/voice
EOF
  echo -e "${GREEN}[+] Created .env file.${NC}"
else
  echo -e "${BLUE}[*] Existing .env file found. Preserving current secrets and settings.${NC}"
  
  # Ensure UID/GID are present and correct
  if ! grep -q "^UID=" .env; then echo "UID=${CURRENT_UID}" >> .env; fi
  if ! grep -q "^GID=" .env; then echo "GID=${CURRENT_GID}" >> .env; fi
  if ! grep -q "^HERMES_WEBUI_BIND_ADDR=" .env; then
    echo "HERMES_WEBUI_BIND_ADDR=${BIND_ADDR}" >> .env
  else
    # Update bind address if user explicitly requested local or server mode
    if [[ "$MODE" == "server" ]]; then
      sed -i 's/^HERMES_WEBUI_BIND_ADDR=.*/HERMES_WEBUI_BIND_ADDR=0.0.0.0/' .env
      BIND_ADDR="0.0.0.0"
    elif [[ "$MODE" == "local" ]]; then
      sed -i 's/^HERMES_WEBUI_BIND_ADDR=.*/HERMES_WEBUI_BIND_ADDR=127.0.0.1/' .env
      BIND_ADDR="127.0.0.1"
    fi
  fi
  if [ -n "$CUSTOM_PORT" ]; then
    if grep -q "^HERMES_WEBUI_PORT=" .env; then
      sed -i "s/^HERMES_WEBUI_PORT=.*/HERMES_WEBUI_PORT=${CUSTOM_PORT}/" .env
    else
      echo "HERMES_WEBUI_PORT=${CUSTOM_PORT}" >> .env
    fi
    PORT="$CUSTOM_PORT"
  elif grep -q "^HERMES_WEBUI_PORT=" .env; then
    PORT=$(grep -E '^HERMES_WEBUI_PORT=' .env | cut -d= -f2)
  fi
fi

# Detect Server External IP if in server mode
SERVER_IP="localhost"
if [[ "$MODE" == "server" ]]; then
  SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "your-server-ip")
fi

echo -e "${CYAN}------------------------------------------------------${NC}"
echo -e "${BOLD}Configuration Summary:${NC}"
echo "  Mode:              ${MODE^^}"
echo "  Bind Address:      ${BIND_ADDR}"
echo "  Port:              ${PORT}"
echo "  Host UID / GID:    ${CURRENT_UID} / ${CURRENT_GID}"
echo "  Hermes Home:       ${HERMES_HOME_DIR}"
echo "  Hermes Agent Dir:  ${HERMES_AGENT_PATH}"
echo "  Workspace:         ${WORKSPACE_PATH}"
echo -e "${CYAN}------------------------------------------------------${NC}"

# Check if port is already in use by something other than hermes-webui
if command -v ss >/dev/null 2>&1; then
  PORT_OCCUPIED=$(ss -tuln | grep -E ":${PORT}\b" || true)
elif command -v netstat >/dev/null 2>&1; then
  PORT_OCCUPIED=$(netstat -tuln | grep -E ":${PORT}\b" || true)
else
  PORT_OCCUPIED=""
fi

if [[ -n "$PORT_OCCUPIED" ]]; then
  # Check if it's already our docker container
  CONTAINER_USING_PORT=$($DOCKER_COMPOSE ps -q hermes-webui 2>/dev/null || true)
  if [[ -z "$CONTAINER_USING_PORT" ]]; then
    echo -e "${YELLOW}[!] Warning: Port ${PORT} appears to be in use by another process.${NC}"
    echo "    If container startup fails, specify a different port with: $0 --port <PORT>"
  fi
fi

# Rebuild / Restart / Start Container
BUILD_FLAGS=""
if [[ "$REBUILD" -eq 1 ]]; then
  echo -e "${BLUE}[*] Rebuilding Docker image (--no-cache)...${NC}"
  BUILD_FLAGS="--build --no-cache"
else
  echo -e "${BLUE}[*] Building / verifying Docker image...${NC}"
  BUILD_FLAGS="--build"
fi

echo -e "${BLUE}[*] Launching Lisa WebUI with Docker Compose...${NC}"
$DOCKER_COMPOSE up -d $BUILD_FLAGS

# Health check wait loop
echo -e "${BLUE}[*] Waiting for Lisa WebUI to become healthy on port ${PORT}...${NC}"
MAX_WAIT_SECONDS=180
START_TIME=$(date +%s)
HEALTHY=0

while true; do
  CURRENT_TIME=$(date +%s)
  ELAPSED=$((CURRENT_TIME - START_TIME))

  if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    HEALTHY=1
    break
  fi

  if [[ $ELAPSED -ge $MAX_WAIT_SECONDS ]]; then
    break
  fi

  printf "."
  sleep 2
done
printf "\n"

if [[ "$HEALTHY" -eq 1 ]]; then
  echo ""
  echo -e "${GREEN}======================================================${NC}"
  echo -e "${BOLD}${GREEN}   ✓ Lisa WebUI & Customer Wizard is successfully UP! ${NC}"
  echo -e "${GREEN}======================================================${NC}"
  if [[ "$MODE" == "server" ]]; then
    echo -e "${BOLD}Access URLs:${NC}"
    echo -e "  External WebUI:   ${CYAN}http://${SERVER_IP}:${PORT}${NC}"
    echo -e "  Localhost:        ${CYAN}http://127.0.0.1:${PORT}${NC}"
  else
    echo -e "${BOLD}Access URL:${NC}"
    echo -e "  Local WebUI:      ${CYAN}http://127.0.0.1:${PORT}${NC}"
  fi
  echo ""
  echo -e "${BOLD}Customer Onboarding Wizard:${NC}"
  echo -e "  - Open the WebUI URL in your browser."
  echo -e "  - The wizard opens automatically on first unconfigured launch,"
  echo -e "    or anytime via: ${YELLOW}Settings -> System -> Run Setup Wizard Again${NC}"
  echo -e "  - Wizard API Status: ${CYAN}http://127.0.0.1:${PORT}/api/customer-wizard/status${NC}"
  echo ""
  echo -e "${BOLD}Useful Commands:${NC}"
  echo "  View logs:        $0 --logs"
  echo "  Check status:     $0 --status"
  echo "  Restart:          $0 --restart"
  echo "  Stop container:   $0 --stop"
  echo -e "${GREEN}======================================================${NC}"
else
  echo ""
  echo -e "${RED}======================================================${NC}"
  echo -e "${BOLD}${RED}   [!] Lisa WebUI did not report healthy within ${MAX_WAIT_SECONDS}s.${NC}"
  echo -e "${RED}======================================================${NC}"
  echo "Inspecting recent container logs:"
  echo ""
  $DOCKER_COMPOSE logs --tail=40 hermes-webui
  echo ""
  echo -e "Troubleshooting tips:"
  echo "  1. Check logs with: $0 --logs"
  echo "  2. Inspect permissions of ${HERMES_HOME_DIR}"
  echo "  3. Verify port ${PORT} availability"
  exit 1
fi
