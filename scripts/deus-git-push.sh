#!/usr/bin/env bash
# Constrained git push wrapper for container agents.
# Rejects --force, main/master targets, and non-standard branch prefixes.
# Called by the tool proxy — never directly by containers.

set -euo pipefail

ALLOWED_PREFIXES="feat/ fix/ linear- agent/ refactor/ chore/ ci/ test/ perf/ docs/"

for arg in "$@"; do
  case "$arg" in
    --force|--force-with-lease|-f)
      echo "ERROR: force push is not allowed" >&2
      exit 2
      ;;
  esac
done

branch=""
for arg in "$@"; do
  case "$arg" in
    -u|--set-upstream|--set-upstream-to=*) continue ;;
    origin) continue ;;
    HEAD) continue ;;
    -*) continue ;;
    *)
      if [ -z "$branch" ]; then
        branch="$arg"
      fi
      ;;
  esac
done

if [ -z "$branch" ]; then
  branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)
fi

if [ "$branch" = "main" ] || [ "$branch" = "master" ]; then
  echo "ERROR: pushing to $branch is not allowed" >&2
  exit 2
fi

allowed=false
for prefix in $ALLOWED_PREFIXES; do
  case "$branch" in
    "$prefix"*) allowed=true; break ;;
  esac
done

if [ "$allowed" = false ]; then
  echo "ERROR: branch '$branch' does not match allowed prefixes: $ALLOWED_PREFIXES" >&2
  exit 2
fi

exec git push "$@"
