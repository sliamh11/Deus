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
   - Tools / infra session → $VAULT/INFRA.md
   - If unclear → read both (they're small, ~10 lines each)

3. Check for a mid-session checkpoint from today:
   Run: ls -t "$VAULT/Checkpoints/"$(date +%Y-%m-%d)-*.md 2>/dev/null | head -1
   (A shell glob — not `find | xargs`. `find | xargs` breaks when the vault path contains spaces/unicode, because xargs word-splits the filenames and ls silently fails on the broken partials.)
   If a file is found → read it fully. Note "resuming mid-session checkpoint" in the summary.

4a. Load warm tier — gap-aware (no API cost):

    First, determine the gap since the last session:
    Run: python3 scripts/memory_indexer.py --recent 1 2>/dev/null
    Note the date of the most recent session.
    
    Compute gap = today's date minus that date (in days).
    
    ALWAYS load the most recent session in full (regardless of gap):
    Run: python3 scripts/memory_indexer.py --recent 1
    
    Then load remaining recent sessions based on gap:
    - If gap < 1 day (active session): load sessions from last 3 days in compact mode
      Run: python3 scripts/memory_indexer.py --recent-days 3 --compact
    - If gap >= 1 day (returning after time away): load sessions from last 7 days in compact mode
      Run: python3 scripts/memory_indexer.py --recent-days 7 --compact
      This ensures visibility after vacations/breaks without overloading context.
    
    Deduplicate: skip the most recent session if it appears in the --recent-days output (compare by filename).
    
    Include combined output as "Recent Sessions" context.
    
    FALLBACK — if the script fails, fall back to:
    find "$VAULT/Session-Logs" -name "*.md" -not -path "*/.obsidian/*" -print0 2>/dev/null | xargs -0 ls -t 2>/dev/null | head -6
    (Must use `-print0 | xargs -0` — plain `find | xargs` word-splits on whitespace and silently drops paths with spaces/unicode.)
    Then read frontmatter only (lines between the two --- markers) of those files.

4b. Load learnings — what's new since last /resume (no API cost):
    Run: python3 scripts/memory_indexer.py --learnings --since 7 --top 3
    If output is non-empty, include it as a "What's Emerging" section after recent sessions.
    If no output (nothing new), skip silently — silence signals stability.

4c. Load cold tier — semantically relevant older sessions:
    Extract the `topics:` array from the most recent session log's frontmatter (available in the --recent 1 output from step 4a). Join topics with spaces as the query.
    Example: topics: [evolution, memory, indexer] → query: "evolution memory indexer"
    If currently in a git repo, prepend the current branch: run `git branch --show-current 2>/dev/null` and prepend if non-empty.
    Example: "fix/silent-failures evolution memory indexer"
    Fallback: if topics are empty or unavailable, use "recent work ongoing tasks".
    Run: python3 scripts/memory_indexer.py --query "<constructed query>" --top 2 --recency-boost
    Deduplicate against warm tier: compare session filenames from the --recent-days output against cold tier results. Skip any cold tier session that already appeared in the warm tier.
    Include the output as additional context.
    If the script fails or returns nothing, skip silently — warm tier already provides continuity.
    NOTE: Since warm tier now returns all sessions from 3 days, cold tier is purely for older context.

5. If a search term was passed as argument, grep session logs for it and read frontmatters of matches.

6. Summarize in 2–3 lines: ongoing context, pending tasks, ready to continue.
   The `previous:` field in CLAUDE.md is a rolling list of the last 3 sessions (format: `"YYYY-MM-DD: <tldr>"`). Read all 3 entries to understand recent context — not just the most recent one.
   If a checkpoint was loaded, prepend: "Resuming mid-session: [checkpoint next_action]"

   **Time-label rule (enforced here, no exceptions):** Always label prior work as "Previous session:" — never use "Yesterday", "Earlier today", "Last week", or any relative time phrase. Compare session dates against `currentDate` from system context before writing anything. If the most recent session date equals today → still say "Previous session:", not "Earlier today". Relative labels are always wrong because sessions can span days or repeat within a day.

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
