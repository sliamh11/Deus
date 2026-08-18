# Task Router

**Selection rule:** Pick the most specific match. If unsure, use `general-code`.

| Task type | Pattern file | Extra doc (load only if task touches this area) |
|-----------|--------------|------------------------------------------------|
| channel-add | `patterns/channel-add.md` | `docs/CONTRIBUTING-AI.md` §MCP Channel Servers, `docs/ENVIRONMENT.md` |
| skill-add | `patterns/skill-add.md` | — |
| claude-md-edit | `patterns/claude-md-edit.md` | `docs/TOKEN_OPTIMIZATION.md` (only if reshaping fact list) |
| eval-change | `patterns/eval-change.md` | `docs/decisions/INDEX.md`, `docs/ENVIRONMENT.md` |
| deployment | `patterns/deployment.md` | `docs/gotchas/deploy-state.md`, `docs/gotchas/admin-merge.md` |
| debugging | `patterns/debugging.md` | `docs/DEBUG_CHECKLIST.md`, `docs/gotchas/cross-repo-worktrees.md` (if the target is another repo) |
| cross-platform | `patterns/cross-platform.md` | — |
| container-change | `patterns/cross-platform.md` | — |
| security-review | `patterns/security-review.md` | `docs/SECURITY.md` |
| memory / startup-gate | `patterns/general-code.md` | `docs/decisions/INDEX.md` (mandatory) |
| env-var-add | `patterns/deployment.md` | `docs/ENVIRONMENT.md` |
| monitor-watch | `patterns/monitor-resilience.md` | — |
| hook-change | `patterns/hook-change.md` | `docs/SDK_DEEP_DIVE.md` (hook input/output types), `docs/gotchas/warden-co-gate.md` |
| documentation | `patterns/documentation.md` | `docs/gotchas/markdown-docs.md` |
| general-code (fallback) | `patterns/general-code.md` | `docs/gotchas/ci-verification.md`, `docs/gotchas/admin-merge.md` (merge-time, applies to any PR) |

## Gotcha detail files

Hard-won infra gotchas live in [`docs/gotchas/`](../docs/gotchas/INDEX.md), moved out
of the always-loaded `.claude/rules/orchestration-rules.md` so they cost no context
until the task calls for them. Load by trigger, not by task type alone:

| Read when you are | File |
|-------------------|------|
| marking warden verdicts, running `cogate.py`, or blocked by a gate | `docs/gotchas/warden-co-gate.md` |
| working in a repo other than the session's launch repo | `docs/gotchas/cross-repo-worktrees.md` |
| waiting on CI, reading PR checks, gating a merge on green | `docs/gotchas/ci-verification.md` |
| deploying, restarting a daemon, trusting a live infra test | `docs/gotchas/deploy-state.md` |
| running `gh pr merge --admin --delete-branch` | `docs/gotchas/admin-merge.md` |
| authoring a long markdown doc or ADR | `docs/gotchas/markdown-docs.md` |
| planning a multi-thread effort too big for one session | `docs/gotchas/multi-thread-mapping.md` |

Each section heading still exists in `.claude/rules/orchestration-rules.md` as a stub
pointing here, so a reference to that file by section name still resolves.

## Precedence

When a task matches multiple task types, pick the **most specific** one:

1. Security-sensitive code (mounts, allowlists, credentials, auth) → `security-review`
2. Subsystem-internal changes (evolution/\*, eval/\*, memory indexer) → the subsystem's own pattern
3. `general-code` is the fallback — use it only when no specialized pattern applies

## Universal rules

**The rules in `patterns/general-code.md` §Universal rules always apply**, regardless of which pattern was loaded:
- Don't edit `CHANGELOG.md` or bump version manually
- Don't skip `--no-verify`
- Don't force-push to shared branches
- One logical change per PR, squash fixup commits

## Compound tasks

If a task clearly spans two pattern types, **load both patterns** before starting.

Common compounds:
- `security-review` + `deployment` — security fix that also requires a service restart
- `channel-add` + `deployment` — new channel package that needs a separate build step
- `eval-change` + `general-code` — evolution change that also touches startup-gate.ts
