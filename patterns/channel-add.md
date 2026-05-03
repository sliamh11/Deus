---
governs:
  - src/channels
  - packages/
last_verified: "2026-05-03T10:00" # auto-bump
test_tasks:
  - "Add a Discord channel with OAuth login"
  - "Add capabilities: logging to a new MCP channel server so notifications are delivered"
  - "Register a new MCP tool on the Telegram channel with a Zod input schema"
  - "Create a new Slack channel package under packages/mcp-slack/"
  - "Wire a new channel to emit incoming_reaction notifications via onReaction"
---
# Pattern: channel-add

## Where channel code lives

Channels are split across two locations:

- **`packages/mcp-{channel}/`** — the MCP server process (receives socket events, sends MCP notifications)
- **`src/channels/`** — the host-side adapter (connects to the MCP child, registers with the channel registry)

Both need to be updated when adding or modifying a channel.

## Critical gotcha — silent message loss

Always declare `capabilities: { logging: {} }` when constructing `McpServer`. Without it, `sendLoggingMessage()` silently drops all notifications. No errors, no warnings.

```typescript
// CORRECT
const server = new McpServer(
  { name: '@deus-ai/my-channel', version: '1.0.0' },
  { capabilities: { logging: {} } },
);

// WRONG — total silent message loss
const server = new McpServer({ name: '@deus-ai/my-channel', version: '1.0.0' });
```

## Building a channel package

`npm run build` only rebuilds `src/`. After modifying `packages/mcp-{channel}/`, build the package separately:

```bash
cd packages/mcp-whatsapp && npx tsc
```

Then restart the service to pick up the change.

## Scope rules

- Channel skill PRs touch only `.claude/skills/` (and optionally `docs/`, `README.md`).
- Core changes (`src/`, `packages/`, `package.json`) go in a separate PR first.
- Never commit `host.ts`, `scripts/`, `node_modules/`, or `package-lock.json`.

## Reactions (optional)

If the channel supports message reactions (WhatsApp, Telegram), implement the optional `onReaction` callback on the `ChannelProvider`. `registerCommonTools` wires it to `sendLoggingMessage({ logger: 'incoming_reaction' })` — reactions are ephemeral signals, not buffered for polling. The host dispatches to `logReactionSignal` via the `incoming_reaction` branch in `src/channels/mcp-adapter.ts`. Empty-string emoji = reaction removed (no-op at the sink).

## Tool registration

If the channel exposes MCP tools (not just notifications), each tool must include a JSON Schema for its input parameters. Tools registered without a schema silently fail schema validation at the SDK level — callers receive no error, the tool just isn't invoked.

```typescript
server.tool('send_message', { description: '...', inputSchema: zodToJsonSchema(SendMessageSchema) }, handler);
```

## Tests

Add at least one test covering the capability registration path. Run `npm test` before committing.

## Config

Channel-specific env vars (e.g., `TELEGRAM_BOT_TOKEN`) are static long-lived secrets passed by the host process — they go in the project root `.env`, not `~/.config/deus/.env`. These are not rotating credentials (see `deployment.md` §Credentials rule).

## Extra doc

Load `docs/CONTRIBUTING-AI.md` §MCP Channel Servers for the full SDK pattern.
