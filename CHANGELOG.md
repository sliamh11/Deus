# Changelog

All notable changes to Deus will be documented in this file.

## [Unreleased]

## [1.2.19] - 2026-03-30

### Added
- Semantic memory system with sqlite-vec and Gemini embeddings (tiered retrieval)
- Evolution loop: interaction scoring, reflexion, DSPy optimization
- Eval layer with DeepEval test suite for containerized agents
- Voice transcription via local Whisper on Apple Silicon
- Image vision support (multimodal content in containers)
- Google Calendar integration (MCP server)
- Telegram channel support
- Task scheduler (cron/interval scheduled prompts)
- IPC system for cross-group container communication
- Session checkpoint system (auto-save on session end)
- Startup validation gate (checks prerequisites before launch)
- Credential proxy (injects API keys at runtime, never in container env)
- Mount security (allowlist-based volume mount validation)
- Dynamic concurrency (machine-adaptive worker counts)

### Changed
- Configurable container runtime (Docker, Apple Container, Podman)

---

*Entries before v1.2.19 are from the upstream NanoClaw project and preserved for historical reference.*
