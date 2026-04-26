---
governs:
  - src/
  - setup/
  - packages/
last_verified: "2026-04-26" # close-agnostic-debt
test_tasks:
  - "Deploy a hotfix to a running service and restart it after rebuilding dist/"
  - "Rebuild the WhatsApp MCP package and pick up the change live"
  - "Update a long-lived config value and restart the service to pick it up"
  - "Ship a dist/ fix after a failed deploy where the old binary is still running"
---
# Pattern: deployment

## Critical: service runs dist/, not source

A merged PR does **not** auto-rebuild. Always run `npm run build` before restarting.

```bash
npm run build && launchctl kickstart -k gui/$(id -u)/com.deus  # macOS
npm run build && systemctl --user restart deus                  # Linux
```

Verify: `stat dist/index.js` mtime should be newer than the service startup timestamp in `logs/deus.log`.

## MCP package build

Changes to `packages/mcp-*/` require a separate build — `npm run build` only rebuilds `src/`. Run `npx tsc` inside the specific package:

```bash
cd packages/mcp-whatsapp && npx tsc
```

Missing this step causes the service to silently run stale MCP code with no error.

## Process manager rule

**Use exactly one process manager per platform.** Never mix (e.g., running both pm2 and launchd). Mixing causes orphan processes that hold ports, leading to `EADDRINUSE` on the credential proxy (port 3001).

```bash
# macOS — launchd only
launchctl bootout gui/$(id -u)/com.deus                                   # stop
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.deus.plist   # start

# Linux — systemd only
systemctl --user stop deus    # stop
systemctl --user start deus   # start
```

## Container build cache

`--no-cache` alone does **NOT** invalidate COPY steps — the builder's volume retains stale files. To force a truly clean rebuild, prune the builder first:

```bash
docker builder prune -f && ./container/build.sh
```

## Credentials rule

Never write rotating credentials (OAuth tokens, short-lived session keys) to `.env`. Read them dynamically at request time from the OS credential store (macOS Keychain, Linux libsecret, Windows Credential Manager) with `~/.claude/.credentials.json` as fallback. `.env` is for **static, long-lived secrets only** (API keys, bot tokens like `TELEGRAM_BOT_TOKEN`, model names).

## Config file locations

Different components read config from different locations. Getting this wrong causes **silent failures** (keys not loaded, wrong model used):

| Component | Config source |
|-----------|---------------|
| Main process (`src/`) + evolution layer | Project root `.env` |
| Setup / startup gate | `~/.config/deus/.env` |
| Memory indexer | `~/.config/deus/.env` |
| Credential proxy | OS credential store → `~/.claude/.credentials.json` fallback (dynamic — never `.env`) |

**Common mistake:** Putting a key in `~/.config/deus/.env` but expecting the main process or evolution layer to find it — both read from the project root `.env`.

## Verifying the service is alive

The service has no HTTP health endpoint. Use these to confirm it's actually processing:

```bash
launchctl list | grep deus        # non-empty PID = running
tail -f logs/deus.log             # watch for "Deus running" after restart
sqlite3 store/messages.db "SELECT MAX(created_at) FROM messages;"  # confirms messages flowing
```

A process can be running (PID present) but stuck — a recent `MAX(created_at)` confirms end-to-end health.

## Silent drop: dist/src drift

After deploying a code fix, the service may still run the old binary if `dist/` was not rebuilt. Symptom: fix appears to deploy but has no effect. Fix: `npm run build`, then verify `stat dist/index.js` timestamp is newer than the service start time in `logs/deus.log`.

## Scope

Any change to `src/`, `setup/`, or `packages/` requires a rebuild before the change is live.
