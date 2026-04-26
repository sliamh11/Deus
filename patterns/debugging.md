---
governs:
  - src/container-runner.ts
  - src/message-orchestrator.ts
last_verified: "2026-04-26" # close-agnostic-debt
test_tasks:
  - "Messages from a Telegram group arrive but the agent never responds"
  - "A container exits with code 137 instead of returning a result"
  - "WhatsApp ack messages are not appearing in the log stream"
  - "Debug why a container's working directory appears empty even though the host file exists"
---
# Pattern: debugging

## Diagnosis order

1. **Establish basic facts first** — check the simplest observable state before hypothesizing:
   - `pm2 logs --nostream` / `tail logs/deus.log` — is the service running?
   - `SELECT MAX(timestamp) FROM messages` — are messages being stored?
2. **Instrument boundaries, don't reason about code** — add one log line at each stage boundary, send one test input, read the logs. Definitively locates the break in 2 minutes.
3. **Read SDK source for silent failures** — check `node_modules/` before tracing the whole pipeline.
4. **Use `info` level** for temporary debug logs — service default is `info`; `debug` won't appear.

## Message pipeline (8 stages)

```
Channel → MCP child process (messages.upsert / bot.on)
  → sendLoggingMessage() [requires logging capability — #1 silent drop]
  → Host MCP adapter (setNotificationHandler)
  → onMessage callback (sender allowlist check)
  → storeMessage() [SQLite]
  → Message polling loop (getNewMessages)
  → Trigger check (@Deus for non-main groups)
  → Container spawn (processGroupMessages)
```

Each `→` is a potential silent drop. Instrument boundaries between stages.

**Common silent drops:**

| Stage | Symptom | Cause |
|-------|---------|-------|
| `sendLoggingMessage` | Messages arrive at MCP child but host sees nothing | Missing `logging` capability on McpServer |
| Sender allowlist | Messages stored but never processed | Sender not in allowlist, `logDenied` is false |
| Trigger check | Messages stored and polled but not dispatched | Non-main group without `@Deus` prefix |
| dist/src drift | Code fix deployed but service still uses old binary | `npm run build` not run before restart |

## Quick status check

```bash
launchctl list | grep deus          # PID = running, "-" = stopped
container ls --format '{{.Names}} {{.Status}}' 2>/dev/null | grep deus
grep -E 'ERROR|WARN' logs/deus.log | tail -20
```

## Known issues (open bugs — document, don't try to fix)

| Issue | Symptom | Root cause |
|-------|---------|-----------|
| **IDLE_TIMEOUT == CONTAINER_TIMEOUT** (both 30 min) | Containers always exit via SIGKILL (code 137), never graceful `_close` shutdown | Both timers fire simultaneously — idle timeout should be shorter (~5 min) so containers wind down between messages while container timeout stays as a safety net |
| **Cursor advanced before agent succeeds** | Messages permanently lost on container timeout | `processGroupMessages` advances `lastAgentTimestamp` before the agent runs; on timeout, retries find no messages (cursor already past them) |

These are known and open. Do not add workarounds without reading `docs/DEBUG_CHECKLIST.md` §Known Issues first.

## Async boundary instrumentation

When debugging a Promise chain failure: add one log at each `.then()` / `await` boundary, not inside the body. The failure is always between the last log that fired and the first that didn't. Instrument the boundaries, not the internals.

## Extra doc

Load `docs/DEBUG_CHECKLIST.md` for container timeout, mount issues, and WhatsApp auth commands.
