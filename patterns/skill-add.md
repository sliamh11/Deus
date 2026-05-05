---
governs:
  - .claude/skills
last_verified: "2026-05-06" # auto-bump
test_tasks:
  - "Create a new skill under .claude/skills/ that fetches recent Gmail threads"
  - "Add a new skill SKILL.md that documents log rotation steps"
  - "Add a new skill under .claude/skills/ for opening GitHub issues from chat"
  - "Rename an existing .claude/skills/ entry and update its SKILL.md metadata"
---
# Pattern: skill-add

## What a skill PR contains

- `SKILL.md` — required
- `agent.ts` — if the skill adds container-side MCP tools

This pattern governs PRs whose primary change is adding, removing, renaming, or
substantively changing a skill. A source/script PR may still include a docs-only
edit to an existing `SKILL.md` when the skill instructions must document the
changed source behavior. Treat that as a companion documentation edit, not a
skill PR.

## What must never be committed in a skill PR

| File | Reason |
|------|--------|
| `host.ts` / `host.js` | Private host-side implementation |
| `scripts/` | Subprocess scripts |
| `node_modules/`, `package-lock.json` | Local dependencies |

**CI rejects skill PRs that also touch `src/`.** This is enforced automatically — do not attempt to bundle skill + source changes in one PR. If the primary change is source/scripts and the skill file only documents how to use that changed behavior, follow the source pattern for implementation and keep the skill edit documentation-only.

## Core source changes

If a skill genuinely requires `src/` changes, open a separate source PR first. **Explain in the PR description why the feature cannot be a skill** — this is required, not optional. All source code PRs require maintainer review (CODEOWNERS).

## New features belong in skills

New features and enhancements go in skills, not source code. Source PRs are accepted for: bug fixes, security fixes, simplifications, performance improvements only.

## Tests

Every change to `agent.ts` requires a test. Run `npm test` before committing.

## Commit format

`feat(skills): <description>` — scope is `skills`.
