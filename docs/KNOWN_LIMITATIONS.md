# Known Limitations

## Backend Parity Is In Progress

Deus now has a backend-neutral host runtime with per-group and per-task backend selection, but Claude remains the compatibility baseline and default backend. The new OpenAI backend is opt-in and still chasing full parity with the long-established Claude path.

**What this means today:**
- Claude is still the most battle-tested backend for container execution, sessions, and tool orchestration.
- OpenAI support depends on `OPENAI_API_KEY` plus `deus codex`, `DEUS_AGENT_BACKEND=openai`, or an explicit group/task override.
- The global `deus` launcher can use Codex via `deus codex`, `deus openai`, or `DEUS_AGENT_BACKEND=openai`; Claude Code remains the default CLI experience, and `deus claude` explicitly forces the Claude CLI/backend pair for one invocation.
- The credential proxy now supports both Anthropic and OpenAI routes, but other providers still need adapters.
- OpenAI now uses the container-side Deus tool broker for filesystem, shell, web, browser, IPC, and task tools, plus bridged MCP tools for `deus`/`gcal` parity and Deus-owned compacted-session metadata for `/compact`.
- OpenAI still needs live container verification with real credentials before it can be called parity-certified (tracked as AAG-001). The OpenAI adapter uses the Responses API; Agents SDK handoffs/tracing are deferred as optional accelerators.
- Skill parity audit (2026-04-26): all skills except `x-integration` (broken on both backends) and `add-ollama-tool` (OpenAI backend hardcodes MCP server list) are instruction-only and work on both backends.
- Container-side backend changes require rebuilding/restarting the agent container/service before live testing.

**What IS swappable:**
- **Container agent backend** — `claude` or `openai`, selected globally or overridden per group/task
- **Eval/evolution judges** — can use Ollama (local, free), Gemini, or Claude
- **Embedding model** — pluggable via `EMBEDDING_PROVIDER` env var (default: Gemini). See `evolution/providers/embeddings.py` to add providers
- **Memory indexer** — uses the pluggable embedding provider
- **Transcription** — local Whisper, independent of any API

**CLAUDE.md compatibility:** `CLAUDE.md` is permanently maintained as a thin redirect to `AGENTS.md`. It is required by the Claude Code CLI convention (`CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1`) and will not be removed.

The long-term goal is full tool/session parity across adapters. Until then, treat Claude as the stable path and OpenAI as the first backend-neutral implementation target.

Tracked open-ended follow-up work lives in
[agent-agnostic-debt.md](agent-agnostic-debt.md).

## macOS Preference

While Linux is supported, several features work best on macOS:
- **Whisper transcription** — Metal acceleration on Apple Silicon
- **Vault sync** — assumes local filesystem access

Linux users can use Docker and skip voice transcription if Whisper Metal isn't available.
