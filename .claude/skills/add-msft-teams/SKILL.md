---
name: add-msft-teams
description: Add Microsoft Teams as a channel. Can replace WhatsApp entirely or run alongside it. Uses the Azure Bot Service (Bot Framework) and requires a public HTTPS messaging endpoint.
---

# Add Microsoft Teams Channel

> **Status:** Available in-repo as `packages/mcp-teams/` (`@deus-ai/teams-mcp`). The published npm package is not out yet, so the channel factory falls back to the in-repo build at `packages/mcp-teams/dist/index.js` — you build it locally (below).

Microsoft Teams is the chat-channel analog of Slack: a bot identity receives
messages and posts replies. Unlike Slack's Socket Mode, the Teams bot uses the
**Azure Bot Service (Bot Framework)**, which delivers activities to a **public
HTTPS messaging endpoint**. The channel runs its own HTTP server on `TEAMS_PORT`
(default 3978) serving `/api/messages`; you must expose that port via a
**dedicated public tunnel** (see below) — Deus's existing ingress tunnel forwards
only the gateway port and does **not** cover `TEAMS_PORT`.

## Phase 1: Setup

### Register the Azure app + bot (if needed)

If the user doesn't have an Azure Bot, walk them through it. Quick summary of
what's needed:

1. In the [Azure Portal](https://portal.azure.com) → **Microsoft Entra ID** →
   **App registrations** → **New registration**. Copy the **Application (client)
   ID** and **Directory (tenant) ID**.
2. **Certificates & secrets** → **New client secret** → copy the secret **value**
   (shown once).
3. Start a dedicated tunnel to the Teams port (any HTTPS tunnel works):

   ```bash
   ngrok http 3978   # → forwards a public https URL to http://localhost:3978
   ```

4. Create an **Azure Bot** resource (Azure Bot Service). Link it to the app
   registration above (the Microsoft App ID). Set the **messaging endpoint** to
   your tunnel's URL: `https://<your-tunnel-domain>/api/messages`.
5. On the bot resource → **Channels** → enable the **Microsoft Teams** channel.
6. Create a Teams app (Developer Portal for Teams / manifest) that references the
   bot's App ID, then install it to a team or chat.

> The messaging endpoint must be a **public HTTPS URL** reaching `TEAMS_PORT`.
> A local-only endpoint (or the gateway tunnel) will not receive Bot Framework
> activities — use a dedicated tunnel to port 3978.

### Configure environment

Add to `.env` (use the values from the app registration — no personal tenant or
domain values belong in this skill):

```bash
TEAMS_APP_ID=<your-application-client-id>
TEAMS_APP_PASSWORD=<your-client-secret-value>
# Single-tenant only (omit for a multi-tenant bot):
TEAMS_APP_TENANT_ID=<your-directory-tenant-id>
# Optional — defaults to 3978:
# TEAMS_PORT=3978
```

### Build the package

The published npm package is not out yet, so build the in-repo package:

```bash
cd packages/mcp-teams && npm install && npm run build && cd ../..
```

The channel runs **host-side** and auto-enables once `TEAMS_APP_ID` +
`TEAMS_APP_PASSWORD` are present. Restart the service so the host picks it up:

```bash
# macOS
launchctl kickstart -k gui/$(id -u)/com.deus
# Linux (systemd)
sudo systemctl restart deus
```

## Phase 2: Registration

### Get the conversation ID

Tell the user:

> 1. Add the bot to a Teams channel or chat (install the Teams app, then
>    @mention the bot once so Teams creates a conversation reference).
> 2. The conversation ID is the Bot Framework `conversation.id` for that
>    channel/chat. The setup helper will capture it from the first inbound
>    activity once the channel is live.
>
> The JID format for Deus is: `teams:<conversation-id>`

### Register the channel

The conversation ID, name, and folder name are needed. Use
`npx tsx setup/index.ts --step register` with the appropriate flags.

For a main channel (responds to all messages):

```bash
npx tsx setup/index.ts --step register -- --jid "teams:<conversation-id>" --name "<channel-name>" --folder "teams_main" --trigger "@${ASSISTANT_NAME}" --channel teams --no-trigger-required --is-main
```

For additional channels (trigger-only):

```bash
npx tsx setup/index.ts --step register -- --jid "teams:<conversation-id>" --name "<channel-name>" --folder "teams_<channel-name>" --trigger "@${ASSISTANT_NAME}" --channel teams
```

## Verify

### Smoke test

```bash
npx tsx setup/index.ts --step smoke-test -- --channel teams
```

The smoke test checks: service running, registered group exists, DB write/read
works, and channel connection appears in logs.

If it passes, tell the user "Microsoft Teams channel is working." Then ask them
to send a test message:

> Send a message in your registered Teams channel to confirm real-time delivery.
> - For main channel: any message works
> - For non-main: @mention the bot

## Troubleshooting

### Bot not responding

1. Check `TEAMS_APP_ID`, `TEAMS_APP_PASSWORD`, and `TEAMS_APP_TENANT_ID` are set
   in `.env`.
2. Confirm the package is built: `ls packages/mcp-teams/dist/index.js`.
3. Check the channel is registered:
   `sqlite3 store/messages.db "SELECT * FROM registered_groups WHERE jid LIKE 'teams:%'"`
4. For non-main channels: the message must include the trigger pattern.
5. Service is running: `launchctl list | grep deus` (macOS) /
   `systemctl status deus` (Linux).

### Bot connected but not receiving messages

1. Verify the messaging endpoint in the Azure Bot resource is your **dedicated
   tunnel's** `https://.../api/messages` URL (reaching `TEAMS_PORT`), reachable
   from the internet.
2. Verify the **Microsoft Teams** channel is enabled on the bot resource.
3. Verify the Teams app (manifest) referencing the bot is installed in the
   target team/chat.
4. Check the dedicated tunnel is up — a Bot Framework activity to an unreachable
   endpoint is silently dropped by Azure. (The Deus ingress tunnel does NOT cover
   `TEAMS_PORT` — Teams needs its own tunnel.)

### Authorization / 401 from the Bot Framework

1. Confirm the client secret **value** (not the secret ID) is in
   `TEAMS_APP_PASSWORD`, and that it has not expired (Azure secrets expire).
2. Confirm `TEAMS_APP_TENANT_ID` matches the app registration's directory (for a
   single-tenant bot).
