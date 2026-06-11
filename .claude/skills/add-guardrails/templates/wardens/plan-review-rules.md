# Plan Review Rules — plan-reviewer

> Rules the `plan-reviewer` agent checks a plan against BEFORE implementation.
> Add a rule by appending a section. No agent edit needed.
>
> Format per rule: `Severity`, `Applies when`, `Check`, `Rule`, and (for blocking/warning) `Remediation`.
> Severity: `blocking` (must fix before SHIP) · `warning` (should address) · `informational` (flag for awareness).

## secrets-design
**Severity:** blocking
**Applies when:** Plan handles credentials, API keys, OAuth tokens, or webhook secrets.
**Check:** Does the plan commit secrets to git anywhere (`.env`, fixtures, tests)? Does it rely on env vars without documenting them in an example file?
**Rule:** No credentials in git, ever. Use `.env` + a committed `.env.example` placeholder; gitignore strictly.
**Remediation:** Move any credential to a gitignored `.env`, add a placeholder to `.env.example`, reference it via an env lookup. Confirm `git check-ignore -v .env` before committing.

## no-data-deletion
**Severity:** blocking
**Applies when:** Plan deletes user data, removes records, or drops tables.
**Check:** Does the plan propose a hard delete?
**Rule:** Prefer soft-delete — a `deleted_at` field, archive, or tombstone — for anything a user could miss.
**Remediation:** Replace the hard delete with a soft-delete column and filter `WHERE deleted_at IS NULL` in reads.

## cross-platform-intent
**Severity:** warning
**Applies when:** Plan adds new code or scripts.
**Check:** Does the plan acknowledge OS-specific behavior (paths, shell commands, syscalls) and route through a portable abstraction where relevant?
**Rule:** Default to cross-platform. Flag any OS-specific code loudly.
**Remediation:** Replace OS-specific commands with portable equivalents, or guard them behind a platform check and call it out in the plan.

## commit-preview
**Severity:** informational
**Applies when:** Plan ends with "and then commit" or implies auto-commit.
**Check:** Does the plan note that the commit message will be shown for approval first?
**Rule:** Always show the commit message preview and wait for explicit approval before committing.

## prior-decisions
**Severity:** blocking
**Applies when:** Plan proposes an architectural choice, new abstraction, storage layout, or anything touching a surface with a known decision record, AND the repo has a `docs/decisions/` directory (or equivalent ADR log).
**Check:** Does any existing decision record overlap the plan? Does the plan align with it or silently contradict it?
**Rule:** Don't re-litigate settled decisions. If the plan contradicts a record, either revise the plan or author an explicit superseding record alongside the change.
**Remediation:** Revise to align, or draft a superseding decision record that states why the prior decision is reversed; link both in the PR.

## scope-creep
**Severity:** warning
**Applies when:** Plan bundles multiple concerns — a fix plus a refactor, a config change plus an adjacent cleanup, a feature plus an opportunistic rewrite.
**Check:** Does the plan touch files or add logic beyond the minimum for the stated task?
**Rule:** One concern per plan. Adjacent cleanups get their own plan.
**Remediation:** Split the unrelated work into a separate plan/branch; implement and review them independently.

## reversibility
**Severity:** warning
**Applies when:** Plan touches CI config, database migrations, shared production state, credential rotation, or anything with user-visible blast radius.
**Check:** Is there an explicit rollback path? Can this be undone in a single revert? Are intermediate states safe?
**Rule:** Risky changes need a documented rollback path. If not trivially reversible, split into reversible phases or state how to recover.
**Remediation:** Add a rollback section to the plan, or restructure into independently revertible steps.

## premise-verification
**Severity:** blocking
**Applies when:** Plan cites repo state as the problem — "X is tracked in git", "dependency Y is unused", "file Z is orphaned", "A and B have diverged".
**Check:** For each premise, is there a concrete verification command and its expected output? If absent, run the verification before issuing SHIP.
**Rule:** Repo-state premises must be verified, not assumed. Minimum checks:
- "tracked" → `git ls-files <path>` non-empty and `git check-ignore -v <path>` returns nothing.
- "unused dependency" → a repo-wide search returns no imports.
- "orphan file" → a repo-wide search for the filename returns no callers.
- counts / sizes / baselines → re-run the command the plan relies on and confirm the numbers match.
**Remediation:** Run the relevant command, paste the output into the plan, and correct any premise that turns out false before resubmitting.

## means-end-consistency
**Severity:** blocking
**Applies when:** Plan's purpose is to remove, block, redact, or prevent some value/pattern X (secrets, deprecated APIs, unsafe inputs).
**Check:** Does the implementation itself contain, expose, or reproduce X in another form? (e.g. "scrub secrets" but the matching pattern hardcodes a real secret; "redact logs" but the redactor logs the value before masking.)
**Rule:** The fix must not reproduce the problem it solves. Run the fix through the same check it creates.
**Remediation:** Source sensitive patterns from a secret store or gitignored file (never inline), then run the new gate against its own implementation; if it triggers, revise.

## design-pattern-selection
**Severity:** blocking
**Applies when:** Plan introduces new architecture, abstractions, registries, event systems, or other non-trivial structural code.
**Check:** Does the plan name which design pattern(s) apply and justify the choice? Does it justify data-structure choices with Big-O rationale where complexity matters?
**Rule:** Every non-trivial plan names the pattern(s) it uses and why they fit. If no standard pattern applies, state why and describe the custom approach.
**Remediation:** Add a "Design" section naming the pattern (e.g. "Strategy — each handler implements a common interface") and justifying data-structure choices.

## task-granularity
**Severity:** warning
**Applies when:** Plan has implementation steps or a task breakdown.
**Check:** Is each step a single, independently verifiable action?
**Rule:** Decompose into bite-sized tasks. "Implement the feature" is not a step; "write the failing test for X" is.
**Remediation:** Break large steps into single actions, each with its own verification.

## verification-strategy
**Severity:** warning
**Applies when:** Plan describes any implementation work.
**Check:** Does the plan specify HOW each change is verified — which commands, which tests, what they cover and what they don't?
**Rule:** Every plan answers "how will we know this works?" "Tests pass" is insufficient.
**Remediation:** Add the concrete commands/tests that prove the change, and note any coverage gaps.

## file-map-first
**Severity:** informational
**Applies when:** Plan touches 3+ files or creates new files.
**Check:** Does the plan start with a file map showing which files are created/modified and why?
**Rule:** List the files involved and each file's responsibility before the task breakdown. This catches decomposition errors early.

## api-surface-verification
**Severity:** blocking
**Applies when:** Plan calls, wraps, or extends existing functions, methods, or module APIs.
**Check:** For each referenced function, has the actual signature been read and verified — parameter names, types, return type?
**Rule:** Plans must verify the API surface they depend on by reading the source. Wrong signatures and phantom APIs are the top cause of multi-round review cycles.
**Remediation:** Open each referenced source file, read the real signature, and update the plan to match — cite the file and line.
