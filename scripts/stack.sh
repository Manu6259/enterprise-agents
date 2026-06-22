#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
#  stack.sh — start / stop / status helper for the enterprise-agents
#  stack: 6 MCP servers + 3 A2A agent servers.
#
#  Usage:
#     ./scripts/stack.sh start    # bring everything up
#     ./scripts/stack.sh stop     # tear everything down
#     ./scripts/stack.sh status   # show what's running
#     ./scripts/stack.sh restart  # stop + start
# ─────────────────────────────────────────────────────────────────────

set -u

# ── Project root (directory containing this script's parent) ─────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

VENV_PY="$PROJECT_ROOT/venv/bin/python"
LOG_DIR="$PROJECT_ROOT/logs"
mkdir -p "$LOG_DIR"

# ── Stack definition ─────────────────────────────────────────────────
MCP_SERVERS=(
  "database:8001:mcp_servers/database_server.py"
  "file:8002:mcp_servers/file_server.py"
  "scoring:8003:mcp_servers/scoring_server.py"
  "report:8004:mcp_servers/report_server.py"
  "recommendation:8005:mcp_servers/recommendation_server.py"
  "outreach:8006:mcp_servers/outreach_server.py"
)
A2A_AGENTS=(
  "data_analysis:9001:agents/data_analysis/server.py"
  "customer_intelligence:9002:agents/customer_intelligence/server.py"
  "sales_intelligence:9003:agents/sales_intelligence/server.py"
  "sales_outreach:9004:agents/sales_outreach/server.py"
)

# ── Colors (fall back to no-op if not a TTY) ─────────────────────────
if [ -t 1 ]; then
  C_GRN="\033[32m"; C_RED="\033[31m"; C_YEL="\033[33m"; C_RST="\033[0m"
else
  C_GRN=""; C_RED=""; C_YEL=""; C_RST=""
fi

# ── Helpers ──────────────────────────────────────────────────────────
preflight() {
  if [ ! -x "$VENV_PY" ]; then
    echo -e "${C_RED}✗ venv not found at $VENV_PY${C_RST}"
    echo "  Create it first:  python3 -m venv venv && ./venv/bin/pip install -r requirements.txt"
    exit 1
  fi
  if [ ! -f "$PROJECT_ROOT/.env" ]; then
    echo -e "${C_YEL}⚠ .env not found at $PROJECT_ROOT/.env${C_RST}"
    echo "  Create it with LLM_PROVIDER, OPENAI_API_KEY (if using OpenAI), MCP_TRANSPORT=http"
    echo "  See STARTUP.md for the exact template."
    exit 1
  fi
}

# Ports from both groups
all_ports() { echo "8001 8002 8003 8004 8005 8006 9001 9002 9003 9004"; }

is_listening() {
  # 1 = listening, 0 = not
  local port="$1"
  lsof -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
}

wait_for_port() {
  local port="$1"; local name="$2"; local timeout="${3:-20}"
  local elapsed=0
  while ! is_listening "$port"; do
    sleep 1
    elapsed=$((elapsed + 1))
    if [ $elapsed -ge "$timeout" ]; then
      echo -e "  ${C_RED}✗ $name ($port) did not bind within ${timeout}s${C_RST}"
      return 1
    fi
  done
  echo -e "  ${C_GRN}✓${C_RST} $name bound to :$port"
  return 0
}

launch() {
  local name="$1"; local port="$2"; local script="$3"
  local log_file="$LOG_DIR/${name}.log"
  if is_listening "$port"; then
    echo -e "  ${C_YEL}⚠${C_RST} $name already running on :$port — skipping"
    return 0
  fi
  # shellcheck disable=SC1090,SC1091
  (
    set -a
    source "$PROJECT_ROOT/.env"
    set +a
    "$VENV_PY" "$PROJECT_ROOT/$script" > "$log_file" 2>&1 &
    disown
  )
  wait_for_port "$port" "$name" 20
}

# ── Commands ─────────────────────────────────────────────────────────
cmd_start() {
  preflight

  echo ""
  echo "────────────────────────────────────────────────"
  echo " Starting enterprise-agents stack"
  echo "────────────────────────────────────────────────"

  echo ""
  echo "Stage 1 — MCP servers (6):"
  for entry in "${MCP_SERVERS[@]}"; do
    IFS=':' read -r name port script <<< "$entry"
    launch "$name" "$port" "$script"
  done

  echo ""
  echo "Stage 2 — A2A agent servers (3):"
  for entry in "${A2A_AGENTS[@]}"; do
    IFS=':' read -r name port script <<< "$entry"
    launch "$name" "$port" "$script"
  done

  echo ""
  echo "────────────────────────────────────────────────"
  echo "Stack ready. Agent cards:"
  for entry in "${A2A_AGENTS[@]}"; do
    IFS=':' read -r name port _ <<< "$entry"
    echo "  http://127.0.0.1:$port/.well-known/agent.json"
  done
  echo ""
  echo "Run a question through the orchestrator:"
  echo "  ./venv/bin/python orchestrator/main.py \"How many VIP customers?\""
  echo "────────────────────────────────────────────────"
}

cmd_stop() {
  echo ""
  echo "────────────────────────────────────────────────"
  echo " Stopping enterprise-agents stack"
  echo "────────────────────────────────────────────────"

  for entry in "${MCP_SERVERS[@]}" "${A2A_AGENTS[@]}"; do
    IFS=':' read -r name port script <<< "$entry"
    if pkill -f "$script" 2>/dev/null; then
      echo -e "  ${C_GRN}✓${C_RST} stopped $name"
    fi
  done

  sleep 2

  local still_up=0
  for p in $(all_ports); do
    if is_listening "$p"; then
      echo -e "  ${C_RED}✗${C_RST} port $p still listening"
      still_up=$((still_up + 1))
    fi
  done

  if [ $still_up -eq 0 ]; then
    echo ""
    echo -e "${C_GRN}All ports free.${C_RST}"
  else
    echo ""
    echo -e "${C_YEL}$still_up port(s) still listening — may need manual cleanup.${C_RST}"
    echo "  Try:  lsof -iTCP:<port> -sTCP:LISTEN   then kill the PID"
  fi
}

cmd_status() {
  echo ""
  echo "────────────────────────────────────────────────"
  echo " enterprise-agents stack status"
  echo "────────────────────────────────────────────────"
  echo ""
  echo "MCP servers:"
  for entry in "${MCP_SERVERS[@]}"; do
    IFS=':' read -r name port _ <<< "$entry"
    if is_listening "$port"; then
      echo -e "  ${C_GRN}✓${C_RST} $name  :$port"
    else
      echo -e "  ${C_RED}✗${C_RST} $name  :$port (not running)"
    fi
  done

  echo ""
  echo "A2A agent servers:"
  for entry in "${A2A_AGENTS[@]}"; do
    IFS=':' read -r name port _ <<< "$entry"
    if is_listening "$port"; then
      echo -e "  ${C_GRN}✓${C_RST} $name  :$port"
    else
      echo -e "  ${C_RED}✗${C_RST} $name  :$port (not running)"
    fi
  done
  echo ""
}

cmd_restart() {
  cmd_stop
  sleep 1
  cmd_start
}

# ── Dispatch ─────────────────────────────────────────────────────────
case "${1:-}" in
  start)    cmd_start ;;
  stop)     cmd_stop ;;
  status)   cmd_status ;;
  restart)  cmd_restart ;;
  *)
    echo "Usage: $0 {start|stop|status|restart}"
    exit 2
    ;;
esac
