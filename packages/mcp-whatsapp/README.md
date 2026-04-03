# deus-mcp-whatsapp

Standalone MCP server for WhatsApp messaging. Uses [Baileys](https://github.com/WhiskeySockets/Baileys) for the WhatsApp Web API.

Works with any MCP client — Claude Code, Claude Desktop, or your own application.

## Quick Start

```json
{
  "mcpServers": {
    "whatsapp": {
      "command": "npx",
      "args": ["deus-mcp-whatsapp"],
      "env": {
        "WHATSAPP_AUTH_DIR": "/path/to/auth/dir"
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
| `get_status` | Connection status and identity info |
| `list_chats` | List known chats and groups |
| `sync_groups` | Refresh group metadata from WhatsApp |
| `get_new_messages` | Poll for incoming messages (cursor-based) |
| `connect` / `disconnect` | Connection lifecycle |
| `get_auth_status` | Check if WhatsApp credentials exist |
| `start_auth` | Begin QR or pairing code authentication |

## Incoming Messages

Messages are pushed in real-time via MCP logging notifications with `logger: "incoming_message"`. For clients that don't support notifications, use the `get_new_messages` polling tool.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `WHATSAPP_AUTH_DIR` | `./store/auth` | Path to WhatsApp credential storage |
| `ASSISTANT_NAME` | `Deus` | Name prefix for outgoing messages |
| `ASSISTANT_HAS_OWN_NUMBER` | `false` | Skip name prefix when bot has its own phone |
| `LOG_LEVEL` | `info` | Pino log level (debug, info, warn, error) |

## License

MIT
