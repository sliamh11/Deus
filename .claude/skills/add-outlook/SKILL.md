---
name: add-outlook
description: Add Outlook (Microsoft 365 email) integration to Deus. Can be configured as a tool (agent reads/sends mail when triggered) or as a full channel that polls the inbox. Uses the Microsoft Graph API and Azure AD OAuth.
disable-model-invocation: true
---

# Add Outlook Integration

> **Status:** Available in-repo as `packages/mcp-outlook/` (`@deus-ai/outlook-mcp`). The published npm package is not out yet, so the channel factory falls back to the in-repo build at `packages/mcp-outlook/dist/index.js` — you just build it locally (below).

Outlook is the email-channel analog of Gmail, built on the **Microsoft Graph
API**. It polls the inbox for unread mail, forwards each conversation to the
agent, and replies in-thread. It also exposes tools (read, send, search, draft).

## Phase 1: Pre-flight

### Ask the user

Use `AskUserQuestion`:

AskUserQuestion: Should incoming emails be able to trigger the agent?

- **Yes** — Full channel mode: the agent listens on the Outlook inbox and
  responds to incoming emails automatically.
- **No** — Tool-only: the agent gets full Outlook tools (read, send, search,
  draft) but won't monitor the inbox. No channel code is added.

## Phase 2: Setup

### Azure AD app registration

Tell the user:

> I need you to register an Azure AD application for Microsoft Graph:
>
> 1. Open https://portal.azure.com → **Microsoft Entra ID** → **App
>    registrations** → **New registration**. Copy the **Application (client) ID**
>    and **Directory (tenant) ID**.
> 2. Go to **API permissions** → **Add a permission** → **Microsoft Graph** →
>    **Delegated permissions**, and add: `Mail.Read`, `Mail.Send`,
>    `Mail.ReadWrite`, `offline_access`, `User.Read`. Click **Grant admin
>    consent** (or have a tenant admin grant it).
> 3. Under **Authentication → Advanced settings**, enable **Allow public client
>    flows** (required for the device-code sign-in).
>
> Tell me the Application (client) ID and Directory (tenant) ID, or paste the
> values here.

Auth is delegated, device-code only (a user signs in once). The confidential
(clientSecret / app-only) flow is not supported.

Store the app credentials (no personal values belong in this skill — these go in
the user's local config):

```bash
mkdir -p ~/.outlook-mcp
# Written by setup: ~/.outlook-mcp/app-credentials.json
#   { "clientId": "...", "tenantId": "..." }
```

### Build the package + sign in

The published npm package is not out yet, so build the in-repo package and run a
one-time device-code sign-in:

```bash
cd packages/mcp-outlook && npm install && npm run build && cd ../..
node packages/mcp-outlook/dist/index.js auth
```

`auth` prints a URL and a short code — open the URL, sign in to your Microsoft
365 account, enter the code, and approve the requested mail permissions. It
caches the token to `~/.outlook-mcp/token.json` (with a refresh token so it
renews without re-prompting). Verify with `ls ~/.outlook-mcp/token.json`.

The channel auto-enables once `app-credentials.json` + `token.json` are present.
Restart the service so the host picks up the channel:

```bash
# macOS
launchctl kickstart -k gui/$(id -u)/com.deus
# Linux (systemd)
sudo systemctl restart deus
```

## Verify

### Smoke test

```bash
npx tsx setup/index.ts --step smoke-test -- --channel outlook
```

The smoke test checks: service running, registered group exists, DB write/read
works, and channel connection appears in logs.

If it passes, tell the user "Outlook channel is working." Then, for channel mode,
ask them to send a test email:

> Send a test email to your connected Outlook account to confirm real-time
> delivery. Check `logs/deus.log` for processing confirmation.

## Troubleshooting

### Outlook connection not responding

Test the Graph token directly:

```bash
curl -s -H "Authorization: Bearer $(jq -r .accessToken ~/.outlook-mcp/token.json)" \
  "https://graph.microsoft.com/v1.0/me" | jq '{displayName, mail, userPrincipalName}'
```

A `200` with your account details means the token is valid; a `401` means it
expired or lacks scope.

### OAuth token expired

Re-authorize (the refresh token should renew automatically; force a fresh
sign-in if it doesn't):

```bash
rm ~/.outlook-mcp/token.json
# re-run the device-code authorization
```

### "insufficient privileges" / missing scope

1. In **API permissions**, confirm `Mail.Read`, `Mail.Send`, `Mail.ReadWrite`,
   `offline_access`, and `User.Read` are present.
2. Click **Grant admin consent** — delegated permissions on some tenants require
   admin approval.
3. Delete `~/.outlook-mcp/token.json` and re-authorize so the new scopes are
   included in the token.

### Channel not connecting

- The Outlook MCP server runs **host-side** (spawned by the host process via
  stdio), so it reads `~/.outlook-mcp/` directly — there is no container mount.
- Confirm the package is built: `ls packages/mcp-outlook/dist/index.js`.
- Confirm both files exist: `ls ~/.outlook-mcp/app-credentials.json ~/.outlook-mcp/token.json`.
- Check `logs/deus.log` for `Outlook channel connected` / connect errors.

### Emails not being detected (Channel mode only)

- By default, the channel polls unread inbox messages:
  `/me/mailFolders/Inbox/messages?$filter=isRead eq false`.
- Check `logs/deus.log` for Graph polling errors (throttling returns HTTP 429
  with a `Retry-After` header).

## Known limitation

- **First reply to a pre-existing thread is skipped.** The reply target (a
  message id in the conversation) is cached only when the channel sees an inbound
  message during a poll. A reply to a thread that arrived before the channel
  started has no stored target, so it is logged and skipped rather than misdelivered.
