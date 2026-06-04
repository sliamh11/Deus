#!/bin/zsh
PLIST="$HOME/Library/LaunchAgents/com.deus.plist"
DEUS_PROJECTS_DIR="$HOME/.config/deus/projects"
DEUS_SKILLS_DIR="$HOME/.claude/skills"

# Resolve symlinks so SCRIPT_DIR always points to the repo, even when
# called via /usr/local/bin/deus → ~/deus/deus-cmd.sh symlink.
# Seed from $ZSH_ARGZERO, not $0: inside a zsh function $0 is the function name
# (FUNCTION_ARGZERO, on by default), so $0 here would be "_resolve_script_dir"
# and SCRIPT_DIR would collapse to cwd from any foreign directory — breaking
# subcommands like `deus init` that run from inside another project.
# $ZSH_ARGZERO holds the real argv[0] (the path/symlink used to invoke).
_resolve_script_dir() {
  local src="${ZSH_ARGZERO:-$0}"
  while [ -L "$src" ]; do
    local dir="$(cd "$(dirname "$src")" && pwd)"
    src="$(readlink "$src")"
    [[ "$src" != /* ]] && src="$dir/$src"
  done
  echo "$(cd "$(dirname "$src")" && pwd)"
}
SCRIPT_DIR="$(_resolve_script_dir)"

# Prefix selection changes both the foreground CLI and runtime backend for this
# invocation. Plain `deus` still defaults to Claude unless env/config says
# otherwise.
if [ "$1" = "codex" ] || [ "$1" = "claude" ] || [ "$1" = "fcc" ]; then
  if [ "$1" = "claude" ]; then
    export DEUS_CLI_AGENT="claude"
    export DEUS_AGENT_BACKEND="claude"
  elif [ "$1" = "fcc" ]; then
    export DEUS_CLI_AGENT="fcc"
    export DEUS_AGENT_BACKEND="claude"
  else
    export DEUS_CLI_AGENT="codex"
    export DEUS_AGENT_BACKEND="openai"
  fi
  shift
fi

_read_config_key() {
  python3 -c "
import json; from pathlib import Path
p = Path('~/.config/deus/config.json').expanduser()
d = json.loads(p.read_text()) if p.exists() else {}
print(d.get('$1', ''))" 2>/dev/null
}

_write_config_key() {
  python3 -c "
import json, sys; from pathlib import Path
p = Path('~/.config/deus/config.json').expanduser()
p.parent.mkdir(parents=True, exist_ok=True)
d = json.loads(p.read_text()) if p.exists() else {}
d[sys.argv[1]] = sys.argv[2]
p.write_text(json.dumps(d, indent=2))
" "$1" "$2"
}

_write_env_key() {
  local env_file="$SCRIPT_DIR/.env"
  [ ! -f "$env_file" ] && return
  if grep -q "^$1=" "$env_file" 2>/dev/null; then
    local tmp="$env_file.tmp.$$"
    sed "s|^$1=.*|$1=$2|" "$env_file" > "$tmp" && mv "$tmp" "$env_file"
  fi
}

_build_and_restart() {
  local no_restart=false quiet=false
  for arg in "$@"; do
    case "$arg" in
      --no-restart) no_restart=true ;;
      --quiet) quiet=true ;;
    esac
  done
  $quiet || printf "  Building...\r"
  (cd "$SCRIPT_DIR" && npm run build --silent) || { echo "Build failed."; exit 1; }
  if [[ "$OSTYPE" != msys* && "$OSTYPE" != cygwin* ]]; then
    LINK_DIR="$HOME/.local/bin"
    mkdir -p "$LINK_DIR"
    ln -sf "$SCRIPT_DIR/deus-cmd.sh" "$LINK_DIR/deus"
  fi
  if ! $no_restart; then
    # Linux/Windows restart not implemented yet — project_windows_support.md
    if [[ "$OSTYPE" == darwin* ]]; then
      launchctl kickstart -k "gui/$(id -u)/com.deus" 2>/dev/null
      $quiet || echo "Deus built and restarted (CLI symlink refreshed)."
    else
      $quiet || echo "Built. Service restart: not implemented on this platform."
    fi
  else
    $quiet || echo "Built (restart skipped)."
  fi
}

# Warn (never block) when the live tree drifts off main or behind origin/main, so
# `deus <cmd>` doesn't silently ship stale behavior from a feature branch.
# darwin/Linux only (date +%s, git); Windows port pending — project_windows_support.md
_deus_freshness_check() {
  [[ "$OSTYPE" == darwin* || "$OSTYPE" == linux* ]] || return 0
  # Skip for sync (does its own reporting) and help/no-arg paths.
  case "$1" in sync|""|-h|--help|help) return 0 ;; esac
  git -C "$SCRIPT_DIR" rev-parse --git-dir >/dev/null 2>&1 || return 0

  local stamp_dir="$HOME/.config/deus" stamp now last
  stamp="$stamp_dir/freshness-stamp"
  now=$(date +%s 2>/dev/null) || return 0
  mkdir -p "$stamp_dir" 2>/dev/null || {
    [ -n "$DEUS_DEBUG" ] && echo "deus: freshness stamp dir unwritable, skipping" >&2
    return 0
  }
  last=0
  [ -f "$stamp" ] && last=$(cat "$stamp" 2>/dev/null || echo 0)
  # Throttle: at most one real check (and one background fetch) per 600s.
  [ $((now - last)) -lt 600 ] 2>/dev/null && return 0
  echo "$now" > "$stamp" 2>/dev/null || {
    [ -n "$DEUS_DEBUG" ] && echo "deus: freshness stamp unwritable" >&2
  }

  # Refresh the cached origin/main ref in the background — no hot-path network/hang.
  # Stamp-race: concurrent calls at window-expiry may each spawn one harmless fetch.
  ( git -C "$SCRIPT_DIR" fetch --quiet origin main >/dev/null 2>&1 & ) >/dev/null 2>&1

  # Offline compare local state vs the cached origin/main ref.
  local branch behind
  branch=$(git -C "$SCRIPT_DIR" symbolic-ref --short -q HEAD 2>/dev/null)
  # Detached HEAD (e.g. a future pinned-worktree install) — don't nag.
  [ -z "$branch" ] && return 0
  if [ "$branch" != "main" ]; then
    echo "deus: live tree on '$branch', not main — run 'deus sync'" >&2
    return 0
  fi
  git -C "$SCRIPT_DIR" rev-parse --verify -q origin/main >/dev/null 2>&1 || return 0
  behind=$(git -C "$SCRIPT_DIR" rev-list --count HEAD..origin/main 2>/dev/null || echo 0)
  if [ "${behind:-0}" -gt 0 ] 2>/dev/null; then
    echo "deus: live tree $behind commit(s) behind origin/main — run 'deus sync'" >&2
  fi
  return 0
}

_fcc_validate_model_name() {
  if ! printf '%s' "$1" | grep -qE '^[a-zA-Z0-9_./:@-]+$'; then
    echo "Error: invalid characters in model name."
    exit 1
  fi
}

_fcc_current_full() {
  grep '^MODEL=' ~/.fcc/.env 2>/dev/null | cut -d= -f2
}

_fcc_current_provider() {
  _fcc_current_full | cut -d/ -f1
}

_fcc_current_model() {
  _fcc_current_full | cut -d/ -f2-
}

_backend_to_display() {
  case "$1" in
    openai) echo "codex" ;;
    *) echo "$1" ;;
  esac
}

_display_to_backend() {
  case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
    codex) echo "openai" ;;
    *) echo "$1" ;;
  esac
}

_normalize_cli_agent() {
  local agent="${DEUS_CLI_AGENT:-${DEUS_AGENT_BACKEND:-}}"
  if [ -z "$agent" ]; then
    agent="$(_read_config_key agent_backend)"
  fi
  agent="${agent:-claude}"
  agent="$(printf '%s' "$agent" | tr '[:upper:]' '[:lower:]')"
  case "$agent" in
    openai|codex) echo "codex" ;;
    ollama) echo "ollama" ;;
    llama-cpp) echo "llama-cpp" ;;
    *) echo "claude" ;;
  esac
}

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
  local current_version="3"
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

First, resolve the vault path using this **per-instance** order (highest priority first). `$VAULT` means the resolved path:

1. `DEUS_VAULT_PATH` env var, if set.
2. `vault_path` in `./.deus/config.json` (the current working directory's instance-local config), if that file exists. If the file exists but has no usable `vault_path`, STOP and tell the user it is present but missing `vault_path` — do **not** fall through to the global config (that fall-through is what reads from another instance's vault).
3. `vault_path` in `~/.config/deus/config.json` (global fallback).

Tiers 1 and 2 are the per-instance mechanisms: when several Deus instances run on one machine they share the global config (tier 3), so resolving from it alone can silently point this instance's `/resume` at a different instance's vault. The instance-local `./.deus/config.json` keeps each instance self-contained. The `memory_indexer.py` calls below resolve the vault by the same order, so their reads come from this instance's vault too.

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
  local current_version="3"
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

Resolve the vault path using this **per-instance** order (highest priority first). `$VAULT` means the resolved path:

1. `DEUS_VAULT_PATH` env var, if set.
2. `vault_path` in `./.deus/config.json` (the current working directory's instance-local config), if that file exists. If the file exists but has no usable `vault_path`, STOP and tell the user it is present but missing `vault_path` — do **not** fall through to the global config (that fall-through is what corrupts another instance's vault).
3. `vault_path` in `~/.config/deus/config.json` (global fallback).

Tiers 1 and 2 are the per-instance mechanisms: when several Deus instances run on one machine they share the global config (tier 3), so resolving from it alone can silently point this instance's `/compress` at a different instance's vault and corrupt its memory. The instance-local `./.deus/config.json` keeps each instance self-contained. The `memory_indexer.py` calls below resolve the vault by the same order, so their writes land in this instance's vault too.

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

# Nudge if the live tree has drifted off/behind main (warn-only, throttled).
_deus_freshness_check "$1"

case "$1" in
  init|onboard)
    # Onboard a project into Deus code intelligence (codegraph + code_search)
    # and register it. scripts/deus_init.sh owns the safety gate + indexing;
    # registration stays here so it can use _write_project_config (single
    # source of truth — the script can't reach it without sourcing this file's
    # top-level dispatch). The realpath is resolved ONCE here and passed to the
    # script, so the DB md5 (computed by code_search from the same dir) and the
    # config md5 (computed below) are guaranteed equal. macOS/Linux only.
    shift
    init_force=""
    init_target=""
    for a in "$@"; do
      case "$a" in
        --force) init_force="--force" ;;
        -h|--help) exec "$SCRIPT_DIR/scripts/deus_init.sh" --help ;;
        -*) ;;  # ignore unknown flags (forward-compat)
        *) [ -z "$init_target" ] && init_target="$a" ;;
      esac
    done
    init_base="${init_target:-$PWD}"
    if [ ! -d "$init_base" ]; then
      echo "deus init: not a directory: $init_base" >&2; exit 1
    fi
    init_root="$(cd "$init_base" 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null)" || init_root=""
    [ -z "$init_root" ] && init_root="$init_base"
    init_root="$(cd "$init_root" 2>/dev/null && pwd -P)" || { echo "deus init: cannot resolve: $init_base" >&2; exit 1; }
    # Index + safety gate. The script exits non-zero iff the gate refuses; only
    # then do we skip registration (do not register an un-onboarded dir).
    if "$SCRIPT_DIR/scripts/deus_init.sh" "$init_root" $init_force; then
      # Register — merge, never clobber: preserve any existing memory settings.
      if [ -z "$(_read_project_config "$init_root")" ]; then
        _write_project_config "$init_root" "standard" "true"
        echo "  ✓ registered (memory=standard, summaries=on) — change with /project-settings"
      else
        _update_project_access "$init_root"
        echo "  ✓ already registered — existing settings preserved"
      fi
      echo "Done. Deus code intelligence is active for $(basename "$init_root")."
    else
      exit $?
    fi
    ;;
  auth)
    # `deus auth refresh [--dry-run]` → proactive OAuth refresh CLI
    # (keeps idle containers from hitting /login after 8h token expiry).
    if [ "$2" = "refresh" ]; then
      shift 2
      exec node "$SCRIPT_DIR/dist/auth-refresh.js" "$@"
    fi
    # Validate credentials exist (file or macOS Keychain) before restarting
    python3 -c '
import sys, json, subprocess, os
# Try credentials file first
try:
    d = json.load(open(os.path.expanduser("~/.claude/.credentials.json")))
    assert d.get("claudeAiOauth", {}).get("accessToken")
    sys.exit(0)
except Exception:
    pass
# Fallback: macOS Keychain
try:
    raw = subprocess.check_output(
        ["security", "find-generic-password", "-s", "Claude Code-credentials",
         "-a", os.environ.get("USER", ""), "-w"],
        text=True, stderr=subprocess.DEVNULL).strip()
    d = json.loads(raw)
    assert d.get("claudeAiOauth", {}).get("accessToken")
    sys.exit(0)
except Exception:
    pass
sys.exit(1)
' 2>/dev/null
    if [ $? -ne 0 ]; then
      echo "Error: no OAuth token found in ~/.claude/.credentials.json or macOS Keychain"
      echo "Run: claude auth login"
      exit 1
    fi
    # Do NOT write token to .env — the credential proxy reads credentials.json
    # directly via getDynamicOAuthToken() with a 5-min cache. Writing to .env
    # would permanently freeze the token and cause a login loop on next refresh.
    #
    # Rebuild and restart — prevents silent dist/src drift where a source fix
    # is present but the running binary is stale.
    _build_and_restart
    ;;
  gcal)
    case "${2:-status}" in
      auth)
        echo "Google Calendar OAuth2 re-authorization..."
        node "$SCRIPT_DIR/scripts/setup-gcal-auth.mjs"
        if [ $? -eq 0 ]; then
          echo ""
          echo "Rebuilding and restarting Deus to pick up new tokens..."
          _build_and_restart
        fi
        ;;
      ping)
        # Lightweight keep-alive: refreshes the access token by listing 1 event.
        # Run via launchd daily to prevent the 7-day refresh_token expiry.
        node -e "
          const { google } = require('googleapis');
          const fs = require('fs');
          const credsPath = '$SCRIPT_DIR/integrations/gcal/credentials.json';
          const tokensPath = '$SCRIPT_DIR/integrations/gcal/tokens.json';
          if (!fs.existsSync(tokensPath)) { console.log('No gcal tokens — skipping ping'); process.exit(0); }
          const creds = JSON.parse(fs.readFileSync(credsPath));
          const tokens = JSON.parse(fs.readFileSync(tokensPath));
          const { client_id, client_secret, redirect_uris } = creds.installed || creds.web;
          const auth = new google.auth.OAuth2(client_id, client_secret, redirect_uris[0]);
          auth.setCredentials(tokens);
          auth.on('tokens', (t) => {
            Object.assign(tokens, t);
            fs.writeFileSync(tokensPath, JSON.stringify(tokens, null, 2));
          });
          google.calendar({ version: 'v3', auth }).calendarList.get({ calendarId: 'primary' })
            .then(r => console.log('gcal ping OK:', r.data.summary))
            .catch(e => { console.error('gcal ping FAILED:', e.message); process.exit(1); });
        " 2>&1
        ;;
      status)
        TOKENS_PATH="$SCRIPT_DIR/integrations/gcal/tokens.json"
        if [ -f "$TOKENS_PATH" ]; then
          AGE_DAYS=$(( ( $(date +%s) - $(stat -f %m "$TOKENS_PATH") ) / 86400 ))
          echo "Google Calendar tokens: $TOKENS_PATH (${AGE_DAYS}d old)"
          echo "Refresh token expires after ~7 days of no use."
          echo ""
          echo "  deus gcal auth   Re-authorize (browser flow)"
          echo "  deus gcal ping   Keep-alive (refreshes token)"
        else
          echo "No Google Calendar tokens found."
          echo "Run: deus gcal auth"
        fi
        ;;
      *)
        echo "Usage: deus gcal [status|auth|ping]"
        echo ""
        echo "  deus gcal          Show token status"
        echo "  deus gcal auth     Re-authorize via browser"
        echo "  deus gcal ping     Keep-alive ping (prevents token expiry)"
        ;;
    esac
    ;;
  backend)
    shift
    CURRENT_BACKEND="$(_read_config_key agent_backend)"
    [ -z "$CURRENT_BACKEND" ] && CURRENT_BACKEND="${DEUS_AGENT_BACKEND:-claude}"
    CURRENT_DISPLAY="$(_backend_to_display "$CURRENT_BACKEND")"
    CURRENT_MODEL="$(_read_config_key agent_backend_model)"

    case "${1:-show}" in
      show)
        echo "Backend: $CURRENT_DISPLAY"
        if [ -n "$CURRENT_MODEL" ]; then
          echo "Model:   $CURRENT_MODEL"
        fi
        if [ -n "$DEUS_AGENT_BACKEND" ]; then
          echo "(env override: DEUS_AGENT_BACKEND=$DEUS_AGENT_BACKEND)"
        fi
        ;;
      list)
        for b in claude codex ollama llama-cpp; do
          if [ "$b" = "$CURRENT_DISPLAY" ]; then
            echo "* $b (active)"
          else
            echo "  $b"
          fi
        done
        ;;
      set)
        if [ -z "$2" ]; then
          echo "Usage: deus backend set <claude|codex|ollama|llama-cpp>"
          exit 1
        fi
        INPUT="$(printf '%s' "$2" | tr '[:upper:]' '[:lower:]')"
        case "$INPUT" in
          claude|codex|ollama|llama-cpp) ;;
          *)
            echo "Unknown backend: $2"
            echo "Available: claude, codex, ollama, llama-cpp"
            exit 1
            ;;
        esac
        NEW_BACKEND="$(_display_to_backend "$INPUT")"
        _write_config_key "agent_backend" "$NEW_BACKEND"
        _write_env_key "DEUS_AGENT_BACKEND" "$NEW_BACKEND"
        echo "Default backend set to: $INPUT"
        echo "Takes effect on next 'deus' launch. Background service uses .env."
        ;;
      model)
        if [ -z "$2" ]; then
          if [ -n "$CURRENT_MODEL" ]; then
            echo "Current model: $CURRENT_MODEL (backend: $CURRENT_DISPLAY)"
          else
            echo "No model override set (using backend default)"
          fi
          exit 0
        fi
        _write_config_key "agent_backend_model" "$2"
        case "$CURRENT_BACKEND" in
          openai)
            _write_env_key "DEUS_OPENAI_MODEL" "$2"
            _write_env_key "DEUS_CODEX_MODEL" "$2"
            ;;
        esac
        echo "Model set to: $2 (backend: $CURRENT_DISPLAY)"
        echo "Takes effect on next 'deus' launch."
        ;;
      bench)
        EVAL_DIR="$(cd "$(dirname "$0")" && pwd)/eval"
        if [ ! -d "$EVAL_DIR" ]; then
          echo "Error: eval/ directory not found at $EVAL_DIR"
          exit 1
        fi
        echo "Running backend parity benchmark (claude vs openai)..."
        echo "This requires both ANTHROPIC_API_KEY/CLAUDE_CODE_OAUTH_TOKEN and OPENAI_API_KEY."
        echo ""
        DEUS_PARITY_TEST=1 python3 -m pytest "$EVAL_DIR" \
          --json-report --json-report-file="$EVAL_DIR/.report.json" \
          -v 2>&1
        PYTEST_EXIT=$?
        if [ -f "$EVAL_DIR/.report.json" ]; then
          python3 "$EVAL_DIR/parity_report.py" --report "$EVAL_DIR/.report.json"
        fi
        exit $PYTEST_EXIT
        ;;
      *)
        echo "Usage: deus backend [show|set|model|list|bench]"
        echo ""
        echo "  deus backend           Show current backend and model"
        echo "  deus backend set <be>  Set default backend (claude|codex|ollama|llama-cpp)"
        echo "  deus backend model <m> Set model for current backend (e.g. gpt-4o)"
        echo "  deus backend list      List available backends"
        echo "  deus backend bench     Run parity benchmark (claude vs openai)"
        ;;
    esac
    ;;
  home|web|"")
    # `deus web` launches with --chrome for Claude-in-Chrome browser integration.
    # Otherwise identical to bare `deus` / `deus home`.
    CHROME_FLAG=""
    TUI_DEFAULT="false"
    AGENTS_MODE="false"
    if [ "$1" = "web" ] || [ "$(_read_config_key chrome_default)" = "true" ]; then
      CHROME_FLAG="--chrome"
    fi
    if [ "$1" != "web" ] && [ "$(_read_config_key tui_default)" = "true" ]; then
      TUI_DEFAULT="true"
    fi
    for _arg in "$@"; do
      if [ "$_arg" = "--agents" ]; then
        AGENTS_MODE="true"
        break
      fi
    done
    if [ "$AGENTS_MODE" = "true" ]; then
      exec claude agents
    fi

    _launch_tui_with_context() {
      local ctx_content="$1"
      local initial_prompt="$2"
      local mode="$3"
      local tui_bin="$SCRIPT_DIR/tui/target/release/deus-tui"
      if [ ! -x "$tui_bin" ]; then
        printf "  Building TUI...\r"
        (cd "$SCRIPT_DIR/tui" && cargo build --release) || { echo "TUI build failed."; exit 1; }
      fi
      local ctx_file=""
      if [ -n "$ctx_content" ]; then
        ctx_file=$(mktemp "${TMPDIR:-/tmp}/deus-tui-ctx.XXXXXX")
        chmod 0600 "$ctx_file"
        printf '%s' "$ctx_content" > "$ctx_file"
        export DEUS_TUI_CONTEXT_FILE="$ctx_file"
      fi
      [ -n "$initial_prompt" ] && export DEUS_TUI_INITIAL_PROMPT="$initial_prompt"
      export DEUS_TUI_BYPASS="${PREFS_BYPASS:-true}"
      export DEUS_TUI_MODE="$mode"
      export DEUS_TUI_BACKEND="$CLI_AGENT"
      exec "$tui_bin"
    }
    TOKEN=$(python3 -c '
import json, os, subprocess, sys
# Try file first
try:
    d = json.load(open(os.path.expanduser("~/.claude/.credentials.json")))
    print(d["claudeAiOauth"]["accessToken"]); sys.exit(0)
except Exception: pass
# Fallback: macOS Keychain
try:
    raw = subprocess.check_output(
        ["security", "find-generic-password", "-s", "Claude Code-credentials",
         "-a", os.environ.get("USER", ""), "-w"],
        text=True, stderr=subprocess.DEVNULL).strip()
    print(json.loads(raw)["claudeAiOauth"]["accessToken"]); sys.exit(0)
except Exception: pass
sys.exit(1)
' 2>/dev/null)
    if [ -z "$TOKEN" ]; then
      echo "Error: no OAuth token found in ~/.claude/.credentials.json or macOS Keychain"
      echo "Run: claude auth login"
      exit 1
    fi
    # Do NOT export CLAUDE_CODE_OAUTH_TOKEN — the Claude CLI reads
    # ~/.claude/.credentials.json directly and auto-refreshes on /login.
    # Exporting a frozen token causes 401s after token rotation because
    # the CLI prioritizes the env var over the credentials file.
    [[ "$OSTYPE" == darwin* ]] && launchctl kickstart -k "gui/$(id -u)/com.deus" 2>/dev/null
    # Launch claude with bypass mode; fall back to normal mode if user declines
    launch_claude() {
      claude $CHROME_FLAG --dangerously-skip-permissions "$@"
      if [ $? -ne 0 ]; then
        claude $CHROME_FLAG "$@"
      fi
    }

    launch_fcc() {
      if ! command -v fcc-claude >/dev/null 2>&1; then
        echo "Error: fcc-claude not found. Install: uv tool install free-claude-code@git+https://github.com/Alishahryar1/free-claude-code.git"
        return 127
      fi
      if ! curl -s http://127.0.0.1:8082/health >/dev/null 2>&1; then
        echo "Starting fcc-server..."
        mkdir -p ~/.fcc/logs
        fcc-server > ~/.fcc/logs/server.log 2>&1 &
        sleep 3  # wait for server to bind before health check
        if ! curl -s http://127.0.0.1:8082/health >/dev/null 2>&1; then
          echo "Error: fcc-server failed to start. Check ~/.fcc/logs/server.log"
          return 1
        fi
      fi
      local fcc_model=$(grep '^MODEL=' ~/.fcc/.env 2>/dev/null | cut -d= -f2)
      echo "Proxy: $fcc_model"
      fcc-claude "$@"
    }

    launch_codex() {
      if ! command -v codex >/dev/null 2>&1; then
        echo "Error: Codex CLI not found. Install/login to Codex, or use DEUS_CLI_AGENT=claude."
        return 127
      fi

      local prompt="$1"
      local codex_args=()
      local codex_model="${DEUS_CODEX_MODEL:-${DEUS_OPENAI_MODEL:-$(_read_config_key agent_backend_model)}}"
      [ -n "$codex_model" ] && codex_args+=("--model" "$codex_model")
      [ "$CHROME_FLAG" = "--chrome" ] && codex_args+=("--search")

      if [ "$PREFS_BYPASS" = "false" ]; then
        codex "${codex_args[@]}" "$prompt"
      else
        codex "${codex_args[@]}" --dangerously-bypass-approvals-and-sandbox "$prompt"
        if [ $? -ne 0 ]; then
          codex "${codex_args[@]}" "$prompt"
        fi
      fi
    }

    launch_agent() {
      if [ "$CLI_AGENT" = "fcc" ]; then
        launch_fcc "$@"
        return $?
      fi
      if [ "$CLI_AGENT" = "ollama" ]; then
        echo "Error: Ollama backend is not yet available as a CLI agent."
        echo "Use 'deus backend set claude' or 'deus backend set openai' instead."
        return 1
      fi
      if [ "$CLI_AGENT" = "llama-cpp" ]; then
        echo "Error: llama-cpp backend is not yet available as a CLI agent."
        echo "It works for channel messages and scheduled tasks once the host"
        echo "llama-server is running (see /add-llama-cpp). For an interactive"
        echo "session, run 'deus backend set claude' or 'deus backend set codex'."
        return 1
      fi
      if [ "$CLI_AGENT" != "codex" ]; then
        launch_claude "$@"
        return $?
      fi

      local system_prompt=""
      local user_prompt=""
      while [ $# -gt 0 ]; do
        case "$1" in
          --append-system-prompt)
            system_prompt="$2"
            shift 2
            ;;
          *)
            user_prompt="${user_prompt}${user_prompt:+
}$1"
            shift
            ;;
        esac
      done

      local prompt="$system_prompt"
      if [ -n "$user_prompt" ]; then
        prompt="$prompt

USER REQUEST:
$user_prompt"
      fi
      launch_codex "$prompt"
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

	    CLI_AGENT="$(_normalize_cli_agent)"
	    EXTERNAL_MODE="false"
	    PROJECT_CONFIG=""
	    JUST_ONBOARDED="false"
	    MEMORY_LEVEL="standard"
	    EXTRA_ENV=""

	    if [ "$CURRENT_DIR" != "$DEUS_HOME" ]; then
	      EXTERNAL_MODE="true"

	      # Ensure external-project skills are installed before onboarding.
	      _ensure_project_settings_skill
	      _ensure_resume_skill
	      _ensure_checkpoint_skill
	      _ensure_compress_skill
	      _ensure_preserve_skill
	      _ensure_preferences_skill

	      PROJECT_CONFIG=$(_read_project_config "$CURRENT_DIR")
	      if [ -z "$PROJECT_CONFIG" ]; then
	        _run_onboarding "$CURRENT_DIR"
	        PROJECT_CONFIG=$(_read_project_config "$CURRENT_DIR")
	        JUST_ONBOARDED="true"
	      else
	        _update_project_access "$CURRENT_DIR"
	      fi

	      MEMORY_LEVEL=$(echo "$PROJECT_CONFIG" | python3 -c "import sys,json; print(json.load(sys.stdin).get('memory_level','standard'))" 2>/dev/null)
	      [ -z "$MEMORY_LEVEL" ] && MEMORY_LEVEL="standard"
	      if [ "$MEMORY_LEVEL" = "restricted" ]; then
	        EXTRA_ENV="CLAUDE_CODE_DISABLE_AUTO_MEMORY=1"
	      fi
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
        launch_agent --append-system-prompt "$DEUS_IDENTITY"
        exit $?
      else
        cd "$HOME/deus" && launch_agent --append-system-prompt "$DEUS_IDENTITY"
        exit $?
      fi
    fi
    CONTEXT=""

	    if [ "$EXTERNAL_MODE" = "true" ] && [ "$MEMORY_LEVEL" = "restricted" ]; then
	      printf "  Restricted memory: skipping vault recall...\r"
	    else
	      printf "  Reading vault...\r"
	      # vault_autoload in config.json controls which files load at startup.
	      # Default: ["CLAUDE.md"] — all others are on-demand via /resume or hooks.
	      # See docs/decisions/vault-autoload.md for rationale.
	      VAULT_AUTOLOAD=$(python3 -c "
import json; from pathlib import Path
c = json.loads(Path('~/.config/deus/config.json').expanduser().read_text())
for f in c.get('vault_autoload', ['CLAUDE.md']):
    print(f)
" 2>/dev/null || echo "CLAUDE.md")

	      while IFS= read -r VFILE; do
	        [ -z "$VFILE" ] && continue
	        VCONTENT=$(cat "$VAULT/$VFILE" 2>/dev/null)
	        if [ -n "$VCONTENT" ]; then
	          if [ -z "$CONTEXT" ]; then
	            CONTEXT="=== VAULT: $VFILE ===\n$VCONTENT"
	          else
	            CONTEXT="$CONTEXT\n\n=== VAULT: $VFILE ===\n$VCONTENT"
	          fi
	        fi
	      done <<< "$VAULT_AUTOLOAD"

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
	    fi

    printf "✓ Ready.                        \n"

    # Signal to vault_context_hook.py that vault was already injected via
    # --append-system-prompt, preventing double-injection in the session.
    export DEUS_VAULT_PRELOADED=1

    # ─── EXTERNAL PROJECT MODE ───
    # Same full Deus brain, different working directory and startup.
    # Memory level controls how much project data persists between sessions.
	    if [ "$EXTERNAL_MODE" = "true" ]; then

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

	      if [ "$MEMORY_LEVEL" = "restricted" ]; then
	        MEMORY_SCOPE="Saved vault/session memory was intentionally not preloaded for this project. Use Deus core behavior, live repo state, and live tools only."
	      else
	        MEMORY_SCOPE="You have your full memory, preferences, and capabilities."
	      fi

	      STARTUP_INSTRUCTION="STARTUP INSTRUCTION: You are Deus, operating in EXTERNAL PROJECT MODE. The current directory is an external codebase at $CURRENT_DIR — not the Deus project. $MEMORY_SCOPE Focus on this codebase while applying all your behavioral rules and knowledge. The project may have its own CLAUDE.md — follow it alongside yours.

$MEMORY_INSTRUCTION

Available commands: /resume (deep context reload) | /checkpoint (save mid-session state) | /compress (save session to vault) | /preserve (save lasting insights) | /project-settings (data handling)

$GIT_CONTEXT

$STARTUP_GREETING"

      # Launch claude with appropriate env vars
      if [ -n "$EXTRA_ENV" ]; then
        export $EXTRA_ENV
      fi

      FULL_PROMPT=""
      if [ -n "$CONTEXT" ]; then
        FULL_PROMPT="$(printf '%s' "$CONTEXT")

$STARTUP_INSTRUCTION"
      else
        FULL_PROMPT="$STARTUP_INSTRUCTION"
      fi

      if [ "$TUI_DEFAULT" = "true" ]; then
        cd "$CURRENT_DIR" && _launch_tui_with_context "$FULL_PROMPT" "" "external"
      fi
      launch_agent --append-system-prompt "$FULL_PROMPT"
      exit $?
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
    else
      STARTUP_INSTRUCTION="STARTUP INSTRUCTION: Context from the memory vault has been pre-loaded above, BUT it is a snapshot taken at deus launch and does not refresh across /clear or same-session work.

PRIORITY RULE: If the user's first message contains an explicit directive — an 'execute' command, a file path to act on, a task to perform, or any instruction beyond 'Catch me up' — skip the catch-up routine entirely and execute that directive immediately. The catch-up below is a DEFAULT for when no directive is given, not a mandatory preamble.

If no directive is present, verify freshness before catching up:

  1. ls -t \"$VAULT/Checkpoints\" | head -3
  2. ls -t \"$VAULT/Session-Logs/$(date +%Y-%m-%d)\" 2>/dev/null
  3. If anything on disk is newer than the newest date in the === RECENT SESSIONS === block, re-run: python3 \$HOME/deus/scripts/memory_indexer.py --recent 3
     and lead the catch-up from that output plus the newest same-day checkpoint's next_action / in_progress fields. Ignore the stale pre-loaded block.
  4. If disk matches the block, the snapshot is fresh — use it.

Then catch the user up using exactly this format:

• Previous session: [1-2 lines of ongoing context and last session topic]
• Pending: [bullet list of pending tasks, max 3 items]

Then stop and wait for the user."
    fi

    FULL_PROMPT=""
    INITIAL_MSG=""
    if [ -n "$CONTEXT" ]; then
      FULL_PROMPT="$(printf '%s' "$CONTEXT")

$STARTUP_INSTRUCTION"
    fi
    if [ "$PREFS_CATCH_ME_UP" = "true" ]; then
      INITIAL_MSG="Catch me up."
    fi

    if [ "$TUI_DEFAULT" = "true" ]; then
      cd "$HOME/deus" && _launch_tui_with_context "$FULL_PROMPT" "$INITIAL_MSG" "home"
    fi

    if [ -n "$FULL_PROMPT" ] && [ -n "$INITIAL_MSG" ]; then
      cd "$HOME/deus" && launch_agent --append-system-prompt "$FULL_PROMPT" "$INITIAL_MSG"
    elif [ -n "$FULL_PROMPT" ]; then
      cd "$HOME/deus" && launch_agent --append-system-prompt "$FULL_PROMPT"
    else
      cd "$HOME/deus" && launch_agent
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
  usage)
    # Token-usage efficiency + cost report across all projects.
    # $SCRIPT_DIR (not $HOME/deus) so it works from any install path / worktree.
    shift
    exec python3 "$SCRIPT_DIR/scripts/analyze_token_efficiency.py" "$@"
    ;;
  sync)
    # Make the live install current with origin/main, non-destructively.
    # See ADR: docs/decisions/live-command-freshness.md
    sync_repo="$SCRIPT_DIR"
    if ! git -C "$sync_repo" rev-parse --git-dir >/dev/null 2>&1; then
      echo "deus sync: $sync_repo is not a git repository." >&2; exit 1
    fi
    sync_branch=$(git -C "$sync_repo" symbolic-ref --short -q HEAD 2>/dev/null || echo "DETACHED")
    if [ "$sync_branch" != "main" ]; then
      echo "deus sync: live tree is on '$sync_branch', not main." >&2
      echo "  Feature work belongs in a worktree. Switch the live tree to main first:" >&2
      echo "    git -C \"$sync_repo\" checkout main && deus sync" >&2
      exit 1
    fi
    if ! git -C "$sync_repo" diff --quiet || ! git -C "$sync_repo" diff --cached --quiet; then
      echo "deus sync: live tree has uncommitted changes — commit or stash first." >&2
      exit 1
    fi
    echo "Fetching origin/main..."
    if ! git -C "$sync_repo" fetch origin main; then
      echo "deus sync: fetch failed." >&2; exit 1
    fi
    if ! git -C "$sync_repo" merge --ff-only origin/main; then
      echo "deus sync: cannot fast-forward (live tree has diverged from origin/main)." >&2
      exit 1
    fi
    _build_and_restart
    echo "deus: synced to origin/main."
    ;;
  pipeline)
    shift
    # cd required: config.ts evaluates PROJECT_ROOT from process.cwd() at import time
    cd "$SCRIPT_DIR" && exec node "$SCRIPT_DIR/dist/linear-pipeline-cli.js" "$@"
    ;;
  solution)
    # Solution atom management — structured lesson capture.
    shift
    exec node "$SCRIPT_DIR/dist/solutions/cli.js" "$@"
    ;;
  sweep)
    shift
    local bench_file="${1:-$SCRIPT_DIR/scripts/tests/fixtures/memory_tree_queries.jsonl}"
    echo "Running threshold sweep on $(wc -l < "$bench_file" | tr -d ' ') queries..."
    exec python3 "$SCRIPT_DIR/scripts/memory_tree.py" calibrate-sweep "$bench_file" --json
    ;;
  tui)
    shift
    local tui_bin="$SCRIPT_DIR/tui/target/release/deus-tui"
    if [[ ! -x "$tui_bin" ]]; then
      echo "TUI binary not found. Building..."
      (cd "$SCRIPT_DIR/tui" && cargo build --release) || { echo "Build failed. Install Rust: https://rustup.rs"; exit 1; }
    fi
    exec "$tui_bin" "$@"
    ;;
  build)
    shift
    _build_and_restart "$@"
    ;;
  provider)
    shift
    FCC_PROXY="http://127.0.0.1:8082"
    FCC_PROVIDERS="ollama llamacpp gemini"

    case "${1:-}" in
      "")
        FCC_PROV=$(_fcc_current_provider)
        echo "Provider: ${FCC_PROV:-not configured}"
        echo ""
        echo "Available:"
        for p in ollama llamacpp gemini; do
          if [[ "$p" == "$FCC_PROV" ]]; then
            echo "  * $p (active)"
          else
            echo "    $p"
          fi
        done
        echo ""
        echo "Usage: deus provider <name>"
        echo ""
        echo "Examples:"
        echo "  deus provider ollama      # local models via Ollama"
        echo "  deus provider llamacpp    # local models via llama-server (:8080)"
        echo "  deus provider gemini      # Google AI Studio (needs API key)"
        ;;
      *)
        FCC_NEW_PROV="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
        case "$FCC_NEW_PROV" in
          ollama|llamacpp|gemini) ;;
          llama-cpp|llama_cpp) FCC_NEW_PROV="llamacpp" ;;
          *)
            echo "Unknown provider: $1"
            echo "Available: $FCC_PROVIDERS"
            exit 1
            ;;
        esac
        FCC_OLD_MODEL=$(grep '^MODEL=' ~/.fcc/.env 2>/dev/null | cut -d= -f2 | cut -d/ -f2-)
        FCC_NEW_MODEL="${FCC_NEW_PROV}/${FCC_OLD_MODEL}"
        _fcc_validate_model_name "$FCC_NEW_MODEL"
        FCC_HTTP=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$FCC_PROXY/admin/api/config/apply" \
          -H "Content-Type: application/json" \
          -d "{\"values\":{\"MODEL\":\"$FCC_NEW_MODEL\"}}" 2>&1)
        if [[ "$FCC_HTTP" == "200" ]]; then
          echo "Provider: $FCC_NEW_PROV"
          echo "Model:    $FCC_NEW_MODEL"
        else
          echo "Failed. Is fcc-server running? (HTTP $FCC_HTTP)"
          exit 1
        fi
        ;;
    esac
    ;;
  model)
    shift
    FCC_PROXY="http://127.0.0.1:8082"

    case "${1:-}" in
      "")
        FCC_FULL=$(_fcc_current_full)
        FCC_PROV=$(_fcc_current_provider)
        FCC_MOD=$(_fcc_current_model)
        echo "Provider: ${FCC_PROV:-not set}"
        echo "Model:    ${FCC_MOD:-not set}"
        echo ""
        echo "Commands:"
        echo "  deus model <name>         Switch model (auto-prefixes active provider)"
        echo "  deus model pull <name>    Download a model for the active provider"
        echo "  deus model dashboard      Open proxy admin UI in browser"
        echo ""
        echo "Examples (by provider):"
        case "${FCC_PROV:-ollama}" in
          ollama)
            echo "  deus model qwen3.6                     # ollama/qwen3.6"
            echo "  deus model gemma4:e4b                   # ollama/gemma4:e4b"
            echo "  deus model pull qwen3:32b               # download via ollama pull"
            ;;
          llamacpp)
            echo "  deus model ggml-org/gemma-4-E4B-it-GGUF:Q4_K_M"
            echo "  deus model pull ggml-org/gemma-4-E4B-it-GGUF:Q4_K_M  # download from HuggingFace"
            ;;
          gemini)
            echo "  deus model gemini-2.5-flash             # no download needed"
            echo "  deus model gemini-2.5-pro"
            ;;
          *)
            echo "  deus model <model-name>"
            ;;
        esac
        ;;
      dashboard|dash|admin)
        FCC_ADMIN_URL="http://127.0.0.1:8082/admin"
        if command -v open >/dev/null 2>&1; then
          open "$FCC_ADMIN_URL"
        elif command -v xdg-open >/dev/null 2>&1; then
          xdg-open "$FCC_ADMIN_URL"
        else
          echo "Open in browser: $FCC_ADMIN_URL"
        fi
        ;;
      pull)
        if [[ -z "$2" ]]; then
          FCC_PROV=$(_fcc_current_provider)
          echo "Usage: deus model pull <model-name>"
          echo ""
          case "${FCC_PROV:-ollama}" in
            ollama)
              echo "Examples (provider: ollama):"
              echo "  deus model pull qwen3:32b"
              echo "  deus model pull llama3.3:70b"
              echo "  deus model pull deepseek-r1:14b"
              ;;
            llamacpp)
              echo "Examples (provider: llamacpp — uses HuggingFace):"
              echo "  deus model pull ggml-org/gemma-4-E4B-it-GGUF:Q4_K_M"
              echo "  deus model pull bartowski/Qwen3-32B-GGUF:Q4_K_M"
              ;;
            gemini)
              echo "Gemini models are cloud-hosted — no download needed."
              echo "Switch directly: deus model gemini-2.5-flash"
              ;;
          esac
          exit 1
        fi
        _fcc_validate_model_name "$2"
        FCC_PROV=$(_fcc_current_provider)
        case "${FCC_PROV:-ollama}" in
          ollama)
            echo "Pulling $2 via Ollama..."
            ollama pull "$2"
            if [[ $? -eq 0 ]]; then
              echo ""
              echo "Ready. Switch with: deus model $2"
            fi
            ;;
          llamacpp)
            FCC_HF_REPO=$(echo "$2" | cut -d: -f1)
            FCC_HF_FILE=$(echo "$2" | cut -d: -f2)
            if [[ "$FCC_HF_FILE" == "$FCC_HF_REPO" ]]; then
              echo "Usage for llamacpp: deus model pull <org/repo:filename>"
              echo "Example: deus model pull ggml-org/gemma-4-E4B-it-GGUF:Q4_K_M"
              exit 1
            fi
            echo "Downloading $FCC_HF_REPO ($FCC_HF_FILE) from HuggingFace..."
            huggingface-cli download "$FCC_HF_REPO" --include "*${FCC_HF_FILE}*" --local-dir "$HOME/.cache/llama-cpp-models"
            if [[ $? -eq 0 ]]; then
              echo ""
              echo "Downloaded to ~/.cache/llama-cpp-models/"
              echo "To use: update LLAMA_CPP_MODEL in ~/.config/deus/llama-cpp.env"
              echo "  then: launchctl kickstart -k gui/\$(id -u)/com.deus.llama-cpp"
              echo "  then: deus model $2"
            fi
            ;;
          gemini)
            echo "Gemini models are cloud-hosted — no download needed."
            echo "Switch directly: deus model gemini-2.5-flash"
            ;;
          *)
            echo "Pull not supported for provider: $FCC_PROV"
            ;;
        esac
        ;;
      *)
        FCC_PROV=$(_fcc_current_provider)
        if [[ -z "$FCC_PROV" ]]; then
          echo "No provider set. Run 'deus provider <ollama|llamacpp|gemini>' first."
          exit 1
        fi

        FCC_NEW_MODEL="${FCC_PROV}/${1}"
        _fcc_validate_model_name "$FCC_NEW_MODEL"

        if pgrep -qx "claude" 2>/dev/null || pgrep -qx "fcc-claude" 2>/dev/null; then
          echo "Warning: Claude Code session active - switch takes effect next session."
          echo -n "Continue? [y/N] "
          read -r reply
          [[ "$reply" != [yY] ]] && exit 0
        fi

        FCC_HTTP=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$FCC_PROXY/admin/api/config/apply" \
          -H "Content-Type: application/json" \
          -d "{\"values\":{\"MODEL\":\"$FCC_NEW_MODEL\"}}" 2>&1)

        if [[ "$FCC_HTTP" == "200" ]]; then
          echo "Switched to: $FCC_NEW_MODEL"
        else
          echo "Failed. Is fcc-server running? (HTTP $FCC_HTTP)"
          exit 1
        fi
        ;;
    esac
    ;;
  *)
    echo "Usage: deus [claude|codex] [home|init|auth|build|web|backend|gcal|listen|logs|model|provider|pipeline|solution|sweep|tui] [--agents]"
    echo ""
    echo "  deus            Launch in current directory (external project mode if not ~/deus)"
    echo "  deus codex      Launch with Codex (OpenAI) for this session"
    echo "  deus fcc        Launch with proxy model (see: deus provider, deus model)"
    echo "  deus home       Launch in home mode (~/deus) regardless of current directory"
    echo "  deus init       Onboard the current project: index it for code intelligence"
    echo "                    (codegraph + code_search) and register it (alias: onboard, --force)"
    echo "  deus auth       Validate credentials and rebuild+restart"
    echo "  deus auth refresh [--dry-run]  Proactive OAuth token refresh (scheduled every 30 min by launchd)"
    echo "  deus build      Compile TypeScript and restart the service (--no-restart, --quiet)"
    echo "  deus web        Same as 'deus' but launches claude with --chrome (Claude-in-Chrome integration)"
    echo "  deus backend    Manage default AI backend and model (show|set|model|list)"
    echo "  deus gcal       Google Calendar token management (status|auth|ping)"
    echo "  deus listen     Record from mic, transcribe, and copy to clipboard"
    echo "  deus provider   Switch proxy provider (ollama|llamacpp|gemini)"
    echo "  deus model      Switch proxy model or open dashboard (model-name|dashboard)"
    echo "  deus logs       Review system health logs (rotate|review|summary|pinned)"
    echo "  deus usage      Token-efficiency + cost report (--since|--project|--pricing|--json)"
    echo "  deus sync       Update the live install to origin/main (fetch + ff + rebuild)"
    echo "  deus pipeline   Pipeline event audit (LIA-XX | --failed | --active | --all)"
    echo "  deus solution   Manage solution atoms (list|search|add)"
    echo "  deus sweep      Run threshold calibration sweep against benchmark queries"
    echo "  deus tui        Interactive terminal UI (set tui_default=true in config to use by default)"
    echo ""
    echo "Flags:"
    echo "  --agents        Open the claude agents preview UI (append to any launch command)"
    ;;
esac
