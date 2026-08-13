---
name: compress
description: Save this session to the vault and update the semantic memory index
user_invocable: true
---

# /compress

Context-aware session saving. Behavior adapts to home mode vs external project mode.

## Detect mode

Check if the current working directory is the Deus home directory (`~/deus`). If it is → **Home Mode**. Otherwise → **External Project Mode**.

## Resolve `REPO_ROOT` (External Project Mode only)

Resolve this here, unconditionally, the same way `$VAULT` is resolved below -- this file is
processed top-to-bottom by the orchestrating flow, so nothing downstream needs to infer whether a
conditionally-read secondary file happened to establish it first:
```bash
REPO_ROOT=$(git -C "$(pwd)" rev-parse --show-toplevel 2>/dev/null || pwd)
```
Fails soft to `pwd` if the project isn't a git repo. `branches/external-mode.md`'s own sections
reference this value; none of them re-derive it (same relationship as `$VAULT` below -- `external-
mode.md` explicitly defers to skill.md for `$VAULT` and now does the same for `REPO_ROOT`).

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

## Step 0.5 — Flag capturable gotchas

Scan the conversation for a **gotcha**: a single-fact tool/infra surprise discovered at real
cost mid-session (a flag whose default silently misroutes something, an API quirk that wasted
retries, a CLI behavior that contradicted its docs) — distinct from the multi-step *procedures*
`/learn-procedure` captures, and distinct from the user-preference facts Step 0 saves to
`$VAULT/CLAUDE.md`.

If the conversation contains one or more such gotchas that were NOT already captured in a rules
file or memory node during the session, ask **once** (not once per gotcha):

> "This session found N gotcha(s) — <one-line summary each>. Any belong in a rules file or a
> knowledge node before I save?"

Pre-resolve the likely destination from the session's own `project_path` (a project's own
`.claude/rules/*.md`, its `docs/decisions/`, or — for a genuinely cross-project infra fact — the
personal `$AUTOMEM` procedures store) so the user only has to confirm or redirect, not derive the
path themselves. If the user confirms, write the fact directly to the named destination in the
same session — append it, don't defer as a pending task. If the user declines, or the
conversation has no gotcha-shaped content, skip silently — do not ask on every `/compress` run
regardless of content.

## Step 0.6 — Collect retrospective candidates (External Project Mode only, silent)

Home mode: skip entirely — home mode already has its own separate capture paths (CLAUDE.md Step 0,
`/learn-procedure`, `.claude/rules/`). This step exists only to give external projects an equivalent,
since they don't have those paths.

External Project Mode only: scan the just-finished conversation for retro-worthy notes — either a
cross-project methodology insight (something Deus-wide, not tied to this repo) or a repo-specific
insight worth persisting (an architecture decision, a root cause, a gotcha). Do not interrupt the user
and do not grill; this step only stages candidates for later classification when a retrospective
actually fires (see `branches/external-mode.md`).

