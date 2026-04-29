# Agent-Agnostic Technical Debt

This file tracks the remaining open-ended work between today's
backend-neutral foundation and a fully agent-agnostic Deus.

Rules:

- Every row needs a stable debt ID.
- Every row must name the affected surface and user-visible risk.
- Every row must have explicit exit criteria.
- If a change intentionally leaves parity or onboarding incomplete, update this
  file in the same diff.

## Active Debt

| ID | Surface | Why still open | User-visible risk | Exit criteria |
|---|---|---|---|---|
| `AAG-010` | Codex Warden hook parity | Codex hooks mirror the main Claude Code Warden gates, but Codex currently does not expose exact `ExitPlanMode`/built-in `Plan` subagent invalidation semantics and its `PreToolUse`/`PostToolUse` interception is documented as incomplete for some shell and non-MCP paths. | A Codex session can still need manual marker invalidation after a new plan, and unusual edit paths may bypass the local hook guardrail until review/CI catches them. | Codex exposes hook events or tool coverage equivalent to Claude Code for plan-mode/subagent transitions and all write-capable tool paths, and `scripts/codex_warden_hooks.py check` verifies those hooks end-to-end. |

## Recently Closed

| ID | Closed by | Resolution |
|---|---|---|
| `AAG-C001` | `feat/agent-backend-abstraction` worktree | `AGENTS.md` became the canonical onboarding source, `CLAUDE.md` was reduced to a compatibility mirror, and runtime-loaded `AGENT_DEUS_101.md` onboarding was removed from the always-load context path. |
| `AAG-C002` | `feat/agent-backend-abstraction` worktree | `deus backend [show\|set\|model\|list]` CLI command added to both `deus-cmd.sh` and `deus-cmd.ps1`. Persists to both `~/.config/deus/config.json` and `.env`. |
| `AAG-C002a` | PR #252 (`feat/add-codex-skill`) | `/add-codex` interactive skill shipped: guided API key setup, backend config, parity warnings, verification, and troubleshooting. Resolves AAG-007. |
| `AAG-C003` | `feat/close-agnostic-debt` | Responses API chosen over Agents SDK. Decision documented in the backend-neutral ADR. Handoffs/tracing deferred as optional accelerators. Resolves AAG-002. |
| `AAG-C004` | `feat/close-agnostic-debt` | Supported backend boundary documented in `MULTI_BACKEND.md`: Claude and OpenAI/Codex are the implemented adapters; `ollama` is a forward reservation. Resolves AAG-003. |
| `AAG-C005` | `feat/close-agnostic-debt` | `CLAUDE.md` frozen as permanent compatibility shim — required by the Claude Code CLI convention (`CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD`). The file is a thin redirect to `AGENTS.md`. Resolves AAG-004. |
| `AAG-C006` | `feat/close-agnostic-debt` | Skill parity audit complete. All skills except `x-integration` (broken on both backends — wrong export, unfinished wiring) and `add-ollama-tool` (OpenAI backend hardcodes MCP server list) are instruction-only and work on both backends. Known gaps documented. Resolves AAG-005. |
| `AAG-C007` | Qodo removal (2026-04-25) | Qodo was removed from the project entirely. Discovery parity is no longer applicable. Resolves AAG-006. |
| `AAG-C008` | `feat/close-agnostic-debt` | Orchestrator and scheduler now call `backend.runTurn()` instead of `runContainerAgent()` directly. `turn_complete` event added to `RuntimeEvent`. `onProcess` removed from `SchedulerDependencies`. Resolves AAG-009. |
| `AAG-C009` | PR #258 + live E2E (2026-04-26) | Codex OAuth landed and verified E2E on host: startup gate passes, auth provider reads `~/.codex/auth.json` JWT, `injectAuth()` injects real token, auth-refresh daemon detects fresh token. Resolves AAG-001. |
