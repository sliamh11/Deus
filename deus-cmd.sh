#!/bin/zsh
PLIST="$HOME/Library/LaunchAgents/com.deus.plist"
DEUS_PROJECTS_DIR="$HOME/.config/deus/projects"
DEUS_SKILLS_DIR="$HOME/.claude/skills"

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
  local dir="$1" level="$2" summaries="$3"
  mkdir -p "$DEUS_PROJECTS_DIR"
  local config_file
  config_file=$(_project_config_path "$dir")
  local name
  name=$(basename "$dir")
  local now
  now=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  (umask 077 && cat > "$config_file" <<PROJEOF
{
  "path": "$dir",
  "name": "$name",
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

Load context from the Obsidian vault before starting work.

First, resolve the vault path by reading `~/.config/deus/config.json` and using the `vault_path` value. If the env var `DEUS_VAULT_PATH` is set, use that instead. All paths below use `$VAULT` to mean this resolved path.

1. Always read core memory:
   $VAULT/CLAUDE.md

2. Based on likely task context, also read:
   - Study session → $VAULT/STUDY.md
   - NanoClaw / tools / infra session → $VAULT/INFRA.md
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
  local current_version="1"
  if [ -f "$marker" ] && [ "$(cat "$marker")" = "$current_version" ]; then
    return
  fi
  mkdir -p "$skill_dir"
  cat > "$skill_dir/skill.md" <<'SKILLEOF'
---
name: project-settings
description: View or modify Deus external project settings (memory level, session summaries)
user_invocable: true
---

# /project-settings

Manage Deus data handling settings for the current external project.

## Config location

Project configs are stored at `~/.config/deus/projects/<hash>.json`. To find the config for the current directory, compute the MD5 hash of the absolute path of the current working directory and look for that file.

## When invoked with no arguments

Read the config file and display current settings in this format:

```
Project: <name> (<path>)
Memory level: <full|standard|restricted>
Session summaries: <on|off>
Created: <date>
Last accessed: <date>
```

Then show available commands:
- `/project-settings memory full|standard|restricted` — change memory level
- `/project-settings summaries on|off` — toggle session summaries
- `/project-settings delete` — delete all Deus data for this project

## When invoked with arguments

Parse the argument and update the config JSON file accordingly.

### `memory full|standard|restricted`

Update the `memory_level` field. If changing to `restricted`, also set `save_summaries` to false and inform the user.

Memory level descriptions:
- **full**: Remember everything. Claude auto-memory enabled. Session summaries saved to vault.
- **standard**: Remember decisions and architecture, skip code details. Auto-memory enabled with guidance. Summaries saved but redacted.
- **restricted**: Nothing persists. Auto-memory disabled. No summaries.

### `summaries on|off`

Update the `save_summaries` field. If memory level is `restricted` and user tries to enable summaries, warn that restricted mode doesn't support summaries.

### `delete`

Ask for confirmation, then delete the config file. Inform the user that Claude Code's own session data at `~/.claude/projects/` is not affected (that's managed by Claude Code itself).

## Important

- The config file uses the MD5 hash of the current working directory's absolute path as filename
- Use `md5 -q` on macOS or `md5sum | cut -d' ' -f1` on Linux to compute the hash
- Always use `umask 077` when writing config files (they may contain path information)
- After modifying settings, confirm the change and remind the user the new settings take effect on the next message
SKILLEOF
  echo "$current_version" > "$marker"
}

