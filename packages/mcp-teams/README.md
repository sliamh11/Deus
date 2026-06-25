# @deus-ai/teams-mcp

Standalone Microsoft Teams MCP server. Connects a Teams bot via the Azure Bot
Service (Bot Framework) and exposes it as an MCP channel: it receives activities
on a public `/api/messages` endpoint, forwards each conversation to the MCP
client as an incoming message, and sends proactive replies.

It implements the `ChannelProvider` contract from `@deus-ai/channel-core` and is
the Teams analog of `@deus-ai/slack-mcp`.

## Configuration (env vars)

| Var | Default | Meaning |
|-----|---------|---------|
| `TEAMS_APP_ID` | — | Azure Bot / Entra app (client) ID |
| `TEAMS_APP_PASSWORD` | — | client secret value |
| `TEAMS_APP_TENANT_ID` | — | directory (tenant) ID for single-tenant; omit for multi-tenant |
| `TEAMS_PORT` | `3978` | messaging-endpoint port |
| `TEAMS_CREDENTIALS_DIR` | `~/.teams-mcp/` | where conversation references persist |
| `LOG_LEVEL` | `info` | pino log level (stderr) |

## Public endpoint required

Unlike Slack's Socket Mode, the Bot Framework pushes activities to a **public
HTTPS endpoint**. This server listens on `TEAMS_PORT` locally; you must expose
that port via a dedicated tunnel (e.g. ngrok/Cloudflare → `http://localhost:3978`)
and set the Azure Bot **messaging endpoint** to `https://<your-tunnel>/api/messages`.
Deus's existing ingress tunnel forwards only the gateway port and does **not**
cover `TEAMS_PORT`.

## Run

```bash
npm install && npm run build
node dist/index.js   # stdio MCP server; auto-connects if credentials exist
```

## JID format

Each conversation maps to `teams:<conversationId>`.

## Known limitation

Outbound replies use Bot Framework proactive messaging, which needs a stored
conversation reference captured from a prior inbound activity (persisted to
`~/.teams-mcp/conversations.json`). A reply to a conversation the bot has never
received a message from is logged and skipped.
