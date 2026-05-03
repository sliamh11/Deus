---
governs:
  - src/
  - evolution/
  - src/startup-gate.ts
  - src/checks.ts
  - setup/
last_verified: "2026-05-03T10:00" # auto-bump
test_tasks:
  - "Refactor src/router.ts into smaller modules"
  - "Add a new utility function for parsing timestamps"
  - "Fix a memory leak in the startup gate"
  - "Add a new startup-gate check in `src/checks.ts` that validates a required config value before boot"
---
# Pattern: general-code

## Branch (required)

Always create a feature branch before making changes. Never commit directly to `main`.
Verify clean working tree first: `git status`.
Branch naming: `feat/`, `fix/`, `docs/`, `refactor/`, `chore/`, `ci/`, `test/`, `perf/`

## Tests (required, no exceptions)

Every source change must include unit tests.
- TypeScript: `*.test.ts` alongside source files → `npm test`
- Python: `scripts/tests/test_*.py` → `python3 -m pytest scripts/tests/`

Both must pass before committing.

## Commit format

`type(scope): description` — Conventional Commits, enforced by commit-msg hook.
Allowed types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `chore`, `ci`
Scope required (e.g. `evolution`, `container`, `skills`, `startup-gate`).

**If a pre-commit hook fails, the commit did NOT happen.** Create a NEW commit after fixing — never `--amend`. Amending after a hook failure modifies the previous commit, not the failed one.

## ADR gate

**Before modifying `eval/`, `evolution/`, `src/startup-gate.ts`, `src/checks.ts`, `setup/`, `tui/src/backend/`, or `scripts/memory_indexer.py`**: read `docs/decisions/INDEX.md` first. Past decisions have non-obvious permanent constraints. Skipping the index has caused regressions.

**Provider integrations follow the Backend strategy trait** (ADR: `backend-strategy-trait.md`). Each provider is a single file implementing `Backend`. Do not inline provider-specific CLI commands, JSONL parsing, or model lists in app-level code — implement the trait instead.

## Startup-gate checks

Add new checks via `registerStartupCheck()` — never modify the gate's control flow directly. Three severity levels:

- **fatal** — blocks startup. Only for hard requirements (API credentials).
- **warn** — allows startup with warning. For optional-but-important components (memory vault, Python deps).
- **suggest** — one-line hint. For truly optional features (channels, groups, Gemini key).

**Channels are optional, not fatal.** The `process.exit(1)` on zero channels was intentionally removed (ADR: startup-gate.md). Never make a channel check fatal — it breaks new-user onboarding.

## Error handling

Use the four-class error taxonomy from `src/errors/` (ADR: `docs/decisions/error-discipline.md`). Pick the class that answers *what should the caller do?* — not "what went wrong?":

- `RetryableError` — transient (HTTP 5xx, ECONNRESET, rate limits) → caller retries with backoff
- `UserError` — bad input / denied auth → surface to user, log at `warn`, not `error`
- `FatalError` — corrupt state / missing required config → log + shut down this boundary
- `DeusError` — base class; use only when none of the three fits (e.g. wrapping an unclassified 3rd-party error)

Rules:
- Every throw in new code picks a subclass. Prefer a subclass over bare `throw new Error(...)`.
- Pass wrapped errors as `cause: err` — never stringify into `message`. The `toJSON()` serializer flattens the chain for structured logs.
- Narrow with `instanceof` (or `isDeusError`) when catching. `catch (e) { log; throw; }` is fine — silent swallows without classification are not.
- Structured context goes in `context: { ... }`. Never put secrets in `context`.

Async primitives (`fireAndForget`, `withTimeout`, `allSettledOrThrow`) and the `bootstrap()` entry-point harness land in follow-up PRs #2/#4; this pattern will reference them once those files exist on main.

## Security

Never commit credentials, API keys, or tokens — not even in test files. New credentials go in `.env.example`. Design as if the repo is public.

## Context hygiene

To keep sessions token-efficient and avoid re-loading the same content:

- **Don't re-read files you have already read** unless the file may have changed (you edited it, another process touched it, or a long time has passed).
- **Skip files over 100KB** unless the task explicitly requires their full content. Use `Read` with `offset`/`limit`, `Grep`, or targeted `head` instead.
- **Prefer pattern files + targeted doc loads** over reading full-size docs. The ROUTER already routes to the right pattern; follow the "Extra doc" pointer only when the task is clearly in that area.

## Universal rules (apply to all tasks)

These apply regardless of which pattern file was loaded. Every contributor — human or AI — must follow them:

- Don't manually edit `CHANGELOG.md` or bump version in `package.json` (release-please handles both)
- Don't add features as source code changes — use skills
- Don't skip pre-commit hooks (`--no-verify`)
- Don't force-push to shared branches
- Each PR must contain a single logical change; squash fixup commits before merging
