# Parallel Agent Orchestration (TUI Sidechains)

**Status:** Accepted  
**Date:** 2026-05-03  
**Scope:** `tui/src/app.rs`, `tui/src/backend/`, `tui/src/panels/`, `tui/src/ui.rs`, `tui/src/main.rs`

## Context

The TUI currently supports a single chat session. Users need to spawn background agents, monitor them, and interact with any running session — parallel orchestration for research, code review, and multi-task workflows.

## Decision

### Design Pattern: Strategy + Mediator

- **Strategy:** existing `Backend` trait (`Box<dyn Backend>`). Each sidechain picks up its backend via `backend_for(model_id)`. The orchestrator never touches concrete types. Future backends get sidechain support for free.
- **Mediator:** `App` as session manager with `HashMap<SessionId, Session>`. Routes all lifecycle events through a unified channel protocol (`mpsc::sync_channel` per session). The event loop polls one interface.

### Key Rulings

1. **Sessions are backend-scoped 1:1.** A `SessionId` maps to exactly one `Box<dyn Backend>`. Never resume a Claude session on Codex (per `backend-neutral-agent-runtime.md`).
2. **Explicit spawn first.** `/agent <prompt>` is the primary spawn mechanism (Phase 3+4). Hint-driven spawn (SubagentStart → "Ctrl+B to detach") requires process re-parenting — transferring an in-flight stream_rx to a new Session — and is deferred to Phase 5. Full auto-spawn (orchestrator decides without AI signal) is also deferred.
3. **`std::sync::mpsc::sync_channel(256)`** — bounded backpressure, no external crate needed. O(N) `try_recv` per 50ms tick for N sessions.
4. **`HashMap<SessionId, Session>`** for O(1) lookup by ID. `Vec<SessionId>` for insertion-ordered picker display.
5. **Cross-platform kill** via `std::process::Child::kill()`, not `libc::kill`.
6. **Token efficiency:** `--bare` + `--no-session-persistence` for sidechains, `--resume <id>` for multi-turn context management, bounded transcript buffers.

### Cross-Backend Session Asymmetry

Session management capabilities differ between backends:

| Capability | Claude Code | Codex CLI |
|-----------|-------------|-----------|
| Pin session ID | `--session-id <uuid>` | Not available — sessions created implicitly |
| Resume session | `--resume <id>` | `codex exec resume <id> <prompt>` (subcommand) |
| Ephemeral mode | `--bare` + `--no-session-persistence` | `--ephemeral` |
| Continuation | `--continue` | Not available in `exec` mode |

The `RunMode` enum abstracts this: each backend maps the enum variant to its own CLI contract. Codex silently ignores `Normal { session_id: Some(_) }` since it has no equivalent flag — callers should not rely on session ID pinning for Codex backends.

### Implementation Phases

| Phase | PR | Scope |
|-------|----|-------|
| 1 | Session extraction | Refactor `App` fields into `Session` struct, zero behavior change |
| 2 | Backend session mgmt | `RunMode` enum (`Normal`/`Resume`/`Ephemeral`) in `RunConfig` + both backends |
| 3+4 | Spawn + picker UI | `/agent` command, multi-session polling, Ctrl+B session picker, status bar indicator |
| 5 | In Progress | Bounded transcripts, dynamic effort, completion summaries, hint-driven spawn (discoverability only — see §Phase 5 Scoping below) |

### Phase 5 Scoping

The original Phase 5 spec included "process re-parenting" for hint-driven spawn: when the AI spawns a subagent via the `Agent` tool, detach the in-flight `stream_rx` into a new background `Session`. This is architecturally infeasible — the subagent runs as a tool call *within* the parent CLI process, sharing the same stdout stream. There is no separate process to re-parent; splitting one `stream_rx` between two Sessions would require protocol-level changes to the backend CLIs.

**Revised scope:** Phase 5d provides *discoverability* instead of re-parenting. When `SubagentStart` appears in the stream, the status bar shows a hint ("Ctrl+B: parallel agent"). Pressing Ctrl+B prefills the input with `/agent <description>`, letting the user spawn a new independent agent with the same task. This delivers the UX value (awareness + one-key spawn) without the architectural complexity.

Full process re-parenting is deferred indefinitely — it would require backend CLI support for mid-stream session splitting, which neither Claude Code nor Codex CLI offers.
