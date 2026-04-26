---
name: add-gcal
description: Add Google Calendar integration to Deus. Agents can list, create, update, and delete calendar events. Guides through GCP OAuth setup, token generation, keep-alive timer, and CLI command installation.
---

# Add Google Calendar

This skill adds Google Calendar tools to Deus container agents via the built-in `@deus-ai/gcal-mcp` package. Once set up, agents can list events, create meetings, check availability, and manage calendars from any channel.

## Phase 1: Pre-flight

### Check if already configured

```bash
ls integrations/gcal/credentials.json 2>/dev/null && echo "CREDS_EXIST" || echo "NO_CREDS"
ls integrations/gcal/tokens.json 2>/dev/null && echo "TOKENS_EXIST" || echo "NO_TOKENS"
ls packages/mcp-gcal/dist/index.js 2>/dev/null && echo "PACKAGE_BUILT" || echo "NOT_BUILT"
```

- If all three exist, skip to **Phase 4: Verify**.
- If tokens exist but are stale (older than 7 days), skip to **Phase 3: Authorize**.
- If credentials exist but no tokens, skip to **Phase 3: Authorize**.
- Otherwise, continue to Phase 2.

## Phase 2: GCP Project Setup

Tell the user:

> I need you to create Google Cloud OAuth credentials. This is a one-time setup:
>
> 1. Open https://console.cloud.google.com - create a new project or select an existing one
> 2. Go to **APIs & Services > Library**, search **"Google Calendar API"**, click **Enable**
> 3. Go to **APIs & Services > Credentials**, click **+ CREATE CREDENTIALS > OAuth client ID**
>    - If prompted for a consent screen: choose **External**, fill in app name ("Deus") and your email, save
>    - Application type: **Desktop app**, name: "Deus"
> 4. Click **DOWNLOAD JSON** and tell me where you saved it (or paste the contents)

Once the user provides the file path or JSON content:

```bash
mkdir -p integrations/gcal
```

If user gave a path:
```bash
cp "<user-provided-path>" integrations/gcal/credentials.json
```

If user pasted JSON, write it to `integrations/gcal/credentials.json`.

Verify the file is gitignored:
```bash
git check-ignore integrations/gcal/credentials.json || echo "WARNING: credentials.json is NOT gitignored"
```

## Phase 3: Authorize

### Build the gcal package (if not already built)

```bash
cd packages/mcp-gcal && npm install && npm run build && cd ../..
```

### Ensure googleapis is available for the auth script

```bash
npm list googleapis 2>/dev/null | grep -q googleapis || npm install --no-save googleapis
```

### Run the OAuth flow

Tell the user:

> I'll start the Google Calendar authorization. A URL will appear - open it in your browser, sign in with your Google account, and grant calendar access. Then paste the authorization code back here.

```bash
node scripts/setup-gcal-auth.mjs
```

This is interactive - the user must paste the code. The script saves tokens to `integrations/gcal/tokens.json` and verifies the connection.

If the script can't run interactively (e.g., in a container), tell the user:

> Run this in a separate terminal:
> ```
> node scripts/setup-gcal-auth.mjs
> ```
> Let me know when it's done.

### Verify tokens were created

```bash
ls -la integrations/gcal/tokens.json
```

## Phase 4: Verify

### Test the connection

```bash
deus gcal ping
```

Expected output: `gcal ping OK: <user-email>`

If `deus gcal` command is not found, the CLI may not have the latest version. Run:

```bash
deus auth
```

### Verify container detection

The container agent-runner auto-detects gcal when all three files exist:
- `packages/mcp-gcal/dist/index.js`
- `integrations/gcal/credentials.json`
- `integrations/gcal/tokens.json`

No manual container configuration is needed.

## Phase 5: Keep-alive Timer

The Google OAuth refresh token expires after ~7 days of inactivity. Install a daily keep-alive to prevent this:

```bash
cp setup/com.deus.gcal-keepalive.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.deus.gcal-keepalive.plist 2>/dev/null || launchctl kickstart gui/$(id -u)/com.deus.gcal-keepalive
```

Verify it's running:
```bash
launchctl list | grep gcal-keepalive
```

### For Linux

Create a systemd timer instead:

```bash
mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/deus-gcal-keepalive.service << 'EOF'
[Unit]
Description=Deus Google Calendar keep-alive

[Service]
Type=oneshot
ExecStart=/usr/local/bin/deus gcal ping
EOF

cat > ~/.config/systemd/user/deus-gcal-keepalive.timer << 'EOF'
[Unit]
Description=Daily Deus gcal keep-alive

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl --user enable --now deus-gcal-keepalive.timer
```

## Phase 6: Restart Deus

```bash
deus auth
```

Tell the user:

> Google Calendar is now connected. Your agents can list events, create meetings, and check availability from any channel.
>
> **CLI commands:**
> - `deus gcal` - show token status
> - `deus gcal auth` - re-authorize (if token expires)
> - `deus gcal ping` - test connection / keep token alive
>
> **From chat:** Just ask naturally - "What's on my calendar tomorrow?", "Schedule a meeting at 3pm", etc.

## Removal

1. Remove the keep-alive timer:
   ```bash
   launchctl bootout gui/$(id -u)/com.deus.gcal-keepalive 2>/dev/null
   rm ~/Library/LaunchAgents/com.deus.gcal-keepalive.plist
   ```

2. Remove tokens (keeps credentials for easy re-setup):
   ```bash
   rm integrations/gcal/tokens.json
   ```

3. Remove everything:
   ```bash
   rm -rf integrations/gcal/
   ```

4. Restart Deus:
   ```bash
   deus auth
   ```
