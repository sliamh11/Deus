#!/usr/bin/env bash
# Open WebUI as the Deus web front-end (thin client).
#
# Runs Open WebUI (https://github.com/open-webui/open-webui) via `uvx` and points
# it at Deus's OpenAI-compatible /v1 endpoint (src/odysseus-server.ts). Deus is the
# brain: Open WebUI's own memory / RAG / web-search / tools / image-gen / code-
# interpreter and all auxiliary side-model calls (title/tag/autocomplete/etc.) are
# DISABLED so the UI never injects competing context or second-guesses Deus.
#
# Native uvx (not Docker) on purpose: the Deus endpoint binds 127.0.0.1 only
# (src/odysseus-server.ts), which a Docker container cannot reach portably (works
# only via Docker-Desktop NAT on macOS, never on Linux/WSL2). A native host process
# reaches loopback on every platform. Data persists in DATA_DIR so config + chat
# history survive restarts. Most ENABLE_*/permission vars are Open WebUI
# PersistentConfig — read from env on FIRST launch, then stored in DATA_DIR; to
# re-apply, wipe DATA_DIR.
#
# Usage: scripts/webui-serve.sh            (foreground)
#        deus web                          (backgrounds this + opens the browser)
set -euo pipefail

DEUS_ROOT="${DEUS_ROOT:-$HOME/deus}"
ENV_FILE="$DEUS_ROOT/.env"
SECRET_FILE="$DEUS_ROOT/.webui_secret_key"
WEBUI_PORT="${WEBUI_PORT:-8090}"
WEBUI_HOST="${WEBUI_HOST:-127.0.0.1}"

# Deus endpoint token + port (from the gitignored .env; never hard-coded).
# Port resolution: caller-provided $DEUS_PORT (e.g. `deus web` passes the
# plist-aware value) → .env ODYSSEUS_HTTP_PORT → 3005 (the .env.example /
# config.ts default; user-agnostic, never a host-specific value).
[ -f "$ENV_FILE" ] || { echo "FATAL: $ENV_FILE not found" >&2; exit 1; }
DEUS_TOKEN="$(grep -E '^ODYSSEUS_HTTP_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
DEUS_PORT="${DEUS_PORT:-$(grep -E '^ODYSSEUS_HTTP_PORT=' "$ENV_FILE" | head -1 | cut -d= -f2- || true)}"
DEUS_PORT="${DEUS_PORT:-3005}"
[ -n "$DEUS_TOKEN" ] || { echo "FATAL: ODYSSEUS_HTTP_TOKEN missing in $ENV_FILE" >&2; exit 1; }

# Stable signing key so sessions survive restarts (generated once, gitignored).
if [ ! -f "$SECRET_FILE" ]; then
  head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 32 > "$SECRET_FILE"
fi

export DATA_DIR="${DATA_DIR:-$HOME/.deus/owui}"
mkdir -p "$DATA_DIR"

# ── Backend: Deus only ──────────────────────────────────────────────────────
export OPENAI_API_BASE_URL="http://127.0.0.1:${DEUS_PORT}/v1"
export OPENAI_API_KEY="$DEUS_TOKEN"
export ENABLE_OLLAMA_API="False"
export DEFAULT_MODELS="Deus"

# ── Identity / auth (single local user) ─────────────────────────────────────
export WEBUI_NAME="Deus"
export WEBUI_AUTH="False"
export WEBUI_SECRET_KEY="$(cat "$SECRET_FILE")"

# ── No side-model calls: Deus answers, nothing else generates ───────────────
export ENABLE_TITLE_GENERATION="False"
export ENABLE_TAGS_GENERATION="False"
export ENABLE_AUTOCOMPLETE_GENERATION="False"
export ENABLE_FOLLOW_UP_GENERATION="False"
export ENABLE_SEARCH_QUERY_GENERATION="False"
export ENABLE_RETRIEVAL_QUERY_GENERATION="False"

# ── Disable features that overlap Deus's brain (thin client) ────────────────
export ENABLE_WEB_SEARCH="False"
export ENABLE_WEB_LOADER="False"
export ENABLE_IMAGE_GENERATION="False"
export ENABLE_CODE_INTERPRETER="False"
export ENABLE_RAG_HYBRID_SEARCH="False"
export ENABLE_GOOGLE_DRIVE_INTEGRATION="False"
export ENABLE_ONEDRIVE_INTEGRATION="False"
export ENABLE_COMMUNITY_SHARING="False"
export ENABLE_DIRECT_CONNECTIONS="False"
export ENABLE_EVALUATION_ARENA_MODELS="False"
# Kept ON: message rating feeds the future 👍/👎 → Deus evolution bridge.
export ENABLE_MESSAGE_RATING="True"

# ── Hide overlapping surfaces from the user (workspace + features) ──────────
export USER_PERMISSIONS_WORKSPACE_KNOWLEDGE_ACCESS="False"
export USER_PERMISSIONS_WORKSPACE_TOOLS_ACCESS="False"
export USER_PERMISSIONS_WORKSPACE_MODELS_ACCESS="False"
export USER_PERMISSIONS_WORKSPACE_PROMPTS_ACCESS="False"
export USER_PERMISSIONS_WORKSPACE_SKILLS_ACCESS="False"
export USER_PERMISSIONS_FEATURES_WEB_SEARCH="False"
export USER_PERMISSIONS_FEATURES_IMAGE_GENERATION="False"
export USER_PERMISSIONS_FEATURES_CODE_INTERPRETER="False"
export USER_PERMISSIONS_FEATURES_NOTES="False"

echo "[webui-serve] Open WebUI → Deus on http://127.0.0.1:${DEUS_PORT}/v1 (model: Deus)"
echo "[webui-serve] UI: http://${WEBUI_HOST}:${WEBUI_PORT}/  DATA_DIR=$DATA_DIR"
exec uvx --python 3.11 open-webui serve --host "$WEBUI_HOST" --port "$WEBUI_PORT"
