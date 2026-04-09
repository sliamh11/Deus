---
governs:
  - src/
  - evolution/
  - scripts/memory_indexer.py
  - src/startup-gate.ts
  - src/checks.ts
  - setup/
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

## ADR gate

**Before modifying `eval/`, `src/startup-gate.ts`, `src/checks.ts`, `setup/`, or `scripts/memory_indexer.py`**: read `docs/decisions/INDEX.md` first. Past decisions have non-obvious permanent constraints. Skipping the index has caused regressions.

## Security

Never commit credentials, API keys, or tokens — not even in test files. New credentials go in `.env.example`. Design as if the repo is public.

## What not to do

- Don't manually edit `CHANGELOG.md` or bump version in `package.json` (release-please handles both)
- Don't add features as source code changes — use skills
- Don't skip pre-commit hooks (`--no-verify`)
- Don't force-push to shared branches
