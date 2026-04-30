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

1. **Understands you** - It doesn't just store memories - it breaks conversations into facts, indexes by meaning, and builds a model of what you care about. Ask about something from three weeks ago and it recalls the details, even if you don't remember what you called it. (95% recall on the [LongMemEval](https://arxiv.org/abs/2410.10813) benchmark.)

2. **Adapts to how you think** - Scores its own responses, generates self-critiques, and rewrites its system prompt based on what worked. Tone, judgment, the kind of suggestions it surfaces - all of it improves at the personality level.

3. **Picks up where you left off** - Context carries over between sessions. Start a project Monday, come back Thursday, and it knows where you left off.

4. **Lives where you already are** - WhatsApp, Telegram, Slack, Discord, Gmail. Add only the ones you need. Your memory follows you across all of them.

5. **Private by default** - Runs on your machine in isolated containers. No cloud sync, no tracking, no data leaving your computer.

6. **Works on your code too** - Run `deus` in any project directory for a coding assistant that already knows your preferences and past work.

<details>
<summary>And more</summary>

- **Voice** - Send a voice message and it transcribes and responds. Runs locally on Apple Silicon.
- **Vision** - Send a photo or screenshot and it sees and responds to it.
- **Calendar** - Reads and manages your Google Calendar. Ask what's coming up, or tell it to book something.
- **Scheduled tasks** - Daily summaries, weekly recaps, reminders - set it and forget it.
- **Web & video** - Summarize YouTube videos, fetch web pages, or research a topic, all from a chat message.

</details>

---

## Quick Start

### What you need

- macOS (Apple Silicon recommended), Linux, or Windows
- [Claude Code](https://claude.ai/download) or [Codex CLI](https://github.com/openai/codex) installed and authenticated
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (handles WSL 2 on Windows automatically)
- Node.js 20+, Python 3.11+
- A [Gemini API key](https://aistudio.google.com/apikey) (free tier is enough)
- [Ollama](https://ollama.ai/download) for local embeddings and scoring (not an agent backend) - `/setup` pulls the right models automatically based on your hardware

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
| `deus codex` | Use OpenAI/Codex backend for this session |
| `deus auth` | Rebuild and restart background services |
| `deus gcal` | Google Calendar token management (`status`, `auth`, `ping`) |
| `deus listen` | Record from mic, transcribe locally, copy to clipboard |

For direct Codex CLI sessions outside the `deus` launcher, register Deus memory
recall as an MCP tool through the repo launcher:

```bash
codex mcp add deus-memory -- /path/to/deus/scripts/deus-memory-mcp
```

To mirror the repo's Warden gates in direct Codex CLI sessions, install the
local Codex hooks:

```bash
python3 scripts/codex_warden_hooks.py install --dry-run
python3 scripts/codex_warden_hooks.py install
python3 scripts/codex_warden_hooks.py check
```

---

## Comparison

|  | **Deus** | **[OpenClaw](https://github.com/openclaw/openclaw)** | **[NemoClaw](https://github.com/NVIDIA/NemoClaw)** | **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** | **Plain Claude** |
|---|---|---|---|---|---|
| **Memory** | Understands you - indexes facts by meaning, recalls in context | Markdown files | Via OpenClaw | Full-text search + preference profiling | Conversation only |
| **Learning** | Adapts at the personality level - tone, judgment, suggestions | No | No | Auto-creates & refines skills | No |
| **Channels** | 5 (WhatsApp, Telegram, Slack, Discord, Gmail) | 10+ | Via OpenClaw | 15+ (WhatsApp, Telegram, Signal, Matrix...) | None |
| **Isolation** | Container per conversation | Opt-in Docker | Landlock + seccomp | Per-session | None |
| **LLM support** | Claude default, OpenAI opt-in | Any provider | Any (via OpenClaw) | Any (10+ providers) | Claude only |
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
| Security model | [Security](docs/SECURITY.md) |
| Benchmarks & token costs | [Benchmarks](docs/benchmarks.md) |
| Environment variables | [Environment](docs/ENVIRONMENT.md) |
| Using different AI backends | [Multi-backend](docs/MULTI_BACKEND.md) |
| Backend quality benchmark | [Claude vs Codex parity report](docs/research/backend-quality-benchmark-2026-04-26.md) |
| Development setup | [Development](docs/DEVELOPMENT.md) |
| Contributing | [Contributing](CONTRIBUTING.md) |
| Known limitations | [Limitations](docs/KNOWN_LIMITATIONS.md) |

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

## License

MIT
