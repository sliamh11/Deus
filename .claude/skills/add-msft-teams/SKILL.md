---
name: add-msft-teams
description: Add Microsoft Teams as a channel. Can replace WhatsApp entirely or run alongside it. Uses the Azure Bot Service (Bot Framework) and requires a public HTTPS messaging endpoint.
---

# Add Microsoft Teams Channel

> **Status:** Coming soon — this channel will be available as `@deus-ai/teams-mcp`. The MCP package is not yet available. In the meantime, the setup/registration phases below describe what the integration will look like, mirroring the other channel skills.

Microsoft Teams is the chat-channel analog of Slack: a bot identity receives
messages and posts replies. Unlike Slack's Socket Mode, the Teams bot uses the
**Azure Bot Service (Bot Framework)**, which delivers activities to a **public
HTTPS messaging endpoint** — so this channel depends on Deus's existing ingress
gateway / tunnel to expose `/api/messages`.

## Phase 1: Setup (Future)

### Register the Azure app + bot (if needed)

If the user doesn't have an Azure Bot, walk them through it. Quick summary of
what's needed:

1. In the [Azure Portal](https://portal.azure.com) → **Microsoft Entra ID** →
   **App registrations** → **New registration**. Copy the **Application (client)
   ID** and **Directory (tenant) ID**.
2. **Certificates & secrets** → **New client secret** → copy the secret **value**
   (shown once).
3. Create an **Azure Bot** resource (Azure Bot Service). Link it to the app
   registration above (the Microsoft App ID). Set the **messaging endpoint** to
   your public URL: `https://your-domain.example.com/api/messages`.
4. On the bot resource → **Channels** → enable the **Microsoft Teams** channel.
5. Create a Teams app (Developer Portal for Teams / manifest) that references the
   bot's App ID, then install it to a team or chat.

> The messaging endpoint must be a **public HTTPS URL**. Deus already runs an
> ingress gateway / tunnel — point the bot at that host's `/api/messages` path.
> A local-only endpoint will not receive Bot Framework activities.

### Configure environment

Add to `.env` (use the values from the app registration — no personal tenant or
domain values belong in this skill):

```bash
TEAMS_APP_ID=<your-application-client-id>
TEAMS_APP_PASSWORD=<your-client-secret-value>
TEAMS_TENANT_ID=<your-directory-tenant-id>
```

Channels auto-enable when their credentials are present — no extra configuration
needed.

Sync to container environment:

```bash
mkdir -p data/env && cp .env data/env/env
```

The container reads environment from `data/env/env`, not `.env` directly.

## Phase 2: Registration (Future)

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

> **Expected while this skill is a stub:** the smoke test will fail with a
> "channel not registered / adapter not found" result until `@deus-ai/teams-mcp`
> ships and the Teams channel factory is wired. That failure is the documented
> state today, not a misconfiguration.

Once the package is available and the smoke test passes, tell the user
"Microsoft Teams channel is working." Then ask them to send a test message:

> Send a message in your registered Teams channel to confirm real-time delivery.
> - For main channel: any message works
> - For non-main: @mention the bot

## Troubleshooting

### Bot not responding

1. Check `TEAMS_APP_ID`, `TEAMS_APP_PASSWORD`, and `TEAMS_TENANT_ID` are set in
   `.env` AND synced to `data/env/env`.
2. Check the channel is registered:
   `sqlite3 store/messages.db "SELECT * FROM registered_groups WHERE jid LIKE 'teams:%'"`
3. For non-main channels: the message must include the trigger pattern.
4. Service is running: `launchctl list | grep deus`.

### Bot connected but not receiving messages

1. Verify the messaging endpoint in the Azure Bot resource is your **public**
   `https://.../api/messages` URL and is reachable from the internet.
2. Verify the **Microsoft Teams** channel is enabled on the bot resource.
3. Verify the Teams app (manifest) referencing the bot is installed in the
   target team/chat.
4. Check the ingress tunnel is up — a Bot Framework activity to an unreachable
   endpoint is silently dropped by Azure.

### Authorization / 401 from the Bot Framework

1. Confirm the client secret **value** (not the secret ID) is in
   `TEAMS_APP_PASSWORD`, and that it has not expired (Azure secrets expire).
2. Confirm `TEAMS_TENANT_ID` matches the app registration's directory.
3. Regenerate the secret in **Certificates & secrets** if in doubt, update
   `.env`, sync `data/env/env`, and restart:
   `launchctl kickstart -k gui/$(id -u)/com.deus`.

## After Setup

The Microsoft Teams channel will support:

- **Team channels** — the bot must be installed in the team and @mentioned.
- **Group chats** — the bot must be added to the chat.
- **1:1 chats** — users can message the bot directly.
- **Multi-channel** — can run alongside WhatsApp or other channels (auto-enabled
  by credentials).

## Known Limitations

- **Public endpoint required** — unlike Slack's Socket Mode, the Bot Framework
  pushes activities to a public HTTPS endpoint. Without the ingress tunnel
  exposing `/api/messages`, the channel cannot receive messages.
- **Threads / replies** — Teams reply chains will be delivered as flat messages
  unless thread-aware routing is added (database schema, `NewMessage` type, and
  `Channel.sendMessage` interface changes), the same limitation the Slack channel
  documents.
- **Adaptive Cards / attachments** — only text content is forwarded to the agent
  in the initial integration. Adaptive Cards, files, and images are not handled.
- **Secret expiry** — Azure client secrets expire; the channel will start
  failing auth when the secret lapses, with no proactive warning until the
  credential-probe maintenance script is extended to cover Teams.
