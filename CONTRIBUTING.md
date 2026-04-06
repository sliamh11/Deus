# Contributing to Deus

## Ground Rules

Every change — from maintainers and contributors alike — follows this workflow:

1. **Branch from `main`** — create a feature branch (`feat/...`, `fix/...`, etc.)
2. **Develop and test** — ensure `npm run build` and `npm test` pass
3. **Open a PR** — CI runs automatically; all checks must pass
4. **Review and merge** — maintainer approves, then merges to `main`

No direct pushes to `main`. No exceptions.

## PR Scope and Squashing

Each PR must contain a **single squashed commit** when merged. This means:

- All commits in a PR should be squashable into one coherent change.
- If two commits are too different to squash (different scope, different purpose), they belong in **separate PRs**.
- Use GitHub's "Squash and merge" option when merging.

This keeps `main` history clean and every commit meaningful.

## Commit Messages

We use [Conventional Commits](https://www.conventionalcommits.org/). This drives automated changelogs and versioning via [release-please](https://github.com/googleapis/release-please).

| Prefix | Meaning | Version bump |
|--------|---------|-------------|
| `feat(scope):` | New feature | minor |
| `fix(scope):` | Bug fix | patch |
| `docs(scope):` | Documentation only | none |
| `chore(scope):` | Maintenance, deps | none |
| `refactor(scope):` | Restructure, no behavior change | none |
| `test(scope):` | Test changes only | none |
| `perf(scope):` | Performance improvement | patch |
| `BREAKING CHANGE:` | Breaking change (in commit footer) | major |

Scope is the module name: `channels`, `ipc`, `orchestration`, `container`, `startup-gate`, `evolution`, `eval`, `memory`, etc.

## What's Accepted

**Source code changes (`src/`, `container/`, `scripts/`):**
Bug fixes, security fixes, simplifications, performance improvements. These go through maintainer review via CODEOWNERS. Every source code change must include unit tests — PRs without tests will not be merged.

**Skills (`.claude/skills/`):**
New features should be skills — markdown files that teach Claude Code how to add a capability. A skill PR should not modify source files. See `/add-telegram` for a good example.

**Not accepted as source changes:** New features, enhancements, or capabilities. These should be skills.

### Why skills?

Every user should have clean, minimal code that does exactly what they need. Skills let users selectively add features without inheriting code for features they don't want.

## Development

See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for setup, key files, and service management.
See [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) for architecture patterns (adding channels, commands, IPC types, startup checks).

## Automated Enforcement

These checks run automatically — you don't need to remember them:

| When | What | Tool |
|------|------|------|
| `git commit` | Formatting (Prettier + ESLint) | lint-staged (pre-commit hook) |
| `git commit` | Commit message format | commitlint (commit-msg hook) |
| PR opened | Lint, typecheck, tests | CI workflow |
| PR opened | Commit message validation | commitlint in CI |
| PR opened | PR title format | semantic PR check |
| PR opened | Skill/core boundary | skill boundary check |
| PR opened | Auto-labeling | labeler |

If a hook rejects your commit, read the error message — it tells you exactly what to fix. Hooks install automatically via `npm install` (husky).

## Pre-PR Checklist

- [ ] `npm run build` passes
- [ ] `npm test` passes
- [ ] Unit tests included for all source code changes
- [ ] Commit messages follow Conventional Commits
- [ ] Cross-platform rules followed (see [docs/CROSS_PLATFORM.md](docs/CROSS_PLATFORM.md))
- [ ] ADR index consulted for changed modules (see [docs/decisions/INDEX.md](docs/decisions/INDEX.md))
- [ ] New credentials added to `.env.example` with comments (never in code)
- [ ] No secrets, API keys, or credentials in code or git history

## Testing Skills

Test your skill by running it on a fresh clone before submitting.

## Reporting Issues

Use [GitHub Issues](https://github.com/sliamh11/Deus/issues) with the provided templates.

## Security

Report security vulnerabilities privately via [GitHub Security Advisories](https://github.com/sliamh11/Deus/security/advisories/new).

## Code of Conduct

See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