3. Regenerate the secret in **Certificates & secrets** if in doubt, update
   `.env`, and restart (`launchctl kickstart -k gui/$(id -u)/com.deus` on macOS /
   `sudo systemctl restart deus` on Linux).

## After Setup

The Microsoft Teams channel will support:

- **Team channels** — the bot must be installed in the team and @mentioned.
- **Group chats** — the bot must be added to the chat.
- **1:1 chats** — users can message the bot directly.
- **Multi-channel** — can run alongside WhatsApp or other channels (auto-enabled
  by credentials).

## Known Limitations

- **Dedicated public endpoint required** — unlike Slack's Socket Mode, the Bot
  Framework pushes activities to a public HTTPS endpoint. The channel listens on
  `TEAMS_PORT` (3978); you must expose it with a dedicated tunnel and point the
  Azure Bot messaging endpoint at it. Deus's existing ingress tunnel covers only
  the gateway port, not `TEAMS_PORT`.
- **Reply needs a prior inbound** — outbound replies use Bot Framework proactive
  messaging, which requires a conversation reference captured from a prior inbound
  activity (persisted to `~/.teams-mcp/conversations.json`). A reply to a
  conversation the bot has never received a message from is logged and skipped.
- **Threads / replies** — Teams reply chains will be delivered as flat messages
  unless thread-aware routing is added (database schema, `NewMessage` type, and
  `Channel.sendMessage` interface changes), the same limitation the Slack channel
  documents.
- **Adaptive Cards / attachments** — only text content is forwarded to the agent
  in the initial integration. Adaptive Cards, files, and images are not handled.
- **Secret expiry** — Azure client secrets expire; the channel will start
  failing auth when the secret lapses, with no proactive warning until the
  credential-probe maintenance script is extended to cover Teams.
