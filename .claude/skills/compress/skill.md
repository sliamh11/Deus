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

> If External Project Mode: read `branches/external-mode.md` for memory level checks, redaction rules, and Step 0 scope.

Home mode: always proceed.

## Step 0 — Preserve permanent memories

Before saving the session log, scan the conversation for knowledge worth persisting beyond this session:

- Preferences or habits the user revealed
- Decisions made with lasting effect
- Things the user corrected or clarified
- Facts worth knowing in future sessions

Do **not** preserve one-off requests or temporary context.

**Where to save:** Update `$VAULT/CLAUDE.md` using the same compact `key: value` format as the file — no prose bullets. One line per insight. If nothing qualifies, skip silently.

External Project Mode scope: see `branches/external-mode.md`.

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
continues: "prior-session-filename.md"
superseded_by: "later-session-filename.md"
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

**Cross-linking multi-session investigations (`continues` / `superseded_by`):**
- Both fields are optional. Omit them for standalone sessions (the common case).
- Values are bare filenames (e.g. `auto-compress-bg-gate.md`) when the linked session is in the same date folder. For cross-date links, use a relative path from `Session-Logs/` (e.g. `2026-05-13/prior-topic.md`).
- `continues` — set when this session resumes a prior investigation. Value: the filename of the earlier session. When setting this field, also update the prior session's frontmatter to add `superseded_by` pointing to the new log.
- `superseded_by` — forward-pointer added retroactively to a prior session when a continuation is created. Not set directly; always set via the `continues` step above.

External Project Mode redaction: see `branches/external-mode.md`.

Rules for `decisions:` array:
- Maximum 3 items. Only include decisions that affect future sessions.
- Each item: quoted string, verb-first, ≤12 words.
- Omit the key entirely if no future-relevant decisions were made.

Keep `tldr` to 2–3 lines. Skip sections with no content.

## Post-save steps

After saving the session log:

1. **Update vault CLAUDE.md** (home mode only):

   a. Extract the one-liner tldr from the session log just saved (first line of the `tldr:` frontmatter field).

   b. Extract all unchecked `[ ]` items from the `## Pending Tasks` section of the session log. Also extract any checked `[x]` items — these are tasks completed during this session.

   c. In vault CLAUDE.md:
      - Update the `previous:` block (rolling list of the last 3 sessions) via the
        atomic, lock-serialized splice — do NOT hand-edit the block (concurrent
        `/compress` runs race on a manual read-modify-write and have corrupted the
        file by gluing `pending:` onto `previous:`):
        - Run: `python3 ~/deus/scripts/sync_linear_pending.py --write-previous "YYYY-MM-DD: <tldr one-liner>"`
          (date prefix + first line of tldr, ≤120 chars total).
        - The script prepends the entry, trims to the 3 most recent, converts a
          single-line `previous: "..."` to list form, inserts the block before
          `pending:` if absent, and writes atomically under a file lock — refusing
          (file unchanged, nonzero exit) rather than ever dropping a body key.
        - If it exits non-zero, leave `previous:` as-is and note it; never hand-splice.
      - **Sync pending tasks from Linear** (preferred) or merge from session log (fallback):

        **Linear sync path** (preferred):
        Run: `python3 ~/deus/scripts/sync_linear_pending.py --write`
        The `--write` flag makes the script splice the fresh block into vault CLAUDE.md
        **in place, safely**: it replaces ONLY the indented lines under `pending:` and aborts
        rather than ever dropping a column-0 rule key. Linear IS the source of truth.

        NEVER hand-splice with a "replace everything below `pending:`" / slice-to-EOF
        operation: vault CLAUDE.md has an opening `---` but NO closing `---`, so the rule body
        (`project:` … `index:`) is bare column-0 keys right after the pending list, and such a
        replace deletes the whole body. Use `--write`; if you must edit by hand, replace only
        the `- [ ]` lines and STOP at the first column-0 key.

        If any `[x]` items from the session log reference a Linear identifier that is still in the active list, log a note but do NOT remove it -- the issue's state in Linear is authoritative.
        If the script exits non-zero, read `branches/fallback-merge.md` for the manual merge path.

        No hard item cap on `pending:`. Total file size is the governor: if `pending:` growth pushes CLAUDE.md over the 75-line check in step (d) below, the oldest non-critical items are archived per (d). Do NOT drop a live `[ ]` solely to hit a count limit.

   d. After writing, count total lines in CLAUDE.md. If > 75 lines: read the `critical:` list from the CLAUDE.md frontmatter — that is the authoritative set of protected keys. Identify the oldest non-critical content block (any line whose `key:` prefix is NOT in the `critical:` list) and move it to `$VAULT/CLAUDE-Archive.md` with a date header. Never archive lines whose key appears in `critical:`. If no `critical:` block exists in the frontmatter, fall back to refusing to archive and log a warning — missing schema is safer than guessing. When in doubt, prefer NOT archiving — a 5-line overshoot is fine; losing a load-bearing rule is not.

2. **Auto-redact sensitive patterns** (External Project Mode, standard memory level only):
   See `branches/external-mode.md` for redaction details. Skip in home mode.

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

7. **Trigger session retrospective** (home mode only, background, opt-in):
   Read `branches/retrospective.md` for conditions and dispatch instructions. Skip silently if any check fails.

8. **Render a Decision Receipt** (always, in the chat reply — home + external):
   After the log is saved, render a short user-facing digest so the user can follow what
   changed despite fast delivery — a RENDERING of the data already written (no new content):
   - **Headline** = the saved log's `tldr` first line (the one-line outcome; it usually already
     carries the main PR/issue reference).
   - Up to **3 pivotal-decision bullets** = the saved `decisions[]` array, verbatim, each prefixed
     `→`. The `decisions[]` strings carry no link field, so do NOT fabricate one; where this
     session clearly maps a decision to a specific PR/issue, you may append that reference for
     depth — otherwise render the string as-is.
   - Optional single closing line WITHIN this receipt block: one open thread or "what's next"
     (let the user drive). It is part of the receipt, before the operational Confirm line below.
   Hard cap: **≤5 sentences AND ≤5 bullets, plain language, no deep technical detail** (depth
   lives in the linked PRs/issues). Skip the receipt for a trivial session — one with no
   `decisions[]` recorded AND no PR/merge this session; in that case the Confirm line still
   reports the save. This is the comprehension digest; the operational Confirm line is separate.

Confirm with the filename saved, number of pending tasks carried forward, redaction result (standard mode only), indexing result, atom extraction result, and whether a session retrospective was triggered (home mode only — report "retrospective triggered (background)" or "retrospective skipped: <reason>").
