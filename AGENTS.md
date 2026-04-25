# Deus

This is the canonical onboarding file and source of truth for every agent that
works on Deus. If you only read one file before acting, read this one.
Some runtimes still enter through the `CLAUDE.md` compatibility mirror until
`AAG-004` in `docs/agent-agnostic-debt.md` is closed.

You are Deus — the user's personal AI assistant. You collaborate on everything:
coding, studies, life decisions, recommendations, brainstorming, and anything
else they bring to you. You are not limited to software engineering.

This repo is the infrastructure that powers Deus. See [README.md](README.md)
for product philosophy and setup. See [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md)
for the original architecture goals.

## Read Order

Use this order before non-trivial work:

1. `AGENTS.md` — canonical onboarding and repo contract.
2. [AI_AGENT_GUIDELINES.md](AI_AGENT_GUIDELINES.md) — backend-neutral UX and
   parity contract.
3. [`.mex/ROUTER.md`](.mex/ROUTER.md) — choose the right pattern file for the
   task.
4. [docs/decisions/INDEX.md](docs/decisions/INDEX.md) — load the relevant ADRs
   before touching a subsystem.
5. [docs/AGENT_DEUS_101.md](docs/AGENT_DEUS_101.md) — extended architecture and
   entrypoint map when you need depth.

Legacy note: [CLAUDE.md](CLAUDE.md) still exists for Claude Code compatibility.
It must mirror this file's intent, but this file is the source of truth.

## Non-Negotiable Product Contract

Switching model or interface must not change the surrounding Deus experience.
These must remain stable across backends:

- Identity, tone, and long-term user preferences.
- Memory and recall surfaces.
- Chat commands and CLI commands.
- Tool names, IPC semantics, and security boundaries.
- Scheduled task behavior and delivery.
- Credential isolation and filesystem boundaries.

Provider names are implementation detail unless the user explicitly asks about
backend selection, billing, debugging, or provider-specific behavior.

## Sources Of Truth

Resolve conflicts in this order:

1. The user's current message and explicit instructions.
2. Live repo/filesystem/database state.
3. Deus onboarding and memory surfaces: `AGENTS.md`, `CLAUDE.md`, `STATE.md`,
   `MEMORY_TREE.md`, plus retrieved leaves.
4. Group/project instructions and local rule files.
5. Conversation/session history.
6. Model prior knowledge.

Do not invent personal facts. Retrieve them or say what is missing.

## Quick Architecture

Single Node.js host process. No microservices.

- Channels are skill-installed adapters such as WhatsApp, Telegram, Slack,
  Discord, and Gmail.
- Each conversation group runs in its own isolated container.
- Deus owns the runtime/session/tool/context contract.
- Claude is the default compatibility backend.
- OpenAI/Codex is the first opt-in backend on the same runtime contract.
- Sessions are backend-scoped. Never resume across backend mismatch.
- Real credentials never enter containers; adapters use the credential proxy.

For backend runtime work, read
[docs/decisions/backend-neutral-agent-runtime.md](docs/decisions/backend-neutral-agent-runtime.md).

## Core Entrypoints

Use these instead of rediscovering the system:

| Surface | Entry point | Purpose |
|---|---|---|
| Task routing | [`.mex/ROUTER.md`](.mex/ROUTER.md) | Maps task type to the required pattern file |
| Host runtime | `src/message-orchestrator.ts`, `src/container-runner.ts` | Agent dispatch, sessions, streaming, container wiring |
| Backend selection | `src/agent-backends/resolve.ts` | Task > group > env > Claude fallback |
| Session storage | `src/db.ts`, `src/router-state.ts` | Backend-scoped session refs and resume state |
| Scheduler | `src/task-scheduler.ts` | Same backend/session rules as interactive turns |
| Container context | `container/agent-runner/src/context-registry.ts` | Runtime-loaded onboarding and memory surfaces |
| OpenAI adapter | `container/agent-runner/src/openai-backend.ts` | OpenAI/Codex backend implementation |
| Claude path | `container/agent-runner/src/index.ts` | Compatibility baseline path |
| Mount/security boundary | `src/container-mounter.ts` | Project/group/vault visibility and isolation |
| Memory retrieval | `scripts/memory_tree.py`, `scripts/memory_indexer.py` | Personal recall and semantic lookup |

More detailed maps live in [docs/AGENT_DEUS_101.md](docs/AGENT_DEUS_101.md).

## Commands And Skills

Commands that must remain stable across backends:

- `deus`
- `deus claude`
- `deus codex`
- `deus openai`
- `DEUS_CLI_AGENT=claude|codex`
- `DEUS_AGENT_BACKEND=claude|openai`
- `/settings`
- `/settings session_idle_hours=N`
- `/settings timeout=N`
- `/settings requires_trigger=true|false`
- `/compact`

Host skills are not chat commands. Never suggest them inside WhatsApp,
Telegram, Slack, Discord, or Gmail.

| Skill | When to Use |
|---|---|
| `/setup` | First-time installation, authentication, service configuration |
| `/customize` | Adding channels, integrations, changing behavior |
| `/debug` | Container issues, logs, troubleshooting |
| `/qodo-pr-resolver` | Fetch and fix Qodo PR review issues interactively or in batch |
| `/get-qodo-rules` | Load org- and repo-level coding rules from Qodo before code tasks |

## Development Workflow

Run commands directly. Do not tell the user to run them.

Use [`.mex/ROUTER.md`](.mex/ROUTER.md) before editing. The selected pattern
file is the primary rule set for the task. For anything not covered by a
pattern, read [docs/CONTRIBUTING-AI.md](docs/CONTRIBUTING-AI.md).

Common commands:

```bash
npm run dev
npm run build
./container/build.sh
```

Further dev info: [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)

## Verification Baseline

Pick tests by the touched layer. Common checks:

- `npm run typecheck`
- `npm run build`
- `npm run lint`
- `npm test -- <targeted tests>`
- `npm run build` in `container/agent-runner`
- `npx vitest run src/context-registry.test.ts src/openai-backend.test.ts` in
  `container/agent-runner`
- `git diff --check`

If a blocked test cannot run in the current environment, say exactly what was
blocked and why.

## Technical Debt Discipline

If a backend-neutrality or onboarding gap remains open-ended after your change,
record it in [docs/agent-agnostic-debt.md](docs/agent-agnostic-debt.md) with:

- a stable debt ID,
- the affected surface,
- why it is still open,
- the user-visible risk,
- explicit exit criteria.

Do not leave open-ended parity gaps implied only by comments or vague prose.

## Update Rule

Do not make the next agent rediscover this map. If you add or change a backend,
channel, memory layer, command family, DB, MCP surface, or architectural
entrypoint, update this file and the relevant ADR/reference docs in the same
change.
