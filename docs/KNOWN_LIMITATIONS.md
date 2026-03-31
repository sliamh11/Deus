# Known Limitations

## Claude SDK Lock-in

The core agent uses the [Claude Agent SDK](https://github.com/anthropic-ai/claude-agent-sdk) for container-side execution. This is an architectural dependency — the agent runner, session management, tool handling, and MCP integration all depend on this SDK.

**What this means:**
- The agent LLM is always Claude. It cannot be swapped for GPT-4, Gemini, or a local model.
- Container execution requires a valid Claude API key or OAuth token.
- The credential proxy is designed specifically for Anthropic's API format.

**What IS swappable:**
- **Eval/evolution judges** — can use Ollama (local, free), Gemini, or Claude
- **Embedding model** — pluggable via `EMBEDDING_PROVIDER` env var (default: Gemini). See `evolution/providers/embeddings.py` to add providers
- **Memory indexer** — uses the pluggable embedding provider
- **Transcription** — local Whisper, independent of any API

This lock-in is a deliberate trade-off: the Claude Agent SDK provides session management, tool orchestration, and MCP integration that would take significant effort to replicate with a generic LLM client.

## macOS Preference

While Linux is supported, several features work best on macOS:
- **Apple Container** — macOS-native container runtime (lighter than Docker)
- **Whisper transcription** — Metal acceleration on Apple Silicon
- **Obsidian vault sync** — assumes local filesystem access

Linux users can use Docker and skip voice transcription if Whisper Metal isn't available.
