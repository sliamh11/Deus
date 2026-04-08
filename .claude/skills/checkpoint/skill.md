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
