# ADR: Startup validation gate

**Status:** Accepted
**Date:** 2026-03-30
**Scope:** `src/startup-gate.ts`, `src/checks.ts`

## Context

A new user who clones Deus and runs `npm start` without completing setup gets a cryptic "No channels connected" fatal exit. For public release, first-run UX must be clear and actionable.

Additionally, the memory system — not messaging channels — is the core differentiator. The startup gate should prioritize memory system readiness over channel configuration.

## Decision

A startup gate (`src/startup-gate.ts`) validates prerequisites before heavy initialization. It uses a **check registry pattern** (mirrors `src/channels/registry.ts`) so new checks can be added via `registerStartupCheck()` without modifying the gate.

### Three severity levels

- **fatal** — blocks startup. Only for hard requirements: API credentials.
- **warn** — allows startup, prints warning. For the memory system (vault, Python deps).
- **suggest** — one-line hint. For optional features (Gemini key, channels, groups).

### Key choices

**Channels are optional, not fatal.** The old `process.exit(1)` on zero channels was removed. Deus can run as a local Claude Code assistant without messaging channels.

**Memory vault path is configurable.** Reads from `~/.config/deus/config.json` → `vault_path`, with `DEUS_VAULT_PATH` env var override. Hardcoded paths in `memory_indexer.py` were replaced.

**Container runtime check stays separate.** `ensureContainerRuntimeRunning()` already has its own error box and throw behavior. The startup gate runs after it, not as a replacement.

**Registered groups are a suggestion.** Groups can be registered at runtime via IPC during the setup flow, so their absence shouldn't block or warn.

## Consequences

- New users see a formatted checklist of what's missing and what to run.
- The app starts in a degraded but functional state when optional components are missing.
- Adding new prerequisite checks requires only a `registerStartupCheck()` call.
