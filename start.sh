#!/usr/bin/env zsh

# ─────────────────────────────────────────────────
#  Crowd Count Estimation — One-Shot Launcher
# ─────────────────────────────────────────────────

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

# ── Resolve the script's own directory so it works from anywhere ──
SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR" || { echo "${RED}❌  Cannot cd into project directory${RESET}"; exit 1; }

echo ""
echo "${BOLD}${CYAN}╔══════════════════════════════════════════╗${RESET}"
echo "${BOLD}${CYAN}║   Crowd Count Estimation — Launcher      ║${RESET}"
echo "${BOLD}${CYAN}╚══════════════════════════════════════════╝${RESET}"
echo ""

# ── 1. Activate virtual environment ──────────────────────────────
VENV_ACTIVATE="$SCRIPT_DIR/.venv/bin/activate"

if [[ ! -f "$VENV_ACTIVATE" ]]; then
  echo "${RED}❌  .venv not found at: $VENV_ACTIVATE${RESET}"
  echo "${YELLOW}    Run:  python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt${RESET}"
  exit 1
fi

echo "${GREEN}✔  Activating virtual environment...${RESET}"
source "$VENV_ACTIVATE"
echo "${GREEN}   Python: $(which python)${RESET}"
echo ""

# ── 2. Cleanup helper — kill children on Ctrl-C ──────────────────
BACKEND_PID=""
FRONTEND_PID=""

cleanup() {
  echo ""
  echo "${YELLOW}⚡  Shutting down...${RESET}"
  [[ -n "$BACKEND_PID"  ]] && kill "$BACKEND_PID"  2>/dev/null
  [[ -n "$FRONTEND_PID" ]] && kill "$FRONTEND_PID" 2>/dev/null
  wait "$BACKEND_PID"  2>/dev/null
  wait "$FRONTEND_PID" 2>/dev/null
  echo "${GREEN}✔  All processes stopped. Goodbye!${RESET}"
  exit 0
}
trap cleanup INT TERM

# ── 3. Start FastAPI backend ──────────────────────────────────────
echo "${BOLD}${CYAN}▶  Starting FastAPI backend  (http://localhost:8000)${RESET}"
python backend.py 2>&1 | sed "s/^/${CYAN}[BACKEND]${RESET} /" &
BACKEND_PID=$!

# Give the backend a moment to bind its port
sleep 2

# ── 4. Start Streamlit frontend ───────────────────────────────────
echo "${BOLD}${GREEN}▶  Starting Streamlit frontend (http://localhost:8501)${RESET}"
streamlit run app.py \
  --server.headless true \
  --server.port 8501 \
  2>&1 | sed "s/^/${GREEN}[FRONTEND]${RESET} /" &
FRONTEND_PID=$!

echo ""
echo "${BOLD}Both services are running. Press ${RED}Ctrl-C${RESET}${BOLD} to stop.${RESET}"
echo ""

# ── 5. Wait — keep script alive until user kills it ───────────────
wait
