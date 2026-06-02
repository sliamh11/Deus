Load context from the vault before starting work.

NOTE: This is the home-mode (~/deus) version. For external projects, the user-level /resume skill at ~/.claude/skills/resume/ handles project-focused context loading automatically.

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
    Run: python3 scripts/memory_indexer.py --recent-days 3
    This returns ALL sessions from the last 3 calendar days, sorted newest-first.
    Include as "Recent Sessions" context.

    FALLBACK — if the script fails, fall back to:
    find "$VAULT/Session-Logs" -name "*.md" -not -path "*/.obsidian/*" | xargs ls -t 2>/dev/null | head -6
    Then read frontmatter only (lines between the two --- markers) of those files.

4b. Load learnings — what's new since last /resume (no API cost):
    Run: python3 scripts/memory_indexer.py --learnings --since 7 --top 3
    If output is non-empty, include it as a "What's Emerging" section after recent sessions.
    If no output (nothing new), skip silently — silence signals stability.

4c. Load cold tier — semantically relevant older sessions:
    Formulate a 1-sentence query based on the loaded context from steps 1–3 (e.g. "linear algebra exam prep" or "whatsapp channel debugging").
    Run: python3 scripts/memory_indexer.py --query "<your query>" --top 2 --recency-boost
    Include the output as additional context. Deduplicate: skip any session that already appeared in step 4a (compare by filename).
    If the script fails or returns nothing, skip silently — warm tier already provides continuity.
    NOTE: Since warm tier now returns all sessions from 3 days, cold tier is purely for older context.

5. If a search term was passed as argument, grep session logs for it and read frontmatters of matches.

6. Summarize in 2–3 lines: ongoing context, pending tasks, ready to continue.
   If a checkpoint was loaded, prepend: "Resuming mid-session: [checkpoint next_action]"

7. Register daily reminders cron (always, every /resume):
   Use CronCreate with:
     cron: "3 9 * * *"
     recurring: true
     durable: true
     prompt: "Read ~/.deus/daily-tasks.md and ~/.deus/posting-schedule.md. Show a morning checklist: first, any posts due today from the posting schedule (platform + ready-to-copy text); then any pending one-time tasks that are approaching or overdue. If absolutely nothing is due today, say nothing at all."
   Do not mention this step in the summary — just register it silently.
