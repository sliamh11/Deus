# Gotcha detail files

Task-specific detail moved out of the always-loaded [`.claude/rules/orchestration-rules.md`](../../.claude/rules/orchestration-rules.md) so it costs no context until the task calls for it. Every rule is verbatim — nothing was reworded on the way out.

Each section heading still exists in that core file as a stub pointing here, so references by section name keep resolving.

| File | Read it when | Rules |
|------|--------------|------:|
| [`admin-merge.md`](admin-merge.md) | you are running `gh pr merge --admin --delete-branch` | 4 |
| [`ci-verification.md`](ci-verification.md) | you are waiting on CI, reading PR checks, or gating a merge on green — which is every PR, not only deployment work | 11 |
| [`cross-repo-worktrees.md`](cross-repo-worktrees.md) | you are working in any repo other than this session's launch repo | 9 |
| [`deploy-state.md`](deploy-state.md) | you are deploying, restarting a daemon, or about to trust a live test against local infra | 3 |
| [`markdown-docs.md`](markdown-docs.md) | you are authoring a long markdown doc or an ADR | 2 |
| [`multi-thread-mapping.md`](multi-thread-mapping.md) | you are planning a multi-thread effort too big for one session | 4 |
| [`warden-co-gate.md`](warden-co-gate.md) | you are marking warden verdicts, running `scripts/cogate.py`, or a plan-review / commit gate has blocked you | 28 |

**Capturing a new gotcha:** append it to the owning file above, never back into the core rules file — that is what pushed it over the 40.8k limit.
