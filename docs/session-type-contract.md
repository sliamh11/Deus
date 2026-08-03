# Session Type Contract

Implements RETRO-2026-05-14-02. The hook + settings + permissions surface was
designed against a single session type (primary CLI) but is now consumed by 5
distinct session types. This document is the authoritative contract specifying
what each session type guarantees regarding settings loading, hook activation,
permissions, and environment variables.

## Matrix Overview

| Property | CLI | Agent View (TUI) | Worktree Subagent | Background | Container |
|---|---|---|---|---|---|
| Launch mechanism | `deus-cmd.sh` → `claude` | `tui/backend/claude.rs` → `claude -p` | Claude Code Agent tool | `claude --bg` / `run_in_background` | Agent SDK `query()` |
| Permission mode | `bypassPermissions` via `--dangerously-skip-permissions` | `--permission-mode <mode>` from `tui-permissions.json` | Inherits parent session | Inherits parent session | `bypassPermissions` (hardcoded) |
| Host `~/.claude/settings.json` | Loaded | Loaded | Loaded | Loaded | **Not loaded** (filesystem isolation) |
| Project `.claude/settings.json` | Loaded | Loaded | Worktree's own copy (git-tracked snapshot) | Loaded | **Not loaded** |
| `.claude/settings.local.json` | Loaded | Loaded | Not copied to worktree | Loaded | **Not loaded** |
| Container session settings | N/A | N/A | N/A | N/A | `data/sessions/<group>/.claude/settings.json` |
| `CLAUDE_PROJECT_DIR` | Set by Claude Code | Set by Claude Code | Set to worktree path | Set by Claude Code | Set by SDK (`cwd` option) |
| `CLAUDE_JOB_DIR` | Not set | Not set | Not set | **Set** | Not set |
| Shell hooks fire | Yes (host + project) | Yes (host + project + TUI permission bridge) | Yes (host + worktree project) | Yes (host + project) | **No** |
| SDK hooks fire | No | No | No | No | **Yes** (TypeScript) |

## CLI

### Launch path

`deus-cmd.sh` detects home mode (`cwd == ~/deus`) vs external project mode and
calls `launch_claude()` which invokes `claude --dangerously-skip-permissions`.

### Permission mode

`bypassPermissions` via `--dangerously-skip-permissions` CLI flag. Gated by
`PREFS_BYPASS` which reads `bypass_permissions` from `~/.config/deus/config.json`.
When `PREFS_BYPASS=false`, the flag is stripped and the default permission mode
from settings applies.

### Settings files loaded (in precedence order)

1. `~/.claude/settings.json` (host)
2. `<project>/.claude/settings.json` (project)
3. `<project>/.claude/settings.local.json` (project local, gitignored)

### Hook events active

All hook events from both host and project settings fire:

- **SessionStart:** hook-integrity-check, plan-mode-session-init, memory-cite-seed,
  standards-pack, vault-context (host) + warden-shim session-init (project)
- **UserPromptSubmit:** catchup-freshness, orchestrator-preflight, memory-retrieval
  (host) + memory_retrieval_hook.py (project)
- **PreToolUse:** plan-review-gate, plan-mode-invalidator, sonnet-default-reminder,
  code-review-gate, memory-cite (host) + plan-review-gate, tdd-test-lock,
  plan-mode-invalidator, code-review-gate, admin-merge-gate (project)
- **PostToolUse:** memory-tree-hook, threat-model-gate, code-review-invalidator,
  path-leak-detector, plan-revise-logger, warden-verdict-tracker (host) +
  code-review-invalidator, threat-model-gate, path-leak-detector,
  warden-verdict-tracker (project)
- **Stop:** stop_hook.py (host)

### Required env vars

None specific to CLI beyond `CLAUDE_PROJECT_DIR` (set automatically).

### Negative contract

- `CLAUDE_JOB_DIR` must NOT be set (would trigger compress gate).
- `DEUS_TUI_*` vars must NOT be set (would confuse TUI detection).

## Agent View (TUI)

### Launch path

`tui/src/backend/claude.rs` → `build_command()` invokes
`claude -p --output-format stream-json --verbose --model <model> --effort <level>
--permission-mode <mode>`.

