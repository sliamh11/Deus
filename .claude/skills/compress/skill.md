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

## Step 0 — Preserve permanent memories

Before saving the session log, scan the conversation for knowledge worth persisting beyond this session:

- Preferences or habits the user revealed
- Decisions made with lasting effect
- Things the user corrected or clarified
- Facts worth knowing in future sessions

Do **not** preserve one-off requests or temporary context.

**Where to save:** Update `$VAULT/CLAUDE.md` using the same compact `key: value` format as the file — no prose bullets. One line per insight. If nothing qualifies, skip silently.

**External Project Mode — standard:** Only preserve USER preferences and behavioral corrections (things about the user, not the project). Skip project-specific architecture decisions or code patterns.

**External Project Mode — full:** Preserve both user preferences AND project-relevant decisions.

If `$VAULT/CLAUDE.md` exceeds 200 lines, archive old content to `$VAULT/CLAUDE-Archive.md`.

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

   a. Extract the one-liner tldr from the session log just saved (first line of the `tldr:` frontmatter field).

   b. Extract all unchecked `[ ]` items from the `## Pending Tasks` section of the session log. Max 10 items. If the section is missing or has 0 items, keep the current `pending:` unchanged and only update `previous:`.

   c. In vault CLAUDE.md:
      - Update the `previous:` block as a rolling list of the last 3 sessions (parallel-safe, prepend-only):
        - Format each entry as: `  - "YYYY-MM-DD: <tldr one-liner>"` (date prefix + first line of tldr, ≤120 chars total)
        - Read the current `previous:` block. If it's a single line (`previous: "..."`), convert it to list format with that entry as the first item.
        - Prepend the new entry at the top of the list.
        - Trim to the 3 most recent entries (drop the oldest).
        - Replace the entire `previous:` block with the updated list.
        - If `previous:` doesn't exist yet, add it before `pending:`.
      - Replace the entire `pending:` block with the extracted `[ ]` items — one item per line, formatted as `  - [ ] ...`.
      - Append the OLD pending block content to `$VAULT/CLAUDE-Archive.md` with header `## Archived YYYY-MM-DD` (create file if missing).

   d. After writing, count total lines in CLAUDE.md. If > 60 lines: identify the oldest non-identity content block (not name/location/style/channels/security/goal/previous/pending) and move it to `$VAULT/CLAUDE-Archive.md` with a date header. Never archive identity fields.

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
