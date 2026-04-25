# Using Different AI Backends

Deus is backend-neutral: the same assistant experience runs on different LLMs. Claude is the default and most battle-tested backend. OpenAI (GPT-4o via Responses API) is opt-in and approaching parity.

## Quick Start

### Use OpenAI instead of Claude

```bash
# 1. Add your OpenAI API key to .env
echo 'OPENAI_API_KEY=sk-...' >> .env

# 2. Set OpenAI as the default backend
echo 'DEUS_AGENT_BACKEND=openai' >> .env

# 3. Restart Deus
deus auth
```

### Use OpenAI for one group only

Leave the global default as Claude, override a specific group:

```bash
# Via the CLI
deus openai    # Start a one-off OpenAI session

# Via group config (persisted)
# Set containerConfig.agentBackend = 'openai' on the group
```

### Use OpenAI for one scheduled task only

Set `agent_backend: 'openai'` when creating the task. The rest of the group stays on Claude.

### Switch back to Claude

Remove `DEUS_AGENT_BACKEND` from `.env` (or set it to `claude`) and restart.

## Resolution Order

When Deus decides which backend to use for a message or task:

1. **Task override** -- if a scheduled task has `agent_backend` set, use that
2. **Group override** -- if the group has `containerConfig.agentBackend` set, use that
3. **Global default** -- `DEUS_AGENT_BACKEND` env var (default: `claude`)

## What Stays the Same

Regardless of backend, Deus preserves:

- Same persona, tone, and memory
- Same tool access (shell, filesystem, web, browser, IPC)
- Same chat commands (/compact, /settings, etc.)
- Same session management and idle reset
- Same scheduled task execution
- Same channel support (WhatsApp, Telegram, Slack, Discord, Gmail)
- Same context loading (CLAUDE.md, group config, registered context files)

## What Differs

| Feature | Claude | OpenAI |
|---------|--------|--------|
| Tool streaming | Yes (live output) | No (batch response) |
| Session protocol | Claude Code SDK | OpenAI Responses API |
| Model default | Claude (via SDK) | gpt-4o (configurable via `DEUS_OPENAI_MODEL`) |
| Handoffs | Not yet | Not yet |
| MCP tools | Native | Bridged via tool-broker |

## Known Parity Gaps

See [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) for the current parity status and [agent-agnostic-debt.md](agent-agnostic-debt.md) for the tracked gap register.

Key gaps as of this writing:
- OpenAI backend has not been verified end-to-end in production containers
- Dynamic skill parity depends on skills exposing MCP-style tools
- Agents SDK handoffs/tracing not yet implemented for OpenAI

## CLI Usage

```bash
deus              # Uses default backend (claude unless configured otherwise)
deus claude       # Force Claude for this session
deus codex        # Force Codex (OpenAI) for this session
```

## Backend Management

Manage the default backend and model from the command line:

```bash
deus backend              # Show current backend and model
deus backend show         # Same as above
deus backend list         # List available backends with active marker
deus backend set codex    # Set default backend
deus backend model gpt-4o # Set model for current backend
```

Changes persist to `~/.config/deus/config.json` (user preferences) and `.env` (service runtime). They take effect on the next `deus` launch.

### Precedence

The backend is resolved in this order (first non-empty wins):

1. **Per-session prefix** -- `deus codex` / `deus claude` (env vars for this process)
2. **Environment variable** -- `DEUS_AGENT_BACKEND` in `.env` or shell
3. **User config** -- `agent_backend` in `~/.config/deus/config.json` (set via `deus backend set`)
4. **Default** -- `claude`

`deus backend set` writes to both config.json and `.env` so both the CLI and background service pick up the change.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DEUS_AGENT_BACKEND` | `claude` | Global default: `claude` or `openai` |
| `DEUS_OPENAI_MODEL` | `gpt-4o` | Model for the OpenAI backend |
| `OPENAI_API_KEY` | -- | Required when using the OpenAI backend |
| `DEUS_CODEX_MODEL` | `DEUS_OPENAI_MODEL` | Optional model override for `deus codex` |

## Adding a New Backend (for contributors)

1. Create a factory function in `src/agent-backends/` (see `claude-backend.ts` as template)
2. Define capabilities in a `BackendCapabilities` constant
3. Register it in `src/index.ts` via `registry.register(createYourBackend(deps))`
4. Add container-side dispatch in `container/agent-runner/src/index.ts`
5. Add the backend name to `AgentBackendName` union in `src/agent-backends/types.ts`
6. Update `parseAgentBackend()` in `src/ipc.ts` and `DEFAULT_AGENT_BACKEND` in `src/config.ts`
