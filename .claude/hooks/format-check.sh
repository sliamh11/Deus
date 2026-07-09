#!/bin/bash
# PreToolUse hook: blocks git commit when staged .ts files fail prettier --check.
# Mechanical pass/fail — no warden verdict lifecycle.
# Non-zero exit blocks the tool call (Claude Code PreToolUse hook contract).
set -e

INPUT=$(cat)

command -v jq >/dev/null 2>&1 || exit 0

TOOL=$(printf '%s' "$INPUT" | jq -r '.tool_name // empty')
[ "$TOOL" = "Bash" ] || exit 0

CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty')
[ -n "$CMD" ] || exit 0

if ! printf '%s' "$CMD" | grep -qE '(^|[;&|]\s*)git(\s+(-C\s+\S+|--\S+))*\s+commit(\s|$)'; then
  exit 0
fi

# Use the SAME repo the actual command targets (via its own -C, if any) for
# our own git queries below -- not this hook process's ambient cwd, which may
# differ (e.g. `git -C <worktree> commit` run from the main repo session).
# Best-effort, last -C wins: adequate for this non-gating formatting check
# (worst case on a mis-parse is a skipped or wrong-repo format check, not a
# review bypass -- see scripts/warden_hooks/command_parse.py's
# _parse_git_invocation for the rigorous version used by the actual commit
# review gates).
GIT_C_PATH=$(printf '%s' "$CMD" | grep -oE '\-C[[:space:]]+[^[:space:]]+' | tail -1 | sed -E 's/^-C[[:space:]]+//') || true

git_query() {
  if [ -n "$GIT_C_PATH" ]; then
    git -C "$GIT_C_PATH" "$@" 2>/dev/null
  else
    git "$@" 2>/dev/null
  fi
}

TOPLEVEL=$(git_query rev-parse --show-toplevel || true)
[ -n "$TOPLEVEL" ] || exit 0

PROJECT_ROOT="$(cd "$(dirname "$(git_query rev-parse --git-common-dir)")" && pwd)" 2>/dev/null || exit 0

case "$TOPLEVEL" in
  "$PROJECT_ROOT"|"$PROJECT_ROOT/.claude/worktrees/"*) ;;
  *) exit 0 ;;
esac

STAGED_TS=$(git_query diff --cached --name-only --diff-filter=ACMR -- '*.ts')
[ -n "$STAGED_TS" ] || exit 0

PRETTIER="$PROJECT_ROOT/node_modules/.bin/prettier"
if [ ! -x "$PRETTIER" ]; then
  echo "[format-check] WARNING: prettier not found at $PRETTIER, skipping" >&2
  exit 0
fi

cd "$TOPLEVEL"

if ! printf '%s' "$STAGED_TS" | tr '\n' '\0' | xargs -0 "$PRETTIER" --check -- 2>/dev/null; then
  echo "[format-check] BLOCKED — staged .ts files have formatting issues."
  echo "Fix with:  $PRETTIER --write <files>"
  echo "Then re-stage and retry the commit."
  echo "Files with issues:"
  echo "$STAGED_TS" | sed 's/^/  /'
  exit 2
fi
