#!/usr/bin/env bash
#
# no-numb gate — Deus comprehension warden, Stop-hook doorbell.  LIA-328.
#
# Adapted from the MIT-licensed no-numb plugin (github.com/Ciucky/no-numb).
# See .claude/skills/ATTRIBUTION-no-numb.md.
#
# Job: a "doorbell," nothing more. Did Claude edit a file this run, and have we
# not already quizzed for it? If so, block the stop and tell Claude to run the
# quiz-me skill. ALL judgement (what/whether/how to quiz) lives in the skill.
#
# DEFAULT OFF. Enabled only when the interactive shell exports DEUS_NONUMB (or
# ~/.config/deus/nonumb.json sets "enabled": true). This gates ONLY interactive
# sessions by construction: launchd (com.deus) and container agents do not
# inherit an interactive shell's environment. DO NOT add DEUS_NONUMB to any
# launchd EnvironmentVariables block — that would silently opt a daemon in and
# trap a non-interactive run at AskUserQuestion.
#
# Pattern: Guard / Chain-of-Responsibility — bail on the first exit condition,
# fall through to the block. Fails OPEN everywhere (missing jq, missing/garbled
# transcript, missing config): trapping the session on a parse bug is worse than
# missing one quiz.
#
# Requires: jq (a documented Deus prereq). Missing jq → no-op.
#
# Stop-hook I/O schema — input `transcript_path` + the `stop_hook_active`
# re-entry guard (true while already continuing from a Stop hook), and the
# `{decision:"block", reason}` output contract: https://code.claude.com/docs/en/hooks

input="$(cat)"

# Read a field from the hook's stdin JSON. Falls back to empty on error.
field() { printf '%s' "$input" | jq -r "$1" 2>/dev/null; }

# 1) Already quizzed this cycle → let Claude stop. When we block, the quiz runs
#    as a CONTINUATION of the same run, so the edits are still "this run" when
#    Claude tries to stop again. Without this guard the hook re-blocks forever.
#    Claude Code sets stop_hook_active=true on that continuation.
[ "$(field '.stop_hook_active // false')" = "true" ] && exit 0

# 2) jq drives the transcript scan; if it is absent, fail open (no quiz).
command -v jq >/dev/null 2>&1 || exit 0

# 3) Enabled check (DEFAULT OFF). Enabled iff the env opt-in is truthy OR the
#    config sets "enabled": true.
config="${HOME}/.config/deus/nonumb.json"
# Env opt-in is the primary switch; config.enabled is the alternative.  LIA-328
case "${DEUS_NONUMB:-}" in
  1 | true | on | yes | TRUE | On | Yes | YES) env_on=1 ;;
  *) env_on=0 ;;
esac
cfg_on=0
if [ -f "$config" ] && [ "$(jq -r '.enabled == true' "$config" 2>/dev/null)" = "true" ]; then
  cfg_on=1
fi
if [ "$env_on" != "1" ] && [ "$cfg_on" != "1" ]; then
  exit 0
fi

# 4) Ensure the config exists with defaults — created once, when first enabled.
#    It lives in the deus config dir (not the repo) so it is user-agnostic,
#    survives updates, and is a stable, editable home for the `depth` dial.
if [ ! -f "$config" ]; then
  mkdir -p "${HOME}/.config/deus" 2>/dev/null &&
    printf '{\n  "enabled": false,\n  "depth": "standard"\n}\n' >"$config" 2>/dev/null || true
fi

# 5) Doorbell: did Claude Edit/Write/MultiEdit/NotebookEdit any file since the
#    user's last prompt (i.e. during this run)? No transcript → fail open.
transcript="$(field '.transcript_path // empty')"
[ -n "$transcript" ] && [ -f "$transcript" ] || exit 0

edited="$(jq -s '
  # Content can be nested as .message.content (common) or .content; handle both.
  def blocks: (.message.content // .content // []);

  # A genuine user prompt: a user line carrying real text (string content, or an
  # array with a text block) — as opposed to a user line that is only tool_result.
  def is_prompt:
    .type == "user"
    and (
      ((blocks | type) == "string" and (blocks | length) > 0)
      or ((blocks | type) == "array" and (blocks | any(.[]?; .type == "text")))
    );

  # An assistant turn that edited a file.
  def is_edit:
    .type == "assistant"
    and (blocks | type) == "array"
    and (blocks | any(.[]?;
          .type == "tool_use"
          and (.name == "Edit" or .name == "Write"
               or .name == "MultiEdit" or .name == "NotebookEdit")));

  . as $arr
  | (([ range(0; ($arr | length)) | select($arr[.] | is_prompt) ] | last) // -1) as $start
  | [ $arr[($start + 1):][] | select(is_edit) ] | length > 0
' "$transcript" 2>/dev/null || echo false)"

if [ "$edited" = "true" ]; then
  jq -n '{
    decision: "block",
    reason: ("You edited files this turn. Before ending your turn, invoke the /quiz-me skill via the "
      + "Skill tool: quiz the user (multiple-choice, via AskUserQuestion) on what you just did this turn, "
      + "honoring the configured depth, and sampling across the five comprehension axes (what-changed, "
      + "why-this-shape, what-would-break, how-was-it-verified, what-to-review-later). Do not end your turn "
      + "until they pass. Skip with a one-line note only if the change is genuinely cosmetic (see the skill).")
  }'
fi

exit 0
