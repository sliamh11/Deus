Save this Claude Code session to the vault and update the semantic memory index.

First, resolve the vault path by reading `~/.config/deus/config.json` and using the `vault_path` value. If the env var `DEUS_VAULT_PATH` is set, use that instead. All paths below use `$VAULT` to mean this resolved path.

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

   **`pending:` block** — replace entirely with unchecked `[ ]` items from `## Pending Tasks` (max 10). If section is missing or empty, keep current `pending:` unchanged.

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
