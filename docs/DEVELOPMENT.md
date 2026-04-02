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

## Troubleshooting

**WhatsApp not connecting after upgrade:** WhatsApp is now a separate channel fork, not bundled in core. Run `/add-whatsapp` (or `git remote add whatsapp https://github.com/qwibitai/nanoclaw-whatsapp.git && git fetch whatsapp main && (git merge whatsapp/main || { git checkout --theirs package-lock.json && git add package-lock.json && git merge --continue; }) && npm run build` — note: this URL is from the upstream NanoClaw fork`) to install it. Existing auth credentials and groups are preserved.

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
