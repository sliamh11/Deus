#!/usr/bin/env bash
# Real-Claude behavior probe for CLAUDE.md variants.
#
# Drops a target CLAUDE.md into an isolated temp cwd and runs `claude -p`
# with 5 fixed probes covering formatting, bullets, internal-tag usage,
# voice-reminder handling, and persona recall. Emits one JSON line per
# probe to stdout so you can diff two runs.
#
# Uses `claude -p` which reuses the user's OAuth — no API key needed.
#
# Usage:
#   real_claude_probe.sh <path/to/CLAUDE.md> [label]
#
# Example — compare baseline vs compressed:
#   real_claude_probe.sh /tmp/CLAUDE.md.orig baseline > baseline.jsonl
#   real_claude_probe.sh ./CLAUDE.md           after    > after.jsonl
#   diff <(jq -S . baseline.jsonl) <(jq -S . after.jsonl)
#
# Requires: claude, jq.

set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "usage: $0 <path/to/CLAUDE.md> [label]" >&2
  exit 64
fi

SRC="$1"
LABEL="${2:-run}"

if [[ ! -f "$SRC" ]]; then
  echo "error: $SRC not found" >&2
  exit 66
fi

for dep in claude jq; do
  if ! command -v "$dep" >/dev/null 2>&1; then
    echo "error: $dep not on PATH" >&2
    exit 69
  fi
done

TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT
cp "$SRC" "$TMPDIR/CLAUDE.md"

run_probe() {
  local id="$1"
  local prompt="$2"
  local resp
  resp=$(cd "$TMPDIR" && claude -p "$prompt" --dangerously-skip-permissions 2>/dev/null || echo "ERROR")
  jq -cn --arg probe "$id" --arg label "$LABEL" --arg resp "$resp" \
    '{probe: $probe, label: $label, response: $resp}'
}

run_probe "fmt_bold"       "Say hello to me. Include the word important in bold. Just the hello line, nothing more."
run_probe "fmt_no_heading" "Give me three bullet points about using a vault for long-term memory. No preamble."
run_probe "internal_tag"   "Plan your response privately, then answer: what is 2+2? Keep everything minimal."
run_probe "voice_reminder" "[Voice: remind me to call the doctor tomorrow at 2pm]"
run_probe "persona_recall" "In one sentence, what do you know about me?"