### Permission mode

`--permission-mode <mode>` loaded from `~/.config/deus/tui-permissions.json`.
When mode is `bypassPermissions`, also passes `--dangerously-skip-permissions`.
In non-bypass mode, the TUI permission bridge hook (`tui/hooks/permission-bridge.sh`)
intercepts `PreToolUse` events via file-based IPC.

### Settings files loaded

Same as CLI, plus TUI-specific `--settings` override when the permission bridge
is active.

### Hook events active

Same as CLI, plus:

- **PreToolUse:** `permission-bridge.sh` (TUI-specific, only in non-bypass mode)

### Required env vars

| Variable | Set by | Purpose |
|---|---|---|
| `DEUS_TUI_BYPASS` | `deus-cmd.sh` | TUI reads to decide `--dangerously-skip-permissions` |
| `DEUS_TUI_MODE` | `deus-cmd.sh` | `"home"` or `"external"` |
| `DEUS_TUI_BACKEND` | `deus-cmd.sh` | Backend selection for TUI |
| `DEUS_TUI_PERMISSIONS_DIR` | TUI Rust backend | Path for permission bridge IPC (non-bypass only) |

### Negative contract

- `CLAUDE_JOB_DIR` must NOT be set.

## Worktree Subagent

### Launch path

Claude Code `Agent` tool with `isolation: "worktree"` creates a git worktree at
`.claude/worktrees/<name>/` on a new branch. The subagent runs in this directory.

### Permission mode

Inherits the parent session's permission mode. The worktree itself does not
configure permissions.

### Settings files loaded

