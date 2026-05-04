# Agent Deus 101

This is the extended architecture and entrypoint map for agents that already
read [AGENTS.md](../AGENTS.md).

Use this file when you need depth: subsystem entrypoints, memory surfaces,
runtime boundaries, and verification hints. It is no longer the canonical
first-read onboarding file.

## When To Read This

Read [AGENTS.md](../AGENTS.md) first.

Then use this file when you need one of these:

- a deeper map of where runtime, memory, scheduler, or DB behavior lives,
- a list of concrete architectural entrypoints,
- a more detailed verification menu,
- a way to avoid rediscovering the same surfaces from scratch.

For parity/UX rules, use [AI_AGENT_GUIDELINES.md](../AI_AGENT_GUIDELINES.md).
For remaining open-ended backend/onboarding work, use
[agent-agnostic-debt.md](agent-agnostic-debt.md).

## Identity And UX Contract

Every backend presents as Deus, the user's personal AI assistant. The model may
change, but these must remain stable:

- Memory and personal context.
- Tone, preferences, and behavioral rules.
- Chat commands and CLI commands.
- Skills, MCPs, tools, and IPC semantics.
- Scheduling behavior and task delivery.
- Security boundaries and credential isolation.

Provider names are implementation detail unless the user asks about backend
selection, billing, debugging, or model-specific behavior.

## Memory And Data Entrances

Use these entry points instead of rediscovering the memory system from scratch:

| Layer | Entry point | Purpose |
|---|---|---|
| Vault config | `~/.config/deus/config.json` | Host config; `vault_path` points to the live vault. Read-only unless explicitly asked to write. |
| Vault startup files | `AGENTS.md`, `CLAUDE.md`, `AI_AGENT_GUIDELINES.md`, `MEMORY_TREE.md` | Canonical onboarding plus compatibility, parity, and recall surfaces. |
| CLI memory load | `deus-cmd.sh`, `deus-cmd.ps1` | Global `deus` command context assembly, external-project mode, restricted memory handling. |
| Container context registry | `container/agent-runner/src/context-registry.ts` | Provider-neutral context surfaces for container agents. |
| Runtime context | `src/container-runner.ts`, `src/message-orchestrator.ts` | Host-side prompt, snapshot, backend, image, and session wiring. |
| Mounts | `src/container-mounter.ts` | Group/project/vault filesystem visibility and isolation. |
| Live chat/task DB | `store/messages.db` | Messages, chats, groups, scheduled tasks, backend-scoped sessions. |
| Semantic memory DB | `~/.deus/memory.db` or `DEUS_DB` | Warm/cold session recall via `scripts/memory_indexer.py`. |
| Memory tree DB | `~/.deus/memory_tree.db` or `DEUS_MEMORY_TREE_DB` | Cold-start/cross-branch recall via `scripts/memory_tree.py`. |
| Evolution DB | `~/.deus/evolution.db` | Response scoring, reflexions, principles, and optimization state. |

Personal facts should come from loaded vault context or memory retrieval, not
model guesses. If confidence is low, retrieve first or say what is missing.

## Runtime And Backend Entrances

Backend neutrality starts at the host runtime:

- `src/agent-backends/types.ts` defines `AgentBackend`, `BackendSessionRef`,
  `BackendCapabilities`, and `RunContext`.
- `src/agent-backends/resolve.ts` implements selection precedence: task
  override, group override, global default, then Claude fallback.
- `src/db.ts` stores backend-aware session refs and task backend overrides.
- `src/router-state.ts` keeps in-memory backend-scoped session state.
- `src/task-scheduler.ts` runs scheduled tasks with the same backend/session
  resolution as interactive turns.
- `container/agent-runner/src/openai-backend.ts` is the OpenAI/Codex adapter.
- `container/agent-runner/src/index.ts` keeps Claude as the compatibility path.

Never resume a session across backend mismatch. Starting fresh is better than
cross-contaminating vendor session state; re-load Deus memory/context instead.

## Tools, Skills, And MCP

Deus-owned tools are the canonical capability plane:

- Filesystem, shell, grep/glob/edit-style file operations.
- Deus IPC: `send_message`, task scheduling and management, group
  registration.
- Browser/computer-use and web fetch/search wrappers.
- Skill-provided MCP servers and channel packages.

Provider-native tools are optional accelerators only. They must preserve the
same user-visible command names, permissions, output semantics, and security
boundaries.

Repo-owned host skills live under `.claude/skills/`. Some agent runtimes may
consume a generated `.agents/skills/` compatibility tree. Host skills are for
host coding sessions, not commands to suggest inside WhatsApp, Telegram, Slack,
Discord, or Gmail.

## Commands To Preserve

CLI interface choices:

- `deus`
- `deus claude`
- `deus codex` (sets CLI to Codex and runtime backend to OpenAI for that invocation)
- `deus openai` (alias for `deus codex`)
- `DEUS_CLI_AGENT=claude|codex`
- `DEUS_AGENT_BACKEND=claude|openai`

Chat/channel commands must not depend on backend:

- `/settings`
- `/settings session_idle_hours=N`
- `/settings timeout=N`
- `/settings requires_trigger=true|false`
- `/compact`

Host/session skills are native slash skills for direct Claude/Codex sessions,
not commands to suggest inside messaging channels. The canonical inventory is
the table in [AGENTS.md](../AGENTS.md#commands-and-skills); every new
`.claude/skills/` entry must be added there in the same change.

## Warden Gates

Wardens are specialized review agents. Use them whenever available for
non-trivial work:

- Plan Warden before large design or architecture changes.
- Code Warden after implementation and before merge/commit.
- Use `.claude/wardens/plan-review-rules.md` and
  `.claude/wardens/code-review-rules.md` as their source of truth.

If background agents are unavailable because of usage limits or tooling
failure, record that explicitly and continue with a local review instead of
pretending the gate passed.

## Verification Baseline

Pick tests by the touched layer. Common checks:

- `zsh -n deus-cmd.sh`
- `npm run typecheck`
- `npm run build`
- `npm run lint`
- `npm test -- <targeted tests>`
- `npm run build` in `container/agent-runner`
- `npx vitest run src/context-registry.test.ts src/openai-backend.test.ts` in
  `container/agent-runner`
- `python3 -m pytest scripts/tests/<targeted_test>.py`
- `git diff --check`

Some full-suite tests need privileges that the sandbox may not grant, such as
binding localhost ports or writing the global `~/.local/bin/deus` symlink. When
that happens, report the exact blocked tests and the permission reason.

## Final Rule

Do not make the next agent rediscover this map. If you add a new backend,
channel, memory layer, command family, DB, MCP, or architectural entry point,
update this file and the relevant ADR/pattern docs in the same change.