case "$1" in
  on)
    launchctl load "$PLIST" 2>/dev/null
    launchctl kickstart -k "gui/$(id -u)/com.deus" 2>/dev/null
    echo "Deus started."
    ;;
  off)
    launchctl unload "$PLIST" 2>/dev/null
    echo "Deus stopped."
    ;;
  restart)
    launchctl unload "$PLIST" 2>/dev/null
    launchctl load "$PLIST" 2>/dev/null
    launchctl kickstart -k "gui/$(id -u)/com.deus" 2>/dev/null
    echo "Deus restarted."
    ;;
  status)
    if launchctl list | grep -q "com.deus"; then
      echo "Deus is running."
    else
      echo "Deus is stopped."
    fi
    ;;
  logs)
    tail -f "$HOME/deus/logs/deus.log"
    ;;
  auth)
    TOKEN=$(python3 -c 'import sys,json; print(json.load(open(sys.argv[1]))["claudeAiOauth"]["accessToken"])' "$HOME/.claude/.credentials.json" 2>/dev/null)
    if [ -z "$TOKEN" ]; then
      echo "Error: could not read token from ~/.claude/.credentials.json"
      exit 1
    fi
    (umask 077 && echo "CLAUDE_CODE_OAUTH_TOKEN=$TOKEN" > "$HOME/deus/.env")
    launchctl kickstart -k "gui/$(id -u)/com.deus" 2>/dev/null
    echo "Auth token refreshed and Deus restarted."
    ;;
  "")
    TOKEN=$(python3 -c 'import sys,json; print(json.load(open(sys.argv[1]))["claudeAiOauth"]["accessToken"])' "$HOME/.claude/.credentials.json" 2>/dev/null)
    if [ -z "$TOKEN" ]; then
      echo "Error: could not read token from ~/.claude/.credentials.json"
      exit 1
    fi
    (umask 077 && echo "CLAUDE_CODE_OAUTH_TOKEN=$TOKEN" > "$HOME/deus/.env")
    launchctl kickstart -k "gui/$(id -u)/com.deus" 2>/dev/null
    export CLAUDE_CODE_OAUTH_TOKEN="$TOKEN"
    # Resolve vault path from config (DEUS_VAULT_PATH env var → ~/.config/deus/config.json)
    VAULT="${DEUS_VAULT_PATH:-$(python3 -c "import json; from pathlib import Path; print(json.loads(Path('~/.config/deus/config.json').expanduser().read_text()).get('vault_path',''))" 2>/dev/null)}"

    DEUS_HOME="$HOME/deus"
    CURRENT_DIR="$(pwd)"

    # ─── SHARED CONTEXT LOADING ───
    # Full vault + memory + sessions loaded identically regardless of mode.
    # The only difference between home mode and external project mode is
    # the working directory and the startup instruction.
    if [ -z "$VAULT" ]; then
      echo "Warning: No vault configured. Set DEUS_VAULT_PATH or vault_path in ~/.config/deus/config.json"
      if [ "$CURRENT_DIR" != "$DEUS_HOME" ]; then
        exec claude --dangerously-skip-permissions
      else
        cd "$HOME/deus" && exec claude --dangerously-skip-permissions
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

      # Check for existing project config or run onboarding
      PROJECT_CONFIG=$(_read_project_config "$CURRENT_DIR")
      if [ -z "$PROJECT_CONFIG" ]; then
        _run_onboarding "$CURRENT_DIR"
        PROJECT_CONFIG=$(_read_project_config "$CURRENT_DIR")
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
      LAST_ACCESSED=$(echo "$PROJECT_CONFIG" | python3 -c "import sys,json; print(json.load(sys.stdin).get('last_accessed',''))" 2>/dev/null)
      [ -n "$LAST_ACCESSED" ] && IS_RETURNING="true"

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

Available commands: /project-settings (data handling) | /resume (deep context reload)

$GIT_CONTEXT

$STARTUP_GREETING"

      # Launch claude with appropriate env vars
      if [ -n "$EXTRA_ENV" ]; then
        export $EXTRA_ENV
      fi

      if [ -n "$CONTEXT" ]; then
        exec claude --dangerously-skip-permissions --append-system-prompt "$(printf '%s' "$CONTEXT")

$STARTUP_INSTRUCTION"
      else
        exec claude --dangerously-skip-permissions --append-system-prompt "$STARTUP_INSTRUCTION"
      fi
    fi

    # ─── HOME MODE ───
    # Ensure /resume skill is available globally (home mode uses project command,
    # but the skill provides the external-project-aware version for other dirs)
    _ensure_resume_skill

    # Running from ~/deus — full startup with catch-me-up greeting.
    STARTUP_INSTRUCTION="STARTUP INSTRUCTION: Context from the memory vault has been pre-loaded above. Catch the user up using exactly this format:

• Previous session: [1-2 lines of ongoing context and last session topic]
• Pending: [bullet list of pending tasks, max 3 items]

Then stop and wait for the user."

    if [ -n "$CONTEXT" ]; then
      cd "$HOME/deus" && exec claude --dangerously-skip-permissions --append-system-prompt "$(printf '%s' "$CONTEXT")

$STARTUP_INSTRUCTION" "Catch me up."
    else
      cd "$HOME/deus" && exec claude --dangerously-skip-permissions
    fi
    ;;
  *)
    echo "Usage: deus [on|off|restart|status|logs|auth]"
    ;;
esac
