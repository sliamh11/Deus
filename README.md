# Deus

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Node](https://img.shields.io/badge/Node-%3E%3D20-green.svg)](https://nodejs.org/)
[![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Linux-lightgrey.svg)]()

A personal AI assistant that lives in your messaging apps, remembers everything, and gets smarter over time. Built for developers who want a private, self-hosted assistant with container isolation, semantic memory, and a self-improvement loop — all running locally on your machine.

---

## Features

1. **Memory** — Remembers everything across all your conversations. Ask it something you discussed weeks ago and it'll recall it precisely, using semantic search to find the most relevant context.
2. **Messaging apps** — Works inside WhatsApp, Telegram, Slack, Discord, and more. Switch between them freely — memory and context follow you everywhere.
3. **Voice** — Send a voice message and it transcribes and responds. Runs locally on Apple Silicon — nothing leaves your machine.
4. **Vision** — Send a photo or screenshot and it sees and responds to it.
5. **Calendar** — Reads and creates Google Calendar events. Ask what's on your schedule, or tell it to book something.
6. **Web & video** — Fetch YouTube transcripts, summarize videos, or browse the web — all from a chat message.
7. **Scheduled tasks** — Set it to do things automatically on a schedule (daily summaries, weekly recaps, reminders).
8. **Self-improvement** — Scores its own responses over time and automatically generates better answers for cases where it fell short. Uses DSPy to optimize its own system prompt once enough samples accumulate.
9. **Sandboxed & secure** — Every conversation runs in an isolated Linux container. The AI can't access your host system beyond what you explicitly allow.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         YOU                                      │
│            WhatsApp  ·  Telegram                                 │
└───────────────────┬─────────────────────────────────────────────┘
                    │ message
                    ▼
┌─────────────────────────────────────────────────────────────────┐
│                    HOST  (Node.js process)                        │
│                                                                   │
│   Channel Registry ──► SQLite ──► Message Loop                   │
│                                        │                         │
│                          Scheduler ────┤                         │
│                          IPC Watcher ──┘                         │
│                                        │ spawn                   │
└────────────────────────────────────────┼────────────────────────┘
                                         │
                    ┌────────────────────▼────────────────────────┐
                    │            CONTAINER  (Linux VM)             │
                    │                                              │
                    │   Claude Agent SDK                           │
                    │        │                                     │
                    │        ├── Google Calendar (MCP)             │
                    │        ├── YouTube Transcript (MCP)          │
                    │        └── Filesystem (mounted groups/)      │
                    └──────────────────────────────────────────────┘
                                         │ response
                    ┌────────────────────▼────────────────────────┐
                    │           MEMORY LAYER                       │
                    │                                              │
                    │   Session Logs ──► Memory Indexer            │
                    │   (Obsidian vault)   (sqlite-vec + Gemini)   │
                    │                         │                    │
                    │              Tiered retrieval:               │
                    │              warm (recency, free)            │
                    │              cold (semantic + recency boost) │
                    └──────────────────────────────────────────────┘
```

**Key points:**
- One Node.js process on the host. No microservices.
- Each conversation group runs in its own container with an isolated filesystem.
- Memory is stored in a local SQLite vector database, optionally synced to an Obsidian vault.
- An evolution loop scores every production interaction and generates reflections for low-scoring responses. Once enough samples accumulate, DSPy optimizes the system prompt automatically.

---

## Quick Start

### Prerequisites

- macOS (Apple Silicon recommended) or Linux
- [Claude Code](https://claude.ai/download) installed and authenticated
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) or [Apple Container](https://github.com/apple/container)
- Node.js 20+, Python 3.11+
- A [Gemini API key](https://aistudio.google.com/apikey) (free tier is enough)

### Setup

```bash
git clone https://github.com/YOUR_USERNAME/deus.git
cd deus
claude
```

Inside the Claude Code prompt:

```
/setup                  # Install deps, configure container runtime, authenticate
/add-whatsapp           # Scan QR code to connect WhatsApp
/add-telegram           # Paste bot token to connect Telegram
```

Start talking:

```
@Andy what's on my calendar tomorrow?
@Andy summarize the YouTube video at <url>
@Andy remind me every Monday morning what I worked on last week
```

---

## Memory System

| Command | What it does |
|---|---|
| `/compress` | Save the current session to the vault and update the semantic index |
| `/resume` | Load core memory + warm tier (last 3 sessions, free) + cold tier (semantic search) |

**Retrieval tiers:**

| Tier | How it works | Cost |
|---|---|---|
| Warm | Last N sessions by date | Free — no embedding call |
| Cold | Semantic search over all indexed sessions with recency boost | One Gemini embedding call |

A stop hook auto-saves a checkpoint at the end of every Claude Code session with no LLM calls.

---

## Design Principles

| Principle | What it means |
|---|---|
| **Machine-adaptive** | Never hardcode thread counts or resource limits. Scale to available CPU/RAM with env var overrides. |
| **Modular** | Components connect and disconnect cleanly. Adding or removing a channel or integration shouldn't touch unrelated code. |
| **Token-efficient** | Minimize redundant API calls. Cache aggressively. Prefer local models (Ollama) for workloads where quality allows it. |
| **Secure by default** | Credentials never appear in code or git history. Use `.env` files + `.gitignore`. Design as if the repo is public. |

---

## Security & Privacy

- **Container isolation** — Every agent runs in a Linux container (Docker or Apple Container). Agents cannot access your host filesystem beyond explicitly mounted directories.
- **No credentials in code** — All secrets live in `.env` files that are gitignored. The codebase is designed as if the repo is always public.
- **Mount allowlist** — Only directories you explicitly configure are visible to the agent. Everything else is inaccessible.
- **Local-first** — Memory lives in a local SQLite database. Voice transcription runs on-device. No data is sent to external services unless you configure it.

---

## FAQ

**How much does it cost?**
Claude API usage (for the agent) plus optionally Gemini (free tier is sufficient for memory and scoring). Voice transcription is local and free.

**What platforms are supported?**
macOS (Apple Silicon recommended) and Linux. Windows is not supported.

**Can I use a different LLM?**
The core agent uses the Claude Agent SDK — this is architectural and not swappable. The evolution/eval judges can use Ollama (local, free) or Gemini.

**Where is my data?**
All local. Memory in SQLite, session logs optionally in an Obsidian vault, no cloud sync.

**How do I add a new channel?**
Use the skill system: `/add-whatsapp`, `/add-telegram`, `/add-slack`, `/add-discord`, `/add-gmail`. Or build your own channel skill.

**How do I customize behavior?**
Tell Claude Code directly ("change the trigger word to @Max", "make responses shorter") or run `/customize` for guided changes. No config files — the codebase is small enough for Claude to modify safely.

---

## Comparison

| | **Deus** | **Auto-GPT** | **OpenDevin** | **CrewAI** |
|---|---|---|---|---|
| **Container isolation** | Per-conversation Linux containers | No | Sandboxed runtime | No |
| **Semantic memory** | SQLite-vec + tiered retrieval | Plugin-based | None | Short-term only |
| **Self-improvement** | Scores responses, reflexion, DSPy prompt tuning | No | No | No |
| **Messaging integration** | WhatsApp, Telegram, Slack, Discord, Gmail | None | None | None |
| **Local-first** | All data on your machine | Cloud-dependent | Local possible | Cloud-dependent |
| **Architecture** | Single Node.js process | Multi-agent loop | Client-server | Multi-agent framework |

---

## Project Structure

```
src/
  index.ts              # Orchestrator: state, message loop, agent invocation
  channels/             # WhatsApp and Telegram channel implementations
  container-runner.ts   # Spawns and streams agent containers
  task-scheduler.ts     # Runs scheduled tasks
  db.ts                 # SQLite operations
  router.ts             # Outbound message routing
  ipc.ts                # File-based IPC watcher
scripts/
  memory_indexer.py     # Semantic memory: index, query, extract, wander
  stop_hook.py          # Auto-checkpoint on session end
  gcal.mjs              # Google Calendar MCP server
evolution/
  judge/                # OllamaJudge + GeminiJudge (DeepEvalBaseLLM wrappers)
  reflexion/            # Reflexion generator: critiques low-scoring responses
  optimizer/            # DSPy optimizer: tunes system prompt from scored interactions
  ilog/                 # Interaction log: stores and retrieves scored interactions
  db.py                 # Evolution database (SQLite)
  cli.py                # CLI: score, optimize, export
eval/
  conftest.py           # Fixtures: agent cache, parallel pre-warm, dynamic concurrency
  judge_model.py        # make_judge(): auto-detect Ollama → Gemini → ClaudeProxy
  test_core_qa.py       # Factual Q&A tests
  test_tool_use.py      # Tool-calling tests
  test_safety.py        # Refusal and safety tests
  datasets/             # JSONL test cases
groups/
  */CLAUDE.md           # Per-group memory (isolated per conversation)
```

---

## Built on NanoClaw

Deus is built on **[NanoClaw](https://github.com/qwibitai/nanoclaw)** by [qwibitai](https://github.com/qwibitai) — the core framework providing container-isolated Claude agents, multi-channel messaging, and a skill system for safe customization. This repo extends NanoClaw with semantic memory, voice transcription, a self-improvement loop, and more.

Want to start from scratch? Fork the [NanoClaw repo](https://github.com/qwibitai/nanoclaw) and build your own setup.

---

## License

MIT
