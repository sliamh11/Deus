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

## `REPO_ROOT` is already resolved

`$REPO_ROOT` is resolved by `skill.md` itself, right after "Detect mode" -- unconditionally, before
this file is ever read, the same relationship this file already has with `$VAULT` ("`$VAULT` is
resolved per the vault-path resolution logic in skill.md — not re-derived here", see the External
retrospective section below). Every section in this file uses that value directly; none of them
re-derive it. (An earlier version of this fix tried to establish `REPO_ROOT` inside this file
instead, reasoning that the whole file gets read as soon as external mode is detected -- but that
reasoning rests on inferring which parts of a conditionally-read secondary file an agent actually
executes, not a guaranteed dependency edge. Resolving it in `skill.md`, which is guaranteed
top-to-bottom, removes the inference entirely. `skill.md`'s own Step 0.6 also carries a defensive
`${REPO_ROOT:-...}` fallback at its actual point of use, as a second, independent line of defense --
see that file.)

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

Every `project_path:` match below uses the `REPO_ROOT` `skill.md` already resolved (see
"`REPO_ROOT` is already resolved" above) -- it is not re-derived here. Every match is a PREFIX match
(exact root, or anything under it), never bare exact-string equality, because `project_path:` records
the working directory a past `/compress` was invoked from, which is not guaranteed to be the repo
root -- see the `grep` pattern in condition (c) for the concrete form.

a. `$VAULT/Retrospectives/external/<project-name>/` exists (`<project-name>` = basename of the repo
   root). This directory's existence IS the opt-in gate — a human creates it by hand
   (`mkdir -p "$VAULT/Retrospectives/external/<project-name>/"`) to opt their project in, which is the
   whole point of keying it by a plain, memorable, human-typable basename rather than an opaque hash —
   never create it yourself, and never key this specific directory by anything else, or opting in
   stops being something a human can do without first running a command to compute an identifier.

   **Ownership check, immediately after confirming the directory exists (before conditions b-d):**
   basename collision between two genuinely different repos is real (an earlier round of this fix
   documented it as an accepted risk and shipped that way; a subsequent independent review round
   correctly rejected that as insufficient -- documenting a state-corruption risk is not the same as
   preventing it). Detect and refuse the collision instead of silently sharing state with whichever
   repo happened to create the directory first:
   **The claim itself must be atomic, not check-then-write** -- `if [ -f "$OWNER_FILE" ]` followed by
   a separate `echo > "$OWNER_FILE"` is exactly the non-atomic shape this feature's other three locks
   (the dispatch marker, the inbox lock, the ledger lock) all deliberately avoid via `mkdir`, for the
   same reason: two different repos both hitting "file doesn't exist yet" simultaneously would both
   proceed to write, and whichever write lands last silently wins -- reopening the exact collision
   this check exists to prevent. Use `set -C` (noclobber) to make the create-if-absent step a single
   atomic operation instead of two:
   ```bash
   OWNER_FILE="$VAULT/Retrospectives/external/<project-name>/.repo-root-owner"
   if ( set -C; echo "$REPO_ROOT" > "$OWNER_FILE" ) 2>/dev/null; then
     : # we just atomically created it -- we are the first (and now only) claimant, proceed
   else
     OWNED_ROOT=$(cat "$OWNER_FILE" 2>/dev/null)
     if [ "$OWNED_ROOT" != "$REPO_ROOT" ]; then
       : # a DIFFERENT repo already claimed this basename -- see below
     fi
     # else: $OWNED_ROOT already equals $REPO_ROOT -- same repo as before, no collision
   fi
   ```
   (`set -C` inside the subshell makes the `>` redirection fail if the target already exists, via the
   same O_EXCL-style atomicity `mkdir` relies on elsewhere in this feature -- verified by hand: two
   sequential atomic-create attempts against the same fresh path, the first succeeds and the second
   fails cleanly with the first repo's content intact, never overwritten.)
   - The `if` branch succeeded (we created the file): first claimant, proceed normally to condition
     (b).
   - The `else` branch, and `$OWNED_ROOT` equals `$REPO_ROOT`: same repo as before (the file already
     existed from an earlier run), no collision. Proceed normally to condition (b).
   - The `else` branch, and `$OWNED_ROOT` does NOT equal `$REPO_ROOT`: a DIFFERENT repo already
     claimed this basename. **Stop here entirely.** Do not evaluate conditions b-d. Do not read or
     write ANY file under this directory -- not the same-day sentinel, not the latest-retrospective
     lookup, not the dispatch marker, not an artifact -- since every one of those is exactly the state
     a real collision would corrupt. Report `"skipped: Retrospectives/external/<project-name>/ is
     claimed by a different repo ($OWNED_ROOT) -- basename collision, not opted in for this repo"` and
     treat this run as not-opted-in for this specific repo.

   This ownership check runs once per basename's entire lifetime (the claim, once made, never
   changes), not once per run -- it is intentionally NOT nested inside the dispatch-in-progress lock
   below, which scopes a single run's dispatch, a narrower and unrelated lifetime.

   This turns a silent state-corruption risk into a loud, safe no-op for whichever repo loses the
   race to claim a shared basename -- it simply never gets automatic retrospectives under that name,
   which is a far better failure mode than corrupting or overwriting the other repo's history. A human
   who hits this can rename their `Retrospectives/external/<name>/` directory to something unambiguous
   and re-opt-in; that's a one-time manual step, not a design gap.

