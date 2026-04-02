# Changelog

All notable changes to Deus will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Windows support via Docker Desktop + NSSM/Servy service management
- Personality kickstarter in `/setup` (behavioral bundles, seed reflections)
- `/settings` channel command with per-channel `session_idle_hours`, `timeout`, `requires_trigger`
- Idle-based session reset for all channels
- Host slash command registry (`HOST_COMMAND_HANDLERS`) for extensible command dispatch
- Ko-fi as additional sponsor platform

### Fixed
- OAuth token no longer written to `.env`, preventing login loop on auto-refresh
- Host slash commands (`/settings`, `/compact`) intercepted before container dispatch
- 8 critical flaws in reflexion loop
- Cross-platform path handling in tests

### Changed
- Tool filtering excludes swarm tools for non-orchestration queries (~600 token savings)
- Orchestration logic extracted to `message-orchestrator.ts` and `router-state.ts`
- Container mount logic extracted to `container-mounter.ts`

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
- Docker container runtime (cross-platform, default runtime)

---

*Entries before v1.2.19 are from the upstream NanoClaw project and preserved for historical reference.*
