---
name: project-settings
description: View or modify Deus external project settings (memory level, session summaries, description)
user_invocable: true
---

# /project-settings

Manage Deus data handling settings for the current external project.

## Config location

Project configs are stored at `~/.config/deus/projects/<hash>.json`. To find the config for the current directory, compute the MD5 hash of the absolute path of the current working directory and look for that file.

```bash
# macOS
dir_hash=$(echo -n "$(pwd)" | md5 -q)
# Linux
dir_hash=$(echo -n "$(pwd)" | md5sum | cut -d' ' -f1)
config_file="$HOME/.config/deus/projects/${dir_hash}.json"
```

## Resolve vault path

Read `~/.config/deus/config.json` and use the `vault_path` value. If the env var `DEUS_VAULT_PATH`
is set, use that instead. All paths below use `$VAULT` to mean this resolved path -- same recipe
`checkpoint/skill.md` uses. Without this, `$VAULT` in the "External retrospectives status" section
below and in `delete`'s scope text is an unresolved reference, not a real path.

## When invoked with no arguments or `show`

Read the config file and display current settings. Also detect the project type by scanning for marker files (Cargo.toml → rust, go.mod → go, package.json → node/typescript, pyproject.toml/requirements.txt → python, Gemfile → ruby, pom.xml → java).

Display in this format:

```
Project: <name> (<path>)
Description: <description, or "(none — set with /project-settings description <text>)">
Type: <detected type, e.g. "typescript / next.js" or "python / fastapi", or "unknown">
Memory level: <full|standard|restricted>
  full       — Remember everything. Sessions saved to vault with full details.
  standard   — Remember decisions & architecture, redact code details.
  restricted — Nothing persists. Each session starts fresh.
Session summaries: <on|off>
External retrospectives: <STATUS from below -- "opted-in", "not opted-in", or "not opted-in
  (basename claimed by a different repo -- dispatch will always be refused)">
  <if opted-in>: threshold <N sessions> <"(default)" or "(override)">
Created: <date>
Last accessed: <date>
```

**External retrospectives status** -- this is a SEPARATE opt-in from everything else in this config
file. **Resolve `<project-name>` from the canonical repo root, not the raw invocation directory** --
`REPO_ROOT=$(git -C "$(pwd)" rev-parse --show-toplevel 2>/dev/null || pwd)`, then
`<project-name>=$(basename "$REPO_ROOT")` -- the same resolution every other part of this feature
uses (dispatch, artifact storage, the ownership claims). Using a bare `basename "$(pwd)"` instead
would report "not opted-in" and check the wrong vault path whenever `/project-settings` is invoked
from a subdirectory of an opted-in repo, even though retrospectives are genuinely enabled for it.

Directory existence alone is NOT enough to report "opted-in" -- `branches/external-mode.md`
condition (a)'s basename-collision ownership claim means a repo whose basename lost that claim to a
different repo will have every dispatch refused as a collision, even though
`$VAULT/Retrospectives/external/<project-name>/` still exists (the winning repo created it). Check
ownership the same way dispatch does, not just existence:
```bash
RETRO_DIR="$VAULT/Retrospectives/external/<project-name>/"
OWNER_FILE="${RETRO_DIR}.repo-root-owner"
if [ -d "$RETRO_DIR" ] && { [ ! -f "$OWNER_FILE" ] || [ "$(cat "$OWNER_FILE" 2>/dev/null)" = "$REPO_ROOT" ]; }; then
  STATUS="opted-in"
elif [ -d "$RETRO_DIR" ]; then
  STATUS="not opted-in (basename claimed by a different repo -- dispatch will always be refused)"
else
  STATUS="not opted-in"
fi
```
(No owner file yet is treated as opted-in, not a collision -- the claim is made lazily on first
dispatch, per condition (a), so a freshly-created opt-in directory with no dispatch having run yet
legitimately has no owner file. Only an owner file that names a DIFFERENT `REPO_ROOT` means a real
collision.) If `STATUS` is `opted-in`, also read `retro_threshold` from this config file if present
(show as the override value) or fall back to `session_window_external` from
`~/deus/.claude/wardens/retrospective-schema.md` (show as the default value). This status has never
been surfaced by this command before this project's own retrospective feature was added -- it lives
in the vault, not this config, precisely because it's the one setting `delete` below does NOT touch
(see that section).

