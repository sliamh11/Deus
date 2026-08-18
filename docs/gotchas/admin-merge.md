# Admin-Merge Branch Cleanup

**Read this when you are running `gh pr merge --admin --delete-branch`.**

Moved verbatim out of `.claude/rules/orchestration-rules.md` (which is
always-loaded, and was over Claude Code's 40.8k per-file limit) so it loads
only when the task calls for it. No rule below has been reworded.
Routed by [`.mex/ROUTER.md`](../../.mex/ROUTER.md); index at
[`docs/gotchas/INDEX.md`](INDEX.md).

- `gh pr merge --admin --delete-branch` can succeed at merging on GitHub while failing its own local post-merge step (`failed to run git: fatal: 'main' is already used by worktree at '<path>'`) whenever the invoking session's `main` checkout is a separate worktree — the common case in this repo, which runs many parallel worktrees off one bare/primary checkout. When that local step fails, the remote branch is silently left behind despite `--delete-branch` being passed (confirmed twice, 2026-08-03/04, PR #1106 and PR #1115 — identical failure both times).

- Always verify after any `--admin --delete-branch` merge: `gh pr view <pr> --json state,mergedAt,mergeCommit` to confirm the merge actually landed (it does, even when the branch-delete step errors), then check the branch is really gone: `gh api repos/<owner>/<repo>/branches/<branch>` (404 = clean; 200 = still present). If still present, delete it manually: `gh api -X DELETE repos/<owner>/<repo>/git/refs/heads/<branch>`.

- Don't treat the local git error as a merge failure — it isn't one. Don't retry the `gh pr merge` command on that error either (the PR is already merged; a retry just re-errors on "already merged").

- **Related but distinct**: personal memory `feedback_worktree_workflow.md` documents a different symptom of a related root cause — gh's local cleanup instead fails at `git branch -D <feature-branch>` (refused because that branch is checked out in its own worktree), recommending *omitting* `--delete-branch` entirely and doing manual `git push origin --delete` + `git worktree remove` + `git branch -D` afterward. This section's symptom is gh instead failing to `git checkout main` locally. Both are plausible depending on which worktree cwd `gh pr merge` is invoked from; either way, verify-after-merge (this section) is the safe fallback regardless of which local step fails.
