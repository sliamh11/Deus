---
name: add-outlook
description: Add Outlook (Microsoft 365 email) integration to Deus. Can be configured as a tool (agent reads/sends mail when triggered) or as a full channel that polls the inbox. Uses the Microsoft Graph API and Azure AD OAuth.
---

# Add Outlook Integration

> **Status:** Coming soon — this channel will be available as `@deus-ai/outlook-mcp`. The MCP package is not yet available. In the meantime, the setup/config phases below describe what the integration will look like, mirroring the Gmail skill.

Outlook is the email-channel analog of Gmail, built on the **Microsoft Graph
API**. It can run as a **tool** (read, send, search, draft) or as a full
**channel** that polls the inbox and lets incoming mail trigger the agent.

## Phase 1: Pre-flight (Future)

### Ask the user

Use `AskUserQuestion`:

AskUserQuestion: Should incoming emails be able to trigger the agent?

- **Yes** — Full channel mode: the agent listens on the Outlook inbox and
  responds to incoming emails automatically.
- **No** — Tool-only: the agent gets full Outlook tools (read, send, search,
  draft) but won't monitor the inbox. No channel code is added.

## Phase 2: Setup (Future)

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
> 3. For the OAuth flow, either add a **client secret** (Certificates & secrets)
>    for the confidential flow, or enable the **device code flow** under
>    **Authentication → Advanced settings → Allow public client flows**.
>
> Tell me the Application (client) ID and Directory (tenant) ID, or paste the
> values here. Do not paste a client secret into chat — set it in `.env`.

Store the app credentials (no personal values belong in this skill — these go in
the user's local config):

```bash
mkdir -p ~/.outlook-mcp
# Written by setup: ~/.outlook-mcp/app-credentials.json
#   { "clientId": "...", "tenantId": "...", "clientSecret": "..." (optional) }
```

### OAuth authorization

Tell the user:

> I'm going to run Outlook authorization using the device-code flow. I'll print a
> URL and a short code — open the URL, sign in to your Microsoft 365 account, and
> enter the code. If you see a consent screen, approve the requested mail
> permissions.

The authorization caches the token to `~/.outlook-mcp/token.json` (with the
`offline_access` refresh token so it renews without re-prompting). Verify with
`ls ~/.outlook-mcp/token.json`.

## Verify

### Smoke test

```bash
npx tsx setup/index.ts --step smoke-test -- --channel outlook
```

The smoke test checks: service running, registered group exists, DB write/read
works, and channel connection appears in logs.

> **Expected while this skill is a stub:** the smoke test will fail with a
> "channel not registered / adapter not found" result until `@deus-ai/outlook-mcp`
> ships and the Outlook channel factory is wired. That failure is the documented
> state today, not a misconfiguration.

Once the package is available and the smoke test passes, tell the user "Outlook
channel is working." Then, for channel mode, ask them to send a test email:

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

### Container can't access Outlook

- The `~/.outlook-mcp` credentials directory must be mounted into the container
  for the agent to use Outlook tools. That mount is added when
  `@deus-ai/outlook-mcp` ships and the channel is wired — it does not exist yet
  in this stub.
- Check container logs: `cat groups/main/logs/container-*.log | tail -50`.

### Emails not being detected (Channel mode only)

- By default, the channel polls unread inbox messages:
  `/me/mailFolders/Inbox/messages?$filter=isRead eq false`.
- Check `logs/deus.log` for Graph polling errors (throttling returns HTTP 429
  with a `Retry-After` header).