Then show available commands:
- `/project-settings memory full|standard|restricted` — change memory level
- `/project-settings summaries on|off` — toggle session summaries
- `/project-settings description <text>` — set a short project description Deus uses as context
- `/project-settings retro_threshold <N>` — override the external-retrospective session-count
  threshold for this project (default 10, set in `.claude/wardens/retrospective-schema.md`). Only
  meaningful once opted in -- see "External retrospectives status" above for how to opt in (there is
  no command for it; it's a directory a human creates by hand, by design).
- `/project-settings delete` — delete this config file only (memory level, summary settings,
  description, retro_threshold override) — does NOT affect external-retrospective opt-in or history,
  see that command's own confirmation text

## When invoked with arguments

Parse the argument and update the config JSON file accordingly using Python to read/write the JSON safely. Always preserve all existing fields when updating.

### `memory full|standard|restricted`

Update the `memory_level` field. If changing to `restricted`, also set `save_summaries` to false and inform the user.

Memory level descriptions:
- **full**: Remember everything. Claude auto-memory enabled. Session summaries saved to vault with full code details.
- **standard**: Remember decisions and architecture, skip code details. Auto-memory enabled with guidance. Summaries saved but code-redacted.
- **restricted**: Nothing persists. Auto-memory disabled. No summaries. Best for NDA/client work.

### `summaries on|off`

Update the `save_summaries` field. If memory level is `restricted` and user tries to enable summaries, warn that restricted mode doesn't support summaries and don't make the change.

### `description <text>`

Update the `description` field in the config JSON. This text is used by Deus as project context in future sessions. Keep it concise (1–2 sentences). Example: `/project-settings description "E-commerce backend for ACME Corp — Django REST API with PostgreSQL"`

Use Python to update the field:
```python
import json, sys
with open(sys.argv[1], 'r+') as f:
    d = json.load(f)
    d['description'] = sys.argv[2]
    f.seek(0); json.dump(d, f, indent=2); f.truncate()
```

### `retro_threshold <N>`

Update the `retro_threshold` field in the config JSON. Overrides the default
`session_window_external` (10, from `.claude/wardens/retrospective-schema.md`) used by
`branches/external-mode.md`'s "External retrospective" trigger to decide how many new session logs
must accumulate before an external retrospective fires for this project. `<N>` must be a positive
integer.

Use Python to update the field:
```python
import json, sys
with open(sys.argv[1], 'r+') as f:
    d = json.load(f)
    d['retro_threshold'] = int(sys.argv[2])
    f.seek(0); json.dump(d, f, indent=2); f.truncate()
```

### `delete`

First ask for explicit confirmation: "This will delete this project's config file only — memory level, summary settings, description, and any retro_threshold override. Claude Code's own session data at ~/.claude/projects/ is NOT affected — that's managed by Claude Code itself. External-retrospective opt-in and history are ALSO NOT affected — that lives in the vault at Retrospectives/external/<project-name>/, not this config file, and this command does not touch it (retrospectives will keep firing at the default threshold if you were opted in). To fully opt out of retrospectives too, tell the user to remove that vault directory by hand — this command deliberately does not do that automatically, since it would silently delete retrospective artifacts a user might still want. Type 'yes' to confirm."

Only proceed if the user responds with 'yes' (case-insensitive). Then delete the config file. Do not touch anything under `$VAULT/Retrospectives/` — that's a separate, deliberately-manual opt-out path, not part of this command's scope.

## Important

- The config file uses the MD5 hash of the current working directory's absolute path as filename
- Always use `umask 077` when writing config files (they may contain path information)
- Use Python to update JSON fields — never rewrite the whole file from scratch (would reset created_at)
- After modifying settings, confirm the change and remind the user the new settings take effect on the next session start
