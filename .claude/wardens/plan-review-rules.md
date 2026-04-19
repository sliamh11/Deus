# Plan Review Rules — Wardens/plan-reviewer

> Rules the `plan-reviewer` agent checks against BEFORE implementation.
> Add a new rule by appending a section. No agent edit needed.
>
> Format per rule: `Severity`, `Applies when`, `Check`, `Rule`, `Cite`.
> Severity: `blocking` (must fix before SHIP) · `warning` (should address) · `informational` (flag for author's awareness).

## public-repo-generic
**Severity:** blocking
**Applies when:** Plan touches files under `~/deus/src/`, `~/deus/scripts/`, `~/deus/docs/`, or any other tracked public-repo path.
**Check:** Does the plan hardcode personal values (usernames, emails, absolute personal paths, specific IDs, session tokens)?
**Rule:** Public-repo code must be user-agnostic. Personal fixtures belong in `~/.claude/`, `~/.config/deus/`, or `src/private/`.
**Cite:** `feedback_public_repo_generic`

## commit-scoping
**Severity:** blocking
**Applies when:** Plan touches BOTH public-repo files AND personal tooling (`~/.claude/`, `~/.config/deus/`, user-local dotfiles).
**Check:** Are the changes conceptually one concern, or multiple?
**Rule:** Split into separate commits/PRs. Never bundle personal-tooling changes into public-repo PRs.
**Cite:** `feedback_scope_commits_by_concern`

## private-override
**Severity:** warning
**Applies when:** Plan edits a public file that has a counterpart under `src/private/`.
**Check:** Does the plan modify the public file when a private override exists, or vice versa?
**Rule:** Private overrides take precedence. If both exist, plan must specify which to edit and why.
**Cite:** vault CLAUDE.md `private-override:` line; `feedback_private_override`

## active-sequence-conflict
**Severity:** blocking
**Applies when:** An active `project_*.md` memory describes an in-progress sequence (e.g., `project_error_discipline_plan.md` → PRs #214–#216).
**Check:** Does this plan skip ahead in the sequence, contradict a prior commitment, or touch files owned by an open PR in the sequence?
**Rule:** Finish or explicitly re-plan the active sequence before branching into parallel work on the same surface.
**Cite:** The relevant active `project_*.md`

## no-db-deletion
**Severity:** blocking
**Applies when:** Plan involves deleting user data, removing records, or dropping tables.
**Check:** Does the plan propose a hard delete?
**Rule:** Soft-delete only — set a deleted-at field, archive, or tombstone. Never hard-delete user data.
**Cite:** `docs/decisions/no-db-deletion.md`

## cross-platform-intent
**Severity:** warning
**Applies when:** Plan adds new code under `~/deus/src/` or `~/deus/scripts/`.
**Check:** Does the plan acknowledge OS-specific behavior (paths, commands, syscalls) and route through `src/platform.ts` where relevant?
**Rule:** Default to cross-platform. Flag any OS-specific code loudly.
**Cite:** `feedback_cross_platform_default`; `project_windows_sot_plan`

## secrets-design
**Severity:** blocking
**Applies when:** Plan handles credentials, API keys, OAuth tokens, or webhook secrets.
**Check:** Does the plan commit secrets to git (anywhere — `.env`, fixtures, tests)? Does it rely on env vars without `.env.example` documentation?
**Rule:** No credentials in git ever. Use `.env` + `.env.example`; gitignore strictly. For rotation, use the credential-proxy pattern.
**Cite:** vault CLAUDE.md `security:` line; `feedback_deploy_integrity`

## commit-preview-rule
**Severity:** informational
**Applies when:** Plan ends with "and then commit" or otherwise implies auto-commit.
**Check:** Does the plan note that the commit message will be shown for approval before execution?
**Rule:** Always show the commit message preview; wait for explicit approval before committing.
**Cite:** `feedback_commit_preview`

## prior-decisions
**Severity:** blocking
**Applies when:** Plan proposes an architectural choice, design pattern, new abstraction, eval methodology change, memory system change, cross-platform approach, storage layout, or anything touching a surface with a known decision record.
**Check:** Scan `docs/decisions/INDEX.md` for any ADR whose subject overlaps the plan. Also cross-check `docs/KNOWN_LIMITATIONS.md` (standing constraints) and `docs/EFFORT_AB_RESULTS.md` (A/B outcomes — "we already tried this"). If an overlapping ADR or result exists, does the plan align with it or contradict it?
**Rule:** Don't re-litigate settled decisions. If the plan contradicts an existing ADR, either the plan needs revision, or the ADR needs an explicit superseding successor authored alongside the change — never silently diverge.
**Cite:** `docs/decisions/INDEX.md` + the specific ADR(s); `docs/KNOWN_LIMITATIONS.md`; `docs/EFFORT_AB_RESULTS.md`

## scope-creep
**Severity:** warning
**Applies when:** Plan bundles multiple concerns — bug fix plus refactor, config change plus adjacent cleanup, or a feature plus an opportunistic rewrite of touched code.
**Check:** Does the plan touch files or introduce logic beyond the minimum needed for the stated task?
**Rule:** One concern per plan. Adjacent cleanups get their own plan. Split before implementation, not after.
**Cite:** system-prompt "Don't add features, refactor, or introduce abstractions beyond what the task requires"; `feedback_scope_commits_by_concern`

## reversibility
**Severity:** warning
**Applies when:** Plan touches CI config, database migrations, shared production state, auth/credential rotation, deployment manifests, or anything with user-visible blast radius.
**Check:** Is there an explicit rollback plan? Can this be undone in a single revert? Are intermediate states safe (e.g., during a multi-step migration)?
**Rule:** Risky changes need a documented rollback path. If the change isn't trivially reversible, either split into reversible phases or state how we recover.
**Cite:** system-prompt "Executing actions with care" / blast-radius section

## test-strategy
**Severity:** informational
**Applies when:** Plan involves any non-trivial code change (not doc/comment-only edits).
**Check:** Does the plan articulate how the change will be verified end-to-end? Can be existing tests, new tests, manual verification steps, or a benchmark — but *something* concrete.
**Rule:** Every non-trivial plan answers "how will we know this works?" Unverifiable plans are incomplete plans.
**Cite:** Phase 4 pattern from ExitPlanMode workflow; `feedback_predict_before_testing`
