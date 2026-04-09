# Task Router

**Selection rule:** Pick the most specific match. If unsure, use `general-code`.

| Task type | Pattern file | Extra doc (load only if task touches this area) |
|-----------|--------------|------------------------------------------------|
| channel-add | `patterns/channel-add.md` | `docs/CONTRIBUTING-AI.md` §MCP Channel Servers, `docs/ENVIRONMENT.md` |
| skill-add | `patterns/skill-add.md` | — |
| eval-change | `patterns/eval-change.md` | `docs/decisions/INDEX.md`, `docs/ENVIRONMENT.md` |
| deployment | `patterns/deployment.md` | — |
| debugging | `patterns/debugging.md` | `docs/DEBUG_CHECKLIST.md` |
| cross-platform | `patterns/cross-platform.md` | — |
| container-change | `patterns/cross-platform.md` | — |
| security-review | `patterns/security-review.md` | `docs/SECURITY.md` |
| memory / startup-gate | `patterns/general-code.md` | `docs/decisions/INDEX.md` (mandatory) |
| env-var-add | `patterns/deployment.md` | `docs/ENVIRONMENT.md` |
| general-code (fallback) | `patterns/general-code.md` | — |

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
