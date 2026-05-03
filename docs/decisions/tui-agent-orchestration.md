# TUI Agent Orchestration Architecture

**Date:** 2026-05-03
**Status:** Implemented
**Scope:** tui/src/app.rs, tui/src/backend/, tui/src/main.rs
**Related:** [parallel-agent-orchestration.md](parallel-agent-orchestration.md), [backend-strategy-trait.md](backend-strategy-trait.md), [tui-permission-bridge.md](tui-permission-bridge.md)

## Overview

The Deus TUI is a terminal UI that orchestrates one main agent session and N
parallel background agent sessions. Each session is an independent Claude Code
(or Codex) subprocess. The TUI manages lifecycle, communication, permissions,
and user context across all sessions.

## High-Level Architecture

```mermaid
graph TB
    subgraph "Deus TUI Process (Rust)"
        direction TB
        MAIN_LOOP["Main Event Loop<br/>(50ms tick)"]
        APP["App<br/>(state manager)"]

        subgraph "Session Management"
            MAIN_SESSION["Main Session<br/>(SessionId: 0)"]
            BG1["Agent Session 1"]
            BG2["Agent Session 2"]
            BGN["Agent Session N"]
        end

        subgraph "Cross-Cutting"
            PERMS["PermsBridge<br/>(file IPC)"]
            EFFORT["EffortPolicy<br/>(auto-classify)"]
            CONFIG["PermissionsConfig<br/>(allow/deny rules)"]
        end

        MAIN_LOOP -->|"poll_response()"| APP
        MAIN_LOOP -->|"keyboard events"| APP
        APP --> MAIN_SESSION
        APP --> BG1
        APP --> BG2
        APP --> BGN
        APP --> PERMS
    end

    subgraph "Backend Subprocesses"
        CLAUDE_MAIN["claude -p --stream-json<br/>(main)"]
        CLAUDE_BG1["claude -p --stream-json<br/>(--bare --ephemeral)"]
        CLAUDE_BG2["claude -p --stream-json<br/>(--bare --ephemeral)"]
    end

    MAIN_SESSION ---|"mpsc channel"| CLAUDE_MAIN
    BG1 ---|"mpsc channel"| CLAUDE_BG1
    BG2 ---|"mpsc channel"| CLAUDE_BG2
    PERMS ---|"filesystem IPC"| CLAUDE_MAIN
    PERMS ---|"filesystem IPC"| CLAUDE_BG1
```

## Session Lifecycle

```mermaid
stateDiagram-v2
    [*] --> Idle: Session created
    Idle --> Streaming: dispatch_message()
    Streaming --> Idle: ChunkKind::Done (main)
    Streaming --> Completed: ChunkKind::Done (background)
    Streaming --> Failed: error + Done
    Completed --> [*]: auto-GC (immediate)
    Failed --> [*]: auto-GC (immediate)
    Streaming --> Idle: cancel_response()

    note right of Streaming
        Background sessions have
        a timeout thread that sends
        Kill after DEUS_AGENT_TIMEOUT_SECS
    end note

    note right of Completed
        Completion summary posted
        to main session before removal
    end note
```

## Communication Flow: Main Session

```mermaid
sequenceDiagram
    participant User
    participant TUI as TUI Event Loop
    participant App as App State
    participant Thread as Spawn Thread
    participant Claude as claude -p subprocess

    User->>TUI: types message + Enter
    TUI->>App: send_message()
    App->>App: dispatch_message_for(MAIN, msg)
    App->>App: create mpsc channel (tx, rx)
    App->>App: session.stream_rx = Some(rx)
    App->>Thread: thread::spawn(move || ...)

    Thread->>Claude: Command::new("claude").spawn()
    Claude-->>Thread: stdout line (JSON)
    Thread->>Thread: backend.parse_line(line)
    Thread->>App: tx.send(StreamChunk)

    loop Every 50ms tick
        TUI->>App: poll_response()
        App->>App: rx.try_recv() for each session
        App->>App: process chunks → update chat_messages
        TUI->>TUI: terminal.draw(ui::render)
    end

    Claude-->>Thread: Done
    Thread->>App: tx.send(Done)
    App->>App: session.chat_state = Idle
```

## Communication Flow: Background Agent

