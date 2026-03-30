# Deus

A personalized setup built on [NanoClaw](https://github.com/qwibitai/nanoclaw) — a minimal, container-isolated AI assistant. This fork extends the base with semantic memory, an evaluation layer, a self-improvement loop, local LLM judging, and integrations for calendar, voice, and images.

---

## TL;DR — What is this?

A personal AI assistant that lives in your messaging apps and gets smarter the more you use it. Everything runs on your own computer.

**Core abilities:**

1. **Memory** — Remembers everything across all your conversations. Ask it something you discussed weeks ago and it'll recall it precisely, using semantic search to find the most relevant context.
2. **Messaging apps** — Works inside WhatsApp, Telegram, Slack, Discord, and more. Switch between them freely — memory and context follow you everywhere.
3. **Voice** — Send a voice message and it transcribes and responds. Runs locally on Apple Silicon — nothing leaves your machine.
4. **Vision** — Send a photo or screenshot and it sees and responds to it.
5. **Calendar** — Reads and creates Google Calendar events. Ask what's on your schedule, or tell it to book something.
6. **Web & video** — Fetch YouTube transcripts, summarize videos, or browse the web — all from a chat message.
7. **Scheduled tasks** — Set it to do things automatically on a schedule (daily summaries, weekly recaps, reminders).
8. **Self-improvement** — Scores its own responses over time and automatically generates better answers for cases where it fell short. The more you use it, the better it gets.
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
                                         │
                    ┌────────────────────▼────────────────────────┐
                    │           EVOLUTION LOOP                     │
                    │                                              │
                    │   Interaction Log ──► Judge (Ollama/Gemini)  │
                    │        │                                     │
                    │        ├── Score < 0.6 → Reflexion           │
                    │        └── N ≥ 20 samples → DSPy Optimize    │
                    └──────────────────────────────────────────────┘
                                         │
                    ┌────────────────────▼────────────────────────┐
                    │           EVAL LAYER  (DeepEval)             │
                    │                                              │
                    │   test_core_qa · test_tool_use · test_safety │
                    │   Parallel pre-warm · dynamic concurrency    │
                    │   Judge: OllamaJudge (local) or GeminiJudge  │
                    └──────────────────────────────────────────────┘
```

**Key points:**
- One Node.js process on the host. No microservices.
- Each conversation group runs in its own container with an isolated filesystem. Agents can't reach your host.
- Memory is stored in a local SQLite vector database, optionally synced to an Obsidian vault.
- The evolution loop scores every production interaction and automatically generates reflections for low-scoring responses. Once 20+ scored samples accumulate, DSPy can optimize the system prompt.
- The eval layer (DeepEval) tests the agent against curated datasets before merges. Eval uses a local Ollama judge by default — no Gemini quota burned.

---

## What this fork adds

| Feature | Description |
|---|---|
| **Semantic memory** | Session logs are indexed using Gemini embeddings. Retrieval is tiered: recent sessions load automatically at zero API cost (warm tier); older sessions are surfaced by semantic search with a recency boost (cold tier). Query, extract atomic facts, or wander topic associations. |
| **Session checkpoints** | A stop hook auto-saves a checkpoint to the Obsidian vault at the end of each Claude Code session (throttled, no LLM calls). |
| **Voice transcription** | Voice messages are transcribed locally using Whisper on Apple Silicon — no data leaves your machine. |
| **Image vision** | Images sent via WhatsApp or Telegram are passed to Claude as multimodal content. |
| **Google Calendar** | Claude can read and create calendar events via an MCP server. |
| **YouTube transcripts** | Claude can fetch and read YouTube video transcripts via MCP. |
| **Evolution loop** | Every production interaction is scored by a judge (Gemini by default). Scores below 0.6 trigger reflexion — Claude generates a critique and correction and stores it. Once 20+ samples accumulate, `python3 evolution/cli.py optimize` runs DSPy to tune the system prompt automatically. |
| **Eval layer** | DeepEval test suite (`test_core_qa`, `test_tool_use`, `test_safety`) runs against containerized agents. Parallel pre-warm fills the response cache concurrently before tests run — full suite takes minutes, not hours. Uses OllamaJudge by default to preserve Gemini quota. |
| **Local Ollama judge** | OllamaJudge (`qwen3.5:4b`) runs eval without any API calls. Auto-detected when Ollama is running; falls back to Gemini when not. Override with `EVAL_JUDGE=ollama\|gemini`. |
| **Dynamic concurrency** | All parallel workloads (eval pre-warm, future components) scale to the host machine — `max(1, min(cpu_count, 8) // 2)` for I/O-bound work. Never hardcoded. Override with `DEUS_EVAL_CONCURRENT=N`. |
| **`/compact` command** | Manually compact the context window mid-session without losing continuity. |
| **`/resume` command** | Load the latest checkpoint + warm tier + cold tier from the vault before starting work on a long task. |
| **`/compress` command** | Save the current session to the vault and update the memory index. |

---

## Getting started

### Prerequisites

- macOS (Apple Silicon recommended)
- [Claude Code](https://claude.ai/download) installed and authenticated
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) or [Apple Container](https://github.com/apple/container)
- Node.js 20+
- Python 3.11+ (for memory indexer and evolution loop)
- A [Gemini API key](https://aistudio.google.com/apikey) (free tier is enough for personal use)

### Step 1 — Clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/deus.git
cd deus
```

### Step 2 — Open Claude Code

```bash
claude
```

All remaining setup is handled by Claude Code skills — you don't run commands manually.

### Step 3 — Run setup

Inside the `claude` prompt:

```
/setup
```

Claude will install dependencies, configure the container runtime, and walk you through authenticating your messaging channels.

### Step 4 — Add channels

```
/add-whatsapp
/add-telegram
```

Each skill guides you through authentication (QR code for WhatsApp, bot token for Telegram) and registers the channel automatically.

### Step 5 — Configure Google Calendar (optional)

```
/setup
```

Follow the GCP OAuth prompts. Once done, Claude can read and create calendar events from any conversation.

### Step 6 — Set up the memory indexer (optional)

Install Python dependencies:

```bash
pip install sqlite-vec google-genai
```

Add your Gemini API key to `~/.config/deus/.env`:

```bash
GEMINI_API_KEY=your-key-here
```

Index a session log:

```bash
python3 scripts/memory_indexer.py --add path/to/session.md
```

Query your memory:

```bash
# Semantic search (uses Gemini embeddings)
python3 scripts/memory_indexer.py --query "what did we discuss about the auth rewrite"

# With recency boost — promotes sessions from the last 7 or 30 days
python3 scripts/memory_indexer.py --query "auth rewrite" --recency-boost

# Recent sessions by date — no API cost
python3 scripts/memory_indexer.py --recent 5
```

### Step 7 — Start talking

Send a message in WhatsApp or Telegram using the trigger word (default: `@Andy`):

```
@Andy what's on my calendar tomorrow?
@Andy summarize the YouTube video at <url>
@Andy remind me every Monday morning what I worked on last week
```

---

## Memory system

The memory system has three entry points:

| Command | What it does |
|---|---|
| `/compress` | Saves the current session to the vault and updates the semantic index |
| `/resume` | Loads core memory + warm tier (last 3 sessions, free) + cold tier (semantic search, recency-boosted) |
| `/smart-compact` | Saves a checkpoint, outputs a structured context primer to aid compaction, then compacts |

**Retrieval tiers:**

| Tier | How it works | Cost |
|---|---|---|
| Warm | Last N sessions by date (`--recent N`) | Free — no embedding call |
| Cold | Semantic search over all indexed sessions (`--query`) with `--recency-boost` to re-rank by age | One Gemini embedding call |

The stop hook runs automatically — it writes a lightweight checkpoint to the vault at the end of every Claude Code session with no LLM calls, so it's fast and free.

---

## Evolution loop

The evolution loop scores every production interaction and uses the results to improve the system over time.

```bash
# Score a single interaction manually
python3 evolution/cli.py score --prompt "..." --response "..."

# Backfill: score all past interactions in the database
python3 -m evolution.backfill

# Run DSPy optimization (requires 20+ scored samples)
python3 evolution/cli.py optimize
```

**How it works:**
1. Every agent response is scored by a judge (Gemini by default; `EVAL_JUDGE=ollama` to use Ollama locally).
2. Scores below `EVOLUTION_REFLECTION_THRESHOLD` (default: 0.6) trigger reflexion — the system generates a self-critique and stores a corrected response.
3. Once 20+ scored samples exist, DSPy can optimize the system prompt using the scored interactions as a training set.

**Judges:**

| Judge | When to use |
|---|---|
| `OllamaJudge` (local, `qwen3.5:4b`) | Eval runs — preserves Gemini quota |
| `GeminiRuntimeJudge` | Production scoring — higher quality |

Auto-detection: pings `localhost:11434`. Uses Ollama if reachable, Gemini otherwise. Override with `EVAL_JUDGE=ollama|gemini`.

---

## Eval layer

The eval layer runs automated quality checks against the containerized agent using [DeepEval](https://github.com/confident-ai/deepeval).

```bash
# Run full suite with local Ollama judge (no quota risk)
cd eval && EVAL_JUDGE=ollama CLAUDE_CODE_OAUTH_TOKEN=placeholder \
  .venv/bin/pytest test_core_qa.py test_tool_use.py test_safety.py -v

# Single test
cd eval && EVAL_JUDGE=ollama CLAUDE_CODE_OAUTH_TOKEN=placeholder \
  .venv/bin/pytest test_core_qa.py -k "cqa_001" -v
```

**Design:**
- Parallel pre-warm: at session start, all unique prompts are run concurrently (`DEUS_EVAL_CONCURRENT`, default `max(1, min(cpu_count, 8) // 2)`). Tests then hit the in-memory cache instantly.
- Session-scoped cache: multiple metrics on the same case reuse a single container invocation.
- Total wall time: `max(container_latency) × ceil(unique_prompts / concurrency)` instead of the sum of all latencies.

**Datasets** (under `eval/datasets/`):
- `core_qa.jsonl` — factual Q&A
- `tool_use.jsonl` — tool-calling scenarios
- `safety.jsonl` — refusal and boundary tests

---

## Design principles

Every component in this system follows four non-negotiable rules:

| Principle | What it means |
|---|---|
| **Machine-adaptive** | Never hardcode thread counts, worker counts, or resource limits. Always scale to available CPU/RAM with an env var override. |
| **Modular** | Components connect and disconnect cleanly. Adding or removing a channel, judge, or integration shouldn't touch unrelated code. |
| **Token-efficient** | Minimize redundant API calls. Cache aggressively. Prefer local models (Ollama) for workloads where quality requirements allow it. |
| **Secure by default** | Credentials never appear in code or git history — not even in private repos. Use `.env` files + `.gitignore`. Design as if the repo is public. |

---

## Customizing

Deus doesn't use config files. To change behavior, tell Claude Code directly:

```
Change the trigger word to @Max
Make responses shorter and more direct
Add a daily standup summary every weekday at 9am
```

Or run `/customize` for guided changes. The codebase is small enough that Claude can safely modify it.

To pull in upstream fixes and features from the base Deus project, run `/update-nanoclaw` inside Claude Code. It previews the changes and lets you cherry-pick selectively.

---

## Project structure

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
  backfill.py           # Backfill: score all historical interactions
  mcp_server.py         # MCP server exposing evolution tools to Claude Code
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

## Requirements

- macOS or Linux
- Node.js 20+
- Python 3.11+ (memory indexer and evolution loop)
- [Claude Code](https://claude.ai/download)
- [Apple Container](https://github.com/apple/container) (macOS, recommended) or [Docker](https://www.docker.com/products/docker-desktop/)
- Gemini API key (memory indexer + production judge — free tier sufficient)
- Ollama (optional — for local eval judging, no API key needed)

---

## Built on NanoClaw

Deus is built on **[NanoClaw](https://github.com/qwibitai/nanoclaw)** by [qwibitai](https://github.com/qwibitai) — the core framework providing container-isolated Claude agents, multi-channel messaging, and a skill system for safe customization. This repo extends NanoClaw with semantic memory, voice transcription, an evolution loop, and more.

Want to start from scratch? Fork the [NanoClaw repo](https://github.com/qwibitai/nanoclaw) and build your own setup.

---

## License

MIT
