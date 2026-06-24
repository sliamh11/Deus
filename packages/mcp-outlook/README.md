# @deus-ai/outlook-mcp

Standalone Outlook (Microsoft 365) MCP server. Connects to a mailbox over the
Microsoft Graph API and exposes it as an MCP channel: it polls the Inbox for
unread mail, forwards each conversation to the MCP client as an incoming message,
and sends replies in-thread.

It implements the `ChannelProvider` contract from `@deus-ai/channel-core` and is
the Outlook analog of `@deus-ai/gmail-mcp`.

## Configuration (env vars)

| Var | Default | Meaning |
|-----|---------|---------|
| `OUTLOOK_CREDENTIALS_DIR` | `~/.outlook-mcp/` | Holds `app-credentials.json` + `token.json` |
| `OUTLOOK_POLL_INTERVAL_MS` | `60000` | Inbox poll interval |
| `LOG_LEVEL` | `info` | pino log level (stderr) |

`app-credentials.json` holds your Azure AD app registration:

```json
{ "clientId": "<application-client-id>", "tenantId": "<directory-tenant-id>" }
```

Auth is delegated, device-code only (a user signs in once); the public-client app
registration must have **Allow public client flows** enabled. The confidential
(clientSecret / app-only) flow is not supported.

## One-time sign-in

```bash
npm install && npm run build
node dist/index.js auth   # device-code flow — open the URL, enter the code
```

This seeds `token.json` (with a refresh token) so the server acquires access
tokens silently afterwards.

## Run

```bash
node dist/index.js   # stdio MCP server; auto-connects if credentials exist
```

## JID format

Each conversation maps to `outlook:<conversationId>`.

## Known limitation

A reply can only be sent to a conversation the server has already seen in a poll
cycle (the reply target message id is cached on inbound). A reply to a thread
received before the process started is logged and skipped.
