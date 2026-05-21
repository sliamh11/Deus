# Environment Variables

All variables are set in `.env` at the project root. Copy `.env.example` to get started.

## Required

| Variable | Description |
|----------|-------------|
| `CLAUDE_CODE_OAUTH_TOKEN` | OAuth token for Claude Code (or use `ANTHROPIC_API_KEY`) |
| `ANTHROPIC_API_KEY` | Alternative to OAuth token for Claude auth |
| `TZ` | Timezone override (e.g. `Asia/Jerusalem`) |

## Channels

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token from @BotFather |
| `ASSISTANT_NAME` | `Deus` | Display name used in Telegram and logs |
| `ASSISTANT_HAS_OWN_NUMBER` | `false` | Whether the assistant has its own WhatsApp number |
| `SLACK_BOT_TOKEN` | — | Slack bot token |
| `SLACK_APP_TOKEN` | — | Slack app-level token |
| `DISCORD_BOT_TOKEN` | — | Discord bot token |

## AI / API Keys

| Variable | Default | Description |
|----------|---------|-------------|
| `DEUS_AGENT_BACKEND` | `claude` | Default container agent backend: `claude` or `openai` |
| `DEUS_AGENT_EFFORT` | `low` | Default agent reasoning effort: `low`, `medium`, `high`, or `max`. Per-group override via `/settings effort=X`. |
| `DEUS_CLI_AGENT` | `DEUS_AGENT_BACKEND` | Default `deus` global command agent: `claude`, `codex`, or `openai` |
| `DEUS_OPENAI_MODEL` | `gpt-4o` | Default OpenAI model for the `openai` agent backend |
| `DEUS_CODEX_MODEL` | `DEUS_OPENAI_MODEL` | Optional Codex CLI model override for the `deus codex` launcher |
| `DEUS_CONTEXT_FILE_MAX_CHARS` | `20000` | Per-file cap for registered agent context surfaces before provider tokenization |
| `OPENAI_API_KEY` | — | OpenAI API key for the `openai` agent backend and Whisper transcription |
| `OPENAI_BASE_URL` | `https://api.openai.com` | Optional OpenAI-compatible upstream base URL for the credential proxy |
| `GEMINI_API_KEY` | — | Gemini API key for embeddings, memory indexer, and production judge |

## Voice Transcription

| Variable | Default | Description |
|----------|---------|-------------|
| `WHISPER_LANG` | `en` | Language code for Whisper transcription |
| `WHISPER_BIN` | `whisper-cli` | Path to whisper binary |
| `WHISPER_MODEL` | — | Whisper model path (auto-detected if empty) |

## Container Runtime

| Variable | Default | Description |
|----------|---------|-------------|
| `CONTAINER_RUNTIME` | `docker` | Container binary: `docker`, `container`, `podman` |
| `CONTAINER_IMAGE` | `deus-agent:latest` | Container image for agent sandboxes |
| `CONTAINER_TIMEOUT` | `1800000` | Container execution timeout in ms (30 min) |
| `MAX_CONCURRENT_CONTAINERS` | `5` | Max parallel agent containers |
| `IDLE_TIMEOUT` | `1800000` | Idle container shutdown timeout in ms |
| `CONTAINER_MAX_OUTPUT_SIZE` | `10485760` | Max output size per container in bytes (10 MB) |

## Credential Proxy

| Variable | Default | Description |
|----------|---------|-------------|
| `CREDENTIAL_PROXY_PORT` | `3001` | Port for the credential injection proxy |
| `CREDENTIAL_PROXY_HOST` | — | Bind address for proxy (empty = auto-detect) |
| `DEUS_AUTH_PROVIDER` | (auto-detect) | Force a specific auth provider for the credential proxy: `anthropic` or `openai` |

## Ollama / Local Models

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL |
| `OLLAMA_MODEL` | `gemma4:e4b` | Ollama judge model |
| `OLLAMA_EMBED_MODEL` | `embeddinggemma` | Ollama embedding model |
| `LLAMA_CPP_BASE_URL` | `http://localhost:8080/v1` | llama.cpp HTTP base URL (OpenAI-compatible `/v1` prefix); consumed by evolution-loop providers |
| `LLAMA_CPP_MODEL` | (empty — server default) | Catch-all model override. Empty = use whatever llama-server has loaded (single-model) OR auto-pick (router mode) |
| `LLAMA_CPP_AGENT_MODEL` | (falls back to `LLAMA_CPP_MODEL`) | Per-surface override for the agent runtime (chat surface). Used in router mode with multiple GGUFs |
| `LLAMA_CPP_GEN_MODEL` | (falls back to `LLAMA_CPP_MODEL`) | Per-surface override for the evolution-loop generative provider (Reflexion, principle extraction) |
| `LLAMA_CPP_JUDGE_MODEL` | (falls back to `LLAMA_CPP_MODEL`) | Per-surface override for the evolution-loop judge provider |
| `LLAMA_CPP_EMBED_MODEL` | (falls back to `LLAMA_CPP_MODEL`) | Per-surface override for embeddings (reserved; embedding swap is ADR-gated per Phase 4) |
| `EMBEDDING_PROVIDER` | `auto` | Embedding backend: `auto`, `gemini`, or `ollama` |

## Evolution / Eval

