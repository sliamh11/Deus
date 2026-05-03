# TUI Permission Bridge

**Date:** 2026-05-03
**Status:** Implemented (Phase 1-3)
**Related:** [parallel-agent-orchestration.md](parallel-agent-orchestration.md), [backend-strategy-trait.md](backend-strategy-trait.md)

## Problem

The Deus TUI runs Claude Code in piped mode (`claude -p --output-format stream-json`).
In this mode, permission prompts have no interactive path — tool calls that require
approval are silently denied and reported post-hoc via `permission_denials` in the
`result` event. Users see what failed but can never approve anything.

## Architecture

### Selected: Hook-Based File IPC Bridge

```mermaid
sequenceDiagram
    participant User as TUI User
    participant TUI as Deus TUI (Rust)
    participant Claude as Claude Code (subprocess)
    participant Hook as PreToolUse Hook (bash)
    participant FS as Filesystem IPC

    User->>TUI: types prompt
    TUI->>Claude: spawn with --settings (hook config) + DEUS_TUI_PERMS_DIR env
    Claude->>TUI: stream-json: tool_use event (Bash, Write, etc.)
    Note over TUI: Shows tool call in chat

    Claude->>Hook: PreToolUse fires (stdin: tool info JSON)
    Hook->>FS: write request-<id>.json (atomic: .tmp + rename)
    Hook->>Hook: poll for response-<id>.json (0.5s intervals)

    TUI->>FS: poll() detects request file
    TUI->>User: render permission overlay [Y]es [N]o [A]lways
    User->>TUI: presses Y

    TUI->>FS: write response-<id>.json (atomic: .tmp + rename)
    Hook->>FS: read response file
    Hook->>Claude: stdout: {"permissionDecision": "allow"}
    Claude->>Claude: executes tool
    Claude->>TUI: stream-json: tool_result event
```

### Component Diagram

```mermaid
graph TB
    subgraph "TUI Process (Rust)"
        APP[App struct]
        PB[PermsBridge]
        POLL[poll_response loop<br/>50ms tick]
        UI[Permission Overlay<br/>Y/N/A keys]

        APP --> PB
        POLL -->|"poll(session_id)"| PB
        PB -->|"PermissionRequest"| APP
        UI -->|"approve/deny"| APP
        APP -->|"respond()"| PB
    end

    subgraph "Filesystem IPC"
        DIR["/tmp/deus-tui-perms-PID/SESSION_ID/"]
        REQ["request-TOOL_ID.json"]
        RESP["response-TOOL_ID.json"]
        SETTINGS["settings.json"]

        DIR --- REQ
        DIR --- RESP
        DIR --- SETTINGS
    end

    subgraph "Claude Code Subprocess"
        CC[claude -p --stream-json]
        HOOK[permission-bridge.sh<br/>PreToolUse hook]

        CC -->|"PreToolUse event"| HOOK
        HOOK -->|"permissionDecision JSON"| CC
    end

    PB -->|"write response"| RESP
    PB -->|"read request"| REQ
    HOOK -->|"write request"| REQ
    HOOK -->|"read response"| RESP
    CC -->|"--settings"| SETTINGS
    CC -.->|"DEUS_TUI_PERMS_DIR env"| HOOK
```

### Session Isolation

```mermaid
graph LR
    subgraph "PermsBridge base_dir"
        direction TB
        BASE["/tmp/deus-tui-perms-PID/"]
        SCRIPT["permission-bridge.sh"]
        S0["0/ (main session)"]
        S1["1/ (agent session)"]
        S2["2/ (agent session)"]

        BASE --- SCRIPT
        BASE --- S0
        BASE --- S1
        BASE --- S2
    end

    S0 --- S0R["request-toolu_abc.json"]
    S0 --- S0S["settings.json"]
    S1 --- S1S["settings.json"]
    S2 --- S2S["settings.json"]
```

Each session gets its own subdirectory to prevent cross-session ID collisions.
The hook script is shared (embedded via `include_str!` at compile time).

## Alternatives Considered

### Option A: Stdin/Stdout Bidirectional JSON (rejected)

```mermaid
sequenceDiagram
    participant TUI as TUI
    participant Claude as Claude Code

    TUI->>Claude: stdin: {"type": "prompt", "message": "..."}
    Claude->>TUI: stdout: {"type": "permission_request", "tool": "Bash", "input": "rm -rf"}
    TUI->>Claude: stdin: {"type": "permission_response", "decision": "deny"}
    Claude->>TUI: stdout: {"type": "tool_result", ...}
```

**Pros:**
- No filesystem involvement
- Lower latency (direct pipe)
- Cleaner architecture (single communication channel)

