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
**Remediation:** Replace each hardcoded personal value in the plan with a placeholder, env var reference, or config-file lookup. Move any fixture files that contain personal data to `~/.claude/` or `src/private/` and gitignore them.

## commit-scoping
**Severity:** blocking
**Applies when:** Plan touches BOTH public-repo files AND personal tooling (`~/.claude/`, `~/.config/deus/`, user-local dotfiles).
**Check:** Are the changes conceptually one concern, or multiple?
**Rule:** Split into separate commits/PRs. Never bundle personal-tooling changes into public-repo PRs.
**Cite:** `feedback_scope_commits_by_concern`
**Remediation:** Split the plan into two separate branches: one for the public-repo changes and one for the personal-tooling changes. Implement and PR them independently.

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
**Remediation:** Either complete the open steps in the active sequence first, or explicitly supersede the sequence by updating the relevant `project_*.md` memory with a new plan — then resubmit.

## no-db-deletion
**Severity:** blocking
**Applies when:** Plan involves deleting user data, removing records, or dropping tables.
**Check:** Does the plan propose a hard delete?
**Rule:** Soft-delete only — set a deleted-at field, archive, or tombstone. Never hard-delete user data.
**Cite:** `docs/decisions/no-db-deletion.md`
**Remediation:** Replace the hard-delete operation with a soft-delete: add a `deleted_at` timestamp column (or equivalent archive table), set it instead of removing the row, and filter `WHERE deleted_at IS NULL` in queries.

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
**Remediation:** Move any credential value to a `.env` file (gitignored), add a corresponding placeholder entry to `.env.example`, and reference it via `process.env.VAR_NAME` in code. Run `git check-ignore -v .env` to confirm it is ignored before committing.

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
**Remediation:** Either revise the plan to align with the existing ADR, or draft a superseding ADR alongside the change that explicitly records why the prior decision is being reversed. Add a link to both ADRs in the PR body.

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

## premise-verification
**Severity:** blocking
**Applies when:** Plan cites repo state as the problem — e.g. "X is tracked in git," "dep Y is unused," "dir Z is orphaned," "files are drifting," "A and B have diverged."
**Check:** For each premise, is there a concrete verification command and its expected output? If absent, run the verification yourself before issuing SHIP.
**Rule:** Repo-state premises must be verified, not assumed. Minimum checks by premise type:
- "tracked" / "stop committing X" → `git ls-files <path>` (must return non-empty) and `git check-ignore -v <path>` (must return nothing).
- "unused dep" → `grep -r '<pkg>' src/ scripts/ container/ packages/` returns no imports.
- "orphan file" → `grep -r '<filename>' .` returns no callers across `.ts`, `.sh`, `.json`, `.py`, launchd plists, `package.json` scripts.
- "drift between two paths" → grep for code that writes to the derived path (`cpSync`, `shutil.copytree`, `fs.mkdirSync` + write). If a writer exists, the divergence is a cache, not a bug — plan must address why the cache is wrong, not treat it as rot.
- "quantified baseline" / counts / byte sizes (e.g. "N violations", "X tests fail", "Y unused files") → re-run the same command the plan implicitly relies on (`drift_check.py`, `pytest -q`, `grep -c`, etc.) and confirm the numbers match within ±1. If they don't, REVISE with the live numbers — stale baselines block downstream design decisions.
**Cite:** Slice A postmortem (2026-04-20 — the agent-runner-src cache was wrongly flagged as tracked drift); system-prompt "Trust but verify."
**Remediation:** Run the verification command(s) listed above for each unverified premise and paste the output into the plan. If a premise turns out false, remove or correct that step before resubmitting.

## means-end-consistency
**Severity:** blocking
**Applies when:** Plan's stated purpose is to remove, block, protect, redact, or prevent X — where X is personal data, secrets, deprecated APIs, vulnerable patterns, unsafe inputs, or any other value/pattern the plan aims to eliminate from the committed repo.
**Check:** Does the implementation itself contain, expose, enable, or reproduce X in another form?
Common traps:
- "Scrub personal values from repo" — but the CI pattern matching those values is committed inline in a workflow file.
- "Redact secrets from logs" — but the redactor logs them before masking.
- "Block deprecated API" — but the blocking rule's usage example calls the API.
- "Allowlist safe inputs" — but the allowlist itself contains an unsafe value.
- "Delete file X" — but a new file references X's old path.
**Rule:** The fix must not reproduce the problem it solves. For patterns/regexes over sensitive values, source them from a GitHub Actions secret, an external gitignored file, a hash-based match, or other indirection — never commit the values inline. Run the fix through the same check it creates: if the implementation would trigger its own gate, revise.
**Cite:** Slice C round 3 postmortem (2026-04-20 — CI gate was going to hardcode the very personal IDs it was designed to block).
**Remediation:** Move the problematic value out of the committed implementation — use a GitHub Actions secret reference, a gitignored config file, or a hash-based match. Then run the gate the plan creates against its own implementation; if it triggers, revise until it doesn't.

