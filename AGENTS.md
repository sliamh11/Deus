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

## Security

Deus uses a two-layer prompt injection defense. See [parry-guard ADR](docs/decisions/parry-guard-installation.md) for architecture details.

## Core Entrypoints

Use these instead of rediscovering the system:

| Surface                     | Entry point                                                       | Purpose                                                                                                                                                                                                           |
| --------------------------- | ----------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Task routing                | [`.mex/ROUTER.md`](.mex/ROUTER.md)                                | Maps task type to the required pattern file                                                                                                                                                                       |
| Host runtime                | `src/message-orchestrator.ts`, `src/container-runner.ts`          | Agent dispatch, sessions, streaming, container wiring                                                                                                                                                             |
| Backend selection           | `src/agent-runtimes/resolve.ts`, `src/agent-runtimes/registry.ts` | Task > group > env > Claude fallback; three registered runtimes: claude, openai, llama-cpp                                                                                                                        |
| `deus-native` authorization | `src/agent-runtimes/permission-rules.ts` (landing via LIA-407/B7) | Pure declarative allow/deny rule evaluator + named profile registry (`default`, `read-only`) for the `deus-native` middleware's outermost `wrapToolCall` layer — see `docs/decisions/deus-v2-permission-rules.md` |
| Session storage             | `src/db.ts`, `src/router-state.ts`                                | Backend-scoped session refs and resume state                                                                                                                                                                      |
| Scheduler                   | `src/task-scheduler.ts`                                           | Same backend/session rules as interactive turns                                                                                                                                                                   |
| Container context           | `container/agent-runner/src/context-registry.ts`                  | Runtime-loaded onboarding and memory surfaces                                                                                                                                                                     |
| OpenAI adapter              | `container/agent-runner/src/openai-backend.ts`                    | OpenAI/Codex in-container backend implementation                                                                                                                                                                  |
| Claude path                 | `container/agent-runner/src/index.ts`                             | Compatibility baseline path (in-container)                                                                                                                                                                        |
| TUI backends                | `tui/src/backend/`                                                | Strategy trait — one file per provider (Claude, Codex, etc.)                                                                                                                                                      |
| Tool proxy                  | `src/tool-proxy.ts`, `src/tool-registry.ts`                       | HTTP proxy (:3003) executing allowlisted host CLIs for containers; credentials never passed to containers                                                                                                         |
| Mount/security boundary     | `src/container-mounter.ts`                                        | Project/group/vault visibility and isolation                                                                                                                                                                      |
| Memory retrieval            | `scripts/memory_tree.py`, `scripts/memory_indexer.py`             | Personal recall and semantic lookup                                                                                                                                                                               |
| Linear dispatcher           | `src/linear-dispatcher.ts`                                        | Polls Linear for "Ready for Agent" issues, runs container agents with role prompts, posts results back                                                                                                            |
| Linear webhook gates        | `src/linear-webhook.ts`                                           | Receives Linear webhooks, runs warden-style gates on column transitions                                                                                                                                           |
| Linear gate specs           | `.claude/agents/wardens/`                                         | Config-driven gate specs — file presence = gate enabled                                                                                                                                                           |
| Linear auto-merge           | `src/linear-auto-merge.ts`                                        | Polls CI after gate SHIP, squash-merges agent PRs, moves issue to Done                                                                                                                                            |
| Linear notifications        | `src/linear-notifications.ts`                                     | Unified pipeline comment (rolling timeline) + macOS desktop notifications                                                                                                                                         |
| Pipeline CLI                | `src/linear-pipeline-cli.ts`                                      | `deus pipeline` -- event audit from the terminal                                                                                                                                                                  |
| Codex Warden hooks          | `scripts/codex_warden_hooks.py`                                   | Installs and runs Codex hook equivalents for Warden gates (plan-reviewer, code-reviewer, verification-gate, threat-modeler)                                                                                       |
| Development client tiers    | [`docs/DEVELOPMENT_CLIENT_TIERS.md`](docs/DEVELOPMENT_CLIENT_TIERS.md) | Distinguishes the Tier 1 product runtime from optional Tier 2 development CLIs and documents their guardrail guarantees                                                                                      |

More detailed maps live in [docs/AGENT_DEUS_101.md](docs/AGENT_DEUS_101.md).

## Commands And Skills

Commands that must remain stable across backends:

- `deus`
- `deus claude`
- `deus codex`
- `deus openai`
- `DEUS_CLI_AGENT=claude|codex`
- `DEUS_AGENT_BACKEND=claude|openai`
- `deus pipeline`
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