```mermaid
sequenceDiagram
    participant User
    participant App as App State
    participant Main as Main Session
    participant BG as Background Session
    participant Thread as Agent Thread
    participant Claude as claude -p --bare

    User->>App: /agent "find config files"
    App->>App: spawn_agent(prompt, model, effort)
    App->>App: check max_agents() limit
    App->>BG: create Session (Ephemeral mode)
    App->>App: dispatch_message_for(bg_id, prompt)
    App->>Thread: thread::spawn with timeout watcher

    par Timeout Watcher
        Thread->>Thread: cancel_rx.recv_timeout(600s)
        Note over Thread: If timeout fires,<br/>sends Kill signal
    and Stream Processing
        Thread->>Claude: spawn subprocess
        Claude-->>Thread: stream chunks
        Thread->>BG: tx.send(chunks)
    end

    Claude-->>Thread: process exits
    Thread->>BG: tx.send(Done)

    Note over App: poll_response() picks up Done
    App->>BG: session_state = Completed
    App->>BG: completion_summary()
    App->>Main: "[Agent completed: label] summary..."
    App->>App: remove session, update picker
```

## Backend Strategy Pattern

```mermaid
classDiagram
    class Backend {
        <<trait>>
        +name() str
        +display_name() str
        +models() [ModelDef]
        +build_command(RunConfig) Command
        +parse_line(str) [StreamChunk]
    }

    class ClaudeBackend {
        +build_command(): --stream-json, --settings, env vars
        +parse_line(): assistant/user/result events
    }

    class CodexBackend {
        +build_command(): --json, --ephemeral
        +parse_line(): item.completed, function_call events
    }

    class RunConfig {
        model: String
        message: String
        effort: String
        permissions: PermissionsConfig
        run_mode: RunMode
        perms_dir: Option~PathBuf~
    }

    class RunMode {
        <<enum>>
        Normal(session_id?)
        Resume(session_id)
        Ephemeral
    }

    Backend <|.. ClaudeBackend
    Backend <|.. CodexBackend
    Backend ..> RunConfig : uses
    RunConfig --> RunMode
```

## Stream Chunk Processing

```mermaid
flowchart LR
    subgraph "Claude subprocess stdout"
        JSON["JSON lines"]
    end

    subgraph "parse_line()"
        JSON --> TEXT["Text"]
        JSON --> THINK["Thinking"]
        JSON --> TOOL["ToolUse"]
        JSON --> RESULT["ToolResult"]
        JSON --> SUB["SubagentStart"]
        JSON --> COST["CostUpdate"]
        JSON --> PERM["PermissionDenials"]
        JSON --> DONE["Done"]
        JSON --> ERR["Error"]
    end

    subgraph "poll_session()"
        TEXT --> CHAT["Append to chat"]
        THINK --> SUMMARY["Update thinking summary"]
        TOOL --> DETAIL["Show tool details"]
        RESULT --> BLOCK["Complete subagent block"]
        SUB --> HINT["Set subagent hint"]
        COST --> TOKEN["Update token/cost"]
        PERM --> NOTIFY["Show denied tools"]
        DONE --> CLEANUP["Session cleanup"]
        ERR --> FLAG["Set had_error"]
    end
```

## Permission Bridge Integration

```mermaid
flowchart TB
    subgraph "Decision: Does this session need permission bridging?"
        MODEL{Backend?}
        MODEL -->|Claude| BYPASS{Bypass mode?}
        MODEL -->|Codex| NO_BRIDGE["No bridge<br/>(post-hoc denials only)"]
        BYPASS -->|Yes| NO_BRIDGE
        BYPASS -->|No| BRIDGE["Create PermsBridge"]
    end

    subgraph "Per-session setup"
        BRIDGE --> DIR["Create session dir<br/>/tmp/deus-tui-perms-PID/SID/"]
        DIR --> SETTINGS["Write settings.json<br/>(hook config)"]
        DIR --> HOOK["Materialize permission-bridge.sh"]
        SETTINGS --> CMD["Pass --settings to claude"]
        DIR --> ENV["Set DEUS_TUI_PERMS_DIR env"]
    end

    subgraph "Runtime"
        CMD --> STREAM["Claude streams tool_use"]
        ENV --> HOOKFIRE["PreToolUse hook fires"]
        HOOKFIRE --> REQ["Write request-ID.json"]
        REQ --> POLL["TUI polls directory"]
        POLL --> PROMPT["Show Y/N/A prompt"]
        PROMPT --> RESP["Write response-ID.json"]
        RESP --> HOOKREAD["Hook reads response"]
        HOOKREAD --> DECISION["Returns allow/deny to Claude"]
    end
```

