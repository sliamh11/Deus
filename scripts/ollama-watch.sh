#!/usr/bin/env bash
# Watches Deus logs for Ollama activity and surfaces macOS notifications.
# Usage: ./scripts/ollama-watch.sh
set -euo pipefail

LOG_FILE="${DEUS_LOG:-logs/deus.log}"

if [[ ! -f "$LOG_FILE" ]]; then
  echo "Log file not found: $LOG_FILE"
  echo "Start Deus first, then run this watcher."
  exit 1
fi

echo "Watching $LOG_FILE for Ollama activity... (Ctrl+C to stop)"

tail -F "$LOG_FILE" | grep --line-buffered '\[OLLAMA\]' | while IFS= read -r line; do
  # Strip ANSI codes and JSON wrappers for readable notification text
  clean=$(echo "$line" | sed 's/\x1b\[[0-9;]*m//g' | grep -oP '(?<=\[OLLAMA\] ).*' || echo "$line")
  osascript -e "display notification \"$clean\" with title \"Deus · Ollama\"" 2>/dev/null || true
  echo "[$(date '+%H:%M:%S')] $clean"
done
