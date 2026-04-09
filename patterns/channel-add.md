---
governs:
  - src/channels
---
# Pattern: channel-add

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

## Scope rules

- Channel skill PRs touch only `.claude/skills/` (and optionally `docs/`, `README.md`).
- Core changes (`src/`, `package.json`) go in a separate PR first.
- Never commit `host.ts`, `scripts/`, `node_modules/`, or `package-lock.json`.

## Tests

Add at least one test covering the capability registration path. Run `npm test` before committing.

## Extra doc

Load `docs/CONTRIBUTING-AI.md` §MCP Channel Servers for the full SDK pattern.
