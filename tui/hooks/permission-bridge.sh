#!/bin/bash
# PreToolUse hook: bridges permission decisions between Claude Code and the Deus TUI.
# When DEUS_TUI_PERMISSIONS_DIR is set, writes a request file and polls for a response.
# No-op when the env var is absent (non-TUI usage).
set -e

[ -z "$DEUS_TUI_PERMISSIONS_DIR" ] && exit 0
[ ! -d "$DEUS_TUI_PERMISSIONS_DIR" ] && exit 0
command -v jq >/dev/null 2>&1 || exit 0

INPUT=$(cat)

TOOL_NAME=$(printf '%s' "$INPUT" | jq -r '.tool_name // empty')
TOOL_ID=$(printf '%s' "$INPUT" | jq -r '.tool_use_id // empty')
[ -z "$TOOL_ID" ] && exit 0

# Auto-approve if tool is in the allowed list (set by TUI at spawn time)
if [ -n "$DEUS_TUI_ALLOWED_TOOLS" ] && echo ",$DEUS_TUI_ALLOWED_TOOLS," | grep -qF ",$TOOL_NAME,"; then
    cat <<RESP
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"Tool in always-allowed list"}}
RESP
    exit 0
fi

# Sanitize TOOL_ID: only alphanumeric, underscore, hyphen allowed
if ! printf '%s' "$TOOL_ID" | grep -qE '^[A-Za-z0-9_-]+$'; then
    exit 0
fi

PREVIEW=$(printf '%s' "$INPUT" | jq -r '
  .tool_input |
  if .command then (.command | .[0:80])
  elif .file_path then .file_path
  elif .description then (.description | .[0:80])
  else ""
  end
' 2>/dev/null || echo "")

TIMESTAMP=$(date +%s)

# Write request atomically — use jq for safe JSON serialization
REQ_FILE="$DEUS_TUI_PERMISSIONS_DIR/request-$TOOL_ID.json"
jq -n --arg id "$TOOL_ID" --arg name "$TOOL_NAME" --arg preview "$PREVIEW" --argjson ts "$TIMESTAMP" \
  '{tool_use_id:$id, tool_name:$name, tool_input_preview:$preview, timestamp:$ts}' \
  > "${REQ_FILE}.tmp"
mv "${REQ_FILE}.tmp" "$REQ_FILE"

TIMEOUT_SECS=${DEUS_TUI_PERMISSIONS_TIMEOUT:-120}
MAX_ITERATIONS=$((TIMEOUT_SECS * 2))
ITER=0

while [ "$ITER" -lt "$MAX_ITERATIONS" ]; do
    RESP_FILE="$DEUS_TUI_PERMISSIONS_DIR/response-$TOOL_ID.json"
    if [ -f "$RESP_FILE" ]; then
        DECISION=$(jq -r '.permissionDecision // "deny"' "$RESP_FILE" 2>/dev/null || echo "deny")
        REASON=$(jq -r '.permissionDecisionReason // "User decided via TUI"' "$RESP_FILE" 2>/dev/null || echo "User decided via TUI")
        rm -f "$REQ_FILE" "$RESP_FILE"
        cat <<RESP
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"$DECISION","permissionDecisionReason":"$REASON"}}
RESP
        exit 0
    fi
    sleep 0.5
    ITER=$((ITER + 1))
done

# Timeout — deny and clean up
rm -f "$REQ_FILE"
cat <<RESP
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"TUI approval timeout (${TIMEOUT_SECS}s)"}}
RESP
