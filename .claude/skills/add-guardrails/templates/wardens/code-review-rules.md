# Code Review Rules — code-reviewer

> Rules the `code-reviewer` agent checks against POST-implementation, PRE-commit.
> The agent reads the working-tree diff (`git diff` and `git diff --cached`) and applies every rule whose "Applies when" matches.
>
> Format per rule: `Severity`, `Applies when`, `Check`, `Rule`, and (for blocking/warning) `Remediation`.
> Severity: `blocking` · `warning` · `informational`.

## no-hardcoded-secrets
**Severity:** blocking
**Applies when:** Diff touches any tracked file.
**Check:** Grep the diff for hardcoded secrets or environment-specific absolute paths — API keys, tokens, passwords, connection strings, machine-specific home paths.
**Rule:** Committed code must be secret-free and environment-agnostic.
**Remediation:** Replace each value with an env-var or config lookup; move fixtures containing secrets to a gitignored path.

## security-basics
**Severity:** blocking
**Applies when:** Diff handles user input, external API responses, file paths, shell commands, SQL, HTML rendering, or network I/O with user-controllable URLs.
**Check:** Shell injection (user input into `exec`/`spawn` without sanitization), SQL injection (string concat vs parameterized), path traversal (user input into file paths without normalize/basename), XSS (unescaped output), SSRF (user-controllable URLs fetched without a host allowlist).
**Rule:** No classic OWASP vectors. Parameterize queries, escape output, normalize paths, allowlist hosts.
**Remediation:** Apply the specific mitigation: parameterized query, argument-array spawn, `normalize`+`basename`, output escaping, or URL host allowlist.

## cross-platform-actual
**Severity:** blocking
**Applies when:** Diff adds/modifies code or scripts.
**Check:** Are paths built portably? Are OS-specific commands (e.g. `sed -i ''`, platform-only binaries) guarded by a platform check or replaced? Does the diff use `~`/`$HOME` instead of a hardcoded user path?
**Rule:** Default to cross-platform; guard OS-specific code.
**Remediation:** Replace OS-specific commands with portable equivalents or wrap them in a platform guard; replace absolute home paths with `$HOME`/home-dir lookups.

## connectivity-wiring
**Severity:** warning
**Applies when:** Diff adds a new feature-flag gate, a new entry-point/integration module, or a new standalone module exporting a public symbol meant to run in production.
**Check:** Does the diff (or PR body) show the producer→consumer edge — a non-test caller, a live registration, or the flag actually read on a runtime path — or cite a tracked follow-up for the deferred wire?
**Rule:** A new capability must be reachable in the runtime path or explicitly tracked as deferred. Green unit tests do not prove reachability.
**Remediation:** Add the missing caller/registration and show it in the diff, or cite a tracking issue in the PR body and an adjacent code comment.

## cleanup
**Severity:** warning
**Applies when:** Diff touches code files.
**Check:** Dead imports, unused variables/functions/exports, commented-out code, unreachable branches, TODO/FIXME without owner, legacy files deprecated but not deleted, stray debug helpers.
**Rule:** Keep the codebase lean. Delete dead code — don't mark it. Version history preserves deletions.
**Remediation:** Remove the dead code outright rather than commenting it out or leaving a tombstone.

## comment-discipline
**Severity:** warning
**Applies when:** Diff adds or modifies comments.
**Check:** Both failure modes — (a) over-commenting: narrative commentary, WHAT-comments where naming already explains the code, docstrings on trivial functions; (b) under-commenting: genuinely complex logic (non-obvious invariants, subtle workarounds) with zero explanation.
**Rule:** Default to no comments. Comment only when the WHY is non-obvious; keep it tight.
**Remediation:** Delete redundant comments; add a 1–2 sentence WHY only where the logic is genuinely non-obvious.

## error-handling-discipline
**Severity:** warning
**Applies when:** Diff adds try/catch, validation, or null guards — or adds a system-boundary entry point (HTTP handler, CLI entry, parser, external API response handler).
**Check:** (a) Is handling added for scenarios that can't happen (internal call with guaranteed input)? (b) Is validation MISSING at a system boundary where untrusted data enters?
**Rule:** No defensive handling for impossible cases. Always validate at system boundaries.
**Remediation:** Remove handling for impossible internal cases; add validation where untrusted data first enters.

## type-safety
**Severity:** warning
**Applies when:** Diff modifies typed code (e.g. TypeScript).
**Check:** Use of `any`, suppression comments without a reason, type assertions that skip structural verification, generics defaulted to `any`.
**Rule:** Prefer typed unknowns narrowed with guards over `any`. Suppressions need a WHY comment pointing to the specific limitation.
**Remediation:** Replace `any` with a narrowed type or guard; add a justification comment to any necessary suppression.

## log-discipline
**Severity:** warning
**Applies when:** Diff adds logging or print output.
**Check:** Is it transient debug leftover? On a hot path that floods output? Does it leak sensitive values? Is the level appropriate?
**Rule:** Remove debug leftovers before ship. Production logs need clear purpose, correct level, and no secrets.
**Remediation:** Delete development leftovers; downgrade or scrub the rest.

## efficiency
**Severity:** warning
**Applies when:** Diff touches hot paths (request handlers, event loops, parsers, tight loops).
**Check:** N+1 queries, sync-over-async, blocking I/O in async contexts, string concat in loops, anything that changes complexity class.
**Rule:** Flag genuine performance red flags. Don't micro-optimize.
**Remediation:** Address the specific sub-optimality (batch the query, await properly, hoist the work).

## test-presence
**Severity:** informational
**Applies when:** Diff adds or significantly changes non-trivial logic (a new function over ~20 lines, a new module, a changed algorithm, a new endpoint).
**Check:** Is there at least one test covering the new behavior? For a bug fix, a regression test for the specific bug?
**Rule:** Non-trivial changes deserve verification in code, not just manual testing. Informational because depth is judgment-dependent — flag the zero-test case.

## benchmark-validation
**Severity:** blocking
**Applies when:** Diff claims a performance improvement (commit message mentions "faster", "optimize", "speedup").
**Check:** Is there before/after data in the commit message or PR body, on a realistic workload?
**Rule:** Never ship "improvements" without measurement. Predict the outcome before running the benchmark.
**Remediation:** Run the benchmark on a realistic workload and add the before/after numbers before pushing.

## pros-cons
**Severity:** warning
**Applies when:** Diff implements a non-trivial design choice (new architecture, dependency, or pattern).
**Check:** Does the PR body or commit message enumerate alternatives considered and why this one was picked?
**Rule:** Non-trivial decisions deserve documented trade-offs.
**Remediation:** Add a short "alternatives considered" note to the PR body.

## pr-title-format
**Severity:** blocking
**Applies when:** Preparing a commit that becomes a PR.
**Check:** Conventional-commits format (`type(scope): description`), reasonably short, lowercase subject.
**Rule:** Title must pass a standard conventional-commits check on first push.
**Remediation:** Rewrite to `type(scope): description` and trim it.
