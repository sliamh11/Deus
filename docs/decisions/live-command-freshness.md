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

## Migration note

`deus-cmd.sh` is the last Windows hard-blocker (`project_windows_sot_plan.md`,
Phase 2 → `src/deus-cmd.ts`). The shell added here (`_deus_freshness_check`, the
`sync` arm) is darwin/Linux-guarded and will need a straight translation to
TypeScript when that migration lands.
