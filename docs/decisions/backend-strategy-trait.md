# Backend Strategy Trait

**Status:** Accepted
**Date:** 2026-05-02
**Scope:** `tui/src/backend/`, `AGENTS.md`, all future provider integrations

## Context

Deus supports multiple AI backends (Claude, OpenAI/Codex, potentially Ollama and others). The TUI initially hardcoded provider-specific command construction, JSONL parsing, and model lists inline. Adding a new provider required edits across dispatch logic, stream parsing, model registry, suggestion UI, and command handling — high coupling with high risk of breakage.

## Decision

All provider integrations implement a `Backend` trait (strategy pattern). Each provider is a single file that declares its models, builds its CLI command, and parses its output format. The registry auto-derives model lists, suggestions, and dispatch from the trait implementations.

```
tui/src/backend/
├── mod.rs          # Backend trait + registry + helper functions
├── claude.rs       # Anthropic (Claude) — claude -p --stream-json
├── codex.rs        # OpenAI (Codex) — codex exec --json
└── ollama.rs       # (future) — ollama run / API
```

The `Backend` trait:

```rust
pub trait Backend: Send + Sync {
    fn name(&self) -> &'static str;
    fn display_name(&self) -> &'static str;
    fn models(&self) -> &'static [ModelDef];
    fn build_command(&self, config: &RunConfig) -> Command;
    fn parse_line(&self, line: &str) -> Option<StreamChunk>;
}
```

To add a new provider:

1. Create `tui/src/backend/<provider>.rs` implementing `Backend`
2. Add `pub mod <provider>;` to `backend/mod.rs`
3. Add `Box::new(<provider>::<Provider>Backend)` to `all_backends()` in `backend/mod.rs`

No other files need changes — model registry, `/model` command, suggestions, dispatch, and stream parsing all derive from the trait.

## Alternatives Considered

- **Enum dispatch:** Match on a provider enum in app.rs. Rejected because adding a provider still requires edits in every match arm across multiple functions.
- **Config-driven providers:** TOML/JSON files declaring CLI commands and parse rules. Rejected because parsing logic varies enough (Claude's nested `message.content[]` vs Codex's flat `item.completed`) that declarative config can't express it without a DSL.
- **Direct API calls:** Skip CLIs and call provider APIs directly. Deferred to Phase 2 multi-provider — the CLI wrapper approach is simpler and inherits auth, hooks, and tool planes from the existing CLIs.

## Consequences

- Adding a provider is one file + two lines. No architectural changes needed.
- Each provider owns its own JSONL contract — no shared parser to break.
- The trait is `Send + Sync` so providers can be passed to background threads for stream processing.
- Model selection, effort levels, and continuation semantics are per-provider (each `build_command` handles its own flags). Note: background agent effort classification is centralized in `EffortPolicy` (see `parallel-agent-orchestration.md` §Dynamic Effort Classification). Backends continue to own flag encoding.
- This convention applies repo-wide: any new provider integration (TUI, container runtime, or host process) should follow the same trait-based strategy pattern.
