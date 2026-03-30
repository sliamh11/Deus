# Deus Architecture

## Overview

Deus is a personal AI assistant built on NanoClaw. A single Node.js host process orchestrates container-isolated Claude agents across messaging channels (WhatsApp, Telegram, Slack, Discord, Gmail). Each conversation group runs in its own Linux container with an isolated filesystem. A semantic memory layer (sqlite-vec + Gemini embeddings) provides cross-session recall. A self-improvement loop scores every production interaction, generates reflections for low-scoring responses, and optimizes the system prompt via DSPy. An evaluation layer (DeepEval) tests the agent against curated datasets before merges.

## System Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                              USER                                        │
│                 WhatsApp  ·  Telegram  ·  Slack  ·  Discord              │
└────────────────────────────┬─────────────────────────────────────────────┘
                             │ message
                             ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                     HOST PROCESS  (Node.js)                              │
│                                                                          │
│   ┌──────────────┐    ┌─────────┐    ┌───────────────┐                  │
│   │ Channel       │───▶│ SQLite  │───▶│ Message Loop  │                  │
│   │ Registry      │    │ (db.ts) │    │ (index.ts)    │                  │
│   └──────────────┘    └─────────┘    └───────┬───────┘                  │
│                                              │                           │
│   ┌──────────────┐    ┌───────────┐          │                          │
│   │ Startup Gate │    │ Scheduler │──────────┤                          │
│   │              │    │           │          │                           │
│   └──────────────┘    └───────────┘          │                          │
│                                              │                           │
│   ┌──────────────┐    ┌───────────┐          │                          │
│   │ Credential   │    │ IPC       │──────────┘                          │
│   │ Proxy        │    │ Watcher   │         │ spawn container           │
│   └──────────────┘    └───────────┘         ▼                           │
│                                    ┌─────────────────┐                  │
│                                    │ Container Runner │                  │
│                                    └────────┬────────┘                  │
└─────────────────────────────────────────────┼───────────────────────────┘
                                              │
              ┌───────────────────────────────▼──────────────────────────┐
              │                   CONTAINER  (Linux VM)                   │
              │                                                          │
              │   Claude Agent SDK (claude-code CLI)                     │
              │       ├── MCP: Google Calendar, YouTube Transcript       │
              │       ├── Filesystem: /workspace/group (rw)              │
              │       ├── Agent Browser (Chromium)                       │
              │       └── IPC input poller (follow-up messages)          │
              └──────────────────────────────────────────────────────────┘
                                              │ response + interaction log
              ┌───────────────────────────────▼──────────────────────────┐
              │                   MEMORY LAYER                           │
              │                                                          │
              │   Session Logs ──▶ Memory Indexer (scripts/)             │
              │   (Obsidian vault)   sqlite-vec + Gemini embeddings      │
              │                         │                                │
              │              Tiered retrieval:                           │
              │              warm (last N sessions, free)                │
              │              cold (semantic search + recency boost)      │
              └──────────────────────────────────────────────────────────┘
                                              │
              ┌───────────────────────────────▼──────────────────────────┐
              │                   EVOLUTION LOOP  (evolution/)           │
              │                                                          │
              │   Interaction Log ──▶ Judge (Gemini or Ollama)           │
              │        ├── Score < 0.6 ──▶ Reflexion (critique + fix)    │
              │        └── N >= 20 samples ──▶ DSPy Optimize             │
              └──────────────────────────────────────────────────────────┘
                                              │
              ┌───────────────────────────────▼──────────────────────────┐
              │                   EVAL LAYER  (eval/, DeepEval)          │
              │                                                          │
              │   test_core_qa · test_tool_use · test_safety             │
              │   Parallel pre-warm · dynamic concurrency                │
              │   Judge: OllamaJudge (local) or GeminiJudge              │
              └──────────────────────────────────────────────────────────┘
