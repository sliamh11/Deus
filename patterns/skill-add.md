---
governs:
  - .claude/skills
last_verified: "2026-04-09"
---
# Pattern: skill-add

## What a skill PR contains

- `SKILL.md` — required
- `agent.ts` — if the skill adds container-side MCP tools

## What must never be committed in a skill PR

| File | Reason |
|------|--------|
| `host.ts` / `host.js` | Private host-side implementation |
| `scripts/` | Subprocess scripts |
| `node_modules/`, `package-lock.json` | Local dependencies |

**CI rejects skill PRs that also touch `src/`.** This is enforced automatically — do not attempt to bundle skill + source changes in one PR.

## Core source changes

If a skill genuinely requires `src/` changes, open a separate source PR first. **Explain in the PR description why the feature cannot be a skill** — this is required, not optional. All source code PRs require maintainer review (CODEOWNERS).

## New features belong in skills

New features and enhancements go in skills, not source code. Source PRs are accepted for: bug fixes, security fixes, simplifications, performance improvements only.

## Tests

Every change to `agent.ts` requires a test. Run `npm test` before committing.

## Commit format

`feat(skills): <description>` — scope is `skills`.
