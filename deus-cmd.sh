#!/bin/zsh
PLIST="$HOME/Library/LaunchAgents/com.deus.plist"
DEUS_PROJECTS_DIR="$HOME/.config/deus/projects"
DEUS_SKILLS_DIR="$HOME/.claude/skills"

# Resolve symlinks so SCRIPT_DIR always points to the repo, even when
# called via /usr/local/bin/deus → ~/deus/deus-cmd.sh symlink.
_resolve_script_dir() {
  local src="$0"
  while [ -L "$src" ]; do
    local dir="$(cd "$(dirname "$src")" && pwd)"
    src="$(readlink "$src")"
    [[ "$src" != /* ]] && src="$dir/$src"
  done
  echo "$(cd "$(dirname "$src")" && pwd)"
}
SCRIPT_DIR="$(_resolve_script_dir)"

# ─── Project Config Helpers ───
# Config stored at ~/.config/deus/projects/<md5-of-path>.json
# Outside both the project dir (no pollution) and the Deus repo (no cross-user leakage).

_project_config_path() {
  local dir_hash
  dir_hash=$(echo -n "$1" | md5 -q 2>/dev/null || echo -n "$1" | md5sum | cut -d' ' -f1)
  echo "$DEUS_PROJECTS_DIR/${dir_hash}.json"
}

_read_project_config() {
  local config_file
  config_file=$(_project_config_path "$1")
  [ -f "$config_file" ] && cat "$config_file" || echo ""
}

_write_project_config() {
  local dir="$1" level="$2" summaries="$3" description="$4"
  mkdir -p "$DEUS_PROJECTS_DIR"
  local config_file
  config_file=$(_project_config_path "$dir")
  local name
  name=$(basename "$dir")
  local now
  now=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  # description defaults to empty string if not provided
  description="${description:-}"
  (umask 077 && cat > "$config_file" <<PROJEOF
{
  "path": "$dir",
  "name": "$name",
  "description": "$description",
  "memory_level": "$level",
  "save_summaries": $summaries,
  "created_at": "$now",
  "last_accessed": "$now"
}
PROJEOF
  )
}

_update_project_access() {
  local config_file
  config_file=$(_project_config_path "$1")
  [ -f "$config_file" ] || return
  local now
  now=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  # Update last_accessed timestamp
  python3 -c "
import json, sys
with open(sys.argv[1], 'r+') as f:
    d = json.load(f)
    d['last_accessed'] = sys.argv[2]
    f.seek(0); json.dump(d, f, indent=2); f.truncate()
" "$config_file" "$now" 2>/dev/null
}

# ─── First-Run Onboarding ───

_run_onboarding() {
  local dir="$1"
  local name
  name=$(basename "$dir")
  echo ""
  echo "  Welcome to $name! First time here with Deus."
  echo ""
  echo "  How should I handle this project's data?"
  echo ""
  echo "  Memory level:"
  echo "    [F] Full      — Remember everything. Best for personal/open-source projects."
  echo "    [S] Standard  — Remember decisions & architecture, skip code details. (default)"
  echo "    [R] Restricted — Nothing persists between sessions. Best for NDA/client work."
  echo ""
  printf "  Choice [F/S/R]: "
  read -r choice
  case "$choice" in
    [Ff]) level="full" ;;
    [Rr]) level="restricted" ;;
    *)    level="standard" ;;
  esac

  local summaries="true"
  if [ "$level" = "restricted" ]; then
    summaries="false"
  else
    echo ""
    echo "  Save session summaries to your Deus vault?"
    echo "  (Contains topic + decisions, never code.)"
    printf "  [Y/n]: "
    read -r sum_choice
    case "$sum_choice" in
      [Nn]) summaries="false" ;;
      *)    summaries="true" ;;
    esac
  fi

  _write_project_config "$dir" "$level" "$summaries"
  echo ""
  echo "  Saved: memory=$level, summaries=$( [ "$summaries" = "true" ] && echo "on" || echo "off" )"
  echo "  Change anytime with /project-settings"
  echo ""
}

# ─── Install /resume skill (user-level, context-aware) ───

_ensure_resume_skill() {
  local skill_dir="$DEUS_SKILLS_DIR/resume"
  local marker="$skill_dir/.deus-version"
  local current_version="2"
  if [ -f "$marker" ] && [ "$(cat "$marker")" = "$current_version" ]; then
    return
  fi
  mkdir -p "$skill_dir"
  cat > "$skill_dir/skill.md" <<'SKILLEOF'
---
name: resume
description: Load context and catch up on recent work — adapts to home mode vs external project mode
user_invocable: true
---

# /resume

Context-aware session resume. Behavior depends on whether you're in the Deus home directory or an external project.

## Detect mode

Check if the current working directory is the Deus home directory (`~/deus`). If it is → **Home Mode**. Otherwise → **External Project Mode**.

## Home Mode (~/deus)

Load context from the vault before starting work.

First, resolve the vault path by reading `~/.config/deus/config.json` and using the `vault_path` value. If the env var `DEUS_VAULT_PATH` is set, use that instead. All paths below use `$VAULT` to mean this resolved path.

1. Always read core memory:
   $VAULT/CLAUDE.md

2. Based on likely task context, also read:
   - Study session → $VAULT/STUDY.md
   - Deus / tools / infra session → $VAULT/INFRA.md
   - If unclear → read both (they're small, ~10 lines each)

3. Check for a mid-session checkpoint from today:
   Run: find "$VAULT/Checkpoints" -name "$(date +%Y-%m-%d)-*.md" 2>/dev/null | xargs ls -t 2>/dev/null | head -1
   If a file is found → read it fully. Note "resuming mid-session checkpoint" in the summary.

4a. Load warm tier — recent sessions (no API cost):
    Run: python3 scripts/memory_indexer.py --recent 3
    This returns the 3 most recent session frontmatters by date. Include as "Recent Sessions" context.

    FALLBACK — if the script fails, fall back to:
    find "$VAULT/Session-Logs" -name "*.md" -not -path "*/.obsidian/*" | xargs ls -t 2>/dev/null | head -3
    Then read frontmatter only (lines between the two --- markers) of those 3 files.

4b. Load cold tier — semantically relevant older sessions:
    Formulate a 1-sentence query based on the loaded context from steps 1–3.
    Run: python3 scripts/memory_indexer.py --query "<your query>" --top 2 --recency-boost
    Include the output as additional context. Deduplicate: skip any session that already appeared in step 4a.
    If the script fails or returns nothing, skip silently — warm tier already provides continuity.

5. If a search term was passed as argument, grep session logs for it and read frontmatters of matches.

6. Summarize in 2–3 lines: ongoing context, pending tasks, ready to continue.
   If a checkpoint was loaded, prepend: "Resuming mid-session: [checkpoint next_action]"

## External Project Mode

Resume work on the current external project by gathering project-specific context.

### Step 1 — Project config

Compute MD5 hash of the current working directory and read `~/.config/deus/projects/<hash>.json`.
Note the memory level. If restricted, skip steps that involve reading saved memory.

### Step 2 — Git context (always, regardless of memory level)

Run these commands and present the results:

```bash
# Current branch and status
git branch --show-current
git status --short

# Recent commits on this branch (last 10)
git log --oneline -10

# Any stashed work
git stash list 2>/dev/null | head -3

# Open branches with recent activity
git branch --sort=-committerdate --format='%(refname:short) (%(committerdate:relative))' | head -5
```

### Step 3 — Open PRs (if gh is available)

```bash
gh pr list --limit 5 2>/dev/null
```

If gh fails or isn't installed, skip silently.

### Step 4 — Claude auto-memory (standard/full only)

Check if Claude Code has auto-memory for this project:
- Compute the project path encoding (replace / with - , prepend -)
- Check `~/.claude/projects/<encoded-path>/memory/MEMORY.md`
- If it exists, read it and note any saved context

### Step 5 — Project CLAUDE.md

Read the project's own CLAUDE.md if it exists (at the repo root). This contains project-specific instructions and context.

### Step 6 — Summarize

Present a concise project status:

```
Project: <name> (<branch>)
Memory: <level> | Last session: <date from config>

Recent activity:
• <1-2 lines about recent commits>
• <uncommitted changes if any>
• <open PRs if any>

<any auto-memory context, 2-3 lines max>
```

Then: "What would you like to work on?"
SKILLEOF
  echo "$current_version" > "$marker"
}

# ─── Install /project-settings skill ───

_ensure_project_settings_skill() {
  local skill_dir="$DEUS_SKILLS_DIR/project-settings"
  # Only install if missing or outdated
  local marker="$skill_dir/.deus-version"
  local current_version="2"
  if [ -f "$marker" ] && [ "$(cat "$marker")" = "$current_version" ]; then
    return
  fi
  mkdir -p "$skill_dir"
  cat > "$skill_dir/skill.md" <<'SKILLEOF'
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
Created: <date>
Last accessed: <date>
```

Then show available commands:
- `/project-settings memory full|standard|restricted` — change memory level
- `/project-settings summaries on|off` — toggle session summaries
- `/project-settings description <text>` — set a short project description Deus uses as context
- `/project-settings delete` — delete all Deus data for this project

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

### `delete`

First ask for explicit confirmation: "This will delete all Deus tracking data for this project (memory level, summary settings, description). Claude Code's own session data at ~/.claude/projects/ is NOT affected — that's managed by Claude Code itself. Type 'yes' to confirm."

Only proceed if the user responds with 'yes' (case-insensitive). Then delete the config file.

## Important

- The config file uses the MD5 hash of the current working directory's absolute path as filename
- Always use `umask 077` when writing config files (they may contain path information)
- Use Python to update JSON fields — never rewrite the whole file from scratch (would reset created_at)
- After modifying settings, confirm the change and remind the user the new settings take effect on the next session start
SKILLEOF
  echo "$current_version" > "$marker"
}

# ─── Install /checkpoint skill (user-level, context-aware) ───

_ensure_checkpoint_skill() {
  local skill_dir="$DEUS_SKILLS_DIR/checkpoint"
  local marker="$skill_dir/.deus-version"
  local current_version="1"
  if [ -f "$marker" ] && [ "$(cat "$marker")" = "$current_version" ]; then
    return
  fi
  mkdir -p "$skill_dir"
  cat > "$skill_dir/skill.md" <<'SKILLEOF'
---
name: checkpoint
description: Save a mid-session checkpoint — for continuity between sessions of the same day
user_invocable: true
---

# /checkpoint

Context-aware mid-session checkpoint. Behavior adapts to home mode vs external project mode.

## Detect mode

Check if the current working directory is the Deus home directory (`~/deus`). If it is → **Home Mode**. Otherwise → **External Project Mode**.

## Resolve vault path

Read `~/.config/deus/config.json` and use the `vault_path` value. If the env var `DEUS_VAULT_PATH` is set, use that instead. All paths below use `$VAULT` to mean this resolved path.

## Check memory level (External Project Mode only)

Compute MD5 hash of the current working directory and read `~/.config/deus/projects/<hash>.json`.
- If memory level is **restricted**: tell the user "Checkpoints are disabled for restricted projects (nothing persists between sessions)." and stop.
- If memory level is **standard** or **full**: proceed normally.
- Home mode: always proceed.

## Step 1 — Write checkpoint

Identify: what decisions or intermediate conclusions have been reached in this session that are NOT yet saved in a session log?

Write to:
$VAULT/Checkpoints/YYYY-MM-DD-HH.md
(Use current date and 24h hour. Create the Checkpoints/ folder if it doesn't exist.)

Use exactly this format:
```markdown
---
type: checkpoint
created: YYYY-MM-DDTHH:MM
session_topic: short-slug
project_path: "<working directory path, or '~/deus' for home mode>"
decisions:
  - "decision made so far (≤12 words)"
in_progress:
  - "what we are actively working on right now"
next_action: "the exact next step to take after resuming"
context_refs:
  - "file path or resource name needed to continue"
---

## Mid-Session State
3–5 sentences explaining where we are, what has been decided, and what comes next.
Write as if explaining to yourself after a 30-minute break.
```

**External Project Mode additions:**
- Always include `project_path` in frontmatter
- In context_refs, include project-relative paths (not absolute)
- If memory level is **standard**: do NOT include specific code snippets, file contents, or implementation details in the Mid-Session State — focus on decisions and what was tried

Keep the checkpoint under 25 lines total. This is what /resume will load on the next session if it's the same day.

## Step 2 — Confirm the checkpoint path was written.

## Step 3 — Output context primer.

Before running /compact, output the following block verbatim (filling in values from current session state). This is a "compaction seed" — structured content near the end of conversation that the compaction algorithm will preserve as high-signal.

```
---BEGIN CONTEXT PRIMER---
## Active Task
[1 sentence: what we are working on right now]

## Session Decisions
[Bulleted list: decisions made in THIS session, max 5]

## Key Files
[Bulleted list: file paths actively being modified or referenced]

## Pending
[Bulleted list: what still needs to be done, max 3 items]

## Resume Hint
[1 sentence: if resuming after compaction, start by doing X]
---END CONTEXT PRIMER---
```

## Step 4 — Tell the user: "Checkpoint saved. Run /compact now to compact the context."
SKILLEOF
  echo "$current_version" > "$marker"
}

# ─── Install /compress skill (user-level, context-aware) ───

_ensure_compress_skill() {
  local skill_dir="$DEUS_SKILLS_DIR/compress"
  local marker="$skill_dir/.deus-version"
  local current_version="2"
  if [ -f "$marker" ] && [ "$(cat "$marker")" = "$current_version" ]; then
    return
  fi
  mkdir -p "$skill_dir"
  cat > "$skill_dir/skill.md" <<'SKILLEOF'
---
name: compress
description: Save this session to the vault and update the semantic memory index
user_invocable: true
---

# /compress

Context-aware session saving. Behavior adapts to home mode vs external project mode.

## Detect mode

Check if the current working directory is the Deus home directory (`~/deus`). If it is → **Home Mode**. Otherwise → **External Project Mode**.

## Resolve vault path

Read `~/.config/deus/config.json` and use the `vault_path` value. If the env var `DEUS_VAULT_PATH` is set, use that instead. All paths below use `$VAULT` to mean this resolved path.

## Check memory level (External Project Mode only)

Compute MD5 hash of the current working directory and read `~/.config/deus/projects/<hash>.json`.
- If memory level is **restricted**: tell the user "Session saving is disabled for restricted projects. Your work is preserved in git commits and Claude Code's native session transcript. Use /project-settings to change this." and stop.
- If `save_summaries` is **false**: tell the user the same message and stop.
- If memory level is **standard** or **full** with summaries enabled: proceed.
- Home mode: always proceed.

## Save session log

Review the conversation and create a session log at:
$VAULT/Session-Logs/YYYY-MM-DD/{topic}.md

Create the YYYY-MM-DD folder if it doesn't exist. The filename should be the topic only (no date prefix), since the date is already in the folder name.

Use this format:
```markdown
---
type: session
date: YYYY-MM-DD
topics: [topic1, topic2]
project_path: "<working directory path, or '~/deus' for home mode>"
tldr: |
  What happened (1 sentence). Key decision or outcome. Pending: X, Y.
decisions:
  - "chose X over Y: brief reason"
  - "rejected approach A: brief reason"
---

<!-- Full details — only loaded on demand -->

## Decisions Made
- ...

## Key Learnings
- ...

## Files Modified
- ...

## Pending Tasks
- [ ] ...
```

**External Project Mode — standard memory level redaction:**
- Do NOT include specific file paths, function names, or code snippets in the session log
- Focus on decisions, architecture, and what was tried/learned
- Files Modified section should use descriptions ("updated the auth middleware") not paths
- The goal: someone reading this log should understand WHAT was decided and WHY, without leaking code details

**External Project Mode — full memory level:**
- No redaction needed — include full details as in home mode

Rules for `decisions:` array:
- Maximum 3 items. Only include decisions that affect future sessions.
- Each item: quoted string, verb-first, ≤12 words.
- Omit the key entirely if no future-relevant decisions were made.

Keep `tldr` to 2–3 lines. Skip sections with no content.

## Post-save steps

After saving the session log:

1. **Update vault CLAUDE.md** (home mode only):
   Update the `pending:` block in $VAULT/CLAUDE.md

2. **Auto-redact sensitive patterns** (External Project Mode, standard memory level only):
   After saving the file, run the redaction script to strip any code snippets or file contents that leaked through:
   ```bash
   python3 ~/deus/scripts/redact_session.py "<full path to saved log>"
   ```
   Only run this step when memory level is `standard` (not `full` or `restricted`).
   If the script fails, skip silently — the log is still saved; instruct the user to review it manually.

3. **Index the session log** (always, if scripts are available):
   Run: `python3 ~/deus/scripts/memory_indexer.py --add "<full path to saved log>"`
   If the script fails, skip silently — the log is still saved.

4. **Extract atomic facts** (always, if scripts are available):
   Run: `python3 ~/deus/scripts/memory_indexer.py --extract "<full path to saved log>"`
   If the script fails, skip silently.

5. **Delete today's checkpoint** (always):
   Run: `find "$VAULT/Checkpoints" -name "$(date +%Y-%m-%d)-*.md" -delete 2>/dev/null`

6. **Pre-warm semantic cache** (always, background):
   Run: `python3 ~/deus/scripts/memory_indexer.py --query "recent work ongoing tasks" --top 2 --recency-boost > ~/.deus/resume_semantic_cache.txt 2>/dev/null &`

Confirm with the filename saved, number of pending tasks carried forward, redaction result (standard mode only), indexing result, and atom extraction result.
SKILLEOF
  echo "$current_version" > "$marker"
}

# ─── Install /preserve skill (user-level, context-aware) ───

_ensure_preserve_skill() {
  local skill_dir="$DEUS_SKILLS_DIR/preserve"
  local marker="$skill_dir/.deus-version"
  local current_version="1"
  if [ -f "$marker" ] && [ "$(cat "$marker")" = "$current_version" ]; then
    return
  fi
  mkdir -p "$skill_dir"
  cat > "$skill_dir/skill.md" <<'SKILLEOF'
---
name: preserve
description: Scan this conversation and silently save anything worth permanent memory
user_invocable: true
---

# /preserve

Context-aware memory preservation. Behavior adapts to home mode vs external project mode.

## Detect mode

Check if the current working directory is the Deus home directory (`~/deus`). If it is → **Home Mode**. Otherwise → **External Project Mode**.

## Resolve vault path

Read `~/.config/deus/config.json` and use the `vault_path` value. If the env var `DEUS_VAULT_PATH` is set, use that instead. All paths below use `$VAULT` to mean this resolved path.

## Check memory level (External Project Mode only)

Compute MD5 hash of the current working directory and read `~/.config/deus/projects/<hash>.json`.
- If memory level is **restricted**: tell the user "Memory preservation is disabled for restricted projects." and stop.
- If memory level is **standard** or **full**: proceed, but with different scopes (see below).
- Home mode: always proceed.

## What to preserve

Scan the conversation for:
- Preferences or habits the user revealed
- Decisions made with lasting effect
- Things the user corrected or clarified
- Facts worth knowing in future sessions

Do not preserve one-off requests or temporary context.

**External Project Mode — standard:**
Only preserve USER preferences and behavioral corrections — things that are about the user, not the project. Skip project-specific architecture decisions, code patterns, or team info (those belong in Claude Code's auto-memory, not the vault).

**External Project Mode — full:**
Preserve both user preferences AND project-relevant decisions. Include project context where it helps future sessions.

## Where to save

Save findings to: $VAULT/CLAUDE.md

Add findings using the same compact key:value format as the file — no prose bullets.
One line per insight.

If CLAUDE.md exceeds 200 lines, archive old content to:
$VAULT/CLAUDE-Archive.md

Confirm briefly what was added, or say nothing was worth preserving if nothing qualified.
SKILLEOF
  echo "$current_version" > "$marker"
}

_ensure_preferences_skill() {
  local skill_dir="$DEUS_SKILLS_DIR/preferences"
  local marker="$skill_dir/.deus-version"
  local current_version="1"
  if [ -f "$marker" ] && [ "$(cat "$marker")" = "$current_version" ]; then
    return
  fi
  mkdir -p "$skill_dir"
  cat > "$skill_dir/skill.md" <<'SKILLEOF'
---
name: preferences
description: View or modify Deus user preferences (name, catch-me-up behavior, bypass permissions, persona)
user_invocable: true
---

# /preferences

Manage Deus user preferences. These are stored in `~/.config/deus/config.json` alongside the vault path.

## Config location

```bash
~/.config/deus/config.json
```

## Available preferences

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `name` | string | `""` | User's name - Deus uses it in greetings and context |
| `catch_me_up` | boolean | `true` | Auto "Catch me up" greeting when launching in home mode |
| `bypass_permissions` | boolean | `true` | Try `--dangerously-skip-permissions` on launch (falls back to normal if declined) |
| `persona` | string | `""` | Free-text personality/behavior instructions appended to Deus identity |

## When invoked with no arguments or `show`

Read `~/.config/deus/config.json` and display current preferences:

```
Deus Preferences
  name:               <value or "(not set)">
  catch_me_up:        <on|off> (auto-greet with session summary on launch)
  bypass_permissions:  <on|off> (skip permission prompts on launch)
  persona:            <value or "(not set)">

Set with: /preferences <key> <value>
Examples:
  /preferences name "Alex"
  /preferences catch_me_up off
  /preferences persona "Always respond in a casual tone"
```

## When invoked with arguments

Parse the first argument as the key and the rest as the value.

- For boolean keys (`catch_me_up`, `bypass_permissions`): accept `on/off`, `true/false`, `yes/no`
- For string keys (`name`, `persona`): accept the remaining text as the value
- To clear a string value: `/preferences name ""` or `/preferences persona clear`

### Implementation

Use Python to update the JSON file in-place (preserves other keys like `vault_path`):

```bash
python3 -c "
import json, sys
from pathlib import Path
p = Path('~/.config/deus/config.json').expanduser()
d = json.loads(p.read_text()) if p.exists() else {}
key, val = sys.argv[1], sys.argv[2]
if key in ('catch_me_up', 'bypass_permissions'):
    d[key] = val.lower() in ('true', 'on', 'yes', '1')
else:
    d[key] = '' if val.lower() in ('clear', '\"\"', \"''\") else val
p.write_text(json.dumps(d, indent=2))
print(f'Set {key} = {d[key]}')
" "<key>" "<value>"
```

Confirm the change and note that it takes effect on next `deus` launch (not mid-session).

## Validation

- Only allow the 4 documented keys. Reject unknown keys with a helpful message.
- `persona` can be any text. Warn if it exceeds 500 characters (but still save it).
SKILLEOF
  echo "$current_version" > "$marker"
}

case "$1" in
  auth)
    # Validate credentials file is readable before restarting
    python3 -c 'import sys,json; d=json.load(open(sys.argv[1])); assert d.get("claudeAiOauth",{}).get("accessToken")' "$HOME/.claude/.credentials.json" 2>/dev/null
    if [ $? -ne 0 ]; then
      echo "Error: could not read token from ~/.claude/.credentials.json"
      exit 1
    fi
    # Do NOT write token to .env — the credential proxy reads credentials.json
    # directly via getDynamicOAuthToken() with a 5-min cache. Writing to .env
    # would permanently freeze the token and cause a login loop on next refresh.
    #
    # Always rebuild before restarting — prevents silent dist/src drift where
    # a source fix is present but the running binary is stale (root cause of
    # the login loop regression: fix was in src but never compiled into dist).
    printf "  Building...\r"
    (cd "$SCRIPT_DIR" && npm run build --silent) || { echo "Build failed — not restarting."; exit 1; }
    # Re-create CLI symlink — catches repo moves/renames
    LINK_DIR="$HOME/.local/bin"
    mkdir -p "$LINK_DIR"
    ln -sf "$SCRIPT_DIR/deus-cmd.sh" "$LINK_DIR/deus"
    launchctl kickstart -k "gui/$(id -u)/com.deus" 2>/dev/null
    echo "Deus built and restarted (CLI symlink refreshed)."
    ;;
  home|web|"")
    # `deus web` launches with --chrome for Claude-in-Chrome browser integration.
    # Otherwise identical to bare `deus` / `deus home`.
    CHROME_FLAG=""
    [ "$1" = "web" ] && CHROME_FLAG="--chrome"
    TOKEN=$(python3 -c 'import sys,json; print(json.load(open(sys.argv[1]))["claudeAiOauth"]["accessToken"])' "$HOME/.claude/.credentials.json" 2>/dev/null)
    if [ -z "$TOKEN" ]; then
      echo "Error: could not read token from ~/.claude/.credentials.json"
      exit 1
    fi
    # Do NOT export CLAUDE_CODE_OAUTH_TOKEN — the Claude CLI reads
    # ~/.claude/.credentials.json directly and auto-refreshes on /login.
    # Exporting a frozen token causes 401s after token rotation because
    # the CLI prioritizes the env var over the credentials file.
    launchctl kickstart -k "gui/$(id -u)/com.deus" 2>/dev/null
    # Launch claude with bypass mode; fall back to normal mode if user declines
    launch_claude() {
      claude $CHROME_FLAG --dangerously-skip-permissions "$@"
      if [ $? -ne 0 ]; then
        claude $CHROME_FLAG "$@"
      fi
    }

    # Resolve vault path from config (DEUS_VAULT_PATH env var → ~/.config/deus/config.json)
    VAULT="${DEUS_VAULT_PATH:-$(python3 -c "import json; from pathlib import Path; print(json.loads(Path('~/.config/deus/config.json').expanduser().read_text()).get('vault_path',''))" 2>/dev/null)}"

    # Resolve from DEUS_HOME env var → script's own directory → fallback $HOME/deus
    DEUS_HOME="${DEUS_HOME:-$(cd "$(dirname "$0")" && pwd)}"
    # "deus home" forces home mode regardless of cwd
    if [ "$1" = "home" ]; then
      CURRENT_DIR="$DEUS_HOME"
    else
      CURRENT_DIR="$(pwd)"
    fi

    # ─── LOAD USER PREFERENCES ───
    PREFS_NAME=""
    PREFS_CATCH_ME_UP="true"
    PREFS_BYPASS="true"
    PREFS_PERSONA=""
    if [ -f "$HOME/.config/deus/config.json" ]; then
      PREFS_NAME=$(python3 -c "import json; from pathlib import Path; print(json.loads(Path('~/.config/deus/config.json').expanduser().read_text()).get('name',''))" 2>/dev/null)
      PREFS_CATCH_ME_UP=$(python3 -c "import json; from pathlib import Path; d=json.loads(Path('~/.config/deus/config.json').expanduser().read_text()); print(str(d.get('catch_me_up',True)).lower())" 2>/dev/null)
      PREFS_BYPASS=$(python3 -c "import json; from pathlib import Path; d=json.loads(Path('~/.config/deus/config.json').expanduser().read_text()); print(str(d.get('bypass_permissions',True)).lower())" 2>/dev/null)
      PREFS_PERSONA=$(python3 -c "import json; from pathlib import Path; print(json.loads(Path('~/.config/deus/config.json').expanduser().read_text()).get('persona',''))" 2>/dev/null)
    fi

    # Override launch_claude based on bypass preference
    if [ "$PREFS_BYPASS" = "false" ]; then
      launch_claude() {
        claude $CHROME_FLAG "$@"
      }
    fi

    # ─── DEUS IDENTITY (always present, even without vault) ───
    DEUS_IDENTITY="You are Deus - the user's personal AI assistant. You are not a generic coding tool. You collaborate on everything: coding, studies, life decisions, recommendations, brainstorming, and anything the user brings to you.

Key capabilities you have:
- Memory: you remember context across conversations. If a vault is configured, you have access to session logs, preferences, and project history.
- Channels: WhatsApp, Telegram, Slack, Discord, Gmail - the user may talk to you through any of these.
- Vision and voice: you can see images and transcribe voice messages.
- Calendar: you can read and create Google Calendar events.
- Self-improvement: you score your own responses and learn from both successes and failures over time.

Your personality:
- Concise and direct. No filler, no fluff.
- You run commands directly - never ask the user to run things manually.
- You prefer long-term scalable solutions over quick fixes.
- Security-conscious: never commit credentials, design as if the repo is public.

This repo (~/deus) is the infrastructure that powers you. See README.md for philosophy and CLAUDE.md for development rules."

    # Inject user name and persona into identity
    if [ -n "$PREFS_NAME" ]; then
      DEUS_IDENTITY="$DEUS_IDENTITY

The user's name is $PREFS_NAME."
    fi
    if [ -n "$PREFS_PERSONA" ]; then
      DEUS_IDENTITY="$DEUS_IDENTITY

Additional instructions from the user: $PREFS_PERSONA"
    fi

    # ─── SHARED CONTEXT LOADING ───
    # Full vault + memory + sessions loaded identically regardless of mode.
    # The only difference between home mode and external project mode is
    # the working directory and the startup instruction.
    if [ -z "$VAULT" ]; then
      echo "Warning: No vault configured. Set DEUS_VAULT_PATH or vault_path in ~/.config/deus/config.json"
      if [ "$CURRENT_DIR" != "$DEUS_HOME" ]; then
        launch_claude --append-system-prompt "$DEUS_IDENTITY"
      else
        cd "$HOME/deus" && launch_claude --append-system-prompt "$DEUS_IDENTITY"
      fi
    fi
    CONTEXT=""

    printf "  Reading vault...\r"
    CLAUDE_MD=$(cat "$VAULT/CLAUDE.md" 2>/dev/null)
    STUDY_MD=$(cat "$VAULT/STUDY.md" 2>/dev/null)
    INFRA_MD=$(cat "$VAULT/INFRA.md" 2>/dev/null)
    [ -n "$CLAUDE_MD" ] && CONTEXT="=== VAULT: CLAUDE.md ===\n$CLAUDE_MD"
    [ -n "$STUDY_MD" ]  && CONTEXT="$CONTEXT\n\n=== VAULT: STUDY.md ===\n$STUDY_MD"
    [ -n "$INFRA_MD" ]  && CONTEXT="$CONTEXT\n\n=== VAULT: INFRA.md ===\n$INFRA_MD"

    # Memory tree (Phase 4, gated by DEUS_MEMORY_TREE=1 during dogfood).
    if [ "${DEUS_MEMORY_TREE:-0}" = "1" ]; then
      MEMORY_TREE_MD=$(cat "$VAULT/MEMORY_TREE.md" 2>/dev/null)
      if [ -n "$MEMORY_TREE_MD" ]; then
        CONTEXT="$CONTEXT\n\n=== VAULT: MEMORY_TREE.md ===\n$MEMORY_TREE_MD\n\n=== MEMORY TREE USAGE ===\nFor factual personal questions (identity, household, preferences, cross-branch), call:\n  python3 \$HOME/deus/scripts/memory_tree.py query \"<question>\"\nThe top result's path is the vault file to Read. On abstained:true or low confidence, fall back to Persona/INDEX.md. Prefer this over guessing from CLAUDE.md hints."
      fi
    fi

    printf "  Checking checkpoints...\r"
    CHECKPOINT_FILE=$(find "$VAULT/Checkpoints" -name "$(date +%Y-%m-%d)-*.md" 2>/dev/null | xargs ls -t 2>/dev/null | head -1)
    if [ -n "$CHECKPOINT_FILE" ]; then
      CHECKPOINT=$(cat "$CHECKPOINT_FILE" 2>/dev/null)
      [ -n "$CHECKPOINT" ] && CONTEXT="$CONTEXT\n\n=== MID-SESSION CHECKPOINT ===\n$CHECKPOINT"
    fi

    printf "  Loading recent sessions...\r"
    RECENT=$(python3 "$HOME/deus/scripts/memory_indexer.py" --recent 3 2>/dev/null)
    [ -n "$RECENT" ] && CONTEXT="$CONTEXT\n\n=== RECENT SESSIONS ===\n$RECENT"

    SEMANTIC_CACHE="$HOME/.deus/resume_semantic_cache.txt"
    SEMANTIC_TTL=14400  # 4 hours
    SEMANTIC=""
    USE_CACHE=false
    if [ -f "$SEMANTIC_CACHE" ]; then
      CACHE_AGE=$(( $(date +%s) - $(stat -f %m "$SEMANTIC_CACHE") ))
      [ "$CACHE_AGE" -lt "$SEMANTIC_TTL" ] && USE_CACHE=true
    fi
    if $USE_CACHE; then
      printf "  Recalling relevant sessions...\r"
      SEMANTIC=$(cat "$SEMANTIC_CACHE" 2>/dev/null)
    else
      printf "  Retrieving relevant context...\r"
      SEMANTIC=$(python3 "$HOME/deus/scripts/memory_indexer.py" --query "recent work ongoing tasks" --top 2 --recency-boost 2>/dev/null)
      [ -n "$SEMANTIC" ] && echo "$SEMANTIC" > "$SEMANTIC_CACHE"
    fi
    [ -n "$SEMANTIC" ] && CONTEXT="$CONTEXT\n\n=== RELATED SESSIONS ===\n$SEMANTIC"

    printf "✓ Ready.                        \n"

    # ─── EXTERNAL PROJECT MODE ───
    # Same full Deus brain, different working directory and startup.
    # Memory level controls how much project data persists between sessions.
    if [ "$CURRENT_DIR" != "$DEUS_HOME" ]; then

      # Ensure skills are installed
      _ensure_project_settings_skill
      _ensure_resume_skill
      _ensure_checkpoint_skill
      _ensure_compress_skill
      _ensure_preserve_skill
      _ensure_preferences_skill

      # Check for existing project config or run onboarding
      PROJECT_CONFIG=$(_read_project_config "$CURRENT_DIR")
      JUST_ONBOARDED="false"
      if [ -z "$PROJECT_CONFIG" ]; then
        _run_onboarding "$CURRENT_DIR"
        PROJECT_CONFIG=$(_read_project_config "$CURRENT_DIR")
        JUST_ONBOARDED="true"
      else
        _update_project_access "$CURRENT_DIR"
      fi

      # Parse memory level and summaries from config
      MEMORY_LEVEL=$(echo "$PROJECT_CONFIG" | python3 -c "import sys,json; print(json.load(sys.stdin).get('memory_level','standard'))" 2>/dev/null)
      [ -z "$MEMORY_LEVEL" ] && MEMORY_LEVEL="standard"

      # Build env vars based on memory level
      EXTRA_ENV=""
      if [ "$MEMORY_LEVEL" = "restricted" ]; then
        EXTRA_ENV="CLAUDE_CODE_DISABLE_AUTO_MEMORY=1"
      fi

      # Build memory-level-specific system prompt instructions
      MEMORY_INSTRUCTION=""
      case "$MEMORY_LEVEL" in
        full)
          MEMORY_INSTRUCTION="Memory level: FULL. You may remember anything about this project freely — architecture, decisions, code patterns, team context. Treat this project as part of your core working memory." ;;
        standard)
          MEMORY_INSTRUCTION="Memory level: STANDARD. Remember architectural decisions, team context, project conventions, and what was tried/researched. Do NOT memorize specific code contents, file paths, line numbers, or implementation details — read those fresh each session. When saving to memory, focus on the 'what and why' not the 'where and how'." ;;
        restricted)
          MEMORY_INSTRUCTION="Memory level: RESTRICTED. This is a privacy-sensitive project. Do NOT save any project-specific information to memory. Each session starts fresh. Do not reference prior sessions or accumulated knowledge about this codebase. Auto-memory is disabled." ;;
      esac

      # Gather git context for returning users (lightweight, always safe)
      GIT_CONTEXT=""
      if [ -d "$CURRENT_DIR/.git" ] || git -C "$CURRENT_DIR" rev-parse --git-dir >/dev/null 2>&1; then
        printf "  Gathering project context...\r"
        GIT_BRANCH=$(git -C "$CURRENT_DIR" branch --show-current 2>/dev/null)
        GIT_STATUS=$(git -C "$CURRENT_DIR" status --short 2>/dev/null | head -20)
        GIT_LOG=$(git -C "$CURRENT_DIR" log --oneline -8 2>/dev/null)
        GIT_STASH=$(git -C "$CURRENT_DIR" stash list 2>/dev/null | head -3)
        GIT_CONTEXT="=== PROJECT GIT STATE ===
Branch: ${GIT_BRANCH:-detached}
Recent commits:
${GIT_LOG:-  (no commits)}
$([ -n "$GIT_STATUS" ] && echo "
Uncommitted changes:
$GIT_STATUS")$([ -n "$GIT_STASH" ] && echo "
Stashed work:
$GIT_STASH")"
      fi

      # Determine if this is a first-run or returning session
      IS_RETURNING="false"
      if [ "$JUST_ONBOARDED" = "false" ]; then
        IS_RETURNING="true"
      fi

      if [ "$IS_RETURNING" = "true" ]; then
        STARTUP_GREETING="Greet the user with a brief project status based on the git state provided above. Format:

Project: <name> (<branch>) | Memory: $MEMORY_LEVEL
• <1-2 lines about recent commits or uncommitted changes>

Then ask what they'd like to work on. Use /resume for a deeper context reload."
      else
        STARTUP_GREETING="Greet the user briefly: identify the project (from CLAUDE.md, package.json, or directory name), state the memory level ($MEMORY_LEVEL), and wait for instructions."
      fi

      STARTUP_INSTRUCTION="STARTUP INSTRUCTION: You are Deus, operating in EXTERNAL PROJECT MODE. The current directory is an external codebase at $CURRENT_DIR — not the Deus project. You have your full memory, preferences, and capabilities. Focus on this codebase while applying all your behavioral rules and knowledge. The project may have its own CLAUDE.md — follow it alongside yours.

$MEMORY_INSTRUCTION

Available commands: /resume (deep context reload) | /checkpoint (save mid-session state) | /compress (save session to vault) | /preserve (save lasting insights) | /project-settings (data handling)

$GIT_CONTEXT

$STARTUP_GREETING"

      # Launch claude with appropriate env vars
      if [ -n "$EXTRA_ENV" ]; then
        export $EXTRA_ENV
      fi

      if [ -n "$CONTEXT" ]; then
        launch_claude --append-system-prompt "$(printf '%s' "$CONTEXT")

$STARTUP_INSTRUCTION"
      else
        launch_claude --append-system-prompt "$STARTUP_INSTRUCTION"
      fi
    fi

    # ─── HOME MODE ───
    # Ensure skills are available globally (home mode uses project commands,
    # but the skills provide external-project-aware versions for other dirs)
    _ensure_resume_skill
    _ensure_checkpoint_skill
    _ensure_compress_skill
    _ensure_preserve_skill
    _ensure_preferences_skill

    # Running from ~/deus — full startup with optional catch-me-up greeting.
    if [ "$PREFS_CATCH_ME_UP" = "false" ]; then
      STARTUP_INSTRUCTION="STARTUP INSTRUCTION: Context from the memory vault has been pre-loaded above. Wait for the user's instructions."
      if [ -n "$CONTEXT" ]; then
        cd "$HOME/deus" && launch_claude --append-system-prompt "$(printf '%s' "$CONTEXT")

$STARTUP_INSTRUCTION"
      else
        cd "$HOME/deus" && launch_claude
      fi
    else
      STARTUP_INSTRUCTION="STARTUP INSTRUCTION: Context from the memory vault has been pre-loaded above, BUT it is a snapshot taken at deus launch and does not refresh across /clear or same-session work. Before drafting the catch-up, verify freshness:

  1. ls -t \"$VAULT/Checkpoints\" | head -3
  2. ls -t \"$VAULT/Session-Logs/$(date +%Y-%m-%d)\" 2>/dev/null
  3. If anything on disk is newer than the newest date in the === RECENT SESSIONS === block, re-run: python3 \$HOME/deus/scripts/memory_indexer.py --recent 3
     and lead the catch-up from that output plus the newest same-day checkpoint's next_action / in_progress fields. Ignore the stale pre-loaded block.
  4. If disk matches the block, the snapshot is fresh — use it.

Then catch the user up using exactly this format:

• Previous session: [1-2 lines of ongoing context and last session topic]
• Pending: [bullet list of pending tasks, max 3 items]

Then stop and wait for the user."

      if [ -n "$CONTEXT" ]; then
        cd "$HOME/deus" && launch_claude --append-system-prompt "$(printf '%s' "$CONTEXT")

$STARTUP_INSTRUCTION" "Catch me up."
      else
        cd "$HOME/deus" && launch_claude
      fi
    fi
    ;;
  listen)
    # Record from mic, transcribe with whisper.cpp, copy to clipboard.
    # Phase 2+: Node.js with live VU meter. Use --stream for continuous dictation.
    shift
    exec node "$SCRIPT_DIR/dist/deus-listen.js" "$@"
    ;;
  logs)
    # Log review, rotation, and health reporting.
    shift
    case "$1" in
      summary)  exec python3 "$HOME/deus/scripts/log_review.py" --summary ;;
      pinned)   exec python3 "$HOME/deus/scripts/log_review.py" --pinned ;;
      rotate)   exec python3 "$HOME/deus/scripts/log_review.py" --rotate-only ;;
      review)   exec python3 "$HOME/deus/scripts/log_review.py" --review-only ;;
      "")       exec python3 "$HOME/deus/scripts/log_review.py" ;;
      *)
        echo "Usage: deus logs [summary|pinned|rotate|review]"
        echo ""
        echo "  deus logs           Rotate old logs + run Ollama health review"
        echo "  deus logs summary   Print last saved daily report"
        echo "  deus logs pinned    Print pinned issues needing attention"
        echo "  deus logs rotate    Rotate old logs only (no review)"
        echo "  deus logs review    Run health review only (no rotation)"
        ;;
    esac
    ;;
  *)
    echo "Usage: deus [home|auth|web|listen|logs]"
    echo ""
    echo "  deus        Launch in current directory (external project mode if not ~/deus)"
    echo "  deus home   Launch in home mode (~/deus) regardless of current directory"
    echo "  deus auth   Restart background services (credential proxy auto-reads ~/.claude/.credentials.json)"
    echo "  deus web    Same as 'deus' but launches claude with --chrome (Claude-in-Chrome integration)"
    echo "  deus listen Record from mic, transcribe, and copy to clipboard"
    echo "  deus logs   Review system health logs (rotate|review|summary|pinned)"
    ;;
esac
