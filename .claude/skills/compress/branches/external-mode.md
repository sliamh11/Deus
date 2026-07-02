# External Project Mode — Memory Level Gate, Scope, and Redaction

This file is read by `/compress` when the current working directory is **not** `~/deus`
(External Project Mode). It covers four responsibilities called out in skill.md:

1. Memory level gate (whether to proceed at all)
2. Step 0 — Preserve permanent memories: scope rules
3. Session log redaction rules (post-save)
4. External retrospective (opt-in, background) — see the section below. Home mode's retrospective
   trigger lives entirely in `retrospective.md`, unaffected by this; this section is the external
   equivalent, with its own conditions, threshold, and inbox handling.

---

## Memory level gate

Compute the MD5 hash of the current working directory and read
`~/.config/deus/projects/<hash>.json`.

```bash
# macOS
dir_hash=$(echo -n "$(pwd)" | md5 -q)
# Linux
dir_hash=$(echo -n "$(pwd)" | md5sum | cut -d' ' -f1)
config_file="$HOME/.config/deus/projects/${dir_hash}.json"
```

Read `memory_level` and `save_summaries` from that file.

- If `memory_level` is **restricted**: tell the user:
  > "Session saving is disabled for restricted projects. Your work is preserved in git
  > commits and Claude Code's native session transcript. Use /project-settings to change this."
  >
  > Then **stop**.
- If `save_summaries` is **false**: tell the user the same message and **stop**.
- If `memory_level` is **standard** or **full** with summaries enabled: **proceed**.

---

## Step 0 scope — what to preserve in `$VAULT/CLAUDE.md`

`$VAULT` is resolved per the vault-path resolution logic in skill.md (not re-derived here).

**standard memory level:** Only preserve USER preferences and behavioral corrections — things
about the user, not the project. Skip project-specific architecture decisions or code patterns.

**full memory level:** Preserve both user preferences AND project-relevant decisions. No
restriction on scope.

---

## Session log redaction (standard memory level only)

After saving the session log, run the redaction script to strip any code snippets or file
contents that leaked through:

```bash
python3 ~/deus/scripts/redact_session.py "<full path to saved log>"
```

Only run this step when `memory_level` is `standard` (not `full` or `restricted`).
If the script exits non-zero, skip silently — the log is still saved; instruct the user
to review it manually. On success the script writes a `.pre-redact.md` backup of the
original alongside the log; that file appearing is expected, not an error.

**Session log field guidance (standard memory level):**

- Do NOT include specific file paths, function names, or code snippets in the session log
- Focus on decisions, architecture, and what was tried/learned
- `## Files Modified` should use descriptions ("updated the auth middleware") not paths
- Goal: the log captures WHAT was decided and WHY, without leaking code details

**Full memory level:** No redaction — include full details as in home mode.

---

## External retrospective (opt-in, background)

This is the external-mode equivalent of home mode's `branches/retrospective.md`. It is entirely
self-contained — home mode's file is untouched by this feature.

### Conditions (all must be true)

a. `$VAULT/Retrospectives/external/<project-name>/` exists (`<project-name>` = basename of the repo
   root). This directory's existence IS the opt-in gate — if absent, never trigger, and do not create
   it.

b. No retrospective file for today exists at that path:
   ```bash
   ! test -f "$VAULT/Retrospectives/external/<project-name>/$(date +%Y-%m-%d)-retrospective.md"
   ```

