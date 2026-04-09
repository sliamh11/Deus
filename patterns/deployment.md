---
governs:
  - src/
  - setup/
  - dist/
---
# Pattern: deployment

## Critical: service runs dist/, not source

A merged PR does **not** auto-rebuild. Always run `npm run build` before restarting.

```bash
npm run build
launchctl kickstart -k gui/$(id -u)/com.deus
```

Verify: `stat dist/index.js` mtime should be newer than the service startup timestamp in `logs/deus.log`.

## Credentials rule

Never write rotating credentials (OAuth tokens, short-lived keys) to `.env`. Read them dynamically at request time from their source file (e.g., `credentials.json`). `.env` is for static secrets only.

## Restart sequence

```bash
npm run build && launchctl kickstart -k gui/$(id -u)/com.deus
```

Stop only: `launchctl bootout gui/$(id -u)/com.deus`
Start only: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.deus.plist`

## Scope

Any change to `src/`, `setup/`, or `packages/` requires a rebuild before the change is live.