| Skill                         | When to Use                                                                                                                              |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `/add-asana`                  | Add Asana project management MCP integration (read/write tasks & projects)                                                               |
| `/add-claude-context`         | Deprecated — replaced by `scripts/code_search.py`; do not use                                                                            |
| `/add-codex`                  | Add OpenAI/Codex as a backend                                                                                                            |
| `/add-compact`                | Add the backend-neutral `/compact` session command                                                                                       |
| `/add-discord`                | Add Discord as a channel                                                                                                                 |
| `/add-editor`                 | Wire Deus's memory + evolution into an external editor (Zed, ACP/MCP)                                                                    |
| `/add-gcal`                   | Add Google Calendar integration (list, create, update events)                                                                            |
| `/add-gmail`                  | Add Gmail as a tool or channel                                                                                                           |
| `/add-image-vision`           | Add image attachment vision to Deus agents                                                                                               |
| `/add-linear`                 | Add Linear project management MCP integration (read/write issues, projects, cycles)                                                      |
| `/add-listen-hotkey`          | Add a global hotkey for `deus listen`                                                                                                    |
| `/add-llama-cpp`              | Install and verify optional local `llama.cpp` generation                                                                                 |
| `/add-msft-teams`             | Add Microsoft Teams as a channel (Azure Bot Service / Bot Framework)                                                                     |
| `/add-ollama-tool`            | Add Ollama as an MCP tool for local model calls                                                                                          |
| `/add-outlook`                | Add Outlook (Microsoft 365 email) as a tool or channel                                                                                   |
| `/add-parallel`               | Add Parallel AI MCP research tools                                                                                                       |
| `/add-pdf-reader`             | Add PDF text extraction                                                                                                                  |
| `/add-reactions`              | Add WhatsApp emoji reaction support                                                                                                      |
| `/add-slack`                  | Add Slack as a channel                                                                                                                   |
| `/add-telegram`               | Add Telegram as a channel                                                                                                                |
| `/add-telegram-swarm`         | Add Agent Swarm support to Telegram                                                                                                      |
| `/add-understand-anything`    | Install the Understand-Anything plugin (codebase knowledge graphs, `/understand*`)                                                       |
| `/add-voice-transcription`    | Add OpenAI Whisper voice transcription                                                                                                   |
| `/add-whatsapp`               | Add WhatsApp as a channel                                                                                                                |
| `/add-youtube-transcript`     | Add YouTube transcript extraction                                                                                                        |
| `/checkpoint`                 | Save a mid-session continuity checkpoint                                                                                                 |
| `/code-review`                | Run multi-agent code review                                                                                                              |
| `/codebase-design`            | Shared vocabulary for designing deep modules — interface depth, seams, testability                                                       |
| `/compress`                   | Save the session to the vault and update memory indexes                                                                                  |
| `/convert-to-apple-container` | Switch from Docker to Apple Container                                                                                                    |
| `/customize`                  | Add channels, integrations, or behavior changes                                                                                          |
| `/debug`                      | Debug containers, logs, auth, and runtime issues                                                                                         |
| `/deep-research`              | Multi-stage research pipeline — classifies intent (shallow/deep/creative), fans out lit-scout + brainstormer, synthesizes with citations |
| `/design-to-dev`              | Orchestrate frontend implementation from design wireframes (Linear specs + parallel worktrees)                                           |
| `/diagnosing-bugs`            | Structured diagnosis loop for hard bugs and performance regressions (feedback-loop first)                                                |
| `/domain-modeling`            | Build and sharpen a project's domain model — glossary + ADRs                                                                             |
| `/grill-me`                   | Relentless interview to stress-test a plan or design before building                                                                     |
| `/grill-with-docs`            | Grill a plan and capture decisions as ADRs + glossary as you go                                                                          |
| `/grilling`                   | The relentless plan/design interview engine (used by `/grill-me` and `/grill-with-docs`)                                                 |
| `/handoff`                    | Write a structured handoff document so the next agent starts with context                                                                |
| `/learn-this`                 | Teach a topic and persist it as queryable vault memory, grounded in NotebookLM (vault-native companion to `/teach`)                      |
| `/linear-slice`               | Decompose a plan into dependency-ordered Linear issues (tracer-bullet slices) and release them into the dispatch pipeline                |
| `/onboard`                    | Onboard the current project into Deus code intelligence (codegraph + code_search indexing)                                               |
| `/preferences`                | View or modify Deus user preferences                                                                                                     |
| `/preserve`                   | Save durable memories from the current conversation                                                                                      |
| `/project-settings`           | View or modify external project memory settings                                                                                          |
| `/prototype`                  | Build a throwaway prototype to validate a state model or UI direction                                                                    |
| `/resolving-merge-conflicts`  | Disciplined resolution of an in-progress git merge/rebase conflict                                                                       |
| `/resume`                     | Load recent work and memory context                                                                                                      |
| `/review-logs`                | Review Deus system health logs                                                                                                           |
| `/setup`                      | Run first-time installation and configuration                                                                                            |
| `/tdd`                        | Test-driven development loop — red-green-refactor, behavior-first                                                                        |
| `/teach`                      | Teach a skill or concept over multiple sessions (stateful teaching workspace)                                                            |
| `/update-skills`              | Update installed skill branches from upstream                                                                                            |
| `/use-local-whisper`          | Switch voice transcription to local whisper.cpp                                                                                          |
| `/wardens`                    | View, toggle, and configure warden quality gates                                                                                         |
| `/writing-great-skills`       | Reference for authoring predictable skills — leading words, progressive disclosure                                                       |
| `/x-integration`              | Set up or use X/Twitter integration                                                                                                      |

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

