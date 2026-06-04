#!/usr/bin/env bash
# deus_init.sh — onboard a project into Deus code intelligence.
#
# Resolves the project root (git toplevel → realpath), runs a safety gate, then
# indexes the project with BOTH code-intelligence engines:
#   • codegraph   — per-repo .codegraph/ knowledge graph
#   • code_search — per-project sqlite-vec DB under ~/.config/deus/projects/<md5>/
#
# Registration of the project config (~/.config/deus/projects/<md5>.json) is
# done by the calling `deus init|onboard` arm via _write_project_config — NOT
# here. _write_project_config is a deus-cmd.sh function; sourcing that file to
# reach it would also run its unconditional top-level dispatch. The arm resolves
# the realpath once and passes it in, so the md5 keying the code_search DB and
# the md5 keying the config are guaranteed identical.
#
# Cross-platform: macOS/Linux only (bash + git + codegraph), consistent with the
# Windows-pending markers in deus-cmd.sh.
#
# Usage: deus_init.sh [project-dir] [--force]
#   project-dir  Defaults to $PWD. From the `deus init` arm this is the already
#                realpath-resolved git toplevel; re-resolving it here is a no-op.
#   --force      Bypass the safety gate (non-git dirs, or >5000 tracked files).
#
# Exit codes: 0 = onboarded (indexing attempted; a missing engine warns, does
#                 not fail). 1 = safety gate refused / invalid directory — the
#                 caller MUST NOT register the project on a non-zero exit.

set -u

FORCE=0
TARGET=""
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    -h|--help) sed -n '2,26p' "$0" | sed 's/^#\{1,\} \{0,1\}//'; exit 0 ;;
    -*) echo "deus init: unknown flag: $arg" >&2; exit 1 ;;
    *) [ -z "$TARGET" ] && TARGET="$arg" ;;
  esac
done

# Deus repo root (where code_search.py lives), following symlinks on $0.
_self="$0"
while [ -L "$_self" ]; do
  _d="$(cd "$(dirname "$_self")" && pwd)"; _self="$(readlink "$_self")"
  [[ "$_self" != /* ]] && _self="$_d/$_self"
done
DEUS_REPO="$(cd "$(dirname "$_self")/.." && pwd -P)"

# Resolve the project root: nearest git toplevel, else the dir itself; then
# canonicalize with `pwd -P` so the realpath string equals Python's
# Path.resolve() → identical md5 in code_search and the project config.
# Idempotent on an already-resolved toplevel (toplevel-of-toplevel == toplevel).
base="${TARGET:-$PWD}"
if [ ! -d "$base" ]; then
  echo "deus init: not a directory: $base" >&2; exit 1
fi
root="$(cd "$base" 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null)" || root=""
[ -z "$root" ] && root="$base"
root="$(cd "$root" 2>/dev/null && pwd -P)" || { echo "deus init: cannot resolve: $base" >&2; exit 1; }

is_git=0
git -C "$root" rev-parse --is-inside-work-tree >/dev/null 2>&1 && is_git=1

# ─── Safety gate ───
# Refuse to index home/huge trees unless --force. File-count + git-ness only
# (no `du` — sidesteps the macOS/Linux size-divergence the plan-reviewer flagged).
if [ "$FORCE" -ne 1 ]; then
  if [ "$is_git" -ne 1 ]; then
    echo "deus init: '$root' is not a git repository." >&2
    echo "  Refusing to index a non-repo directory (this could be your home folder)." >&2
    echo "  Re-run with --force if you really mean to onboard it." >&2
    exit 1
  fi
  tracked="$(git -C "$root" ls-files 2>/dev/null | wc -l | tr -d ' ')"
  if [ "${tracked:-0}" -gt 5000 ]; then
    echo "deus init: '$root' has $tracked tracked files (> 5000)." >&2
    echo "  Indexing a tree this large is slow and may be unintended." >&2
    echo "  Re-run with --force to proceed." >&2
    exit 1
  fi
fi

name="$(basename "$root")"
echo "Onboarding $name ($([ "$is_git" -eq 1 ] && echo git || echo non-git): $root)"

# ─── Non-git isolation marker ───
# code_search keys its per-project DB on the nearest .git/.deus ancestor. A
# non-git root has no .git, so without a marker its index falls through to the
# SHARED legacy DB — and a second non-git onboard would clobber the first. Drop
# a .deus/ marker (the existing instance-local convention) so the resolver
# isolates this root per-project. Git repos use .git and need no marker.
if [ "$is_git" -ne 1 ] && [ ! -d "$root/.deus" ]; then
  mkdir -p "$root/.deus"
  cat > "$root/.deus/README" <<'MARKER'
This directory marks the project root for Deus code intelligence so its
code-search index is isolated per-project rather than shared. Created by
`deus init` because this folder is not a git repository. Safe to remove if you
stop using Deus here.
MARKER
  echo "  • created .deus/ marker (non-git isolation)"
fi

# ─── codegraph (warn, not fatal) ───
if command -v codegraph >/dev/null 2>&1; then
  if [ -d "$root/.codegraph" ]; then
    echo "  • codegraph: syncing existing index…"
    codegraph sync "$root" || echo "  ! codegraph sync failed (non-fatal)" >&2
  else
    echo "  • codegraph: initializing + indexing…"
    codegraph init "$root" >/dev/null 2>&1
    codegraph index "$root" || echo "  ! codegraph index failed (non-fatal)" >&2
  fi
else
  echo "  ! codegraph not installed — skipping (run /setup to register code intel)" >&2
fi

# ─── code_search (warn, not fatal) ───
# reindex resolves the per-project DB from this directory arg and self-migrates
# the legacy shared DB on first miss (handled inside code_search.py).
cs="$DEUS_REPO/scripts/code_search.py"
if command -v python3 >/dev/null 2>&1 && [ -f "$cs" ]; then
  echo "  • code_search: reindexing…"
  python3 "$cs" reindex "$root" || echo "  ! code_search reindex failed (non-fatal — is Ollama running?)" >&2
else
  echo "  ! code_search unavailable — skipping (need python3 + $cs)" >&2
fi

echo "  ✓ indexing complete"
exit 0