```

## Host Process (`src/`)

The host is a single Node.js process. No microservices. All coordination happens in-process.

### `index.ts` — Main Orchestrator

The entry point. Responsibilities:

- **Message loop**: polls SQLite for new messages across all registered groups. Runs on `POLL_INTERVAL` (configurable). Groups messages by chat JID.
- **Group queue**: ensures one container per group at a time. Messages arriving while a container is active are piped to it via stdin; otherwise a new container is spawned.
- **Session command interception**: intercepts `/compact`, `/resume`, `/compress` before they reach the container. Requires fresh container (not piped) for SDK recognition.
- **IPC watcher startup**: initializes the file-based IPC system for cross-group communication.
- **Scheduler startup**: starts the cron/interval task dispatch loop.
- **Startup recovery**: on boot, scans for messages that arrived between the last cursor advance and the crash, re-enqueues them.
- **Graceful shutdown**: SIGTERM/SIGINT handlers stop the credential proxy, drain the group queue (10s timeout), and disconnect all channels.

### `channels/` — Channel Registry

Channels self-register at import time via the registry pattern (`src/channels/registry.ts`).

**Registry API:**
- `registerChannel(name, factory)` — called by each channel module during import
- `getChannelFactory(name)` — returns the factory, or undefined
- `getRegisteredChannelNames()` — lists all registered channels

**Channel interface** (`src/types.ts`):
- `connect()` — authenticate and start listening
- `sendMessage(jid, text)` — send outbound message
- `isConnected()` — health check
- `ownsJid(jid)` — determines which channel handles a given JID
- `disconnect()` — clean shutdown
- `setTyping?(jid, typing)` — optional typing indicator
- `syncGroups?(force)` — optional group metadata sync

**Implementations:**
- `whatsapp.ts` — uses `@whiskeysockets/baileys`, authenticates via QR code or pairing code
- `telegram.ts` — uses `grammy`, authenticates via bot token

Factories return `null` when credentials are missing, so unconfigured channels are silently skipped at startup.

### `container-runner.ts` — Container Lifecycle

Spawns containers and manages the stdin/stdout protocol.

**Input protocol**: JSON object written to container stdin containing prompt, session ID, group folder, chat JID, main-group flag, image attachments.

**Output protocol**: stdout is parsed for sentinel markers (`---DEUS_OUTPUT_START---` / `---DEUS_OUTPUT_END---`). Each marker pair wraps one JSON result. Multiple results may be emitted per session (streaming output).

**Volume mounts** (built by `buildVolumeMounts`):
- Main group: project root (read-only), group folder (read-write), `.env` shadowed with `/dev/null`
- Non-main groups: group folder (read-write), `groups/global/` (read-only)
- All groups: per-group `.claude/` session directory (read-write), per-group IPC directory (read-write), per-group agent-runner source (read-write)
- Additional mounts validated by `mount-security.ts`

**Evolution integration**: before dispatch, fetches relevant reflections via `evolution-client.ts` and prepends them to the prompt. After dispatch, logs the interaction for async scoring (fire-and-forget).

### `container-runtime.ts` — Runtime Abstraction

Abstracts Docker, Apple Container, and Podman behind a single interface. The runtime binary is set via `CONTAINER_RUNTIME` env var (default: `docker`).

Handles:
- Runtime detection and health check (`ensureContainerRuntimeRunning`)
- Orphan container cleanup (`cleanupOrphans`)
- Host gateway resolution (platform-specific: macOS loopback, Linux docker0 bridge)
- Read-only mount argument generation

### `db.ts` — SQLite Database

Uses `better-sqlite3`. Schema tables:
- `chats` — JID, name, last message time, channel, is_group flag
- `messages` — id, chat_jid, sender, content, timestamp, is_from_me, is_bot_message
- `scheduled_tasks` — id, group_folder, prompt, schedule_type, schedule_value, status, next_run
- `task_run_logs` — execution history for scheduled tasks
- `sessions` — group_folder to session_id mapping
- `registered_groups` — JID to group config mapping
- `router_state` — key-value store for cursor positions and state

### `ipc.ts` — File-Based IPC

Cross-group communication via JSON files in `~/.deus/ipc/<group_folder>/messages/`.

**Authorization model**:
- Main group can send messages to any registered group
- Non-main groups can only send to their own chat JID
- Unauthorized IPC attempts are logged and blocked

Also handles task CRUD operations (create, update, delete) and group registration commands from containers.

### `task-scheduler.ts` — Scheduled Tasks

Polls for due tasks on `SCHEDULER_POLL_INTERVAL`. Supports:
- `cron` — standard cron expressions (parsed via `cron-parser`), timezone-aware
- `interval` — fixed interval in milliseconds
- `once` — single execution, then marked complete

Tasks spawn containers using the same `runContainerAgent` path as regular messages.

### `credential-proxy.ts` — API Key Injection

An HTTP proxy server that containers route Anthropic API calls through. The proxy injects real credentials so containers never see them.

Two auth modes:
- **API key**: proxy injects `x-api-key` header on every request
- **OAuth**: container CLI exchanges a placeholder token for a temporary API key via the OAuth endpoint; proxy injects the real OAuth token on the exchange request

Binds to `127.0.0.1` on macOS (Docker Desktop VM routes `host.docker.internal` to loopback) and to the `docker0` bridge IP on Linux.

### `startup-gate.ts` — Prerequisite Validation

Validates prerequisites before heavy initialization. Uses a check registry pattern (new checks added without modifying the gate itself).

Three severity levels:
- `fatal` — blocks startup (e.g., missing API credentials)
- `warn` — allows startup with warning (e.g., memory vault not configured)
- `suggest` — one-line hint (e.g., Gemini API key, channels)

Checks defined in `src/checks.ts`.

### Other Modules

- `router.ts` — outbound message routing, finds which channel owns a JID, formats messages
- `group-queue.ts` — per-group serialization queue, ensures one container per group, supports stdin piping to active containers
- `group-folder.ts` — resolves and validates group folder paths
- `mount-security.ts` — validates additional volume mounts against an allowlist at `~/.config/deus/mount-allowlist.json`, enforces blocked patterns (`.ssh`, `.env`, credentials, private keys)
- `sender-allowlist.ts` — per-group sender filtering (allow/drop modes)
- `session-commands.ts` — intercepts `/compact`, `/resume`, `/compress` slash commands
- `evolution-client.ts` — bridges Node.js host to the Python evolution package via child_process
- `remote-control.ts` — remote Claude Code session management
- `transcription.ts` — voice message transcription (Whisper on Apple Silicon)
- `image.ts` — image attachment parsing for multimodal content
- `logger.ts` — pino-based structured logging
- `config.ts` — environment variable loading and defaults
- `env.ts` — `.env` file reader

## Container System

### Docker Image (`container/Dockerfile`)

Base: `node:22-slim`. Includes:
- Chromium + font packages (CJK, emoji, liberation) for browser automation
- `agent-browser` and `@anthropic-ai/claude-code` installed globally
- Agent runner source compiled at container startup via `entrypoint.sh`

Runs as non-root `node` user. Workspace directories created at build time:
- `/workspace/group` — group-specific files (read-write, working directory)
- `/workspace/global` — shared memory (read-only for non-main)
- `/workspace/ipc` — IPC message exchange
- `/workspace/extra` — additional mounts

### Agent Runner (`container/agent-runner/`)

The process that runs inside each container.

**Input**: reads full `ContainerInput` JSON from stdin (prompt, session ID, group info, image attachments).

**Follow-up messages**: polls `/workspace/ipc/input/` for JSON files. The `_close` sentinel file signals session end.

**Message streaming**: uses a `MessageStream` class (push-based async iterable) to feed follow-up messages to the Claude Agent SDK without closing the session.

**SDK integration**: calls `query()` from `@anthropic-ai/claude-agent-sdk` with:
- Pre-compact hook for context management
- Session ID for conversation continuity
- `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` enabled for subagent orchestration
- `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD` for loading per-group memory

**Output**: wraps each result in `OUTPUT_START_MARKER` / `OUTPUT_END_MARKER` pairs on stdout. Supports `<internal>...</internal>` blocks for agent reasoning that gets stripped before delivery to the user.

### Skills (`container/skills/`)

Per-container skill definitions synced from `container/skills/` into each group's `.claude/skills/` directory. Skills extend agent capabilities within the container sandbox.

## Channel System

### Architecture

All channels follow the registry pattern. The barrel import `src/channels/index.ts` triggers side-effect imports of each channel module, which calls `registerChannel()` during module load.

At startup, `index.ts` iterates over registered channel names, calls each factory with shared callbacks (`onMessage`, `onChatMetadata`, `registeredGroups`), and connects channels that return non-null instances.

### WhatsApp (`src/channels/whatsapp.ts`)

- Library: `@whiskeysockets/baileys`
- Auth: QR code scan or pairing code
- Supports: text messages, voice notes (transcription), images (vision), typing indicators, group sync, reactions
- Auth state stored in `~/.deus/store/whatsapp-auth/`

### Telegram (`src/channels/telegram.ts`)

- Library: `grammy`
- Auth: bot token from BotFather
- Supports: text messages, typing indicators, group detection

## Memory Layer

### Storage

- **SQLite vector database**: `~/.deus/memory.db` using `sqlite-vec` extension for vector similarity search
- **Embeddings**: Gemini `text-embedding-004` model (768-dimensional vectors)
- **Session logs**: Markdown files stored in an Obsidian vault (path configured via `DEUS_VAULT_PATH` or `~/.config/deus/config.json`)

### Memory Indexer (`scripts/memory_indexer.py`)

Commands:
- `--add <path>` — index a session log (compute embedding, store in sqlite-vec)
- `--query <text>` — semantic search over all indexed sessions
- `--recent N` — retrieve last N sessions by date (no API cost)
- `--rebuild` — recompute all embeddings
- `--extract <path>` — extract atomic facts from a session log
- `--wander [topics]` — explore topic associations

### Tiered Retrieval

| Tier | Mechanism | Cost |
|------|-----------|------|
| Warm | Last N sessions by date (`--recent N`) | Free — no embedding call |
| Cold | Semantic search over all indexed sessions with `--recency-boost` to re-rank by age | One Gemini embedding call per query |

### Session Lifecycle

1. **Stop hook** (`scripts/stop_hook.py`): auto-saves a checkpoint to the Obsidian vault at the end of each Claude Code session. No LLM calls.
2. **`/compress`**: saves the current session and updates the semantic index.
3. **`/resume`**: loads core memory + warm tier + cold tier before starting work.

## Evolution Loop (`evolution/`)

### Data Flow

1. **Interaction logging** (`evolution-client.ts` on host, `ilog/` in Python): every agent response is logged with prompt, response, latency, tools used, group, and session ID.
2. **Judge scoring** (`judge/`): a judge LLM scores each interaction on quality (0.0-1.0).
3. **Reflexion** (`reflexion/`): scores below the threshold (default 0.6, configurable via `EVOLUTION_REFLECTION_THRESHOLD`) trigger reflexion — the system generates a self-critique and a corrected response, stored for future retrieval.
4. **DSPy optimization** (`optimizer/`): once 20+ scored samples accumulate, DSPy can tune the system prompt using the scored interactions as a training set.

### Judges

| Judge | Use Case | Model |
|-------|----------|-------|
| `GeminiRuntimeJudge` | Production scoring | Gemini (cloud) |
| `OllamaJudge` | Eval runs (no API quota) | `qwen3.5:4b` (local) |

Auto-detection: pings `localhost:11434`. Uses Ollama if reachable, Gemini otherwise. Override with `EVAL_JUDGE=ollama|gemini`.

### CLI (`evolution/cli.py`)

```
python3 evolution/cli.py score --prompt "..." --response "..."
python3 evolution/cli.py optimize
python3 evolution/cli.py get_reflections <query>
```

### Supporting Modules

- `db.py` — evolution SQLite database (interactions, scores, reflections)
- `config.py` — evolution configuration
- `backfill.py` — score all historical interactions in bulk
- `mcp_server.py` — MCP server exposing evolution tools to Claude Code

## Eval Layer (`eval/`)

### Design

The eval layer tests the agent against curated datasets using DeepEval. Tests spawn real containers (same path as production) via `agent_wrapper.py`.

### Parallel Pre-Warm (`conftest.py`)

At session start, all unique prompts across active test datasets are run concurrently. Tests then hit the in-memory cache instantly.

Concurrency formula: `max(1, min(cpu_count, 8) // 2)`. Override with `DEUS_EVAL_CONCURRENT=N`.

Total wall time: `max(container_latency) * ceil(unique_prompts / concurrency)` instead of sum of all latencies.

### Test Suites

| Suite | File | Dataset |
|-------|------|---------|
| Core Q&A | `test_core_qa.py` | `datasets/core_qa.jsonl` |
| Tool Use | `test_tool_use.py` | `datasets/tool_use.jsonl` |
| Safety | `test_safety.py` | `datasets/safety.jsonl` |

### Judge Selection (`judge_model.py`)

`make_judge()` auto-detects available judges in order: Ollama, Gemini, ClaudeProxy. Uses `DeepEvalBaseLLM` wrappers for compatibility.

### Thresholds

Per-metric thresholds stored in `eval/thresholds.json`. Loaded at test time to determine pass/fail.

## Security Model

### Container Isolation

- Containers run as non-root `node` user
- Project root mounted read-only (main group only)
- `.env` file shadowed with `/dev/null` — containers cannot read host secrets
- Each group gets isolated filesystem, session directory, and IPC namespace
- Agent-runner source is copied per-group so customizations don't cross boundaries

### Credential Proxy

- API keys never appear in container environment variables
- Containers route all Anthropic API calls through the host's credential proxy
- Proxy injects credentials at request time
- On Linux, proxy binds to the docker0 bridge IP (not 0.0.0.0) to avoid network exposure

### IPC Authorization

- Main group can send messages to any registered group
- Non-main groups restricted to their own chat JID
- Unauthorized IPC attempts are blocked and logged

### Mount Security (`src/mount-security.ts`)

- Additional mounts validated against an allowlist at `~/.config/deus/mount-allowlist.json`
- Allowlist stored outside the project root so container agents cannot modify it
- Default blocked patterns: `.ssh`, `.gnupg`, `.aws`, `.kube`, `.docker`, `.env`, `credentials`, private keys
- Path traversal prevention via `path.resolve` normalization

### Sender Allowlist

- Per-group sender filtering with allow and drop modes
- Controls who can trigger the agent in non-main groups
- Session commands (`/compact`, etc.) restricted to main group or `is_from_me`

## Data Flow

```
1. Message arrives at channel (WhatsApp/Telegram/Slack/Discord)
2. Channel callback stores message in SQLite (db.ts)
3. Message loop detects new messages (index.ts, POLL_INTERVAL)
4. Trigger check: main group always triggers; non-main requires trigger word
5. Sender allowlist check: verify sender is permitted
6. Session command interception: /compact, /resume, /compress handled specially
7. Group queue: serialize per-group, pipe to active container or spawn new one
8. Container runner: build volume mounts, fetch reflections, spawn container
9. Agent runner (in container): read stdin JSON, call Claude Agent SDK
10. Agent response: streamed via stdout markers back to host
11. Host sends response to user via channel
12. Evolution client: log interaction, trigger async judge scoring
13. Memory indexer: session logs indexed on /compress or stop hook
```

## Environment Variables

See `.env.example` for the full list. Key variables:

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | Anthropic API key (or use OAuth) |
| `CLAUDE_CODE_OAUTH_TOKEN` | OAuth token (alternative to API key) |
| `GEMINI_API_KEY` | Gemini API key for memory embeddings and production judge |
| `CONTAINER_RUNTIME` | Container binary: `docker`, `container`, `podman` |
| `DEUS_VAULT_PATH` | Obsidian vault path for session logs |
| `EVOLUTION_ENABLED` | Enable/disable evolution loop (default: enabled) |
| `EVOLUTION_REFLECTION_THRESHOLD` | Score threshold for reflexion (default: 0.6) |
| `EVAL_JUDGE` | Force judge: `ollama` or `gemini` |
| `DEUS_EVAL_CONCURRENT` | Override eval pre-warm concurrency |
| `CREDENTIAL_PROXY_HOST` | Override proxy bind address |
| `LOG_LEVEL` | Logging level (default: `info`) |

## Key Design Decisions

Architecture Decision Records are maintained in `docs/decisions/INDEX.md`. Current decisions:

| Decision | Ruling |
|----------|--------|
| Eval IPC via files | Results via shared-volume files, not stdout (Docker pipe buffering) — do not revert |
| Eval in-memory cache only | Disk cache silently masks regressions across builds |
| Eval selective warmup | Warm only active test datasets (saves ~3x time) |
| Startup gate design | Check registry pattern; channels optional; memory system is the priority |

Read the full ADR files in `docs/decisions/` before modifying the relevant subsystems.
