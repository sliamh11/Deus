#!/bin/zsh
PLIST="$HOME/Library/LaunchAgents/com.deus.plist"

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
    if [ -z "$VAULT" ]; then
      echo "Warning: No vault configured. Set DEUS_VAULT_PATH or vault_path in ~/.config/deus/config.json"
      cd "$HOME/deus" && exec claude --dangerously-skip-permissions
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
