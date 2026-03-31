Load context from the Obsidian vault before starting work.

NOTE: This is the home-mode (~/deus) version. For external projects, the user-level /resume skill at ~/.claude/skills/resume/ handles project-focused context loading automatically.

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
    Formulate a 1-sentence query based on the loaded context from steps 1–3 (e.g. "linear algebra exam prep" or "nanoclaw whatsapp debugging").
    Run: python3 scripts/memory_indexer.py --query "<your query>" --top 2 --recency-boost
    Include the output as additional context. Deduplicate: skip any session that already appeared in step 4a.
    If the script fails or returns nothing, skip silently — warm tier already provides continuity.

5. If a search term was passed as argument, grep session logs for it and read frontmatters of matches.

6. Summarize in 2–3 lines: ongoing context, pending tasks, ready to continue.
   If a checkpoint was loaded, prepend: "Resuming mid-session: [checkpoint next_action]"
