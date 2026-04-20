#!/usr/bin/env bash
# A/B probe: runs the fixed fixtures against `claude -p` with different
# --effort levels. Emits JSON lines per response for later analysis.
#
# Usage:  bash effort_probe.sh <effort:default|low|medium|high> <group:wa|tg>
#
# Combine with real_claude_probe.sh's fixtures by running separately per effort
# level and diffing the outputs.

set -euo pipefail

effort="${1:?effort (default|low|medium|high)}"
group="${2:?group (wa|tg)}"

REPO="${REPO:-$(cd "$(dirname "$0")/../.." && pwd)}"

if [[ "$group" == "wa" ]]; then
  SRC="$REPO/groups/whatsapp_main/CLAUDE.md"
else
  SRC="$REPO/groups/telegram_main/CLAUDE.md"
fi

TMPDIR=$(mktemp -d)
cp "$SRC" "$TMPDIR/CLAUDE.md"

run_probe() {
  local id="$1"
  local prompt="$2"
  local resp start_ms end_ms elapsed_ms
  start_ms=$(python3 -c 'import time; print(int(time.time()*1000))')

  if [[ "$effort" == "default" ]]; then
    resp=$(cd "$TMPDIR" && claude -p "$prompt" --dangerously-skip-permissions 2>/dev/null || echo "ERROR")
  else
    resp=$(cd "$TMPDIR" && claude -p --effort "$effort" "$prompt" --dangerously-skip-permissions 2>/dev/null || echo "ERROR")
  fi

  end_ms=$(python3 -c 'import time; print(int(time.time()*1000))')
  elapsed_ms=$((end_ms - start_ms))

  jq -cn --arg probe "$id" --arg effort "$effort" --arg group "$group" \
         --arg resp "$resp" --argjson elapsed "$elapsed_ms" \
         '{probe: $probe, effort: $effort, group: $group, elapsed_ms: $elapsed, response: $resp}'
}

run_probe "fmt_bold"       "Say hello to me. Include the word important in bold. Just the hello line, nothing more."
run_probe "fmt_no_heading" "Give me three bullet points about using a vault for long-term memory. No preamble."
run_probe "internal_tag"   "Plan your response privately, then answer: what is 2+2? Keep everything minimal."
run_probe "voice_reminder" "[Voice: remind me to call the doctor tomorrow at 2pm]"
run_probe "persona_recall" "In one sentence, what do you know about me?"

rm -rf "$TMPDIR"
