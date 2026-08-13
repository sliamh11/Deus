# Live-Command Freshness

**Status:** Accepted
**Date:** 2026-05-30
**Scope:** The `deus` CLI launcher (`deus-cmd.sh`) and how the live install stays current with `main`

## Context

The `deus` command is a symlink to `~/deus/deus-cmd.sh`. Its subcommands either
`exec` compiled `dist/*.js` (TypeScript, needs a build) or run `scripts/*.py`
straight from the working tree. So **shell and Python features go live the moment
the primary checkout's working tree contains them** — there is no separate install
step for them.

This couples the live command to whatever branch `~/deus` happens to be on. When
the primary checkout drifts off `main`, or sits behind `origin/main`, the live
command silently ships stale behavior. This bit us concretely: `deus usage` was
implemented in a worktree, reviewed, and merged to `main` via PR — but it wasn't
live in the terminal, because `~/deus` was parked on an old feature branch
(`feat/linear-scoring-impact`) left over from a prior session.

A brainstorm reframed the issue as **two distinct problems**:

- **Problem A — work location** (worktree hygiene). "Feature work happens in a
  worktree; `~/deus` stays clean." This is what the rule in
  `core-behavioral-rules.md` already states.
- **Problem B — live-command freshness** (correctness). "When I type `deus <cmd>`,
  do I get merged-`main` behavior?"

The symptom we hit is **Problem B, not A**. Two facts pin this down:

1. The TypeScript service already runs from `dist/` (decoupled — only stale after a
   missed build/restart). The entire live-drift surface is `deus-cmd.sh` plus the
   `scripts/*.py` calls, which run from the mutable working tree.
2. **There is no auto-pull anywhere.** Even with perfect worktree discipline,
   `~/deus` is stale-on-`main` after every merge until a human pulls. Worktree
   discipline cannot fix B.

Framed correctly, this is a **release-freshness problem** (the live command runs
whatever is in the mutable working tree), not a branch-discipline problem.

## Decisions

1. **Ship a freshness nudge + `deus sync` (chosen).**
   - `_deus_freshness_check` runs once on every `deus` invocation (before the main
     dispatch). It is warn-only, never blocks, always returns 0, and is throttled
     to one real check per 600s via `~/.config/deus/freshness-stamp`. It refreshes
     the cached `origin/main` ref with a detached background `git fetch` (no
     hot-path network), then does an **offline** comparison: if the live tree is
     off `main` or behind `origin/main`, it prints one stderr nudge to run
     `deus sync`. darwin/Linux only (Windows port of `deus-cmd.sh` is pending).
   - `deus sync` makes the live install current in one command: `git fetch` +
     `git merge --ff-only origin/main` + rebuild/restart (reusing
     `_build_and_restart`). It is **non-destructive** — it refuses to run on a
     feature branch or a dirty tree, and never auto-switches branches.

