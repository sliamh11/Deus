# @deus-ai/discord-mcp

Standalone MCP server for Discord bots. Uses [discord.js](https://discord.js.org/) for the Discord API.

Works with any MCP client — Claude Code, Claude Desktop, or your own application.

## Quick Start

```json
{
  "mcpServers": {
    "discord": {
      "command": "npx",
      "args": ["@deus-ai/discord-mcp"],
      "env": {
        "DISCORD_BOT_TOKEN": "your-bot-token"
      }
    }
  }
}
```

## Tools

| Tool | Description |
|------|-------------|
| `send_message` | Send a text message to a channel |
| `send_typing` | Show/hide typing indicator |
| `get_status` | Connection status and bot info |
| `list_chats` | List known channels and DMs |
| `get_new_messages` | Poll for incoming messages (cursor-based) |
| `connect` / `disconnect` | Connection lifecycle |

## Incoming Messages

Messages are pushed in real-time via MCP logging notifications with `logger: "incoming_message"`. For clients that don't support notifications, use the `get_new_messages` polling tool.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DISCORD_BOT_TOKEN` | *(required)* | Bot token from the Discord Developer Portal |
| `ASSISTANT_NAME` | `Deus` | Name for @mention translation |
| `LOG_LEVEL` | `info` | Pino log level |

## License

MIT
