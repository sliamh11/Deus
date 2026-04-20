# Code Review Rules — Wardens/code-reviewer

> Rules the `code-reviewer` agent checks against POST-implementation, PRE-commit.
> The agent reads the working-tree diff (`git diff` or staged) and applies every rule whose "Applies when" matches.
>
> Format per rule: `Severity`, `Applies when`, `Check`, `Rule`, `Cite`.
> Severity: `blocking` (must fix before SHIP) · `warning` (should address) · `informational` (author's awareness).

## ci-preservation-95
**Severity:** blocking
**Applies when:** Diff modifies vault `CLAUDE.md`, `STATE.md`, `INFRA.md`, `STUDY.md`, or `MEMORY.md`.
**Check:** Are any `critical:` keys dropped? Are any `**(CRITICAL)**` memory entries removed? Estimate critical-key retention — does it stay ≥95%?
**Rule:** Never reduce CRITICAL preservation below 95%.
**Cite:** `feedback_compression_rule`; vault CLAUDE.md `claude-md-gates` line

## ci-coverage-90
**Severity:** blocking
**Applies when:** Diff modifies any root-loaded file (vault CLAUDE.md + the two CLAUDE.md.template files in `groups/`).
**Check:** Do the canned keyword-coverage queries still hit at ≥90%? Paraphrase misses should be addressed via `kw=` overrides, not by lowering the floor.
**Rule:** Maintain ≥90% behavioral coverage.
**Cite:** `feedback_compression_rule`

## ci-drift-check
**Severity:** blocking
**Applies when:** Diff modifies vault leaf files (STATE.md, INFRA.md, STUDY.md, Persona/*) or memory indexes.
**Check:** Was `memory_tree.py check` / `drift_check.py --indexes` run after the edit?
**Rule:** Vault leaf changes require drift-check validation before commit.
**Cite:** `feedback_memory_tree_check`

## cross-platform-actual
**Severity:** blocking
**Applies when:** Diff adds/modifies code under `~/deus/src/` or `~/deus/scripts/`.
**Check:** Are paths constructed via `src/platform.ts` helpers? Are OS-specific commands (e.g., `pngpaste`, `launchctl`, `osascript`) gated by a platform check? Does the diff use `~`/`$HOME` instead of `/Users/...`?
**Rule:** Default to cross-platform; guard OS-specific code.
**Cite:** `feedback_cross_platform_default`

## efficiency
**Severity:** warning
**Applies when:** Diff touches hot paths (request handlers, event loops, parsers, tight benchmarks).
**Check:** N+1 queries? Sync-over-async? Blocking I/O in async contexts? String concat in loops? Any obvious sub-optimality that changes complexity class?
**Rule:** Flag performance red flags. Don't micro-optimize — call out genuine issues only.
**Cite:** vault CLAUDE.md `design: perf-aware` line

## token-efficiency
**Severity:** warning
**Applies when:** Diff adds output to stdout/stderr that ends up in Claude context (hooks, CLI tools, `deus` subcommands, status lines, system prompts, CLAUDE.md).
**Check:** Is the new output terse? Does it bloat CLAUDE.md-gated behavior? Was the token cost measured or at least estimated?
**Rule:** Token cost matters. Noisy output in context pressure-drops real rules.
**Cite:** vault CLAUDE.md `design: token-efficient` line; `reference_mcp_token_overhead`

## modularity
**Severity:** informational
**Applies when:** Diff adds a new file or significantly extends an existing one (>100 lines added).
**Check:** Is the new code coupled tightly to unrelated modules? Could it be extracted? Does it duplicate logic already elsewhere?
**Rule:** Favor simple, decoupled units. Three similar lines beat a premature abstraction — but genuine duplication should be flagged.
**Cite:** vault CLAUDE.md `design: modular` (implied)

## benchmark-validation
**Severity:** blocking
**Applies when:** Diff claims a performance improvement (commit message mentions "faster", "optimize", "speedup") OR is a compress/eval/retrieval change.
**Check:** Is there before/after data in the commit message, session log, or PR body? Was the bench run on a realistic workload (not a short-prompt probe for long-context code)?
**Rule:** Never ship "improvements" without measurement. Predict the outcome before running the bench.
**Cite:** `feedback_predict_before_testing`; `feedback_measure_on_real_workload`

## pros-cons
**Severity:** warning
**Applies when:** Diff implements a non-trivial design choice (new architecture, new dependency, new pattern).
**Check:** Does the PR body or session log enumerate alternatives considered and why this one was picked?
**Rule:** Non-trivial decisions deserve documented trade-offs. Flag absent justification.
**Cite:** `feedback_no_speculation`

## pr-title-format
**Severity:** blocking
**Applies when:** Preparing a commit that will become a PR.
**Check:** Conventional-commits format (`type(scope): description`)? Under 70 chars? Matches the CI title-gate expectations?
**Rule:** Title must pass the CI title-gate on first push.
**Cite:** `project_error_discipline_plan` (noted #214 was CI-blocked on title)

## no-hardcoded-personal
**Severity:** blocking
**Applies when:** Diff touches public-repo files.
**Check:** Grep the diff for hardcoded personal values — macOS usernames (e.g. a literal like `<user>`), emails, government IDs, Hebrew names, absolute paths under `/Users/...`, personal project IDs.
**Rule:** Public-repo code must be user-agnostic in reality, not just in intent.
**Cite:** `feedback_public_repo_generic`

## container-reload-noted
**Severity:** informational
**Applies when:** Diff modifies `groups/*/CLAUDE.md` or `container/` files.
**Check:** Does the commit message or PR body note that users must run `deus auth` / restart to pick up the change?
**Rule:** Container reloads aren't automatic. Document them.
**Cite:** vault INFRA.md `container:` line

## backwards-compat-hacks
**Severity:** warning
**Applies when:** Diff touches code where something was removed, renamed, or changed.
**Check:** Does the diff contain (a) unused variables renamed to `_var` to silence warnings instead of being deleted, (b) `// removed` / `// deprecated` tombstone comments, (c) re-exports of types or functions whose implementations were removed, or (d) feature-flag gates for behavior changes where rollback is trivial (one-commit revert)?
**Rule:** Delete unused code completely. Change behavior in place when rollback is cheap. Git history preserves deletions — no shims, no tombstone comments, no flags that only exist "just in case".
**Cite:** system-prompt "Avoid backwards-compatibility hacks" rule

## comment-discipline
**Severity:** warning
**Applies when:** Diff adds or modifies code comments (inline, block, or docstrings).
**Check:** Flag BOTH failure modes: (a) over-commenting — narrative commentary ("used by X", "added for feature Y", "fix for issue #123"), WHAT-comments where naming already explains the code, multi-line docstrings on trivial functions; AND (b) under-commenting — genuinely complex logic (non-obvious invariants, subtle workarounds, hidden constraints, performance-critical ordering) with zero explanation.
**Rule:** Default to no comments. Only comment when the WHY is non-obvious. For genuinely complex sections, a 1–2 sentence comment that translates intent or simplifies the logic is appropriate — but keep it tight, never multi-paragraph. Not every function needs a docstring; most don't.
**Cite:** system-prompt "Default to writing no comments" rule

## cleanup
**Severity:** warning
**Applies when:** Diff touches code files.
**Check:** Dead imports, unused variables/functions/exports, commented-out code blocks, unreachable branches, unused parameters, TODO/FIXME without owner or date, legacy files the diff deprecates but doesn't delete, stray debug helpers.
**Rule:** Keep the codebase lean. Every line should have a reason to exist. Delete dead code — don't mark it. Git history preserves deletions.
**Cite:** vault CLAUDE.md `design: modular` + `prefs: scalable > quick-fix`; system-prompt minimalism rules

## error-handling-discipline
**Severity:** warning
**Applies when:** Diff adds try/catch blocks, validation code, defensive null checks, or `if (x === undefined)` guards — OR the diff adds a system-boundary entry point (HTTP handler, CLI entry, file parser, external API response handler).
**Check:** Dual direction — (a) is error handling added for scenarios that can't happen (internal function calling internal function with framework-guaranteed non-null input)? (b) is validation MISSING at system boundaries where untrusted data enters?
**Rule:** No defensive handling for impossible cases. Trust internal code and framework guarantees. But ALWAYS validate at system boundaries.
**Cite:** system-prompt "Don't add error handling for scenarios that can't happen"

## type-safety
**Severity:** warning
**Applies when:** Diff modifies TypeScript files.
**Check:** Use of `any`, `@ts-ignore`, `@ts-expect-error` without an accompanying reason comment, type assertions `as X` that skip structural verification, generics defaulted to `any`, union types widened to `any`.
**Rule:** Prefer typed unknowns + narrow with guards over `any`. `@ts-ignore` and non-obvious `as X` need a WHY comment pointing to the specific limitation they work around.
**Cite:** vault CLAUDE.md `prefs: scalable > quick-fix`; generally accepted TS practice

## log-discipline
**Severity:** warning
**Applies when:** Diff adds `console.log`, `console.debug`, `print()`, `logger.debug` or similar output.
**Check:** Is the log transient debug (leftover from development) or production-intentional? Is it on a hot path that would flood stdout? Does it leak sensitive values (tokens, PII, absolute paths)? Is the level appropriate?
**Rule:** Remove debug leftovers before ship. Production logs must have clear purpose, correct level, and no secrets.
**Cite:** vault CLAUDE.md `design: token-efficient`; `reference_mcp_token_overhead`

## test-presence
**Severity:** informational
**Applies when:** Diff adds or significantly modifies non-trivial logic (new function >~20 lines, new module, changed algorithm, new endpoint, new public export).
**Check:** Is there at least one test (unit, integration, or E2E) covering the new behavior? For bug fixes, a regression test targeting the specific bug?
**Rule:** Non-trivial changes deserve verification in code, not just "I tried it manually". Informational because coverage depth is judgment-dependent — we're flagging the zero-test case.
**Cite:** general engineering practice; not strict TDD

## security-basics
**Severity:** blocking
**Applies when:** Diff handles user input, external API responses, file paths, shell commands, SQL queries, HTML rendering, or network I/O with user-controllable URLs.
**Check:** Shell injection (user input interpolated into `exec`/`child_process.spawn` without sanitization), SQL injection (string concat into queries vs parameterized), path traversal (user input concatenated into file paths without basename/normalize), XSS (user input rendered without escaping), SSRF (user-controllable URLs fetched server-side without host allowlist).
**Rule:** No classic OWASP vectors. Parameterize queries, escape output, normalize paths, allowlist hosts for user-controlled URLs.
**Cite:** system-prompt "Be careful not to introduce security vulnerabilities" rule; `feedback_security_first`