2. **Defer decoupling the install (Option 2) — documented, not built.**
   The root-cause fix is to stop the live command from reading the mutable dev
   checkout at all, by making the install a **pinned detached worktree**:
   - `git worktree add --detach ~/deus-live origin/main` (the `--detach` is
     load-bearing — git refuses to check out the `main` *branch* in two worktrees,
     but a detached HEAD at `origin/main` is allowed).
   - Repoint `/usr/local/bin/deus` and `com.deus.plist` (`WorkingDirectory` +
     `dist/index.js` path) at `~/deus-live`. `deus sync` becomes
     `git reset --hard origin/main` (the live tree is never hand-edited, so a hard
     reset is always safe). `~/deus` then demotes to just-another-dev-worktree and
     its branch drift stops mattering.

   This is a blue-green / symlink-swap deploy pattern applied to a local CLI. It is
   **deferred** because the drift has only bitten once; building it now would be
   solving a problem we have not yet repeatedly encountered.

   **Footguns to resolve before ever building Option 2:**
   - `~/.local/bin` is first on `$PATH` but `~/.local/bin/deus` does not exist
     today, so `/usr/local/bin/deus` wins. `_build_and_restart` *creates*
     `~/.local/bin/deus` on its next run, which would then **shadow** a
     `/usr/local/bin` swap. Both symlinks (or `_build_and_restart`'s `LINK_DIR`)
     must move together.
   - `container-mounter` and `com.deus.plist` hardcode `~/deus` paths — audit
     before relocating the live tree.
   - GitHub merges fire no local hook, so the natural push-refresh trigger is a
     `gh pr merge` wrapper that runs `deus sync` on success.

3. **Trigger to revisit Option 2:** recurrent off-`main`/behind drift despite the
   nudge. If the nudge proves insufficient in practice, escalate to the pinned
   detached worktree.

4. **Passive auto-sync, added 2026-08-08 (LIA-529+) — supersedes this decision's
   "warn-only" stance for the trigger condition only, not Option 2's install design.**
   Decision 1 above deliberately chose nudge-only because "the drift has only bitten
   once." This addendum was **not** triggered by Decision 3's own revisit condition
   (recurrent drift) — it was requested directly by the user, who confirmed the
   tradeoff explicitly (every-invocation trigger, unattended restart) in-session.
   Recording that honestly rather than implying the original trigger fired.

   `_deus_auto_sync "$@"` runs immediately after `_deus_freshness_check "$@"` on
   every `deus` invocation (same darwin/Linux gate, same `root`/`--print-identity`
   exclusion — those stay side-effect-free). Unlike the nudge, it does real work in
   a single detached, non-blocking background subshell, throttled independently
   (its own 600s window, via the atomic lock described below) so it never shares
   cadence with the read-only freshness check:

   - **This checkout**: fetch + `merge --ff-only` `origin/main`. Refuses on a
     linked worktree (the same PRIVATE-WIPE guard `deploy` already has), off-`main`,
     or no `origin` remote. On real HEAD movement (compared before/after, not
     "merge exited 0" — this intentionally borrows `deploy`'s diff-gated restart,
     **not** `sync`'s unconditional one), rebuilds and restarts via the existing
     `_build_and_restart`. On macOS that means `launchctl kickstart -k` — a hard
     kill, not a graceful `SIGTERM` — so an unattended restart can, in principle,
     interrupt an in-flight channel message being processed at that exact instant.
     Pre-existing behavior of `_build_and_restart` itself; new here only in that it
     can now fire without an explicit `deus sync`/`deploy`.
   - **A second, generic repo mirror**: entirely config-driven via three keys
     (`secondary_sync_path`, `secondary_sync_upstream_identity`,
     `secondary_sync_fork_identity`) — unset by default, so this no-ops for anyone
     who hasn't configured it. When all three are set and both remotes'
     (`origin`/`fork`) URLs verify against the configured identity strings: same
     fetch/ff-only-merge contract, then on real movement, `git push fork main`
     (never forced, never retried within a run). This capability is intentionally
     generic — `deus-cmd.sh` itself names no specific real repository, fork, or
     upstream project; which real checkout it points at is local, uncommitted
     runtime config.
   - **Dirty-tree handling** (either repo): stash-safe, never a bare `git
     stash`/`pop` — the entry is snapshotted with a unique tag, restored by that
     exact entry (never by stack position, since the shared stash stack can have
     concurrent activity from other sessions). **Never auto-dropped, even on a
     fully successful restore** — a real CRITICAL finding, confirmed empirically:
     `git stash drop` only accepts the positional `stash@{N}` form (it rejects a
     raw commit SHA outright, unlike `apply`, which accepts one directly), so
     cleanup would require resolving our own SHA to its current position via a
     fresh `stash list`, then dropping that position — and a concurrent process
     pushing a stash in the exact window between those two commands shifts
     every existing entry's position, making the drop delete THEIR entry
     instead of ours. Real, in-scope threat (the shared stash stack is
     explicitly used by concurrent sessions/tools here), not theoretical, with
     no atomic "drop by content identity" primitive available to close it.
     Leaving a successfully-applied entry behind is harmless and recoverable;
     racing to delete the wrong one is not — logged plainly so it's not a
     silent surprise. A restore conflict runs `git reset --merge` to leave the
     tracked/index state clean rather than corrupted, and preserves the stash
     entry intact for manual recovery. `reset --merge` alone doesn't cover
     everything: a failed `--index` apply can already have partially restored
     untracked files (from the stash's own untracked-files parent, present when
     `-u` was used) before hitting the conflict, leaving them behind. **Never
     auto-deleted, even when content-verified as an exact match.** An early
     draft deleted a leftover path once `git hash-object` confirmed its content
     exactly matched what the stash stored — but the check and the `rm -f` are
     still two separate operations, and a concurrent process (this worker runs
     detached while the user keeps working) can replace that exact path with
     genuinely new content in the gap between them; no amount of
     "check right before acting" closes a true TOCTOU race, and POSIX offers no
     atomic "delete only if content still matches X" primitive. Purely
     informational instead: each leftover path is still identified (via the
     untracked-parent's exact file list, NUL-delimited to survive non-ASCII
     filenames) and content-checked, but only to decide what to *log* — a
     content match is reported as likely safe for the user to remove manually;
     a mismatch (someone else's work at that path) is left alone and not even
     mentioned as "ours." An earlier draft also added an unconditional
     tracked-state "safety net" (first `checkout HEAD -- .`, later `reset --hard
     HEAD`) for git-version-agnostic behavior — removed for the identical
     TOCTOU reason, and never proven necessary on this git version either
     (`reset --merge` alone already fully reverts a non-conflicting tracked
     edit, confirmed by ablation testing during review) — tracked state relies
     on `reset --merge`'s own behavior, same as before this feature ever added
     the extra step.
   - **Kill switch**: `DEUS_AUTO_SYNC=0`, or config key `auto_sync_enabled` set to
     `"false"`.
   - **Ignored-file collision guard**: confirmed empirically before this was added —
     `git merge --ff-only` refuses to overwrite a plain untracked file ("would be
     overwritten by merge", exit 1, file untouched), but does NOT extend that
     protection to a gitignored file at the same path; it silently overwrites it,
     exit 0, no warning, the instant an incoming commit starts tracking that path.
     The dirty-tree check above deliberately excludes ignored files, so before
     merging, `_auto_sync_fetch_merge` separately diffs the incoming change against
     locally-ignored files present and aborts (never merges, logs a warning) on any
     collision — rather than sweeping arbitrary ignored content (`node_modules/`,
     `.env`, etc.) into a stash, which has nothing to do with the sync itself.
   - **Throttle**: an `mkdir`-based atomic lock (`~/.config/deus/.auto-sync.lock`),
     not a separate read/compare/write stamp file — `mkdir` is POSIX-atomic, so two
     concurrent `deus` invocations can't both pass the throttle and spawn
     overlapping mutating work the way a plain stamp-file check could. The lock's
     own mtime is the throttle timestamp; past 600s it's reclaimed as stale. The
     reclaim itself (`rmdir` then `mkdir`) isn't atomic as a pair, so two callers
     landing on the exact same stale instant can both proceed — a narrow,
     low-consequence race bounded to that one boundary, not the whole window.
   - **Known limitation**: a stash-restore conflict short-circuits before the
     restart/push decision, even though the underlying merge may have already
     moved HEAD — matches this design's stated stash-safety contract (no restart,
     no push on that path) but means a conflict can leave the live service (or the
     secondary mirror's fork) stale relative to the last successfully-merged commit
     until a later run's fetch finds something new to merge. Fixing this (gating
     restart/push purely on HEAD movement, independent of the stash outcome) is a
     real design change, not a bugfix — it would need its own plan-review round.

   Both directions (this checkout / the secondary mirror) are fully independent —
   one repo missing, misconfigured, or failing never affects the other.

## Migration note

`deus-cmd.sh` is the last Windows hard-blocker (`project_windows_sot_plan.md`,
Phase 2 → `src/deus-cmd.ts`). The shell added here (`_deus_freshness_check`, the
`sync` arm) is darwin/Linux-guarded and will need a straight translation to
TypeScript when that migration lands.