b. No retrospective file for today exists at that path:
   ```bash
   ! test -f "$VAULT/Retrospectives/external/<project-name>/$(date +%Y-%m-%d)-retrospective.md"
   ```

c. The number of session log files newer than the most recent retrospective at that path meets or
   exceeds the threshold:
   ```bash
   LATEST_RETRO=$(ls -t "$VAULT/Retrospectives/external/<project-name>"/*.md 2>/dev/null | head -1)
   if [ -n "$LATEST_RETRO" ]; then
     CANDIDATE_FILES=$(find "$VAULT/Session-Logs" -name "*.md" ! -name "*.pre-redact.md" -newer "$LATEST_RETRO")
   else
     CANDIDATE_FILES=$(find "$VAULT/Session-Logs" -name "*.md" ! -name "*.pre-redact.md")
   fi
   if [ -z "$CANDIDATE_FILES" ]; then
     NEW_COUNT=0
   else
     NEW_COUNT=$(printf '%s\n' "$CANDIDATE_FILES" | xargs -I{} grep -F -l -e "project_path: \"$REPO_ROOT\"" -e "project_path: \"$REPO_ROOT/" {} 2>/dev/null | wc -l | tr -d ' ')
   fi
   ```
   (`Session-Logs/` is shared across every project + home mode -- `-newer` alone counts everyone's
   activity. `! -name "*.pre-redact.md"` excludes the unredacted pre-redaction backups
   `redact_session.py` writes for standard-memory sessions (`branches/external-mode.md`'s own
   redaction section) -- those are plain `.md` files too, and without this exclusion each standard-
   memory session would be counted TWICE (the redacted log AND its own unredacted backup), inflating
   `NEW_COUNT` and triggering the retrospective before the configured number of REAL sessions. The
   two-pattern `grep -F -l -e ... -e ...` matches the exact repo-root path OR any path starting with
   it followed by a real `/` boundary -- NOT a bare substring match, which would also
   false-positive-match an unrelated sibling repo whose name happens to share the same string prefix,
   e.g. matching `/Users/x/project-other` against a `REPO_ROOT` of `/Users/x/project`.)

   Read `session_window_external` from `~/deus/.claude/wardens/retrospective-schema.md` (default: 10).
   Check for a per-project override first: `retro_threshold` in
   `~/.config/deus/projects/<hash>.json` (the same md5-hash config `/project-settings` reads/writes —
   a DIFFERENT hash namespace than the inbox's Claude-Code project dir below; do not conflate them).
   If present, use it instead of the schema default. Proceed only if `NEW_COUNT >= threshold`.

d. **No dispatch is already in progress for this project.** Condition (b) alone is not atomic across
   two concurrent `/compress` invocations -- both can observe "no artifact for today yet" before
   either has actually written one, and would otherwise both dispatch. This condition is enforced by
   the exclusive-create attempt in the Dispatch section below, not by a separate check here -- listed
   as its own lettered condition because failing it means skip, same as a-c.

### Inbox resolution

`inbox_dir` is resolved by `skill.md` Step 0.6 -- that is the canonical definition (an EXACT match
against Claude Code's own path-to-directory-name encoding, plus an atomic ownership-claim
verification even on that exact match, mirroring condition (a)'s ownership check above). This
section does not re-derive or duplicate that logic; use the same resolved value.

**History of getting this right, for context if this section is ever touched again:** a
basename-SUBSTRING glob (`*basename*`) was tried first and initially seemed sufficient once paired
with the ownership claim -- but was correctly BLOCKed on review, because the ownership claim only
stops a SECOND claimant from later overwriting; it can never verify the FIRST claimant matched the
correct directory. A repo named `project` could uniquely substring-match a `project-other` repo's
Claude-Code-tracked directory, claim it as its own, and Step 8.4 would later destructively wipe that
unrelated, non-opted-in repo's inbox -- worse than condition (a)'s retrospective-storage collision,
since that repo never created a `Retrospectives/external/<name>/` directory and never consented to
anything. `skill.md` Step 0.6 now uses an exact match against Claude Code's deterministic full-path
encoding instead of a substring guess, closing that entire collision class -- the ownership claim
remains as defense in depth for the encoding's own narrower residual risk (non-injective when the
original path contains literal hyphens), not as the primary safeguard.

### Dispatch (when conditions a-c hold; condition d is checked inside this section, step 1)

0. **Check the Agent tool is available FIRST, before claiming anything.** If unavailable (e.g. a
   non–Claude Code backend), skip silently -- do not proceed to step 1, and do not create the marker
   below (nothing would ever exist to clean it up).

1. **Claim the dispatch-run marker, before touching the inbox at all.** Use `mkdir` as the atomic
   exclusive-create primitive -- same idiom this repo's `docs/decisions/live-command-freshness.md` ADR
   already established for its own throttle lock, and the same primitive the canonical inbox lock uses
   (see `compress/skill.md` Step 0.6):
   ```bash
   MARKER="$VAULT/Retrospectives/external/<project-name>/.dispatch-in-progress.lock"
   mkdir "$MARKER" 2>/dev/null
   ```
   No retry/backoff here -- an existing marker means a run is genuinely in flight, not a transient
   contention blip, unlike the inbox lock below. If `mkdir` fails (marker already exists): this is
   condition (d) failing -- stop. Report the marker's age alongside the skip so a stale marker
   (background agent died without reaching Step 8.5) is visible rather than silently blocking every
   future run: `"skipped: retrospective already in progress for this project (marker age: <N>h)"`,
   where `<N>` comes from the marker's mtime vs. now. **Get the mtime the same validated way
   `session-retrospective.md`'s mtime-sort loop does, not a plain `||` chain** -- GNU `stat`'s `-f`
   means something different from BSD/macOS's, so `stat -f %m "$MARKER" 2>/dev/null || stat -c %Y
   "$MARKER"` is not a reliable cross-platform fallback:
   ```bash
   MARKER_MTIME=$(stat -f %m "$MARKER" 2>/dev/null)
   case "$MARKER_MTIME" in ''|*[!0-9]*) MARKER_MTIME=$(stat -c %Y "$MARKER" 2>/dev/null) ;; esac
   ```
   Diff `$MARKER_MTIME` against `$(date +%s)` and convert to whole hours -- works identically on a
   directory as it would on a file. If `mkdir` succeeds: condition (d) holds, continue to step 2.
   `session-retrospective.md` removes this marker (`rmdir "$MARKER"`) as its unconditional final action
   (Step 8.5) -- fires on every external-mode run regardless of whether the inbox was empty,
   classification ran, the wipe succeeded/aborted, or artifact generation itself failed. If a
   background agent ever crashes hard enough to skip its own cleanup, the marker is a plain empty
   directory a human can `rmdir` by hand -- there is no automatic staleness expiry by design (Phase 1
   stays manual/simple, matching the ledger's graduation-check philosophy elsewhere in this feature);
   the age reported above is what lets a human notice it's worth deleting, rather than an unexplained
   permanent skip.

2. **Re-run `inbox_dir` resolution here explicitly -- do not assume it's still set from earlier in the
   same invocation.** This dispatch step and `skill.md` Step 0.6 (where `inbox_dir` is canonically
   defined) are separate sections read at separate points in the overall `/compress` flow; relying on
   shell-variable persistence across that gap is exactly the class of cross-section assumption that
   already caused the `REPO_ROOT` bug (fixed in `b5da60a` after 3 rounds of chasing it). Re-running the
   resolution is safe and idempotent -- the ownership claim inside it only writes once per inbox path's
   lifetime; every subsequent run (including this one) just reads and confirms the match:
   ```bash
   ENCODED_ROOT=$(printf '%s' "$REPO_ROOT" | sed 's/\//-/g')
   CANDIDATE_DIR="$HOME/.claude/projects/${ENCODED_ROOT}/memory/"
   if [ -d "$CANDIDATE_DIR" ]; then
     inbox_dir="$CANDIDATE_DIR"
     INBOX_OWNER_FILE="${inbox_dir}.repo-root-owner"
     if ( set -C; echo "$REPO_ROOT" > "$INBOX_OWNER_FILE" ) 2>/dev/null; then
       : # first claim -- proceed
     else
       OWNED_ROOT=$(cat "$INBOX_OWNER_FILE" 2>/dev/null)
       [ "$OWNED_ROOT" != "$REPO_ROOT" ] && inbox_dir=""   # fail closed, different repo owns it
     fi
   else
     inbox_dir=""   # no directory at the exact encoded path -- no inbox, do not guess
   fi
   ```
   (Full rationale for the exact-match encoding, why a basename-substring glob was rejected on
   review, and why the ownership claim stays as defense in depth against the encoding's own residual
   non-injectivity: `skill.md` Step 0.6, the canonical definition -- not repeated here beyond the
   literal steps needed to reproduce the same result.)

   **If `inbox_dir` is empty after the above: skip locking and reading entirely.** Do not attempt to
   acquire any lock -- `${inbox_dir}_retro-inbox.md.lock` with an empty `inbox_dir` is just the literal
   relative path `_retro-inbox.md.lock` in whatever directory this step happens to run from, which is
   either a permissions failure (unrelated to any real inbox) or, worse, a lock silently taken on some
   unrelated file in the current directory. Go straight to dispatch (step 3, empty-inbox branch) with
   `INBOX_DIR=none`, `INBOX_CONTENT=none`, `SNAPSHOT_BYTES=0`, `SNAPSHOT_HASH=none` -- no lock was ever
   acquired on this path, so step 3's dispatch does not release one either.

   Otherwise, acquire the same lock the collection step defines — see `compress/skill.md` Step 0.6 for
   the canonical `mkdir`-based primitive; here it is `mkdir "${inbox_dir}_retro-inbox.md.lock"` (short
   retry/backoff). **If the lock can't be acquired within the retry window: remove the marker from
   step 1 before skipping** (do not leave it claimed with nothing dispatched to ever release it) --
   report `"skipped: could not acquire inbox lock"`. Once acquired: read `${inbox_dir}_retro-inbox.md`
   if it exists. Record its exact current byte length as `SNAPSHOT_BYTES` and its content as
   `INBOX_CONTENT`. If it doesn't exist or is empty, use `INBOX_CONTENT=none`, `SNAPSHOT_BYTES=0`,
   `SNAPSHOT_HASH=none` and skip the hash command below. Otherwise also compute a hash of exactly those
   `SNAPSHOT_BYTES` bytes as `SNAPSHOT_HASH`:
   ```bash
   SNAPSHOT_HASH=$(head -c "$SNAPSHOT_BYTES" "${inbox_dir}_retro-inbox.md" | shasum -a 256 | cut -d' ' -f1)
   ```
   (Linux: `sha256sum` in place of `shasum -a 256`.) **This hash, not `INBOX_CONTENT` itself, is what
   the wipe step verifies against later** -- `INBOX_CONTENT` still travels in the dispatch prompt below
   because the agent needs the actual text to classify, but that text passes through a natural-language
   Agent-tool prompt string, which is not a guaranteed-verbatim channel (whitespace/newline
   normalization, markdown-significant characters). A hash computed directly from the file's bytes,
   never round-tripped through that prompt text, is the thing worth trusting for a safety check --
   using `INBOX_CONTENT` for that check would either falsely abort every wipe if the channel ever
   mangles it (safe but leaves the inbox growing unboundedly forever with no signal), or worse, falsely
   pass if a plausible-but-wrong reconstruction happened to match.

3. **If you took step 2's empty-`inbox_dir` branch: no lock was acquired, so skip straight to
   dispatching below -- there is nothing to release.** Otherwise (step 2's non-empty branch, lock
   held): `rmdir "${inbox_dir}_retro-inbox.md.lock"` to release it, THEN dispatch via the Agent tool
   (in-session, background):
   - `subagent_type`: `"session-retrospective"`
   - `run_in_background`: `true`
   - `prompt`: `"Run a session retrospective. SESSION_LOG_ROOT=<resolved $VAULT>. MODE=external.
     REPO_ROOT=<resolved $REPO_ROOT>. WINDOW=<the threshold value resolved under condition (c) above --
     session_window_external or its per-project retro_threshold override, whichever was actually used>.
     RETRO_ROOT=$VAULT/Retrospectives/external/<project-name>/. INBOX_DIR=<resolved $inbox_dir, or
     'none' if it never matched>. INBOX_CONTENT=<content or 'none'>. SNAPSHOT_BYTES=<N or 0>.
     SNAPSHOT_HASH=<hash or 'none'>."`
     `REPO_ROOT`, `WINDOW`, and `INBOX_DIR` are passed explicitly rather than left for the dispatched
     agent to re-derive, matching how `RETRO_ROOT` already works — a background agent's working
     directory is not guaranteed to match the dispatching session's `$PWD`, so re-deriving `REPO_ROOT`
     via a `.git/` walk-up (session-retrospective.md Step 1's normal home-mode resolution) is
     unreliable in external mode; `INBOX_DIR` in particular has NO other resolution path available to
     the dispatched agent at all -- `inbox_dir` above is a local bash variable in THIS dispatching
     step, never otherwise visible to the background agent, so without passing it explicitly the wipe
     step in `session-retrospective.md` would have no path to read, lock, or strip from.
     (substitute actual resolved values, not the literal placeholder strings. `SESSION_LOG_ROOT` here
     is the VAULT ROOT, not the `Session-Logs/` subdirectory — `session-retrospective.md`'s Step 2
     appends `/Session-Logs` itself when it builds the `find` command. This is identical to how home
     mode's `retrospective.md` already passes `SESSION_LOG_ROOT=<resolved $VAULT>`; do not pass
     `$VAULT/Session-Logs` here, that would double the path segment.)
   If the Agent tool call itself fails to launch (rather than just being unavailable, already handled
   in step 0): remove the marker before reporting the failure, same reasoning as the lock-acquisition
   case above -- a claimed-but-orphaned marker with no agent running to release it is worse than a
   failed dispatch that's free to retry next cycle.

This dispatch step never wipes the inbox itself — `session-retrospective.md` is the sole wiper, and
only after it confirms its own artifact + ledger writes succeeded (see that file). This avoids a
data-loss race: if the dispatch step wiped on launch alone, a crash in the background agent, or a
second same-day `/compress` appending a new note before the agent finishes, would lose data.

### Reporting

- `"retrospective triggered (background)"` — if dispatched
- `"retrospective skipped: <reason>"` — naming the specific condition that failed, e.g.
  `"skipped: Retrospectives/external/<project>/ dir absent"`, `"skipped: already run today"`,
  `"skipped: only 6/10 sessions since last retro"`, `"skipped: retrospective already in progress
  for this project (marker age: 2h)"`, `"skipped: could not acquire inbox lock"`
