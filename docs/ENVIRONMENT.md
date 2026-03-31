# Environment Variables

All variables are set in `.env` at the project root. Copy `.env.example` to get started.

## Required

| Variable | Description |
|----------|-------------|
| `CLAUDE_CODE_OAUTH_TOKEN` | OAuth token for Claude Code (or use `ANTHROPIC_API_KEY`) |

## Channels

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token from @BotFather |
| `ASSISTANT_NAME` | `Deus` | Display name used in Telegram and logs |
| `ASSISTANT_HAS_OWN_NUMBER` | `false` | Whether the assistant has its own WhatsApp number |

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

## Evolution / Eval

| Variable | Default | Description |
|----------|---------|-------------|
| `EVAL_JUDGE` | auto-detect | Force judge backend: `ollama` or `gemini`. Auto-detects Ollama at localhost:11434 |
| `EVOLUTION_REFLECTION_THRESHOLD` | `0.6` | Interactions scoring below this trigger corrective reflections |
| `EVOLUTION_POSITIVE_THRESHOLD` | `0.85` | Interactions scoring above this trigger positive pattern extraction |
| `EVOLUTION_JUDGE_MODEL` | `models/gemini-3.1-flash-lite-preview` | Gemini model used for judging and principle extraction |
| `EVOLUTION_MAX_REFLECTIONS` | `3` | Max reflections retrieved per agent query |
| `EVOLUTION_REFLECTION_DEDUP_L2` | `0.4` | L2 distance threshold for deduplicating similar reflections |
| `DEUS_EVAL_CONCURRENT` | — | Override eval pre-warm concurrency |
| `DEUS_DB` | `~/.deus/memory.db` | Path to the SQLite database for interactions, reflections, and embeddings |

## DSPy Optimizer

| Variable | Default | Description |
|----------|---------|-------------|
| `EVOLUTION_DSPY_MIN_SAMPLES` | `20` | Minimum scored interactions before optimizer can run |
| `EVOLUTION_DSPY_MIN_DOMAIN_SAMPLES` | `10` | Minimum domain-specific samples for domain optimization |
| `EVOLUTION_DSPY_MAX_BOOTSTRAPPED` | `4` | Max bootstrapped demos for DSPy |
| `EVOLUTION_DSPY_MAX_LABELED` | `8` | Max labeled demos for DSPy |
| `EVOLUTION_DSPY_NUM_CANDIDATES` | `4` | Number of candidate prompts DSPy evaluates |

## Memory

| Variable | Default | Description |
|----------|---------|-------------|
| `DEUS_VAULT_PATH` | — | Obsidian vault path for session logs and memory |

## Safety

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_MESSAGE_LENGTH` | `50000` | Max characters per incoming message (truncates, doesn't reject) |

## Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LEVEL` | `info` | Logging level: `debug`, `info`, `warn`, `error` |
