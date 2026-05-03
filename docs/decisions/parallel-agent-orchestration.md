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

### Dynamic Effort Classification

**Amendment (Phase 5):** Background agent effort is classified by `EffortPolicy` in `app.rs`, a stateless classifier that maps prompt keywords to effort levels:

| Task Type | Keywords | Effort |
|-----------|----------|--------|
| Planning/review | review, plan, analyze, audit, design, architect | high |
| Lookup/search | find, grep, search, list, show, check, lookup | low |
| General | (everything else) | medium |

Explicit `--effort` flag always overrides the classifier. This supersedes the per-provider ownership of effort levels from `backend-strategy-trait.md` for the specific case of auto-classifying background agent effort. Backends continue to own flag encoding in `build_command`.

### Health Monitoring

**Amendment (Phase 5):** Background sessions track process health via:

- **`SessionState::Failed`**: set when `had_error` is true at session completion. Shown as red ✗ in the session picker.
- **Exit code detection**: non-zero exit codes from the backend process emit an error chunk before Done.
- **Timeout**: background agents are killed after `DEUS_AGENT_TIMEOUT_SECS` (env var, default 600s). The timeout thread uses `recv_timeout` on a cancel channel instead of `thread::sleep` — when the agent finishes (stdout EOF + stderr join), the spawn thread sends a cancel signal, and the timeout thread exits immediately. If no cancel arrives within the timeout window, the timeout thread sends a kill signal via a separate channel. This prevents zombie sleep threads from accumulating for short-lived agents.

### Session Lifecycle (Amendment — Phase 6)

**Auto-cleanup:** Completed/failed background sessions are removed from `sessions` and `session_order` immediately after posting their completion summary to the main session. If the user was viewing the removed session, `active_session` resets to `SessionId::MAIN`. No TTL or deferred GC — sessions are ephemeral by design.

**Concurrent limit:** `max_agents()` returns a configurable cap on concurrently streaming background agents. Precedence (first match wins): env `DEUS_MAX_AGENTS` → config `max_parallel_agents` → runtime `(available_parallelism() / 2).clamp(2, 8)`. Spawn is rejected at the cap with a user-facing error. The cap applies to actively-streaming agents only; once an agent completes and is GC'd, the slot is freed immediately.
