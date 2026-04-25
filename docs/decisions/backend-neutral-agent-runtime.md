# Backend-Neutral Agent Runtime

**Status:** Accepted
**Date:** 2026-04-23
**Scope:** `src/agent-backends/`, `container/agent-runner/`, `deus-cmd.sh`, `deus-cmd.ps1`, `AGENTS.md`, `AI_AGENT_GUIDELINES.md`

## Context

Deus began as a harness around Claude Code. That kept the first version small and powerful, but it made the core assistant runtime depend on Claude-specific sessions, tools, credentials, and prompt loading. The product goal has changed: the user should be able to switch interface/backend tools while everything around Deus stays the same — memory, tone, personal context, chat commands, vault rules, scheduled tasks, and channel behavior.

## Decision

Deus owns the runtime contract, session routing, credential boundary, and canonical tool plane. Claude is the default and compatibility baseline, but it is one backend adapter rather than the architecture itself. OpenAI/Codex is the first additional adapter.

Backend selection resolves in this order:

1. Scheduled-task override
2. Group override
3. Global `DEUS_AGENT_BACKEND`
4. Fallback to `claude`

Backend adapters must not read raw host secrets. They receive placeholder credentials and route through the credential proxy. Provider-native tools may be used later as accelerators, but Deus-owned tools and IPC remain the product contract.

Context and rules are provider-neutral. Adapters must load the same Deus memory/rule surfaces through a registry-style mechanism, including current `CLAUDE.md` files, future neutral names such as `AGENTS.md`, and `AI_AGENT_GUIDELINES.md` when present, so a naming migration is localized rather than spread through each backend.

Current implementation scope is intentionally phased: this change lands the backend-aware session/config/auth/context contracts and an opt-in OpenAI/Codex adapter foundation. Full OpenAI Agents SDK sessions, handoffs, and tracing remain parity work before OpenAI becomes a default backend.

## Alternatives Considered

- Keep Claude-specific runtime logic and add OpenAI beside it. Rejected because memory, scheduling, IPC, and tool semantics would drift across duplicated runners.
- Rename the vault and root instruction files away from Claude conventions immediately. Rejected because `CLAUDE.md` remains part of the live Claude Code compatibility contract during the migration.
- Rely on provider-hosted tools for OpenAI parity. Rejected because Deus-owned tools are the stable product surface; provider-native tools can be accelerators later, not the source of truth.
- Introduce a generic runtime contract and migrate Claude through the same contract. Chosen because it preserves current behavior while moving provider lock-in into adapters instead of product architecture.

## Consequences

- Claude remains the safest default path and must not regress.
- New backends are allowed only behind explicit selection until parity is verified.
- Sessions are backend-scoped. A stored Claude session must not be resumed by OpenAI/Codex, and vice versa.
- User experience parity is mandatory: memory, tone, commands, vault context, and scheduler behavior should feel like the same Deus through a different interface.
- This supersedes the older “Claude SDK lock-in” limitation. Any docs that still describe the core agent as permanently Claude-only are stale.

## Verification

Every backend adapter change should include or update a parity matrix covering:

- Session start/resume and backend mismatch behavior
- Filesystem, shell, web, browser, and Deus IPC tools
- Scheduled tasks and task-specific backend overrides
- Group/global/project/vault context loading
- Slash commands and user-visible command behavior
- Credential proxy routing and missing-secret failures

At minimum, run TypeScript checks plus targeted backend/session/auth/container tests before merging. Full live verification requires a rebuilt agent container and provider credentials.

## Parity Matrix

| Surface | Claude Default | OpenAI/Codex Opt-In |
|---|---|---|
| Selection | Fallback/default backend | `DEUS_AGENT_BACKEND=openai`, group override, or task override |
| Sessions | Existing Claude session ids wrapped as backend refs | Responses id stored as an OpenAI backend ref |
| Backend mismatch | Starts fresh instead of resuming wrong backend | Starts fresh instead of resuming wrong backend |
| Credentials | Placeholder Anthropic credentials via proxy | Placeholder OpenAI credentials via `/openai` proxy route |
| Context files | Native Claude loading plus registry-managed non-native surfaces | Registry-managed `CLAUDE.md`, `AGENTS.md`, `AI_AGENT_GUIDELINES.md`, `STATE.md`, and `MEMORY_TREE.md` surfaces |
| Tools | Existing Claude/MCP tool path | Container ToolBroker-backed function tools |
| Scheduling | Existing IPC task tools | Same IPC task file contract with optional backend override |
| Global CLI | `deus` / `deus claude` | `deus codex`, `deus openai`, or `DEUS_CLI_AGENT=codex deus` |

## Rollback

Rollback is a single revert while `claude` remains the default. Existing legacy session rows still read as Claude sessions; rows created with `backend='openai'` are ignored when Claude is selected because sessions are backend-scoped.

## Implementation Notes

- OpenAI/Codex tool calls route through `container/agent-runner/src/tool-broker.ts` for filesystem, shell, web, browser, Deus IPC, scheduling, and group registration.
- OpenAI `/compact` stores a Deus-owned continuity summary in `BackendSessionRef.metadata_json` and starts the next turn from that summary instead of resuming a synthetic session id as an OpenAI response id.
