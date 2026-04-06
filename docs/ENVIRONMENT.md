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
| `OPENAI_API_KEY` | — | OpenAI API key (used for Whisper transcription) |
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

## Ollama / Local Models

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL |
| `OLLAMA_MODEL` | `gemma4:e4b` | Ollama judge model |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Ollama embedding model |
| `EMBEDDING_PROVIDER` | `auto` | Embedding backend: `auto`, `gemini`, or `ollama` |

## Evolution / Eval

| Variable | Default | Description |
|----------|---------|-------------|
| `EVOLUTION_ENABLED` | `1` | Toggle evolution loop: `1` or `0` |
| `EVOLUTION_PYTHON` | `python3` | Python binary path for evolution subprocess |
| `EVOLUTION_REFLECTION_THRESHOLD` | `0.6` | Interactions scoring below this trigger corrective reflections |
| `EVOLUTION_POSITIVE_THRESHOLD` | `0.85` | Interactions scoring above this trigger positive pattern extraction |
| `EVOLUTION_JUDGE_MODEL` | `models/gemini-3.1-flash-lite-preview` | Gemini model used for judging and principle extraction |
| `EVOLUTION_JUDGE_PROVIDER` | (auto-detect) | Force a specific judge provider: `ollama`, `gemini`, `claude`, `mock` |
| `EVOLUTION_MAX_REFLECTIONS` | `3` | Max reflections retrieved per agent query |
| `EVOLUTION_REFLECTION_DEDUP_L2` | `0.4` | L2 distance threshold for deduplicating similar reflections |
| `DEUS_EVAL_CONCURRENT` | — | Override eval pre-warm concurrency |
| `EVOLUTION_AUTO_OPTIMIZE_THRESHOLD` | `50` | Auto-optimize after this many new scored interactions (0 = disabled) |
| `EVOLUTION_PRINCIPLES_COOLDOWN_HOURS` | `24` | Cooldown between principle extractions in hours |
| `DEUS_DB` | `~/.deus/memory.db` | Path to the SQLite database for interactions, reflections, and embeddings |

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
| `EVOLUTION_DSPY_MAX_BOOTSTRAPPED` | `4` | Max bootstrapped demos for DSPy |
| `EVOLUTION_DSPY_MAX_LABELED` | `8` | Max labeled demos for DSPy |
| `DSPY_OLLAMA_MODEL` | `gemma4:e4b` | Ollama model for DSPy optimization |

## Memory

| Variable | Default | Description |
|----------|---------|-------------|
| `DEUS_VAULT_PATH` | — | Vault directory path for session logs and memory |

## Sessions

| Variable | Default | Description |
|----------|---------|-------------|
| `SESSION_IDLE_RESET_HOURS` | `8` | Reset a group's session after N idle hours (0 = never reset). Per-channel override via `/settings session_idle_hours=N`. |

## Safety

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_MESSAGE_LENGTH` | `50000` | Max characters per incoming message (truncates, doesn't reject) |

## Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LEVEL` | `info` | Logging level: `debug`, `info`, `warn`, `error` |