| Variable | Default | Description |
|----------|---------|-------------|
| `EVOLUTION_ENABLED` | `1` | Toggle evolution loop: `1` or `0` |
| `EVOLUTION_PYTHON` | `python3` | Python binary path for evolution subprocess |
| `EVOLUTION_REFLECTION_THRESHOLD` | `0.6` | Interactions scoring below this trigger corrective reflections |
| `EVOLUTION_POSITIVE_THRESHOLD` | `0.85` | Interactions scoring above this trigger positive pattern extraction |
| `EVOLUTION_JUDGE_MODEL` | `models/gemini-3.1-flash-lite` | Gemini model used for judging and principle extraction |
| `EVOLUTION_JUDGE_PROVIDER` | (auto-detect) | Force a specific judge provider: `ollama`, `gemini`, `claude`, `mock`, `llama-cpp` |
| `EVOLUTION_GEN_PROVIDER` | (auto-detect) | Force a specific generative provider: `gemini`, `ollama`, `mock`, `llama-cpp` |
| `DEUS_STORAGE_PROVIDER` | (auto-detect) | Force a specific storage provider: `sqlite` |
| `EVOLUTION_GEN_MODEL` | `models/gemini-3.1-flash-lite` | Default generative model (Gemini) |
| `EVOLUTION_MAX_REFLECTIONS` | `3` | Max reflections retrieved per agent query |
| `EVOLUTION_REFLECTION_DEDUP_L2` | `0.4` | L2 distance threshold for deduplicating similar reflections |
| `DEUS_EVAL_CONCURRENT` | — | Override eval pre-warm concurrency |
| `EVOLUTION_AUTO_OPTIMIZE_THRESHOLD` | `50` | Auto-optimize after this many new scored interactions (0 = disabled) |
| `EVOLUTION_PRINCIPLES_COOLDOWN_HOURS` | `24` | Cooldown between principle extractions in hours |
| `DEUS_EVOLUTION_DB` | `~/.deus/evolution.db` | Path to the evolution SQLite database (interactions, reflections, scores) |
| `EVOLUTION_SKIP_GROUPS` | — | Comma-separated group folders to exclude from evolution tracking (e.g. automated agents) |

## Eval

| Variable | Default | Description |
|----------|---------|-------------|
| `DEUS_EVAL_IMAGE` | — | Docker image for eval containers |
| `DEUS_EVAL_TIMEOUT` | — | Eval container timeout in seconds |
| `EVAL_JUDGE` | auto-detect | Judge backend: `ollama`, `gemini`, or `mock` |
| `CREDENTIAL_PROXY_URL` | `http://localhost:3001` | Full proxy URL override |

## DSPy Optimizer

| Variable | Default | Description |
|----------|---------|-------------|
| `EVOLUTION_DSPY_MIN_SAMPLES` | `20` | Minimum scored interactions before optimizer can run |
| `EVOLUTION_DSPY_MIN_DOMAIN_SAMPLES` | `10` | Minimum domain-specific samples for domain optimization |
| `DSPY_OLLAMA_MODEL` | `gemma4:e4b` | Ollama model for DSPy optimization |

## Memory

| Variable | Default | Description |
|----------|---------|-------------|
| `DEUS_VAULT_PATH` | — | Vault directory path for session logs and memory |
| `DEUS_DB` | `~/.deus/memory.db` | Memory indexer SQLite database override |
| `DEUS_MEMORY_TREE` | `0` | Enable memory-tree hooks/context loading when set to `1` |
| `DEUS_MEMORY_TREE_DB` | `~/.deus/memory_tree.db` | Memory-tree SQLite database override |
| `DEUS_AUTO_MEMORY_DIR` | — | Optional external auto-memory directory indexed under `auto-memory/` |
| `DEUS_TREE_LOG` | `~/.deus/memory_tree_queries.jsonl` | Memory-tree query telemetry log |
| `DEUS_TREE_AUDIT` | `~/.deus/memory_tree_audit.jsonl` | Memory-tree audit log |
| `DEUS_TREE_LOW` | `0.55` | Initial low-confidence threshold for memory-tree retrieval |
| `DEUS_TREE_ABSTAIN` | `0.30` | Initial abstention threshold for memory-tree retrieval |
| `DEUS_TREE_GAP` | `0.04` | Score-gap threshold for memory-tree abstention |

## Sessions

| Variable | Default | Description |
|----------|---------|-------------|
| `SESSION_IDLE_RESET_HOURS` | `8` | Reset a group's session after N idle hours (0 = never reset). Per-channel override via `/settings session_idle_hours=N`. |

Group/task backend overrides:

- Registered groups can set `containerConfig.agentBackend` to pin a group to `claude` or `openai`.
- Scheduled tasks can set `agent_backend` to override the group/default backend for that task only.
- Resolution order is: task override, group override, `DEUS_AGENT_BACKEND`, then `claude`.

Group/task effort overrides:

- Registered groups can set `containerConfig.agentEffort` to pin a group to a specific effort level.
- Scheduled tasks can set `agent_effort` to override the group/default effort for that task only.
- Resolution order is: task override, group override, `DEUS_AGENT_EFFORT`, then `low`.
- Effort currently applies to the Claude backend only. OpenAI backend logs and ignores the value.

## Safety

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_MESSAGE_LENGTH` | `50000` | Max characters per incoming message (truncates, doesn't reject) |

## Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LEVEL` | `info` | Logging level: `debug`, `info`, `warn`, `error` |
