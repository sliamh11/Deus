# Session Retrospective — Conditions and Dispatch

This file is read by `/compress` for the "Trigger session retrospective" step. It is only
relevant in **home mode** (`~/deus`). Skip silently if any check fails.

---

## Conditions (all must be true)

a. Current mode is **home mode** (`~/deus`).

b. The `$VAULT/Retrospectives/` directory exists. This is the **opt-in gate** — if the
   directory is absent, never trigger. Do not create the directory.

c. No retrospective file for today exists:
   ```bash
   ! test -f "$VAULT/Retrospectives/$(date +%Y-%m-%d)-retrospective.md"
   ```

d. The number of session log files newer than the most recent retrospective meets or exceeds
   the `session_window` threshold:
   ```bash
   LATEST_RETRO=$(ls -t "$VAULT/Retrospectives"/*.md 2>/dev/null | head -1)
   if [ -n "$LATEST_RETRO" ]; then
     NEW_COUNT=$(find "$VAULT/Session-Logs" -name "*.md" -newer "$LATEST_RETRO" | wc -l | tr -d ' ')
   else
     NEW_COUNT=$(find "$VAULT/Session-Logs" -name "*.md" | wc -l | tr -d ' ')
   fi
   ```
   Read `session_window` from `~/deus/.claude/wardens/retrospective-schema.md` (default: 20).
   Proceed only if `NEW_COUNT >= session_window`. (Here `session_window` is used as a
   trigger threshold — fire only at/above this count — not as the schema's "reading window".)

`$VAULT` is resolved per the vault-path resolution logic in skill.md (not re-derived here).

---

## Dispatch

When all four conditions are met, dispatch via the Agent tool (in-session, background):

- `subagent_type`: `"session-retrospective"`
- `run_in_background`: `true`
- `prompt`: `"Run a session retrospective. SESSION_LOG_ROOT=<resolved $VAULT>"`
  (substitute the actual resolved `$VAULT` path, not the literal string `$VAULT`)

This avoids `claude -p`, which draws from the Agent SDK credit on subscription plans.

If the Agent tool is unavailable (e.g. a non–Claude Code backend), skip silently.

---

## Reporting

In your completion confirmation, report:

- `"retrospective triggered (background)"` — if dispatched
- `"retrospective skipped: <reason>"` — if any check failed, naming the specific condition that
  was false (e.g. `"skipped: Retrospectives/ dir absent"`, `"skipped: already run today"`,
  `"skipped: only 12/20 sessions since last retro"`)
