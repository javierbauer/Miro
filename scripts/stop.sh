#!/usr/bin/env bash
PID_FILE="$(dirname "$0")/../logs/server.pid"
if [ -f "$PID_FILE" ]; then
  PID=$(cat "$PID_FILE")
  kill "$PID" && echo "Stopped server (PID $PID)" || echo "Process $PID not found"
  rm -f "$PID_FILE"
else
  echo "No PID file found — is the server running?"
fi
