---
name: add-gmail
description: Add Gmail integration to Deus. Can be configured as a tool (agent reads/sends emails when triggered from WhatsApp) or as a full channel (emails can trigger the agent, schedule tasks, and receive replies). Guides through GCP OAuth setup and implements the integration.
---

# Add Gmail Integration

> **Status:** Coming soon — this channel will be available as `@deus-ai/gmail-mcp`. The MCP package is not yet available.

This skill will add Gmail support to Deus — either as a tool (read, send, search, draft) or as a full channel that polls the inbox — once the MCP package is released. In the meantime, the setup/config phases below describe what the integration will look like.

## Phase 1: Pre-flight (Future)

### Ask the user

Use `AskUserQuestion`:

AskUserQuestion: Should incoming emails be able to trigger the agent?

- **Yes** — Full channel mode: the agent listens on Gmail and responds to incoming emails automatically
- **No** — Tool-only: the agent gets full Gmail tools (read, send, search, draft) but won't monitor the inbox. No channel code is added.

## Phase 2: Setup (Future)

### GCP Project Setup

Tell the user:

> I need you to set up Google Cloud OAuth credentials:
>
> 1. Open https://console.cloud.google.com — create a new project or select existing
> 2. Go to **APIs & Services > Library**, search "Gmail API", click **Enable**
> 3. Go to **APIs & Services > Credentials**, click **+ CREATE CREDENTIALS > OAuth client ID**
>    - If prompted for consent screen: choose "External", fill in app name and email, save
>    - Application type: **Desktop app**, name: anything (e.g., "Deus Gmail")
> 4. Click **DOWNLOAD JSON** and save as `gcp-oauth.keys.json`
>
> Where did you save the file? (Give me the full path, or paste the file contents here)

If user provides a path, copy it:

```bash
mkdir -p ~/.gmail-mcp
cp "/path/user/provided/gcp-oauth.keys.json" ~/.gmail-mcp/gcp-oauth.keys.json
```

If user pastes JSON content, write it to `~/.gmail-mcp/gcp-oauth.keys.json`.

### OAuth Authorization

Tell the user:

> I'm going to run Gmail authorization. A browser window will open — sign in and grant access. If you see an "app isn't verified" warning, click "Advanced" then "Go to [app name] (unsafe)" — this is normal for personal OAuth apps.

Run the authorization:

```bash
npx -y @gongrzhe/server-gmail-autoauth-mcp auth
```

If that fails (some versions don't have an auth subcommand), try `timeout 60 npx -y @gongrzhe/server-gmail-autoauth-mcp || true`. Verify with `ls ~/.gmail-mcp/credentials.json`.

## Verify

### Smoke test

Run the automated smoke test to verify service, DB, and channel connection:

```bash
npx tsx setup/index.ts --step smoke-test -- --channel gmail
```

The smoke test checks: service running, registered group exists, DB write/read works, and channel connection appears in logs.

If the smoke test passes, tell the user "Gmail channel is working."

If it fails, check the STATUS output for the specific failure (service down, no registered group, DB error, or no log connection). Guide the user to fix the issue before proceeding.

After the automated check, also ask the user to send a test email to verify real-time delivery:

> Send a test email to confirm real-time delivery. Check `logs/deus.log` for processing confirmation.

## Troubleshooting

### Gmail connection not responding

Test directly:

```bash
npx -y @gongrzhe/server-gmail-autoauth-mcp
```

### OAuth token expired

Re-authorize:

```bash
rm ~/.gmail-mcp/credentials.json
npx -y @gongrzhe/server-gmail-autoauth-mcp
```

### Container can't access Gmail

- Verify `~/.gmail-mcp` is mounted: check `src/container-runner.ts` for the `.gmail-mcp` mount
- Check container logs: `cat groups/main/logs/container-*.log | tail -50`

### Emails not being detected (Channel mode only)

- By default, the channel polls unread Primary inbox emails (`is:unread category:primary`)
- Check logs for Gmail polling errors
