# Development Reference

> **Contributing?** Read [`docs/CONTRIBUTING.md`](CONTRIBUTING.md) for how to add channels, commands, and IPC types. Read [`docs/CROSS_PLATFORM.md`](CROSS_PLATFORM.md) before opening a PR — every change must work on macOS, Linux, and Windows.

## Key Files

| File | Purpose |
|------|---------|
| `src/index.ts` | Thin startup: DB init, channel connect, subsystem wiring |
| `src/message-orchestrator.ts` | Poll loop, trigger detection, cursor management, agent dispatch |
| `src/router-state.ts` | Mutable router state (timestamps, sessions, registered groups) |
| `src/container-mounter.ts` | Volume mount assembly (security-critical) |
| `src/container-runner.ts` | Container spawn, stdout streaming, evolution logging |
| `src/channels/registry.ts` | Channel registry (self-registration at startup) |
| `src/ipc.ts` | IPC watcher and task processing |
| `src/router.ts` | Message formatting and outbound routing |
| `src/config.ts` | Trigger pattern, paths, intervals |
| `src/task-scheduler.ts` | Runs scheduled tasks |
| `src/db.ts` | SQLite operations |
| `groups/{name}/CLAUDE.md` | Per-group memory (isolated) |
| `container/skills/agent-browser.md` | Browser automation tool (available to all agents via Bash) |

## Service Management

```bash
# macOS (launchd)
launchctl load ~/Library/LaunchAgents/com.deus.plist
launchctl unload ~/Library/LaunchAgents/com.deus.plist
launchctl kickstart -k gui/$(id -u)/com.deus  # restart

# Linux (systemd)
systemctl --user start deus
systemctl --user stop deus
systemctl --user restart deus
```

**Use exactly one process manager per platform.** macOS uses launchd (`com.deus.plist`), Linux uses systemd (`deus.service`). Never mix (e.g., running both pm2 and launchd). Mixing causes orphan processes that hold ports, leading to `EADDRINUSE` crash loops on the credential proxy (port 3001).

## Configuration Paths

Different components read config from different locations. Getting these wrong causes silent failures (keys not loaded, wrong model used).

| Component | Config Source | Example |
|-----------|-------------|---------|
| Main process (`src/`) | Project root `.env` (via `src/env.ts`, reads from `cwd()`) | `ASSISTANT_NAME`, `ASSISTANT_HAS_OWN_NUMBER` |
| Evolution layer (`evolution/`) | Project root `.env` (via `evolution/config.py`, line 13) | `GEMINI_API_KEY`, `EVAL_JUDGE` |
| Setup / startup gate | `~/.config/deus/` (config.json, .env) | `GEMINI_API_KEY`, vault path |
| MCP channel servers | Env vars passed by host (see `src/channels/mcp-*.ts`) | `WHATSAPP_AUTH_DIR`, `TELEGRAM_BOT_TOKEN` |
| Memory indexer (`scripts/`) | `~/.config/deus/.env` | `GEMINI_API_KEY` |
| Credential proxy | Dynamic read from `~/.claude/.credentials.json` | OAuth token (never put in `.env`) |

**Common mistake:** Putting a key in `~/.config/deus/.env` but expecting the evolution layer or main process to find it — both read from the project root `.env`. Check the source column above before adding config.

## Message Pipeline

Messages flow through these stages. Each stage can silently drop messages if misconfigured.

```
┌─────────────────────────────────────────────────────────────────────┐
│  Channel MCP Server (child process)                                 │
│  packages/mcp-{whatsapp,telegram,...}/                               │
│                                                                     │
│  Socket event (messages.upsert / bot.on) → provider.onMessage()     │
│  → sendLoggingMessage({ logger: 'incoming_message', data: msg })    │
│    ⚠ Requires capabilities: { logging: {} } on McpServer            │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ stdio (JSON-RPC notification)
┌──────────────────────────▼──────────────────────────────────────────┐
│  Host Process (src/)                                                │
│                                                                     │
│  MCP adapter (setNotificationHandler) → onMessage callback          │
│  → Sender allowlist check (silent drop if denied + logDenied=false) │
│  → storeMessage() [SQLite: store/messages.db]                       │
│  → Message polling loop (2s interval, getNewMessages)               │
│  → Trigger check (@Deus for non-main groups)                        │
│  → Container spawn (processGroupMessages → runContainerAgent)       │
└─────────────────────────────────────────────────────────────────────┘
```

**Silent drop points (most common):**

| Stage | Symptom | Cause |
|-------|---------|-------|
| sendLoggingMessage | Messages arrive at MCP child but host sees nothing | Missing `logging` capability on McpServer |
| Sender allowlist | Messages stored but never processed | Sender not in allowlist, `logDenied` is false |
| Trigger check | Messages stored and polled but not dispatched | Non-main group without `@Deus` prefix |
| dist/src drift | Code fix deployed but service still uses old binary | `npm run build` not run before restart |

## Troubleshooting

**WhatsApp not connecting after upgrade:** WhatsApp is now an MCP package in `packages/`. Run `/add-whatsapp` to install it. Existing auth credentials and groups are preserved.

