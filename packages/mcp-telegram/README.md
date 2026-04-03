# deus-mcp-telegram

Standalone MCP server for Telegram bots. Uses [grammY](https://grammy.dev/) for the Telegram Bot API.

Works with any MCP client — Claude Code, Claude Desktop, or your own application.

## Quick Start

```json
{
  "mcpServers": {
    "telegram": {
      "command": "npx",
      "args": ["deus-mcp-telegram"],
      "env": {
        "TELEGRAM_BOT_TOKEN": "your-bot-token"
      }
    }
  }
}
```

## Tools

| Tool | Description |
|------|-------------|
| `send_message` | Send a text message to a chat |
| `send_typing` | Show/hide typing indicator |
| `get_status` | Connection status and bot info |
| `list_chats` | List known chats and groups |
| `get_new_messages` | Poll for incoming messages (cursor-based) |
| `connect` / `disconnect` | Connection lifecycle |

## Incoming Messages

Messages are pushed in real-time via MCP logging notifications with `logger: "incoming_message"`. For clients that don't support notifications, use the `get_new_messages` polling tool.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | *(required)* | Bot token from @BotFather |
| `ASSISTANT_NAME` | `Deus` | Name for @mention translation |
| `LOG_LEVEL` | `info` | Pino log level |

## License

MIT