1. `~/.claude/settings.json` (host — same as parent)
2. `.claude/worktrees/<name>/.claude/settings.json` (worktree's own copy)

Note: `.claude/settings.local.json` is NOT copied to the worktree. The worktree's
`.claude/settings.json` is a git-tracked snapshot from the branch point. Worktrees
created before the portable warden gates commit (`d43d3e0`) have a reduced hook
set (only `UserPromptSubmit` + `PreToolUse`). Worktrees created after have the
full 4-event hook set matching the current project settings.

### Hook events active

Host hooks fire normally. Project hooks fire from the worktree's own settings
snapshot. The minimum invariant hooks present in all worktrees regardless of age:

- **UserPromptSubmit:** `memory_retrieval_hook.py`
- **PreToolUse:** `tdd-test-lock.sh` (Write|Edit matcher)

### Required env vars

| Variable | Value |
|---|---|
| `CLAUDE_PROJECT_DIR` | Set to the worktree path (e.g., `~/deus/.claude/worktrees/<name>`) |

### Negative contract

- `CLAUDE_JOB_DIR` must NOT be set.
- `DEUS_TUI_*` vars must NOT be set.

## Background

### Launch path

`claude --bg <prompt>` or Claude Code Agent tool with `run_in_background: true`.
The session runs asynchronously with output collected in `CLAUDE_JOB_DIR`.

### Permission mode

Inherits the parent session's permission mode and settings.

### Settings files loaded

Same as CLI (host + project + project local).

### Hook events active

Same as CLI. Additionally, the Stop hook has special behavior:

- **Stop:** `stop_hook.py` detects `CLAUDE_JOB_DIR` is set via `_is_bg_session()`
  and activates the compress gate — an advisory `systemMessage` reminder (not a
  hard block, since 2026-08-02) if `/compress` hasn't run yet. A `.compress_gate`
  sentinel file in `CLAUDE_JOB_DIR` prevents repeat reminders. Checkpoint
  writing and drift-scan still run in the same Stop invocation regardless.

### Required env vars

| Variable | Set by | Purpose |
|---|---|---|
| `CLAUDE_JOB_DIR` | Claude Code | Background job output directory. **This is the sole signal** that a session is a background session. |

### Negative contract

- `DEUS_TUI_*` vars must NOT be set.

## Container

### Launch path

`container/agent-runner/src/index.ts` uses the Claude Agent SDK `query()` function.
This is NOT the `claude` CLI — it is a direct SDK call running inside a Docker
container with an isolated filesystem.

### Permission mode

Hardcoded in `agent-runner/src/index.ts`:
```typescript
permissionMode: 'bypassPermissions',
allowDangerouslySkipPermissions: true,
```

### Settings files loaded

The SDK is configured with `settingSources: ['project', 'user']`, which maps to:
- **project:** `data/sessions/<group>/.claude/settings.json` (env vars only, no hooks)
- **user:** Container's own `~/.claude/settings.json` (empty — filesystem isolation)

Host `~/.claude/settings.json` is NOT loaded. Project `.claude/settings.json` from
the repo is NOT loaded.

### Hook events active

Container sessions use TypeScript SDK hooks, NOT shell scripts:

- **UserPromptSubmit:** `createMemoryRetrievalHook()` (imported from `memory-retrieval-hook.ts`)
- **PreCompact:** `createPreCompactHook()` (archives transcript to `conversations/`)
- **PostToolUse:** `createToolSizeLogHook()` (opt-out via `DEUS_TOOL_SIZE_LOG=0`),
  `createToolAuditHook()` (opt-out via `DEUS_TOOL_AUDIT_LOG=0`)

No SessionStart, PreToolUse, or Stop shell hooks fire.

### Required env vars

| Variable | Purpose |
|---|---|
| `DEUS_PROXY_TOKEN` | Auth token for credential proxy and HookDispatchService |
| `DEUS_AGENT_EFFORT` | Override effort level for SDK queries |
| `ANTHROPIC_CUSTOM_HEADERS` | Injects proxy token for API auth |

### Negative contract

- `CLAUDE_JOB_DIR` must NOT be set (container sessions are not background sessions).
- `DEUS_TUI_*` vars must NOT be set.
- No shell hooks from host `~/.claude/` must fire.
- No warden-shim hooks must fire (wardens are host-enforced, not container-enforced).

## Cross-Cutting Invariants

These 5 invariants hold regardless of session type.

1. **BG detection via env var.** Any session with `CLAUDE_JOB_DIR` set is a
   Background session. `stop_hook.py._is_bg_session()` uses `os.environ.get("CLAUDE_JOB_DIR")`
   and only this to detect background sessions. No CLI flag parsing.

2. **Container hook isolation.** Container sessions use TypeScript SDK hooks from
   `agent-runner/src/index.ts`, never shell scripts from the host. The container's
   filesystem isolation prevents host `~/.claude/settings.json` hooks from firing.

3. **Container permission hardcode.** Container sessions always run with
   `permissionMode: 'bypassPermissions'` + `allowDangerouslySkipPermissions: true`,
   hardcoded in `agent-runner/src/index.ts`.

4. **Memory retrieval universality.** `memory_retrieval_hook.py` fires on
   `UserPromptSubmit` in all host-side session types (CLI, Agent View, Worktree,
   Background) via project `.claude/settings.json`. Container sessions have an
   equivalent TypeScript hook (`createMemoryRetrievalHook`).

5. **TDD test lock universality.** `tdd-test-lock.sh` fires on `PreToolUse`
   (Write|Edit|MultiEdit|apply_patch|ExitPlanMode) in all host-side session types.
   Container sessions do not have this hook (containers are not used for TDD
   development).

## Testing

Run the contract test suite:

```bash
python3 -m pytest tests/test_session_type_contract.py -v
```

### What the tests verify (static analysis)

- Hook presence in project `.claude/settings.json` (structural rules, additive-safe)
- Hook script file existence (all referenced `.sh`/`.py` files resolve on disk)
- Container permission mode hardcode in TypeScript source
- Container SDK hook function names in TypeScript source
- Background detection mechanism (`CLAUDE_JOB_DIR`) in `stop_hook.py`
- CLI permission flags and TUI env var exports in `deus-cmd.sh`
- Worktree minimum-invariant hooks (when worktrees exist locally)

### What the tests do NOT verify (requires live testing)

- Host `~/.claude/settings.json` hook presence (file is outside the repo)
- Whether hooks actually fire at runtime (requires spawning real sessions)
- Permission mode effective behavior (requires Claude Code binary)
- Env var values at runtime (requires process inspection)
- Container filesystem isolation (requires Docker runtime)
- TUI permission bridge IPC (requires TUI binary)
