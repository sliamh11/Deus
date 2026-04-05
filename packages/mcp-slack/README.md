# @deus-ai/slack-mcp

Standalone MCP server for Slack bots. Uses [Socket Mode](https://api.slack.com/apis/socket-mode) via [@slack/bolt](https://slack.dev/bolt-js/).

Works with any MCP client — Claude Code, Claude Desktop, or your own application.

## Quick Start

```json
{
  "mcpServers": {
    "slack": {
      "command": "npx",
      "args": ["@deus-ai/slack-mcp"],
      "env": {
        "SLACK_BOT_TOKEN": "xoxb-your-bot-token",
        "SLACK_APP_TOKEN": "xapp-your-app-token"
      }
    }
  }
}
```

## Tools

| Tool | Description |
|------|-------------|
| `send_message` | Send a text message to a channel or DM |
| `send_typing` | No-op (Slack Bot API has no typing indicator) |
| `get_status` | Connection status and bot info |
| `list_chats` | List known channels and DMs |
| `sync_groups` | Refresh channel metadata from Slack |
| `get_new_messages` | Poll for incoming messages (cursor-based) |
| `connect` / `disconnect` | Connection lifecycle |

## Incoming Messages

Messages are pushed in real-time via MCP logging notifications with `logger: "incoming_message"`. For clients that don't support notifications, use the `get_new_messages` polling tool.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SLACK_BOT_TOKEN` | *(required)* | Bot token (`xoxb-...`) |
| `SLACK_APP_TOKEN` | *(required)* | App-level token (`xapp-...`) for Socket Mode |
| `ASSISTANT_NAME` | `Deus` | Name for @mention translation |
| `LOG_LEVEL` | `info` | Pino log level |

## License

MIT
