---
governs:
  - src/
  - evolution/
  - src/startup-gate.ts
  - src/checks.ts
  - setup/
last_verified: "2026-04-12"
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

**Before modifying `eval/`, `evolution/`, `src/startup-gate.ts`, `src/checks.ts`, `setup/`, or `scripts/memory_indexer.py`**: read `docs/decisions/INDEX.md` first. Past decisions have non-obvious permanent constraints. Skipping the index has caused regressions.

## Startup-gate checks

Add new checks via `registerStartupCheck()` — never modify the gate's control flow directly. Three severity levels:

- **fatal** — blocks startup. Only for hard requirements (API credentials).
- **warn** — allows startup with warning. For optional-but-important components (memory vault, Python deps).
- **suggest** — one-line hint. For truly optional features (channels, groups, Gemini key).

**Channels are optional, not fatal.** The `process.exit(1)` on zero channels was intentionally removed (ADR: startup-gate.md). Never make a channel check fatal — it breaks new-user onboarding.

## Security

Never commit credentials, API keys, or tokens — not even in test files. New credentials go in `.env.example`. Design as if the repo is public.

## Universal rules (apply to all tasks)

These apply regardless of which pattern file was loaded. Every contributor — human or AI — must follow them:

- Don't manually edit `CHANGELOG.md` or bump version in `package.json` (release-please handles both)
- Don't add features as source code changes — use skills
- Don't skip pre-commit hooks (`--no-verify`)
- Don't force-push to shared branches
- Each PR must contain a single logical change; squash fixup commits before merging