## Concurrent Agent Limit

```mermaid
flowchart TB
    SPAWN["/agent prompt"]
    SPAWN --> CHECK{"active_agents >= max_agents()?"}

    subgraph "max_agents() resolution (layered)"
        ENV["DEUS_MAX_AGENTS env"] --> CONF["config: max_parallel_agents"]
        CONF --> HW["available_parallelism() / 2<br/>clamped to [2, 8]"]
    end

    CHECK -->|"Yes"| REJECT["'At agent limit' message"]
    CHECK -->|"No"| CREATE["Create session + spawn"]

    CREATE --> TIMEOUT["Timeout watcher thread<br/>(600s default)"]
    TIMEOUT -->|"timeout"| KILL["Kill subprocess"]
    TIMEOUT -->|"completed"| CANCEL["Cancel watcher"]
```

## Alternatives Considered for Agent Communication

### Option A: Shared Thread Pool (rejected)

```mermaid
graph TB
    APP["App"] --> POOL["ThreadPool(N)"]
    POOL --> W1["Worker 1: claude"]
    POOL --> W2["Worker 2: claude"]
    POOL --> W3["Worker 3: claude"]
```

**Pros:** Bounded concurrency built-in, simpler resource management.
**Cons:** Claude Code processes are long-running (minutes). Thread pools are designed
for short tasks. Blocking a pool thread for minutes starves other work. The
`max_agents()` limit achieves the same bound without pool overhead.

### Option B: Async Runtime (rejected)

```mermaid
graph TB
    TOKIO["tokio::Runtime"]
    TOKIO --> T1["spawn: read stdout"]
    TOKIO --> T2["spawn: read stderr"]
    TOKIO --> T3["spawn: timeout"]
    TOKIO --> T4["spawn: poll perms"]
```

**Pros:** Natural fit for I/O-bound work. `select!` for concurrent waits.
**Cons:** Adds tokio as a dependency (~1MB+ binary size). Ratatui's event loop
is synchronous. Mixing async and sync requires `block_on` bridges that negate
the benefit. The 50ms poll loop with `try_recv()` is simpler and sufficient
for the human-interaction latency target.

### Option C: Single Multiplexed Process (rejected)

```mermaid
graph TB
    APP["TUI"] <--> CLAUDE["Single claude process"]
    CLAUDE --> S1["session 1"]
    CLAUDE --> S2["session 2"]
    CLAUDE --> S3["session 3"]
```

Run one Claude Code process and multiplex sessions over its stdin/stdout.

**Pros:** Single process, lower resource usage.
**Cons:** Claude Code's `-p` mode is single-prompt. No multiplexing protocol exists.
Sessions across different models or backends are impossible. Process crash kills
all sessions.

### Selected: Independent Subprocesses with mpsc Channels

```mermaid
graph TB
    APP["App (Rust)"]
    APP -->|"mpsc::channel"| S0["Session 0: claude -p (main)"]
    APP -->|"mpsc::channel"| S1["Session 1: claude -p --bare (agent)"]
    APP -->|"mpsc::channel"| S2["Session 2: claude -p --bare (agent)"]

    S0 ---|"per-session"| P0["stdout reader thread"]
    S1 ---|"per-session"| P1["stdout reader + timeout thread"]
    S2 ---|"per-session"| P2["stdout reader + timeout thread"]
```

**Why:** Each session is an independent subprocess. Crash isolation is free.
Different models/backends per session is trivial. `mpsc::channel` is zero-cost
on idle sessions. The 50ms poll loop processes all channels in one pass.

## Comparison Matrix

| Criterion | Independent Processes | Thread Pool | Async Runtime | Multiplexed |
|---|---|---|---|---|
| Crash isolation | Per-session | Per-session | Per-session | All-or-nothing |
| Multi-backend | Trivial | Trivial | Trivial | Impossible |
| Complexity | Low | Medium | High | Extreme |
| Dependencies | std only | crossbeam | tokio | Custom protocol |
| Resource overhead | 1 process/session | 1 thread/session | 1 task/session | 1 process total |
| Latency | ~50ms poll | ~50ms poll | ~0ms await | ~0ms stdin |
