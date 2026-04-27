# Benchmark Comparison

How Deus compares to other open-source AI assistant frameworks. Last updated: April 2026.

## Philosophy

Different tools solve different problems. **OpenClaw** optimizes for breadth: 10+ channels, 5,700+ community skills, any LLM provider. **Hermes Agent** optimizes for autonomy: self-creating skills, 15+ channels, and the widest LLM provider list. **Deus** optimizes for depth: memory that actually recalls last week's conversation, a learning loop that scores itself and gets better over time, and per-conversation isolation that's on by default.

Choose the right tool for what you care about.

---

## Feature Comparison

|  | **Deus** | **[OpenClaw](https://github.com/openclaw/openclaw)** | **[NemoClaw](https://github.com/NVIDIA/NemoClaw)** | **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** | **Plain Claude** |
|---|---|---|---|---|---|
| **Messaging channels** | 5 (WhatsApp, Telegram, Slack, Discord, Gmail) | 10+ (WhatsApp, Telegram, Slack, Discord, Signal, iMessage, Teams, IRC...) | Via OpenClaw | 15+ (WhatsApp, Telegram, Signal, Matrix...) | None |
| **Agent isolation** | Each conversation runs in its own container | Opt-in Docker sandbox | Kernel-level sandboxing | Per-session | None |
| **Memory** | Remembers past conversations and learns preferences over time | Markdown files on disk | Via OpenClaw | Full-text search + preference profiling | Current conversation only |
| **Learning** | Scores itself, fixes mistakes, improves over time | No | No | Auto-creates & refines skills | No |
| **Testing** | Automated test suite for accuracy, tool use, and safety | No | No | No | No |
| **Credential security** | API keys never enter the container - injected at runtime by a proxy | Keys in environment | Policy-controlled | Keys in environment | N/A (cloud) |
| **LLM support** | Claude default; [OpenAI/Codex opt-in](decisions/backend-neutral-agent-runtime.md) | Any (OpenAI, Anthropic, Ollama, local) | Any (via OpenClaw) | Any (10+ providers) | Claude only |
| **Codebase size** | ~37K lines (TypeScript + Python) | ~430,000 lines | Wrapper over OpenClaw | ~147 MB repo | N/A |
| **Community** | New project | 250K+ GitHub stars, 5,700+ skills | NVIDIA-backed, alpha | ~110K GitHub stars | N/A |
| **License** | MIT | MIT | Open source | MIT | Proprietary |
| **Self-hosted** | Yes (macOS, Linux, Windows) | Yes (any OS) | Yes (NVIDIA GPU preferred) | Yes (Linux, macOS, WSL2) | No (cloud only) |

---

## Deep Dives

### Agent Isolation

Most frameworks treat sandboxing as optional. Deus makes it the default - every conversation group gets its own Linux container with:

- **Non-root execution** - the agent can't escalate privileges
- **Read-only code** - the agent can't modify the host codebase
- **Key injection** - real API keys never enter the container; a proxy on the host adds them to requests on the fly
- **Mount allowlist** - the agent can only see directories you explicitly allow
- **Group isolation** - conversations can't see each other's history

OpenClaw runs agents directly on your machine by default and offers Docker as an option. NemoClaw adds kernel-level sandboxing (stronger at the system-call level, but one sandbox for the whole agent - not per-conversation).

For the full security model, see [Security](SECURITY.md).

### Memory

Ask about something from weeks ago - Deus finds it, even if you used different words at the time.

**How it works:** Three ways to find past conversations, used together:

1. **Recent history** - The last few sessions are loaded by date. No search needed, no cost.
2. **Search across everything** - When recent history isn't enough, Deus searches all past sessions by meaning (not just keywords). One API call.
3. **Topic navigation** - A tree of topics (`MEMORY_TREE.md`) lets Deus walk from broad categories to specific facts, and jump between related topics. This is what handles "what did we decide about X?" on a fresh session with no prior context.

On top of that, Deus breaks conversations into individual facts - "we chose library X because of Y", "the deadline is March 5th" - so it can find specific decisions, not just conversation chunks. It also detects when new facts contradict old ones.

| System | How it stores | How it searches | Remembers across sessions? |
|--------|--------------|----------------|---------------------------|
| **Deus** | Database with meaning-based indexing + individual facts | Meaning-based search + topic tree + recency boost | Yes - recent history (free), full search, and topic navigation |
| **OpenClaw** | Markdown files on disk | Keyword / filename | Basic - no meaning-based search |
| **NemoClaw** | Via OpenClaw | Via OpenClaw | Via OpenClaw |
| **Hermes Agent** | Database with full-text search | Full-text search + preference profiling | Yes - session context, full-text search, user profiling |
| **Plain Claude** | Conversation window | None | No |

Deus never deletes memory data - old facts are soft-deleted, never dropped ([why](decisions/no-db-deletion.md)). For the full technical architecture (vector dimensions, recency decay formulas, embedding models), see [Architecture - Memory](ARCHITECTURE.md#memory-system) and the [memory tree ADR](decisions/memory-tree.md).

### Self-Improvement Loop

Every response Deus gives gets scored by a separate AI model acting as a judge:

```
You ask something
        |
        v
  Deus responds
        |
        v
  A separate AI scores the response (0.0 - 1.0)
        |
        v
  Score below 0.6?
  |-- Yes --> Deus writes a self-critique: what went wrong, how
  |           to do better. Stored for future reference.
  |-- No  --> Logged and moves on
        |
        v
  After 20+ scored responses:
  |-- The system prompt is automatically rewritten
  |   using the best-scoring responses as examples.
  |-- This happens in the background. You don't notice.
```

The result: the longer you use Deus, the better it gets at your specific use cases. Low-scoring responses generate lessons that improve future answers. Once enough data accumulates, the system prompt rewrites itself.

Hermes Agent takes a different approach - it auto-creates reusable skills from tasks it completes. Deus scores responses externally and improves its core instructions rather than building skills.

For implementation details, see [Architecture - Evolution](ARCHITECTURE.md#evolution-loop).

### Testing

Deus includes an automated test suite that validates the agent's behavior before changes are merged:

- **Accuracy tests** - does it answer factual questions correctly?
- **Tool use tests** - does it pick the right tool and pass the right parameters?
- **Safety tests** - does it refuse harmful requests and resist prompt injection?

The judge model can run locally (free, via Ollama) or in the cloud (via Gemini).

No other framework in this comparison ships with built-in behavioral testing for the agent itself.

---

## Token Efficiency

Deus uses a three-layer memory architecture that keeps per-turn overhead near zero:

1. **Rules** (~675 tokens, always loaded) - guardrails and behavioral rules via `.claude/rules/`
2. **Hook retrieval** (0-1000 tokens, on match only) - semantic search retrieves relevant memories per prompt. ~63% of turns cost zero (measured over 357 prompts from hook telemetry).
3. **Background** (0 tokens) - isolation, self-improvement, testing all run off the critical path

`autoMemoryEnabled` is set to `false` in project settings. Memory files stay on disk and are retrieved on demand by the hook using local Ollama embeddings. No flat index is loaded per turn.

| Feature | What it does | Token cost |
|---------|-------------|-----------|
| Rules | Guardrails and behavioral constraints | ~675 always loaded |
| Memory recall | Retrieves relevant past context via semantic search | 0-1000 per turn, on match only |
| Identity | Knows your name, tone, preferences per channel | Part of vault files at session start |
| Self-improvement | Scores responses, generates lessons, rewrites prompts | **0** - runs in the background |
| Container isolation | Separate environment per conversation | **0** - runs on the host |
| Testing | Accuracy, tool use, and safety validation | **0** - runs in CI only |
| Skills | `/compress`, `/resume`, and other commands | **0** - loaded on demand |

For detailed token accounting, see [Architecture - Memory System](ARCHITECTURE.md#memory-system).

---

## Scope

- Deus focuses on depth: memory, self-improvement, and security - not channel breadth or ecosystem size.
- Deus supports Claude (default) and OpenAI/Codex (opt-in). Arbitrary provider support is not a goal.
- Quantitative latency/throughput comparisons are not included because they depend on hardware, network, and API tier - not the framework.

---

## Contributing

Found an error in this comparison? [Open an issue](../../issues) or submit a PR. We want this to be accurate, not favorable.
