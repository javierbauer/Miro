#!/usr/bin/env bash
# run_overnight.sh — start the Polymarket predictor to run all night
# Usage: ./scripts/run_overnight.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"

LOG_FILE="$LOG_DIR/overnight_$(date +%Y%m%d_%H%M%S).log"

echo "Starting Polymarket Predictor overnight run..."
echo "Logs: $LOG_FILE"
echo "Dashboard: http://localhost:8000"
echo ""

cd "$PROJECT_DIR"

# Check .env exists
if [ ! -f ".env" ]; then
  echo "WARNING: .env not found — copying from .env.example (DRY_RUN=true by default)"
  cp .env.example .env
fi

# Install deps if needed
if ! python -c "import fastapi" 2>/dev/null; then
  echo "Installing dependencies..."
  pip install -r requirements.txt --quiet
fi

# Start server in background
nohup python main.py > "$LOG_FILE" 2>&1 &
SERVER_PID=$!
echo "Server PID: $SERVER_PID (saved to logs/server.pid)"
echo "$SERVER_PID" > "$LOG_DIR/server.pid"

sleep 2

# Auto-start the scanner loop via API
curl -s -X POST "http://localhost:8000/api/start_loop?interval=900" > /dev/null
echo "Scanner loop started (every 15 minutes)"
echo ""
echo "To stop: kill \$(cat logs/server.pid)"
echo "Tail logs: tail -f $LOG_FILE"