c. The number of session log files newer than the most recent retrospective at that path meets or
   exceeds the threshold:
   ```bash
   LATEST_RETRO=$(ls -t "$VAULT/Retrospectives/external/<project-name>"/*.md 2>/dev/null | head -1)
   if [ -n "$LATEST_RETRO" ]; then
     CANDIDATE_FILES=$(find "$VAULT/Session-Logs" -name "*.md" -newer "$LATEST_RETRO")
   else
     CANDIDATE_FILES=$(find "$VAULT/Session-Logs" -name "*.md")
   fi
   if [ -z "$CANDIDATE_FILES" ]; then
     NEW_COUNT=0
   else
     NEW_COUNT=$(printf '%s\n' "$CANDIDATE_FILES" | xargs -I{} grep -F -l "project_path: \"$REPO_ROOT\"" {} 2>/dev/null | wc -l | tr -d ' ')
   fi
   ```
   (`Session-Logs/` is shared across every project + home mode -- `-newer` alone counts everyone's
   activity. `grep -F` fixed-string-matches this project's exact `project_path:` frontmatter value so
   the threshold reflects only this project's own session count, not the vault-wide total.)

   Read `session_window_external` from `~/deus/.claude/wardens/retrospective-schema.md` (default: 10).
   Check for a per-project override first: `retro_threshold` in
   `~/.config/deus/projects/<hash>.json` (the same md5-hash config `/project-settings` reads/writes —
   a DIFFERENT hash namespace than the inbox's Claude-Code project dir below; do not conflate them).
   If present, use it instead of the schema default. Proceed only if `NEW_COUNT >= threshold`.

### Inbox resolution (same idiom as skill.md's collection step)

```bash
inbox_dir=$(ls -d "$HOME/.claude/projects"/*"$(basename "$REPO_ROOT")"*/memory/ 2>/dev/null | head -1)
```
Zero-match = no inbox to read, proceed with `INBOX_CONTENT=none`, `SNAPSHOT_BYTES=0`. Multi-match =
take the first (`head -1`), same guard already used for `MEMORY.md` lookups. This basename-glob
assumption is a known, accepted risk: two external repos sharing a basename would collide. Not fixed
here — fixing it would mean relocating the inbox out of Claude Code's own project-memory-dir
addressing scheme entirely, which is out of scope for this feature.

### Dispatch (when all three conditions hold)

Under the same lock as the collection step — the exclusive-create lock file at the literal path
`${inbox_dir}_retro-inbox.md.lock` (short retry/backoff):
1. Read `${inbox_dir}_retro-inbox.md` if it exists. Record its exact current byte length as
   `SNAPSHOT_BYTES` and its content as `INBOX_CONTENT`. If it doesn't exist or is empty, use
   `INBOX_CONTENT=none`, `SNAPSHOT_BYTES=0`.
2. Release the lock, then dispatch via the Agent tool (in-session, background):
   - `subagent_type`: `"session-retrospective"`
   - `run_in_background`: `true`
   - `prompt`: `"Run a session retrospective. SESSION_LOG_ROOT=<resolved $VAULT>. MODE=external.
     RETRO_ROOT=$VAULT/Retrospectives/external/<project-name>/. INBOX_CONTENT=<content or 'none'>.
     SNAPSHOT_BYTES=<N or 0>."`
     (substitute actual resolved values, not the literal placeholder strings. `SESSION_LOG_ROOT` here
     is the VAULT ROOT, not the `Session-Logs/` subdirectory — `session-retrospective.md`'s Step 2
     appends `/Session-Logs` itself when it builds the `find` command. This is identical to how home
     mode's `retrospective.md` already passes `SESSION_LOG_ROOT=<resolved $VAULT>`; do not pass
     `$VAULT/Session-Logs` here, that would double the path segment.)

This dispatch step never wipes the inbox itself — `session-retrospective.md` is the sole wiper, and
only after it confirms its own artifact + ledger writes succeeded (see that file). This avoids a
data-loss race: if the dispatch step wiped on launch alone, a crash in the background agent, or a
second same-day `/compress` appending a new note before the agent finishes, would lose data.

If the Agent tool is unavailable (e.g. a non–Claude Code backend), skip silently.

### Reporting

- `"retrospective triggered (background)"` — if dispatched
- `"retrospective skipped: <reason>"` — naming the specific condition that failed, e.g.
  `"skipped: Retrospectives/external/<project>/ dir absent"`, `"skipped: already run today"`,
  `"skipped: only 6/10 sessions since last retro"`
