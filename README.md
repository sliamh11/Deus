<p align="center">
  <img src="assets/brand-production/readme-banner.png" alt="Deus - Open-source personal AI assistant" width="700">
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
  <a href="https://nodejs.org/"><img src="https://img.shields.io/badge/Node-%3E%3D20-green.svg" alt="Node"></a>
  <img src="https://img.shields.io/badge/Platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey.svg" alt="Platform">
</p>

A personal AI that understands you - not just recalls things you've said. It learns what you care about, how you think, and what you'll actually find useful. The longer you use it, the more it feels like it gets you. Everything runs on your computer. Your data stays yours.

---

## What it does

1. **Understands you** - It doesn't just store memories - it breaks conversations into facts, indexes by meaning, and builds a model of what you care about. Ask about something from three weeks ago and it recalls the details, even if you don't remember what you called it. Works in any language — Hebrew, Arabic, and other non-Latin scripts included. (95% recall on the [LongMemEval](https://arxiv.org/abs/2410.10813) benchmark; multilingual reranker active.)

2. **Adapts to how you think** - Scores its own responses, generates self-critiques, and rewrites its system prompt based on what worked. Tone, judgment, the kind of suggestions it surfaces - all of it improves at the personality level.

3. **Picks up where you left off** - Context carries over between sessions. Start a project Monday, come back Thursday, and it knows where you left off.

4. **Lives where you already are** - WhatsApp, Telegram, Slack, Discord, Gmail. Add only the ones you need. Your memory follows you across all of them.

5. **Private by default** - Runs on your machine in isolated containers. No cloud sync, no tracking, no data leaving your computer.

6. **Works on your code too** - Run `deus` in any project directory for a coding assistant that already knows your preferences and past work. `deus init` indexes a repo for code intelligence (codegraph + semantic search), and `deus arch` opens an interactive 3D map of its architecture.

<details>
<summary>And more</summary>

- **Web UI** - Talk to Deus from a browser. `deus web` launches a local Open WebUI front-end - no chat platform required.
- **Voice** - Send a voice message and it transcribes and responds. Runs locally on Apple Silicon.
- **Vision** - Send a photo or screenshot and it sees and responds to it.
- **Calendar** - Reads and manages your Google Calendar. Ask what's coming up, or tell it to book something.
- **Scheduled tasks** - Daily summaries, weekly recaps, reminders - set it and forget it.
- **Web & video** - Summarize YouTube videos, fetch web pages, or research a topic, all from a chat message.
- **Self-maintaining docs** - A weekly background agent scans for stale documentation and opens fix PRs automatically.
- **Reliable long sessions** - Detects infinite tool-call loops and auto-summarizes large tool outputs so long sessions stay coherent.

</details>

---

## Quick Start

### What you need

- macOS (Apple Silicon recommended), Linux, or Windows
- [Claude Code](https://claude.ai/download) or [Codex CLI](https://github.com/openai/codex) installed and authenticated
  - Codex with an API key is recommended — subscription-only auth disables hooks (see [CLI](#cli))
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (handles WSL 2 on Windows automatically)
- Node.js 20+, Python 3.11+
- A [Gemini API key](https://aistudio.google.com/apikey) (free tier is enough)
- [Ollama](https://ollama.ai/download) for local embeddings and scoring (not an agent backend) - `/setup` pulls the right models automatically based on your hardware
- **Optional:** [llama.cpp](https://github.com/ggerganov/llama.cpp) for a fully local, API-free agent backend — no per-turn cost, works offline. Run `/add-llama-cpp` to install.
- **Optional:** [free-claude-code](https://github.com/Alishahryar1/free-claude-code) proxy for using Claude Code CLI with alternative models (Ollama, llama-server, Gemini). Launch with `deus fcc` after configuring via `deus provider` and `deus model`.

### Install

```bash
git clone https://github.com/sliamh11/Deus.git
cd Deus
claude            # or: codex
```

Then inside the CLI:

```
/setup
```

Setup installs dependencies, builds the container, and walks you through configuration. At the end it offers a **Personality Kickstarter** - choose a behavioral bundle (developer, student, universal) or pick individual behaviors, and optionally give it example conversations so it's useful from day one.

### Connect a channel

A fresh install has zero channels. Add only what you need:

```
/add-whatsapp           # Scan QR code to connect WhatsApp
/add-telegram           # Paste bot token to connect Telegram
```

See [AGENTS.md](AGENTS.md#commands-and-skills) for all available skills.

### Start talking

```
@Deus what's on my calendar tomorrow?
@Deus summarize the YouTube video at <url>
@Deus remind me every Monday morning what I worked on last week
```

> **Switching from another AI?** Paste this into your current AI (ChatGPT, Gemini, etc.) and send the output to Deus in your first conversation:
>
> ```
> I'm switching to a new AI assistant called Deus. Generate a structured summary
> about me that I can give it so it knows me from day one. Include:
>
> 1. About me - name, role, location, languages
> 2. What I use AI for - main topics and tasks
> 3. Communication style - how I like responses
> 4. Preferences - things I've corrected you on
> 5. Key context - ongoing projects, goals, background
>
> Be specific and factual. Skip anything generic. Format as plain text.
> ```

---

## CLI

| Command | What it does |
|---------|-------------|
| `deus` | Launch in the current directory (project mode if outside `~/deus`) |
| `deus home` | Launch in home mode regardless of current directory |
| `deus web` | Launch the local web UI (Open WebUI) and open it in the browser |
| `deus init` / `deus onboard` | Index the current project for code intelligence (codegraph + code_search) and register it (`--seed` adds a memory note) |
| `deus arch` | Visualize a project's architecture in an interactive 3D explorer |
| `deus codex` | Use OpenAI/Codex backend for this session |
| `deus fcc` | Launch with a local proxy model (Ollama, llama-server, Gemini) |
| `deus provider <name>` | Switch proxy provider (`ollama`, `llamacpp`, `gemini`) |
| `deus model <name>` | Switch proxy model (auto-prefixes active provider) |
| `deus model dashboard` | Open proxy admin UI in browser |
| `deus auth` | Rebuild and restart background services |
| `deus gcal` | Google Calendar token management (`status`, `auth`, `ping`) |
| `deus listen` | Record from mic, transcribe locally, copy to clipboard |
| `deus tui` | Full-screen terminal UI for chat, wardens, services, and channels |
| `deus pipeline` | Live pipeline monitor (default), or one-shot audit (`PROJ-123`, `--failed`, `--active`) |
| `deus usage` | Token-efficiency + cost report across all projects (`--since`, `--project`, `--pricing`, `--json`) |
| `deus backend` | Show active agent backend (`claude`, `codex`, `llama-cpp`) |
| `deus backend set <name>` | Switch backend for all future sessions |
| `deus sync` | Update the live install to `origin/main` (`deus sync upstream` for forks) |

For direct Codex CLI sessions outside the `deus` launcher, register Deus memory
recall as an MCP tool through the repo launcher:

```bash
codex mcp add deus-memory -- /path/to/deus/scripts/deus-memory-mcp
```

To use Deus's memory and evolution layers from a code editor (Zed and other
ACP/MCP clients), see [Editor integration](docs/EDITOR_INTEGRATION.md).

To mirror the repo's Warden gates in direct Codex CLI sessions, install the
local Codex hooks:

```bash
python3 scripts/codex_warden_hooks.py install --dry-run
python3 scripts/codex_warden_hooks.py install
python3 scripts/codex_warden_hooks.py check
```

> **Codex auth modes and hooks:** Codex supports two authentication modes: API
> key (`OPENAI_API_KEY`) and subscription/OAuth (`codex login`). Warden hooks
> require an API key — subscription-only auth cannot enable the
> `[features].codex_hooks` flag, so no quality gates, memory retrieval, or
> safety checks will fire. For the full Deus experience with Codex, use an API
> key. See [Multi-backend](docs/MULTI_BACKEND.md) for setup and
> [Hook Dispatch System](docs/decisions/hook-dispatch-system.md) for the
> architectural rationale.

---

## Linear Automation

Use [Linear](https://linear.app) as a Kanban command center for autonomous agent work. Move an issue to **Ready for Agent** and Deus picks it up, implements it in a container, opens a PR, and optionally merges it -- without waiting for you.

### How it works

Issues move through five stages: **Todo → Ready for Agent → Agent Working → In Review → Done**. Three quality checks fire automatically as issues move through the board:

| Check | Fires on | What it does |
|-------|----------|--------------|
| **agent-readiness-gate** | Todo → Ready for Agent | Scopes the issue: implementation plan, acceptance criteria, effort/complexity ratings |
| **output-quality-gate** | Agent Working → In Review | Verifies the agent produced a real deliverable (PR, document, etc.) |
| **completion-gate** | In Review → Done | Checks all acceptance criteria are met and PR is merged |

When `LINEAR_AUTO_MERGE=1`, Deus automatically merges the agent's PR once CI passes.

Each issue gets a single rolling **Pipeline Log** comment that tracks every event (gate verdicts, agent dispatch, PR creation, merge) -- no comment spam.

### Under the hood: event-driven orchestrator

The pipeline is **event-driven**. Instead of each step writing directly to Linear and the event log, the orchestrator emits typed events onto an in-process **event bus**, and independent listeners react:

- one listener **acts** on Linear (e.g. moves an issue to *In Review* when an agent finishes),
- another **records** every event to the pipeline log that backs `deus pipeline` and the rolling comment.

This pub/sub seam keeps the dispatcher, webhook gates, and message path decoupled -- new behavior plugs in as a listener without touching the producers. See [Event hub architecture](docs/decisions/event-hub.md) for the bus contract, the strangler migration, and the phase roadmap.

### Setup

```
/add-linear    # Gives Deus read/write access to your Linear workspace
```

Then configure the automation layer in `.env`:

```bash
LINEAR_API_TOKEN=lin_api_...    # Linear personal API key
LINEAR_WEBHOOK_SECRET=...       # For webhook signature verification
# LINEAR_AUTO_MERGE=1           # Optional: auto-merge agent PRs after CI
```

Quality checks require a public URL to receive Linear webhook events. For local dev, use [ngrok](https://ngrok.com) (`ngrok http 3005`). Register the URL in Linear Settings → API → Webhooks. Dispatch (polling) works without a webhook URL. For an always-on self-host, pin an ngrok static domain and run the tunnel under a process manager - see [Always-on self-host](docs/decisions/linear-webhook-pipeline.md#always-on-self-host-static-domain--auto-start).

See [Linear automation architecture](docs/decisions/linear-webhook-pipeline.md) for the full setup, gate spec format, and configuration reference.

### Pipeline monitor

Run `deus pipeline` to open a live dashboard backed by a webhook-fed SQLite cache (2s refresh when cached, 10s fallback when polling API):

```bash
deus pipeline                        # Live monitor (default)
deus pipeline PROJ-123               # Full timeline for an issue
deus pipeline --failed --since 24h   # Failures in the last 24 hours
deus pipeline --active               # One-shot active view
```

### Usage report

Run `deus usage` for a per-model token-efficiency and cost report across all your
Claude Code projects (deduped by API response):

```bash
deus usage                           # Full report, all projects, all time
deus usage --since 2026-05-01        # Scope to a date window
deus usage --project myrepo          # Filter to one project (by dir substring)
deus usage --pricing none            # Efficiency ratios only, no cost column
deus usage --rates ~/rates.json      # Override per-model rates (API-direct pricing)
deus usage --json                    # Machine-readable output
```

The report breaks down **per project first** (heaviest first; worktrees fold into their
repo and ephemeral temp runs bucket together), then gives the all-projects per-model
totals. The **efficiency layer** is pricing-independent: per-model `cacheRead:output`
(context drag per generated token), `cacheRead:cacheCreation` (cache amortization), and
output-share. The **cost layer** prices tokens via a built-in per-model table (notional
for subscription users; the real bill for API-direct users) and degrades gracefully to
"—" for models it has no rate for. Absolute token totals may differ slightly from
`ccusage` (which counts duplicated subagent messages differently); the efficiency ratios
are unaffected. Direct background/proxy model calls that do not flow through Claude Code
are not yet captured (tracked separately).

### Vault sync

Linear is the source of truth for pending tasks. The vault's `CLAUDE.md` `pending:` block stays in sync automatically:

- **Webhook push**: when issues change in Linear, the webhook handler updates the vault file within ~2 seconds (debounced).
- **Session-start pull**: a `SessionStart` hook queries Linear on every new Claude Code session, ensuring freshness at the moment it matters most.
- **`/compress` sync**: the `/compress` skill pulls the full active issue list from Linear and rebuilds the pending block.

### Adding or customizing gates

Gates are plain markdown files in `.claude/agents/wardens/`. Adding a gate is one file with YAML frontmatter -- no code change:

```yaml
---
name: my-custom-gate
gate_to: "In Review"
allowed_from: ["Agent Working"]
mode: advise          # or strict (reverts on non-SHIP)
cooldown_minutes: 60
---
Your gate prompt here...
```

Select review wardens (`plan-reviewer`, `code-reviewer`, `ai-eng-warden`) can run on Claude, GPT, and GLM as a cross-model **co-gate** — each provider reviews the diff independently, and a `REVISE` or `BLOCK` from any configured backend stops the change (a backend that can't run is skipped, not a blocker). Per-warden backends are set in `.claude/wardens/config.json` (`/wardens` manages the gates themselves); see [Warden co-gate](docs/WARDEN_CO_GATE.md) for the exact pass/fail semantics and how to add a backend.

---

## Comparison

|  | **Deus** | **[OpenClaw](https://github.com/openclaw/openclaw)** | **[NemoClaw](https://github.com/NVIDIA/NemoClaw)** | **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** | **Plain Claude** |
|---|---|---|---|---|---|
| **Memory** | Understands you - indexes facts by meaning, recalls in context | Markdown files | Via OpenClaw | Full-text search + preference profiling | Conversation only |
| **Learning** | Adapts at the personality level - tone, judgment, suggestions | No | No | Auto-creates & refines skills | No |
| **Channels** | 5 (WhatsApp, Telegram, Slack, Discord, Gmail) | 10+ | Via OpenClaw | 15+ (WhatsApp, Telegram, Signal, Matrix...) | None |
| **Isolation** | Container per conversation | Opt-in Docker | Landlock + seccomp | Per-session | None |
| **LLM support** | Claude default, OpenAI/llama.cpp opt-in | Any provider | Any (via OpenClaw) | Any (10+ providers) | Claude only |
| **Setup** | ~5 min | ~15 min | ~20 min | ~10 min | N/A |
| **Repo size** | ~13 MB | ~592 MB | ~22 MB | ~147 MB | N/A |

Deus goes deep on understanding you and adapting over time. Hermes goes wide on channels and LLM flexibility. See [docs/benchmarks.md](docs/benchmarks.md) for detailed numbers.

---

## Docs

| Topic | |
|-------|-|
| How it works | [Architecture](docs/ARCHITECTURE.md) |
| Memory system | [Architecture - Memory](docs/ARCHITECTURE.md#memory-system) |
| Self-improvement loop | [Architecture - Evolution](docs/ARCHITECTURE.md#evolution-loop) |
| Developer tools (CodeGraph, code_search, etc.) | [Architecture - Developer Tools](docs/ARCHITECTURE.md#developer-tools) |
| 3D architecture explorer (`deus arch`) | [Architecture explorer](tools/architecture-explorer/README.md) |
| Security model | [Security](docs/SECURITY.md) |
| Benchmarks & token costs | [Benchmarks](docs/benchmarks.md) |
| Environment variables | [Environment](docs/ENVIRONMENT.md) |
| Using different AI backends | [Multi-backend](docs/MULTI_BACKEND.md) |
| Local backend (llama.cpp) | [Multi-backend — llama.cpp](docs/MULTI_BACKEND.md#llamacpp-local-backend) |
| Use Deus in your editor (Zed, ACP/MCP) | [Editor integration](docs/EDITOR_INTEGRATION.md) |
| Backend quality benchmark | [Claude vs Codex parity report](docs/research/backend-quality-benchmark-2026-04-26.md) |
| Cross-model review co-gate | [Warden co-gate](docs/WARDEN_CO_GATE.md) |
| Development setup | [Development](docs/DEVELOPMENT.md) |
| Contributing | [Contributing](CONTRIBUTING.md) |
| Known limitations | [Limitations](docs/KNOWN_LIMITATIONS.md) |
| Linear automation | [Setup, gates, and pipeline](docs/decisions/linear-webhook-pipeline.md) |
| Event hub architecture | [Orchestrator Event Hub](docs/decisions/event-hub.md) |
| Hook dispatch architecture | [Hook Dispatch System](docs/decisions/hook-dispatch-system.md) |

---

## Contributing

PRs welcome. Every change goes through a pull request - no direct pushes to main. See [CONTRIBUTING.md](CONTRIBUTING.md) for the full guide.

---

## Support

Built and maintained solo - no company, no funding. If Deus is useful to you, sponsoring helps keep it going.

[![GitHub Sponsors](https://img.shields.io/badge/Sponsor-%E2%9D%A4-ea4aaa?logo=github)](https://github.com/sponsors/sliamh11)
[![Ko-fi](https://img.shields.io/badge/Ko--fi-Support-ff5e5b?logo=ko-fi)](https://ko-fi.com/liamsteiner)

<!-- sponsors-start -->
<!-- sponsors-end -->

## Acknowledgments

Built on [NanoClaw](https://github.com/qwibitai/nanoclaw) - thanks to the NanoClaw team for the foundation.

Multi-model proxy support powered by [free-claude-code](https://github.com/Alishahryar1/free-claude-code) - a local reverse proxy that enables Claude Code CLI to work with alternative LLM providers.

## License

MIT

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).
