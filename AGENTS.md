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
3. Deus onboarding and memory surfaces: `AGENTS.md`, `CLAUDE.md`,
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
- Provider integrations follow the **Backend strategy trait** pattern: each
  provider is a single file implementing `Backend` (command construction,
  stream parsing, model list). Adding a provider = 1 file + 2 lines in
  `backend/mod.rs`. Do not inline provider-specific logic in app-level code.
- Vault auto-loading is config-driven: `vault_autoload` in
  `~/.config/deus/config.json` lists which vault files load at startup
  (default: `["CLAUDE.md"]`). All other vault files are on-demand. Do not
  hardcode vault file lists in the launcher.

For backend runtime work, read
[docs/decisions/backend-neutral-agent-runtime.md](docs/decisions/backend-neutral-agent-runtime.md).
For the provider strategy pattern, read
[docs/decisions/backend-strategy-trait.md](docs/decisions/backend-strategy-trait.md).
For vault auto-loading, read
[docs/decisions/vault-autoload.md](docs/decisions/vault-autoload.md).

## Core Entrypoints

Use these instead of rediscovering the system:

| Surface | Entry point | Purpose |
|---|---|---|
| Task routing | [`.mex/ROUTER.md`](.mex/ROUTER.md) | Maps task type to the required pattern file |
| Host runtime | `src/message-orchestrator.ts`, `src/container-runner.ts` | Agent dispatch, sessions, streaming, container wiring |
| Backend selection | `src/agent-runtimes/resolve.ts` | Task > group > env > Claude fallback |
| Session storage | `src/db.ts`, `src/router-state.ts` | Backend-scoped session refs and resume state |
| Scheduler | `src/task-scheduler.ts` | Same backend/session rules as interactive turns |
| Container context | `container/agent-runner/src/context-registry.ts` | Runtime-loaded onboarding and memory surfaces |
| OpenAI adapter | `container/agent-runner/src/openai-backend.ts` | OpenAI/Codex backend implementation |
| Claude path | `container/agent-runner/src/index.ts` | Compatibility baseline path |
| TUI backends | `tui/src/backend/` | Strategy trait — one file per provider (Claude, Codex, etc.) |
| Mount/security boundary | `src/container-mounter.ts` | Project/group/vault visibility and isolation |
| Memory retrieval | `scripts/memory_tree.py`, `scripts/memory_indexer.py` | Personal recall and semantic lookup |
| Codex Warden hooks | `scripts/codex_warden_hooks.py` | Installs and runs Codex hook equivalents for Warden gates |

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

Repo-owned host skills live under `.claude/skills/`. Some runtimes consume the
generated `.agents/skills/` compatibility tree. When adding, removing, or
renaming a repo-owned skill, update this table in the same change.

| Skill | When to Use |
|---|---|
| `/add-codex` | Add OpenAI/Codex as a backend |
| `/add-compact` | Add the backend-neutral `/compact` session command |
| `/add-discord` | Add Discord as a channel |
| `/add-gcal` | Add Google Calendar integration (list, create, update events) |
| `/add-gmail` | Add Gmail as a tool or channel |
| `/add-image-vision` | Add image attachment vision to Deus agents |
| `/add-listen-hotkey` | Add a global hotkey for `deus listen` |
| `/add-llama-cpp` | Install and verify optional local `llama.cpp` generation |
| `/add-ollama-tool` | Add Ollama as an MCP tool for local model calls |
| `/add-parallel` | Add Parallel AI MCP research tools |
| `/add-pdf-reader` | Add PDF text extraction |
| `/add-reactions` | Add WhatsApp emoji reaction support |
| `/add-slack` | Add Slack as a channel |
| `/add-telegram` | Add Telegram as a channel |
| `/add-telegram-swarm` | Add Agent Swarm support to Telegram |
| `/add-voice-transcription` | Add OpenAI Whisper voice transcription |
| `/add-whatsapp` | Add WhatsApp as a channel |
| `/add-youtube-transcript` | Add YouTube transcript extraction |
| `/checkpoint` | Save a mid-session continuity checkpoint |
| `/code-review` | Run multi-agent code review |
| `/compress` | Save the session to the vault and update memory indexes |
| `/convert-to-apple-container` | Switch from Docker to Apple Container |
| `/customize` | Add channels, integrations, or behavior changes |
| `/debug` | Debug containers, logs, auth, and runtime issues |
| `/preferences` | View or modify Deus user preferences |
| `/preserve` | Save durable memories from the current conversation |
| `/project-settings` | View or modify external project memory settings |
| `/resume` | Load recent work and memory context |
| `/review-logs` | Review Deus system health logs |
| `/setup` | Run first-time installation and configuration |
| `/update-skills` | Update installed skill branches from upstream |
| `/use-local-whisper` | Switch voice transcription to local whisper.cpp |
| `/wardens` | View, toggle, and configure warden quality gates |
| `/x-integration` | Set up or use X/Twitter integration |
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
