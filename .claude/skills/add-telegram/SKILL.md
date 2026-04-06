---
name: add-telegram
description: Add Telegram as a channel. Can replace WhatsApp entirely or run alongside it. Also configurable as a control-only channel (triggers actions) or passive channel (receives notifications only).
---

# Add Telegram Channel

This skill adds Telegram support to Deus, then walks through interactive setup.

**IMPORTANT:** Do NOT add git remotes, fetch from external repos, or install npm packages from the public registry during this skill. All channel code is already in the repo under `packages/` and `src/channels/`.

## Phase 1: Pre-flight

### Check if already applied

Check if Telegram is already configured. If `TELEGRAM_BOT_TOKEN` exists in `.env`, skip to Phase 4 (Registration) or Phase 5 (Verify).

### Ask the user

Use `AskUserQuestion` to collect configuration:

AskUserQuestion: Do you have a Telegram bot token, or do you need to create one?

If they have one, collect it now. If not, we'll create one in Phase 3.

## Phase 2: Build Local Packages

The Telegram channel is a local MCP server in `packages/`. Build the packages in order (core first, then telegram):

```bash
cd packages/mcp-channel-core && npm install && npm run build && cd ../..
cd packages/mcp-telegram && npm install && npm run build && cd ../..
```

### Validate

```bash
npm run build
```

Build must be clean before proceeding.

## Phase 3: Setup

### Create Telegram Bot (if needed)

If the user doesn't have a bot token, tell them:

> I need you to create a Telegram bot:
>
> 1. Open Telegram and search for `@BotFather`
> 2. Send `/newbot` and follow prompts:
>    - Bot name: Something friendly (e.g., "Deus Assistant")
>    - Bot username: Must end with "bot" (e.g., "andy_ai_bot")
> 3. Copy the bot token (looks like `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`)

Wait for the user to provide the token.

### Configure environment

Add to `.env`:

```bash
TELEGRAM_BOT_TOKEN=<their-token>
```

Channels auto-enable when their credentials are present — no extra configuration needed.

Sync to container environment:

```bash
mkdir -p data/env && cp .env data/env/env
```

The container reads environment from `data/env/env`, not `.env` directly.

### Disable Group Privacy (for group chats)

Tell the user:

> **Important for group chats**: By default, Telegram bots only see @mentions and commands in groups. To let the bot see all messages:
>
> 1. Open Telegram and search for `@BotFather`
> 2. Send `/mybots` and select your bot
> 3. Go to **Bot Settings** > **Group Privacy** > **Turn off**
>
> This is optional if you only want trigger-based responses via @mentioning the bot.

### Build and restart

```bash
npm run build
```

Restart the service (platform-specific):
- macOS: `launchctl kickstart -k gui/$(id -u)/com.deus`
- Linux: `systemctl --user restart deus`
- Windows: `nssm restart deus` or `servy-cli restart --name=deus`

## Phase 4: Registration

### Get Chat ID

Tell the user:

> 1. Open your bot in Telegram (search for its username)
> 2. Send `/chatid` — it will reply with the chat ID
> 3. For groups: add the bot to the group first, then send `/chatid` in the group

Wait for the user to provide the chat ID (format: `tg:123456789` or `tg:-1001234567890`).

### Register the chat

The chat ID, name, and folder name are needed. Use `npx tsx setup/index.ts --step register` with the appropriate flags.

For a main chat (responds to all messages):

```bash
npx tsx setup/index.ts --step register -- --jid "tg:<chat-id>" --name "<chat-name>" --folder "telegram_main" --trigger "@${ASSISTANT_NAME}" --channel telegram --no-trigger-required --is-main
```

For additional chats (trigger-only):

```bash
npx tsx setup/index.ts --step register -- --jid "tg:<chat-id>" --name "<chat-name>" --folder "telegram_<group-name>" --trigger "@${ASSISTANT_NAME}" --channel telegram
```

## Phase 5: Verify

### Test the connection

Tell the user:

> Send a message to your registered Telegram chat:
> - For main chat: Any message works
> - For non-main: `@Deus hello` or @mention the bot
>
> The bot should respond within a few seconds.

### Smoke test

Run the automated smoke test to verify service, DB, and channel connection:

```bash
npx tsx setup/index.ts --step smoke-test -- --channel telegram
```

The smoke test checks: service running, registered group exists, DB write/read works, and channel connection appears in logs.

If the smoke test passes, tell the user "Telegram channel is working."

If it fails, check the STATUS output for the specific failure (service down, no registered group, DB error, or no log connection). Guide the user to fix the issue before proceeding.

After the automated check, also ask the user to send a test message to verify real-time delivery:

> Send a message to your registered Telegram chat to confirm real-time delivery.
> - For main chat: Any message works
> - For non-main: `@Deus hello` or @mention the bot

### Check logs if needed

```bash
tail -f logs/deus.log
```

## Troubleshooting

### Bot not responding

Check:
1. `TELEGRAM_BOT_TOKEN` is set in `.env` AND synced to `data/env/env`
2. Chat is registered in SQLite (check with: `sqlite3 store/messages.db "SELECT * FROM registered_groups WHERE jid LIKE 'tg:%'"`)
3. For non-main chats: message includes trigger pattern
4. Service is running: `launchctl list | grep deus` (macOS) or `systemctl --user status deus` (Linux)

### Bot only responds to @mentions in groups

Group Privacy is enabled (default). Fix:
1. `@BotFather` > `/mybots` > select bot > **Bot Settings** > **Group Privacy** > **Turn off**
2. Remove and re-add the bot to the group (required for the change to take effect)

### Getting chat ID

If `/chatid` doesn't work:
- Verify token: `curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe"`
- Check bot is started: `tail -f logs/deus.log`

## After Setup

If running `npm run dev` while the service is active:
```bash
# macOS:
launchctl unload ~/Library/LaunchAgents/com.deus.plist
npm run dev
# When done testing:
launchctl load ~/Library/LaunchAgents/com.deus.plist
# Linux:
# systemctl --user stop deus
# npm run dev
# systemctl --user start deus
```

## Agent Swarms (Teams)

After completing the Telegram setup, use `AskUserQuestion`:

AskUserQuestion: Would you like to add Agent Swarm support? Without it, Agent Teams still work — they just operate behind the scenes. With Swarm support, each subagent appears as a different bot in the Telegram group so you can see who's saying what and have interactive team sessions.

If they say yes, invoke the `/add-telegram-swarm` skill.

## Troubleshooting: Re-authentication

If the bot stops responding or the token becomes invalid:

1. Verify the token is still valid:
   ```bash
   curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" | grep -q '"ok":true' && echo "Token valid" || echo "Token invalid"
   ```
2. If the token is invalid, generate a new one via `@BotFather` > `/mybots` > select bot > **API Token** > **Revoke current token**
3. Update the token in `.env` and sync: `mkdir -p data/env && cp .env data/env/env`
4. Restart the service — no re-registration needed (the chat ID stays the same)

Common causes of bot failure:
- Token was revoked via BotFather
- Bot was deleted and recreated (new token needed, chat IDs may change for private chats)
- Telegram API rate limiting (wait a few minutes and retry)

## Removal

To remove Telegram integration:

1. Remove import from `src/channels/index.ts`
2. Remove `TELEGRAM_BOT_TOKEN` from `.env`
3. Remove Telegram registrations: `sqlite3 store/messages.db "DELETE FROM registered_groups WHERE jid LIKE 'tg:%'"`
4. Rebuild and restart