**Cons:**
- Claude Code does not document or support `permission_request` events in stream-json output
- `--input-format stream-json` exists but the permission response schema is undocumented
- Would require reverse-engineering an internal protocol
- Fragile across Claude Code version upgrades

**Why rejected:** The bidirectional JSON protocol is not a public API. Building on it
risks breakage with every Claude Code update.

### Option B: Named Pipe (FIFO) IPC (considered, deferred)

```mermaid
sequenceDiagram
    participant TUI as TUI
    participant FIFO_REQ as request.fifo
    participant FIFO_RESP as response.fifo
    participant Hook as Hook

    Hook->>FIFO_REQ: write tool request (blocks until read)
    TUI->>FIFO_REQ: read (unblocks writer)
    TUI->>FIFO_RESP: write decision (blocks until read)
    Hook->>FIFO_RESP: read decision (unblocks writer)
```

**Pros:**
- No polling needed (blocking I/O)
- Lower latency than file polling
- No orphan file cleanup

**Cons:**
- Harder to debug (can't `ls` and `cat` files during development)
- Blocking semantics require careful ordering (deadlock risk)
- Not available on Windows (though TUI is macOS/Linux only)
- Multiple concurrent permissions per session would need multiplexing

**Why deferred:** File polling at 0.5s is fast enough for human interaction speed.
FIFOs add complexity without meaningful UX improvement. Could revisit if latency
becomes noticeable.

### Option C: Unix Domain Socket (considered, rejected)

```mermaid
graph LR
    TUI["TUI<br/>(socket server)"] <-->|"JSON messages"| SOCK["/tmp/deus-tui.sock"]
    SOCK <-->|"JSON messages"| HOOK["Hook<br/>(socket client)"]
```

**Pros:**
- Full duplex, low latency
- Supports multiplexed concurrent requests
- Clean shutdown semantics

**Cons:**
- Significantly more code (socket server in Rust, client in bash)
- Shell hooks can't easily do socket I/O (need netcat or Python)
- Overkill for the request/response pattern at human-interaction speed

**Why rejected:** The shell hook script needs to be simple and portable.
Socket I/O from bash requires external tools and adds fragility.

### Option D: Global Config Mutation (rejected)

Instead of `--settings`, modify `~/.claude/settings.json` before each launch.

**Pros:**
- No `--settings` flag needed

**Cons:**
- Affects every Claude Code session on the host
- Race condition with concurrent sessions
- Requires cleanup on crash
- Violates the "no global state mutation" principle

**Why rejected:** Per-invocation `--settings` is strictly better.

## Comparison Matrix

| Criterion | File IPC (chosen) | Bidirectional JSON | FIFO | Unix Socket | Global Config |
|---|---|---|---|---|---|
| Complexity | Low | Unknown | Medium | High | Low |
| Debuggability | Excellent | Poor | Poor | Medium | Medium |
| Latency | ~500ms | ~0ms | ~0ms | ~0ms | N/A |
| Stability | High | Fragile | High | High | Fragile |
| Shell compat | Native | N/A | Native | Needs tools | Native |
| Crash cleanup | Sweep orphans | N/A | Auto | Auto | Manual |
| Platform | POSIX | Any | POSIX | POSIX | Any |

## Key Design Decisions

1. **`--settings` per-invocation** — No global config mutation. Each subprocess gets
   its own settings JSON via CLI flag.

2. **Atomic file writes** — `.tmp` + `rename()` prevents partial reads. POSIX guarantees
   `rename()` is atomic within the same filesystem.

3. **Per-session directories** — Prevents tool_use_id collisions between concurrent
   background agents.

4. **120s timeout with deny fallback** — If the user doesn't respond, the tool is denied.
   Configurable via `DEUS_TUI_PERMISSIONS_TIMEOUT` env var.

5. **Claude-only** — Codex has no hook system. Codex sessions use the existing
   `PermissionDenials` post-hoc reporting path. No changes to `codex.rs`.

6. **Orphan sweep on startup** — `PermissionBridge::sweep_orphans()` removes temp dirs from
   crashed TUI instances by checking if the owning PID is still alive.

7. **Always-allow via env var** — `DEUS_TUI_ALLOWED_TOOLS` (comma-separated bare tool names)
   is passed to the subprocess at spawn time. The hook checks this list before IPC — if the
   tool is allowed, it returns `allow` immediately without writing request files. Updated
   on each new turn because `dispatch_message_for` spawns a fresh subprocess.

## Scope

- **Phase 1:** IPC bridge, hook script, backend integration, keyboard shortcuts (Y/N/A)
- **Phase 2:** Full TUI overlay with tool details, input preview, session context
- **Phase 3:** "Always allow" persistence — bare tool names, env var fast path, persisted via `tui-permissions.json`