**Memory-level scope for candidates (mirrors Step 0's scope rule above):** the memory-level gate
(`branches/external-mode.md`) has already stopped this entire step for `restricted` projects before
we get here.
- **standard:** only stage cross-project methodology candidates. Do NOT stage repo-specific
  candidates (anything referencing a file, symbol, PR, migration, or product concept unique to this
  repo) — same restriction Step 0 already applies to `$VAULT/CLAUDE.md`. If a note only makes sense
  with a repo-specific reference, discard it rather than staging it.
- **full:** both candidate types may be staged.

For each candidate that survives the scope check above, append one line to `_retro-inbox.md`, resolved
via the canonical `inbox_dir` resolution below -- **`branches/external-mode.md`'s own "Inbox
resolution" section references this definition, not a copy of it.**

**A basename-substring glob (`*basename*`) was tried here and correctly BLOCKed on review**: it
treats a single unique match as trustworthy, but a single match can still be the WRONG repo entirely
(e.g. `project` uniquely matching `project-other`'s tracked directory) -- the ownership-claim
mechanism only stops a SECOND claimant from later overwriting, it can never verify the FIRST
claimant selected the correct directory in the first place. Verified empirically in this
environment: Claude Code's own `~/.claude/projects/<name>/` directories are named by encoding the
*exact full absolute path* Claude Code was invoked from (`/` replaced with `-`; hyphens, dots, and
other characters passed through unchanged) -- not a basename. A repo at
`/Users/x/Dev/cyber-olympians-platform` produces exactly the directory
`-Users-x-Dev-cyber-olympians-platform`, confirmed by listing real directories in this environment
against `pwd | sed 's/\//-/g'`. This isn't a single-environment guess -- this codebase already
implements and relies on the identical scheme in shipped code:
`scripts/auto_memory_dir.py::_encode_project_dir()` (`path.replace("/", "-")`, called from
`resolve_auto_memory_dir()` to build this exact `~/.claude/projects/.../memory` path, and referenced
elsewhere via `docs/decisions/standards-pack-priority.md` and `.claude/skills/resume/skill.md`).
That implementation also normalizes Windows backslashes first, which this shell snippet doesn't --
consistent with this whole feature already being POSIX-only by design throughout (`mkdir`-based
locks, `set -C`, `shasum`/`sha256sum`, the `stat -f`/`stat -c` fallback), not a gap introduced here.
Each git worktree of the same logical repo gets its OWN separate directory too (a worktree's
`REPO_ROOT` is its own distinct absolute path), which is correct behavior for the inbox -- notes
stay scoped to wherever the session that staged them actually ran.

Use an EXACT match against this deterministic encoding, not a glob, eliminating the substring-collision
class entirely:
```bash
REPO_ROOT="${REPO_ROOT:-$(git -C "$(pwd)" rev-parse --show-toplevel 2>/dev/null || pwd)}"
ENCODED_ROOT=$(printf '%s' "$REPO_ROOT" | sed 's/\//-/g')
CANDIDATE_DIR="$HOME/.claude/projects/${ENCODED_ROOT}/memory/"
if [ -d "$CANDIDATE_DIR" ]; then
  inbox_dir="$CANDIDATE_DIR"
else
  inbox_dir=""
fi
```
The `${REPO_ROOT:-...}` guard is defense in depth, not the primary resolution -- `REPO_ROOT` is
already resolved unconditionally above ("Resolve `REPO_ROOT`"), so this should always be a no-op.
It exists because this exact line was the one that broke (misfiling candidate notes into an
arbitrary project's inbox) the last time `REPO_ROOT` went unset here -- a guard at the consumption
site stays correct even if some future edit to this file's ordering, or a different executing
agent's read path, ever breaks the "resolved earlier" assumption again.

**This closes the broad substring-collision class, but the encoding itself is not perfectly
injective -- keep the ownership claim as defense in depth for the residual risk.** Replacing every
`/` with `-` is ambiguous when the ORIGINAL path also contains a literal `-`:
`/Users/a-b/proj` and `/Users/a/b/proj` both encode to `-Users-a-b-proj`. This is a narrower,
rarer collision than the substring glob's "any repo whose name transitively contains our basename,"
but not zero, and it's a property of a path-encoding scheme this feature doesn't control (Claude
Code's own), not something an exact match alone can rule out. Verify ownership even on an exact
match, using the same atomic noclobber claim `branches/external-mode.md` condition (a) already
established for the sibling basename-collision risk (retrospective storage):
```bash
if [ -n "$inbox_dir" ]; then
  INBOX_OWNER_FILE="${inbox_dir}.repo-root-owner"
  if ( set -C; echo "$REPO_ROOT" > "$INBOX_OWNER_FILE" ) 2>/dev/null; then
    : # first claim on this inbox path -- proceed
  else
    OWNED_ROOT=$(cat "$INBOX_OWNER_FILE" 2>/dev/null)
    if [ "$OWNED_ROOT" != "$REPO_ROOT" ]; then
      inbox_dir=""   # a DIFFERENT repo already owns this inbox path -- fail closed, do not touch it
    fi
    # else: $OWNED_ROOT already equals $REPO_ROOT -- same repo as before, no collision
  fi
fi
```
On a fail-closed result (directory doesn't exist, or ownership mismatch), proceed with
`INBOX_CONTENT=none` exactly as when the directory never existed at all -- never fall back to
reading or writing the ambiguous/foreign path anyway.

If `$inbox_dir` resolves (directory exists at the exact encoded path, ownership verified), append
under the
same lock used by the wipe step in `session-retrospective.md` and the dispatch step in
`branches/external-mode.md`. **This is the canonical definition of that lock -- the other two sites
reference this one, not their own copy.** Use `mkdir` as the atomic exclusive-create primitive, the
same idiom this repo's own `docs/decisions/live-command-freshness.md` ADR already established for
its auto-sync throttle lock (`mkdir` is POSIX-atomic; a plain `test -e || touch` is NOT, and would
silently reopen the exact concurrent-dispatch race `e99cf7d` closed if some future implementation
substituted it in):
```bash
LOCK="${inbox_dir}_retro-inbox.md.lock"
if mkdir "$LOCK" 2>/dev/null; then
  # ... do the append under the lock ...
  rmdir "$LOCK"
else
  # lock held elsewhere -- short retry/backoff, skip silently on failure to acquire within
  # ~2s (a missed candidate this run is low-cost, a corrupted file is not)
fi
```
The appended line itself:
```
<ISO-date> <one-line note>
```

Skip this step entirely if no scripts/Agent tool are available (non-Claude-Code backend).

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

4b. **Archive the source transcript + stamp the backlink** (always, best-effort — LIA-374):
   Run: `python3 ~/deus/scripts/transcript_archive.py --cwd "$PWD" --json --best-effort`
   - On `"ok": true`: append `source_transcript: <sha256>` as a new frontmatter line in the
     just-saved session log (before the closing `---`). Idempotent: if the log already has a
     `source_transcript:` line, leave it unchanged (safe on /compress retries).
   - On `"ok": false`: do NOT block the flow — but surface it in the final Confirm line
     (alongside the indexing/atom-extraction results, which already report operational
     outcomes), e.g. `⚠ source transcript NOT archived: <error>`. Never silently swallow
     the failure. (The Decision Receipt itself stays a pure rendering of saved data.)
   - Restore later via `deus recall --source "<session-log path>"` (byte-exact source).

5. **Delete today's checkpoint** (always):
   Run: `find "$VAULT/Checkpoints" -name "$(date +%Y-%m-%d)-*.md" -delete 2>/dev/null`

6. **Pre-warm semantic cache** (always, background):
   Run: `python3 ~/deus/scripts/memory_indexer.py --query "recent work ongoing tasks" --top 2 --recency-boost > ~/.deus/resume_semantic_cache.txt 2>/dev/null &`

7. **Trigger session retrospective** (background, opt-in):
   - Home mode: read `branches/retrospective.md` for conditions and dispatch instructions (unchanged).
   - External mode: read the "External retrospective" section of `branches/external-mode.md` (has its
     own conditions, threshold, and inbox handling — separate from home mode's).
   Skip silently if any check fails.

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

Confirm with the filename saved, number of pending tasks carried forward, redaction result (standard mode only), indexing result, atom extraction result, source-transcript archival status if it failed, and whether a session retrospective was triggered (home or external mode — report "retrospective triggered (background)" or "retrospective skipped: <reason>").
