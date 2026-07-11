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
| `AAG-010` | Codex hook parity | Codex hooks mirror every current Claude Code hook with public-safe equivalents, but some parity remains best-effort: Codex plan-mode invalidation is inferred from exposed events and `/plan` prompts, Stop checkpointing still needs live Codex transcript-format verification, and Claude's private local orchestrator preflight is default-off behind `DEUS_CODEX_ORCHESTRATOR_PREFLIGHT=1`. Hook interception is still a guardrail rather than a complete boundary for every shell or non-MCP path. | A Codex session can still need manual marker invalidation after unusual plan flows, Stop checkpoints may miss context until live-verified, local orchestrator health is not checked unless explicitly enabled, and unusual edit/merge paths may bypass local guardrails until review/CI catches them. | Codex exposes hook events/tool coverage equivalent to Claude Code for plan-mode/subagent transitions and all write-capable tool paths, Stop hook transcript compatibility is verified by fixture or live test, public-safe orchestrator preflight is configured by setup, and `scripts/codex_warden_hooks.py check` verifies the full hook set end-to-end. |
| `AAG-012` | `/deep-research` skill | Uses Claude Code-specific tools: Agent tool for subagent fan-out (lit-scout, brainstormer), NotebookLM MCP (host-only), AskUserQuestion for scope clarification. Container runtime and Codex backend lack these interfaces. | Skill is invisible on Codex backend and unreachable from chat channels (WhatsApp, Telegram, Slack, Discord, Gmail). | Container-side variant using TeamCreate/SendMessage for fan-out, or a degraded single-pass mode using only WebSearch + vault memory. Alternatively, a container `agent.ts` that proxies the pipeline via IPC. |
| `AAG-013` | `deus-cmd.ps1` `--print-identity` parity | Flag shipped bash-only (GH #1004): the consuming VS Code `claudeProcessWrapper` shim is macOS-first, and the ps1 port needs its own print+exit interception before each `Invoke-Agent @("--append-system-prompt", ...)` call site (deus-cmd.ps1:354/466/468) plus stdout/stderr stream separation. | On Windows, `deus --print-identity` falls through to usage text; VS Code extension-managed sessions cannot splice the Deus identity prompt. | `deus-cmd.ps1` prints the identical payload at all `--append-system-prompt` call sites with progress noise kept off stdout, or the planned TS port of deus-cmd supersedes the ps1 script. |

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
| `AAG-C010` | `feat/portable-warden-gates` | All gate/invalidation/verdict-tracking logic consolidated in `scripts/codex_warden_hooks.py`. Claude Code on macOS/Linux uses `.claude/hooks/warden-shim.sh` (project-level, auto-loaded on clone). Codex and Windows Claude Code use `install` subcommand. Added: verdict tracking, audit log, post-REVISE bypass prevention, `mark` subcommand. Resolves AAG-011. |
