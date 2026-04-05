---
name: add-discord
description: Add Discord bot channel integration to Deus.
---

# Add Discord Channel

> **Status:** Coming soon — this channel will be available as `@deus-ai/discord-mcp`. The MCP package is not yet available.

This skill will add Discord support to Deus once the MCP package is released. In the meantime, the setup/config/registration phases below describe what the integration will look like.

## Phase 1: Setup (Future)

### Create Discord Bot (if needed)

If the user doesn't have a bot token, tell them:

> I need you to create a Discord bot:
>
> 1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
> 2. Click **New Application** and give it a name (e.g., "Deus Assistant")
> 3. Go to the **Bot** tab on the left sidebar
> 4. Click **Reset Token** to generate a new bot token — copy it immediately (you can only see it once)
> 5. Under **Privileged Gateway Intents**, enable:
>    - **Message Content Intent** (required to read message text)
>    - **Server Members Intent** (optional, for member display names)
> 6. Go to **OAuth2** > **URL Generator**:
>    - Scopes: select `bot`
>    - Bot Permissions: select `Send Messages`, `Read Message History`, `View Channels`
>    - Copy the generated URL and open it in your browser to invite the bot to your server

Wait for the user to provide the token.

### Configure environment

Add to `.env`:

```bash
DISCORD_BOT_TOKEN=<their-token>
```

Channels auto-enable when their credentials are present — no extra configuration needed.

Sync to container environment:

```bash
mkdir -p data/env && cp .env data/env/env
```

The container reads environment from `data/env/env`, not `.env` directly.

## Phase 2: Registration (Future)

### Get Channel ID

Tell the user:

> To get the channel ID for registration:
>
> 1. In Discord, go to **User Settings** > **Advanced** > Enable **Developer Mode**
> 2. Right-click the text channel you want the bot to respond in
> 3. Click **Copy Channel ID**
>
> The channel ID will be a long number like `1234567890123456`.

Wait for the user to provide the channel ID (format: `dc:1234567890123456`).

### Register the channel

The channel ID, name, and folder name are needed. Use `npx tsx setup/index.ts --step register` with the appropriate flags.

For a main channel (responds to all messages):

```bash
npx tsx setup/index.ts --step register -- --jid "dc:<channel-id>" --name "<server-name> #<channel-name>" --folder "discord_main" --trigger "@${ASSISTANT_NAME}" --channel discord --no-trigger-required --is-main
```

For additional channels (trigger-only):

```bash
npx tsx setup/index.ts --step register -- --jid "dc:<channel-id>" --name "<server-name> #<channel-name>" --folder "discord_<channel-name>" --trigger "@${ASSISTANT_NAME}" --channel discord
```

## Troubleshooting

### Bot not responding

1. Check `DISCORD_BOT_TOKEN` is set in `.env` AND synced to `data/env/env`
2. Check channel is registered: `sqlite3 store/messages.db "SELECT * FROM registered_groups WHERE jid LIKE 'dc:%'"`
3. For non-main channels: message must include trigger pattern (@mention the bot)
4. Service is running: `launchctl list | grep deus`
5. Verify the bot has been invited to the server (check OAuth2 URL was used)

### Bot only responds to @mentions

This is the default behavior for non-main channels (`requiresTrigger: true`). To change:
- Update the registered group's `requiresTrigger` to `false`
- Or register the channel as the main channel

### Message Content Intent not enabled

If the bot connects but can't read messages, ensure:
1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Select your application > **Bot** tab
3. Under **Privileged Gateway Intents**, enable **Message Content Intent**
4. Restart Deus

### Getting Channel ID

If you can't copy the channel ID:
- Ensure **Developer Mode** is enabled: User Settings > Advanced > Developer Mode
- Right-click the channel name in the server sidebar > Copy Channel ID

## After Setup

The Discord bot supports:
- Text messages in registered channels
- Attachment descriptions (images, videos, files shown as placeholders)
- Reply context (shows who the user is replying to)
- @mention translation (Discord `<@botId>` → Deus trigger format)
- Message splitting for responses over 2000 characters
- Typing indicators while the agent processes