**Messages sent but no response:**
1. Check `store/messages.db`: `SELECT MAX(timestamp) FROM messages;` — if stale, messages aren't reaching the DB.
2. Check `logs/deus.log` for any message-related entries after "Deus running".
3. Check `logs/deus.error.log` for channel connection status (WhatsApp/Telegram child process stderr).
4. If channel connects but no messages stored: verify MCP server has `capabilities: { logging: {} }` declared.
5. If messages stored but no response: check registered groups (`SELECT * FROM registered_groups;`) and trigger pattern.

**Service running stale code after a change:**
1. Run `npm run build` to recompile TypeScript to `dist/`.
2. For MCP packages: run `npx tsc` inside the specific `packages/mcp-*/` directory.
3. Restart: `launchctl kickstart -k gui/$(id -u)/com.deus` (macOS) or `systemctl --user restart deus` (Linux).
4. Verify: compare `stat dist/index.js` timestamp against startup log timestamp.

## Architecture Decisions (Quick Reference)

These decisions were made after real incidents and should **not be reverted** without reading the full ADR in `docs/decisions/`.

| Decision | Why | ADR |
|----------|-----|-----|
| IPC results via shared-volume files, not stdout | Docker pipe buffering causes deadlocks — permanent Docker constraint | [eval-ipc-file-output.md](decisions/eval-ipc-file-output.md) |
| In-memory eval cache only, no disk cache | Disk cache silently masks regressions across builds | [eval-no-disk-cache.md](decisions/eval-no-disk-cache.md) |
| Warm only active test datasets, not all | Universal warmup wastes ~3x time and saturates API rate limits | [eval-selective-warmup.md](decisions/eval-selective-warmup.md) |
| Channels optional, memory system is priority | Deus works as local Claude Code agent without messaging channels | [startup-gate.md](decisions/startup-gate.md) |

## Container Build Cache

The container buildkit caches the build context aggressively. `--no-cache` alone does NOT invalidate COPY steps — the builder's volume retains stale files. To force a truly clean rebuild, prune the builder then re-run `./container/build.sh`.

## Testing External Project Mode

Manual end-to-end test for the external project onboarding and session flow.

**Step 1 — Create a temp test project**

```bash
mkdir /tmp/test-project && cd /tmp/test-project && git init
```

**Step 2 — Run `deus` from that directory (first-run onboarding)**

```bash
cd /tmp/test-project && deus
```

Expected: Deus prints the memory level prompt (Full / Standard / Restricted) and asks about session summaries. Choose a level and confirm.

**Step 3 — Verify project config was created**

```bash
hash=$(echo -n "/tmp/test-project" | md5 -q 2>/dev/null || echo -n "/tmp/test-project" | md5sum | cut -d' ' -f1)
cat ~/.config/deus/projects/${hash}.json
```

Expected: JSON file with `path`, `name`, `description`, `memory_level`, `save_summaries`, `created_at`, `last_accessed`.

**Step 4 — Run `/project-settings` (show current settings)**

Inside the Claude session, run:
```
/project-settings
```

Expected: Displays project name, path, description (empty initially), detected project type (unknown for bare git repo), memory level with description, and session summaries status. Lists available commands.

**Step 5 — Run `/project-settings memory standard`**

```
/project-settings memory standard
```

Expected: Confirms memory level changed to `standard`. Reminds that new settings take effect on next session start.

**Step 6 — Run `/project-settings description "My test project"`**

```
/project-settings description "My test project"
```

Expected: Confirms description saved. Verify with `/project-settings show`.

**Step 7 — Run `/compress` — verify session saved and redacted**

```
/compress
```

Expected (standard mode): Session log saved to vault under `Session-Logs/YYYY-MM-DD/`. Then `redact_session.py` runs and reports how many sections were redacted (or "no changes needed"). Log should contain no fenced code blocks.

To verify redaction manually:
```bash
python3 ~/deus/scripts/redact_session.py /path/to/saved/log.md
```

Running it again should output "no changes needed" (idempotency check).

**Step 8 — Run `/checkpoint` — verify checkpoint created**

```
/checkpoint
```

Expected: Checkpoint written to vault `Checkpoints/YYYY-MM-DD-HH.md`. For standard mode, no code snippets in the Mid-Session State.

**Step 9 — Run `deus home` — verify returns to ~/deus**

Exit the session and run:
```bash
deus home
```

Expected: Deus starts from `~/deus` in home mode, not external project mode.

**Step 10 — Re-run `deus` from test project — verify IS_RETURNING=true**

```bash
cd /tmp/test-project && deus
```

Expected: No onboarding prompt this time. Deus greets with a brief project status (branch, recent commits). This confirms `last_accessed` was updated and the returning-user path is triggered.

**Step 11 — Clean up**

```bash
rm -rf /tmp/test-project
hash=$(echo -n "/tmp/test-project" | md5 -q 2>/dev/null || echo -n "/tmp/test-project" | md5sum | cut -d' ' -f1)
rm -f ~/.config/deus/projects/${hash}.json
```
