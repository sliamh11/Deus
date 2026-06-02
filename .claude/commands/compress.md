Save this Claude Code session to the vault and update the semantic memory index.

First, resolve the vault path using this **per-instance** order (highest priority first). `$VAULT` means the resolved path:

1. `DEUS_VAULT_PATH` env var, if set.
2. `vault_path` in `./.deus/config.json` (the current working directory's instance-local config), if that file exists. If the file exists but has no usable `vault_path`, STOP and tell the user it is present but missing `vault_path` — do **not** fall through to the global config (that fall-through is what corrupts another instance's vault).
3. `vault_path` in `~/.config/deus/config.json` (global fallback).

Tiers 1 and 2 are the per-instance mechanisms: when several Deus instances run on one machine they share the global config (tier 3), so resolving from it alone can silently point this instance's `/compress` at a different instance's vault and corrupt its memory. The instance-local `./.deus/config.json` keeps each instance self-contained. The `memory_indexer.py` calls below resolve the vault by the same order, so their writes land in this instance's vault too.

Review the conversation and create a session log at:
$VAULT/Session-Logs/YYYY-MM-DD/{topic}.md

Create the YYYY-MM-DD folder if it doesn't exist. The filename should be the topic only (no date prefix), since the date is already in the folder name.

Use this format — `tldr` frontmatter is mandatory, `decisions` is mandatory when applicable, full sections are optional:

```markdown
---
type: session
date: YYYY-MM-DD
topics: [topic1, topic2]
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

Rules for `decisions:` array:
- Maximum 3 items. Only include decisions that affect future sessions (tool choices, format preferences, architectural calls, explicit rejections).
- Each item: quoted string, verb-first, ≤12 words.
- Omit the `decisions:` key entirely if the session had no future-relevant stable decisions (e.g. purely exploratory sessions).

Keep `tldr` to 2–3 lines. Skip sections with no content.

After saving the session log, do three things:

1. Update vault CLAUDE.md (`$VAULT/CLAUDE.md`):

   **`previous:` block** — rolling list of the last 3 sessions (parallel-safe, prepend-only):
   - Format each entry as: `  - "YYYY-MM-DD: <tldr one-liner>"` (≤120 chars total)
   - Read the current `previous:` block. If it's a single line (`previous: "..."`), convert it to list format first.
   - Prepend the new entry at the top. Trim to 3 entries max (drop the oldest).
   - If `previous:` doesn't exist, add it before `pending:`.

   **`pending:` block** — merge, never replace:
   1. Read the current `pending:` block from CLAUDE.md. If missing, treat as empty list.
   2. Remove any items that match `[x]` completed tasks from the session log (fuzzy match on description).
   3. Add any new `[ ]` items from the session log that don't already exist (avoid duplicates).
   4. Cap at 10 items. If over 10, archive the oldest to `$VAULT/CLAUDE-Archive.md`.
   5. Write the merged list back to `pending:`.

2. Index the new log into the semantic memory index by running:
   python3 scripts/memory_indexer.py --add "<full path to saved log>"
   (If the script fails, skip silently — the log is still saved.)

3. Extract atomic facts from the session log:
   python3 scripts/memory_indexer.py --extract "<full path to saved log>"
   (If the script fails or prints "No decisions content — skipping extraction", skip silently.)

4. Delete today's checkpoint now that the session log supersedes it:
   find "$VAULT/Checkpoints" -name "$(date +%Y-%m-%d)-*.md" -delete 2>/dev/null
   (Silent — no output expected.)

5. Pre-warm the startup semantic cache in the background (non-blocking):
   python3 scripts/memory_indexer.py --query "recent work ongoing tasks" --top 2 --recency-boost > ~/.deus/resume_semantic_cache.txt 2>/dev/null &
   (Run this after step 2 completes so the new log is already indexed. Always run — no skip condition.)

Confirm with the filename saved, number of pending tasks carried forward, indexing result, and atom extraction result (N new, K corroborated — or skipped).
