# @deus-ai/gmail-mcp

Standalone MCP server for Gmail. Uses the Gmail API with OAuth2 authentication.

Works with any MCP client — Claude Code, Claude Desktop, or your own application.

## Quick Start

```json
{
  "mcpServers": {
    "gmail": {
      "command": "npx",
      "args": ["@deus-ai/gmail-mcp"]
    }
  }
}
```

## OAuth Setup

1. Create a Google Cloud project and enable the Gmail API.
2. Create OAuth 2.0 credentials (Desktop application type).
3. Download the credentials JSON and save it as `~/.gmail-mcp/gcp-oauth.keys.json`.
4. Obtain a refresh token (e.g., using the OAuth playground or a one-time script) and save it as `~/.gmail-mcp/credentials.json` with the following structure:
   ```json
   {
     "access_token": "...",
     "refresh_token": "...",
     "token_type": "Bearer"
   }
   ```

The server automatically refreshes expired access tokens and persists updated tokens to `credentials.json`.

## Tools

| Tool | Description |
|------|-------------|
| `send_message` | Reply to an email thread |
| `send_typing` | No-op (included for interface compatibility) |
| `get_status` | Connection status and account info |
| `list_chats` | List known email threads |
| `get_new_messages` | Poll for incoming emails (cursor-based) |
| `connect` / `disconnect` | Connection lifecycle |
| `read_email` | Read a full email by message ID |
| `send_email` | Send a new email (not a thread reply) |
| `search_emails` | Search emails by Gmail query string |
| `draft_email` | Create a draft email |

## Incoming Messages

Emails are polled every 60 seconds (configurable). New unread emails from the Primary category are delivered as MCP logging notifications with `logger: "incoming_message"`. For clients that don't support notifications, use the `get_new_messages` polling tool.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GMAIL_CREDENTIALS_DIR` | `~/.gmail-mcp/` | Directory containing OAuth key and token files |
| `GMAIL_POLL_INTERVAL_MS` | `60000` | Polling interval in milliseconds |
| `LOG_LEVEL` | `info` | Pino log level |

## License

MIT