## design-pattern-selection
**Severity:** blocking
**Applies when:** Plan introduces new architecture, abstractions, trait hierarchies, registries, event systems, or any non-trivial structural code.
**Check:** Does the plan identify which design pattern(s) apply (Strategy, Observer, Mediator, Factory, etc.) and justify the choice? Does it specify data structures with Big-O rationale where relevant (e.g., HashMap for O(1) lookup vs Vec scan)?
**Rule:** Every non-trivial plan must name the design pattern(s) it uses and why they fit. Data structure choices must be justified when algorithmic complexity matters. Generic, modular designs are the default — new features should plug in without modifying or risking existing logic. If no standard pattern applies, the plan must state why and describe the custom approach.
**Cite:** vault CLAUDE.md `design: pattern-driven | modular-generic`
**Remediation:** Add a "Design" section to the plan that names the pattern(s) used (e.g., "Strategy — each handler implements a common interface") and justifies data structure choices with Big-O rationale. If no standard pattern applies, write a one-sentence explanation of the custom approach.

## task-granularity
**Severity:** warning
**Applies when:** Plan has implementation steps or task breakdown.
**Check:** Is each step a single action (2-5 minutes of work)? Can each step be independently verified?
**Rule:** Plans should decompose into bite-sized tasks — each step should be one action with a clear verification. "Implement the feature" is not a step. "Write the failing test for X" is.
**Cite:** Superpowers writing-plans skill; "Each step is one action (2-5 minutes)"

## verification-strategy
**Severity:** warning
**Applies when:** Plan describes any implementation work.
**Check:** Does the plan specify HOW each change will be verified? Is there a test strategy (not just "run tests")?
**Rule:** Every plan must state what commands prove it works. "Tests pass" is insufficient — specify which tests, what they cover, and what's NOT covered.
**Cite:** Superpowers verification-before-completion; debugging-rules.md

## file-map-first
**Severity:** informational
**Applies when:** Plan touches 3+ files or creates new files.
**Check:** Does the plan start with a file map showing which files are created/modified and why?
**Rule:** Before task breakdown, list the files involved and each file's responsibility. This catches decomposition errors early.
**Cite:** Superpowers writing-plans "File Structure" section

## retrieval-sweep-gate
**Severity:** blocking
**Applies when:** Plan changes memory retrieval thresholds, scoring functions, embedding parameters, fallback mechanisms, or abstain logic under `scripts/`, `src/memory/`, `src/retrieval/`, `evolution/`, or other retrieval-related paths.
**Check:** Does the plan include `deus sweep` benchmark output (recall, MRR, abstain_accuracy) showing the impact? Was the sweep run BEFORE the PR, not after merge?
**Rule:** Retrieval pipeline changes must include sweep evidence in the PR description. Ship-then-disable cycles waste two PR reviews and risk leaving harmful defaults in production. The tool already exists — use it.
**Cite:** RETRO-2026-05-11-01; atom-fallback PR #350→#351 reversal; entity-coverage same-session reversal
**Remediation:** Run `deus sweep` against the modified retrieval code and paste the recall/MRR/abstain_accuracy output into the plan or PR description. Do not proceed until the sweep shows neutral or improved scores vs the baseline.

## api-surface-verification
**Severity:** blocking
**Applies when:** Plan calls, wraps, or extends existing functions, methods, or module APIs.
**Check:** For each function the plan references, has the actual signature been read and verified? Do the parameter names, types, and return types match what the plan assumes?
**Rule:** Plans must verify the API surface they depend on by reading the source. Wrong method signatures, dead parameters, and phantom APIs are the #1 cause of multi-round plan-reviewer cycles. Read the function, then write the plan — not the reverse.
**Cite:** RETRO-2026-05-11-02; Phase 5-6 postmortem (6 rounds caused by RuntimeRegistry.resolve() wrong signature, GroupQueue dead parameter)
**Remediation:** For each referenced function, open the source file and read the actual signature. Update the plan to match the real parameter names, types, and return types. Add a "Verified signatures" note citing the file and line number for each function.