## Working with Deus's memory + evolution (editor agents)

> Deus brain is wired into this editor via MCP (`deus-memory`, `deus-evolution`).
> Use it so Deus learns across projects.

- **Start of a coding task:** call `get_reflections` (omit `group_folder` — global lessons) to
  load prior learnings. If the task touches past decisions, conventions, or research, also call
  `memory_recall`.
- **End of a task, or after a clear success/failure:** call `log_interaction` with a short
  summary of what was asked and what you did. Omit `group_folder` so the lesson is global and
  carries to other projects.
- **When the user gives feedback** ("that was wrong" / "good"): call `record_feedback` for that
  interaction.
- Treat reflections as soft guidance learned from past misses — weigh them, don't obey blindly.

### Quality gates

Claude Code enforces plan-review, code-review, and verification mechanically through
PreToolUse/Stop hooks. How you get the same gates depends on whether your editor has a hook
system:

**Codex CLI (has hooks) — install the bridge once per repo.** `codex_warden_hooks.py` mirrors
the Deus Warden gates into Codex's own `hooks.json`, so they are enforced mechanically:

```bash
python3 ~/deus/scripts/codex_warden_hooks.py install --repo-root "$(pwd)"
python3 ~/deus/scripts/codex_warden_hooks.py check   --repo-root "$(pwd)"   # confirm active
```

After this the hooks block edits/commits until the matching reviewer has approved — they prompt
you to run the reviewer and record its verdict, exactly as Claude Code's hooks do. You do not
invoke the gates by hand. (The `/add-codex` setup skill wires this for you.)

**Zed / other ACP editors (no hook system) — apply the gates as discipline.** Nothing enforces
them for you, so before each step:

- **Before non-trivial source edits:** state your plan and critique it (yourself, or via a
  sub-agent) before touching code. Typos, comments, and single-line fixes are exempt.
- **Before committing:** review the full staged diff for correctness, security, and scope.
- **Before claiming work is done:** re-run the build/tests and confirm the change does what was
  asked.

Always show the commit message and wait for user approval before committing. Never push directly
to `main` — create a feature branch and PR.

### Editor session lifecycle

These replace the `/resume`, `/checkpoint`, `/compress`, `/preserve`, and `/handoff` skills
which are not available outside Claude Code. Resolve the vault path once per session:

```bash
VAULT=$(python3 -c "import json,os; print(os.path.expanduser(json.load(open(os.path.expanduser('~/.config/deus/config.json')))['vault_path']))")
```

**Start of session (replaces /resume):**

```bash
python3 ~/deus/scripts/memory_indexer.py --recent 3
```

Read the output plus any today's checkpoint: `ls -t "$VAULT/Checkpoints/$(date +%Y-%m-%d)"-*.md 2>/dev/null | head -1`.
Summarize ongoing context and pending tasks before starting work.

**Mid-session save (replaces /checkpoint):**
Write a checkpoint to `$VAULT/Checkpoints/YYYY-MM-DD-HH.md` with frontmatter:
`type: checkpoint`, `created`, `session_topic`, `project_path`, `decisions`, `in_progress`,
`next_action`, `context_refs`. Keep under 25 lines.

**End of session (replaces /compress):**

1. Write a session log to `$VAULT/Session-Logs/YYYY-MM-DD/<topic-slug>.md` with frontmatter:
   `type: session`, `date`, `topics`, `project_path`, `tldr`, `decisions`. Include a body with
   what happened, files modified, and a pending tasks checklist.
2. Index and extract atoms:
   ```bash
   python3 ~/deus/scripts/memory_indexer.py --add "<full path to log>"
   python3 ~/deus/scripts/memory_indexer.py --extract "<full path to log>"
   ```
3. Update `$VAULT/CLAUDE.md` pending tasks if any changed.

**Preserve durable knowledge (replaces /preserve):**
If the session produced lasting insights (preferences, decisions, corrections), append them to
`$VAULT/CLAUDE.md` as compact `key: value` lines. Skip for routine sessions.

**Handoff (replaces /handoff):**
When stopping mid-task, write a structured handoff to `$VAULT/Handoffs/YYYY-MM-DD-<slug>.md`
summarizing: what was done, what remains, key files, and the exact next step.

## Update Rule

Do not make the next agent rediscover this map. If you add or change a backend,
channel, memory layer, command family, DB, MCP surface, or architectural
entrypoint, update this file and the relevant ADR/reference docs in the same
change.
