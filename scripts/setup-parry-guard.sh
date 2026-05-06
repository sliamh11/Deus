#!/usr/bin/env bash
# setup-parry-guard.sh — Pre-flight checks and installation for parry-guard.
#
# parry-guard is a Layer 2 prompt injection scanner for host Claude Code
# sessions. See docs/decisions/parry-guard-installation.md for the full ADR.
#
# This script does NOT modify ~/.claude/settings.json. After running it,
# apply the hook configuration manually or via the /update-config skill.

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

info()  { printf "${GREEN}[INFO]${NC}  %s\n" "$1"; }
warn()  { printf "${YELLOW}[WARN]${NC}  %s\n" "$1"; }
fail()  { printf "${RED}[FAIL]${NC}  %s\n" "$1"; }

# --- 1. Check for uvx or cargo -------------------------------------------

INSTALL_METHOD=""

if command -v uvx &>/dev/null; then
  INSTALL_METHOD="uvx"
  info "Found uvx: $(command -v uvx)"
elif command -v cargo &>/dev/null; then
  INSTALL_METHOD="cargo"
  info "Found cargo: $(command -v cargo)"
else
  fail "Neither uvx nor cargo found."
  echo "  Install uv:    curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo "  Or Rust/cargo:  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
  exit 1
fi

# --- 2. Check for existing parry-guard installation -----------------------

if command -v parry-guard &>/dev/null; then
  info "parry-guard already installed: $(parry-guard --version 2>/dev/null || echo 'unknown version')"
elif [ "$INSTALL_METHOD" = "uvx" ]; then
  info "parry-guard not found on PATH. Installing via uvx..."
  uvx parry-guard --version
  info "parry-guard installed via uvx."
elif [ "$INSTALL_METHOD" = "cargo" ]; then
  info "parry-guard not found on PATH. Installing via cargo..."
  cargo install parry-guard
  info "parry-guard installed via cargo."
fi

# --- 3. Check HuggingFace token ------------------------------------------

if [ -z "${HF_TOKEN:-}" ]; then
  warn "HF_TOKEN is not set."
  echo "  parry-guard downloads DeBERTa models from HuggingFace on first run."
  echo "  If the model is gated, you will need: export HF_TOKEN=hf_..."
  echo "  Public models should work without a token."
else
  info "HF_TOKEN is set."
fi

# --- 4. Verify installation ------------------------------------------------

info "Verifying parry-guard responds..."
if [ "$INSTALL_METHOD" = "uvx" ]; then
  VERIFY_CMD="uvx parry-guard --version"
else
  VERIFY_CMD="parry-guard --version"
fi

if $VERIFY_CMD &>/dev/null; then
  info "parry-guard is operational: $($VERIFY_CMD 2>/dev/null)"
else
  warn "parry-guard --version returned non-zero."
  echo "  The binary may need model downloads on first run."
  echo "  Try running manually:  $VERIFY_CMD"
  echo "  Check logs for HuggingFace download progress."
fi

# --- 5. Next steps --------------------------------------------------------

echo ""
info "Setup complete. Next steps:"
echo ""
echo "  Add the following hooks to ~/.claude/settings.json:"
echo ""
echo '  {'
echo '    "hooks": {'
echo '      "PreToolUse": ['
echo '        {'
echo '          "command": "uvx parry-guard hook",'
echo '          "timeout": 1000'
echo '        }'
echo '      ],'
echo '      "PostToolUse": ['
echo '        {'
echo '          "command": "uvx parry-guard hook",'
echo '          "timeout": 5000'
echo '        }'
echo '      ]'
echo '    }'
echo '  }'
echo ""
echo "  Or use the /update-config skill to apply the hooks."
echo ""
info "See docs/decisions/parry-guard-installation.md for the full ADR."
