#!/usr/bin/env bash
# setup_code_intel.sh — install + register Deus's code-intelligence MCP servers.
#
# Idempotent. Registers two USER-scope MCP servers (available in EVERY project,
# reproducibly on a fresh clone):
#   - codegraph   : per-repo structural call graph (npm @colbymchenry/codegraph)
#   - code-search : semantic search (repo-native scripts/code_search_mcp.py)
#
# Invoked by /setup (step 6d); safe to re-run anytime.
#
# NOTE: codegraph is a GLOBAL developer tool (like Node/Docker/Ollama, which
# /setup also installs) — NOT channel code. This is the one sanctioned
# public-registry global install outside setup step 0.
#
# Platform: macOS/Linux (consistent with deus-cmd.sh Windows-pending markers).
# No `set -e` — every sub-step is non-fatal (warn + continue), so a missing
# prerequisite never aborts the rest of setup.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

# Prefer the eval venv (has sqlite_vec + mcp); fall back to system python3.
if [ -x "$REPO_ROOT/eval/.venv/bin/python3" ]; then
  VENV_PY="$REPO_ROOT/eval/.venv/bin/python3"
else
  VENV_PY="$(command -v python3 || true)"
fi

ok()   { printf '  \xe2\x9c\x93 %s\n' "$1"; }
warn() { printf '  \xe2\x9a\xa0 %s\n' "$1" >&2; }

# ── codegraph: global npm tool ───────────────────────────────────────────────
if command -v codegraph >/dev/null 2>&1; then
  ok "codegraph already installed ($(codegraph --version 2>/dev/null || echo present))"
elif command -v npm >/dev/null 2>&1; then
  echo "  installing @colbymchenry/codegraph (global)…"
  if npm i -g @colbymchenry/codegraph >/dev/null 2>&1; then
    ok "codegraph installed"
  else
    warn "codegraph install failed — check Node/npm, then re-run this step"
  fi
else
  warn "npm not found — codegraph needs Node.js; install Node, then re-run this step"
fi

# ── register MCP servers (USER scope = all projects) ─────────────────────────
# Idempotent via exit code: `claude mcp get <name>` returns 0 when registered.
if command -v claude >/dev/null 2>&1; then
  if command -v codegraph >/dev/null 2>&1; then
    if claude mcp get codegraph >/dev/null 2>&1; then
      ok "codegraph MCP already registered (user scope)"
    elif claude mcp add -s user codegraph -- codegraph serve --mcp >/dev/null 2>&1; then
      ok "codegraph MCP registered (user scope)"
    else
      warn "codegraph MCP registration failed — run: claude mcp add -s user codegraph -- codegraph serve --mcp"
    fi
  fi

  if [ -n "$VENV_PY" ] && [ -f "$REPO_ROOT/scripts/code_search_mcp.py" ]; then
    if claude mcp get code-search >/dev/null 2>&1; then
      ok "code-search MCP already registered (user scope)"
    elif claude mcp add -s user code-search -- "$VENV_PY" "$REPO_ROOT/scripts/code_search_mcp.py" >/dev/null 2>&1; then
      ok "code-search MCP registered (user scope)"
    else
      warn "code-search MCP registration failed — run: claude mcp add -s user code-search -- \"$VENV_PY\" \"$REPO_ROOT/scripts/code_search_mcp.py\""
    fi
  else
    warn "code-search prerequisites missing (python3 + scripts/code_search_mcp.py) — skipping registration"
  fi
else
  warn "claude CLI not found — cannot register MCP servers; install Claude Code, then re-run this step"
fi

# ── prerequisite advisory (non-fatal) ────────────────────────────────────────
command -v ollama >/dev/null 2>&1 || \
  warn "Ollama not found — code_search embeddings need it (install from https://ollama.ai)"

echo "  code-intelligence setup complete (idempotent — safe to re-run)"
