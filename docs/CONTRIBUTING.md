# Contributing to Deus

This guide explains how to add new features to Deus without breaking existing behavior.
For cross-platform rules, see [CROSS_PLATFORM.md](CROSS_PLATFORM.md).
For architecture decisions, see [decisions/INDEX.md](decisions/INDEX.md).

---

## How to add a new messaging channel

A channel is a source of inbound messages and a sink for outbound responses (WhatsApp, Telegram, Slack, etc.).

**Files involved:**

| File | Purpose |
|------|---------|
| `src/channels/<name>.ts` | Channel implementation |
| `src/channels/index.ts` | Self-registration barrel |
| `src/channels/<name>.test.ts` | Unit tests |

**Steps:**

1. Implement the `Channel` interface from `src/types.ts`:

```typescript
// src/channels/myplatform.ts
import { registerChannel } from './registry.js';
import type { Channel, ChannelOptions } from './registry.js';

function createMyPlatformChannel(opts: ChannelOptions): Channel | null {
  const token = process.env.MYPLATFORM_BOT_TOKEN;
  if (!token) return null; // credentials missing → skip silently

  return {
    name: 'myplatform',
    async connect() { /* authenticate, register webhook/polling */ },
    async disconnect() { /* clean up */ },
    isConnected() { return /* ... */; },
    ownsJid(jid) { return jid.startsWith('mp:'); }, // unique JID prefix
    async sendMessage(jid, text) { /* send via platform API */ },
    // Optional:
    async setTyping(jid, isTyping) { /* ... */ },
    async syncGroups(force) { /* ... */ },
  };
}

registerChannel('myplatform', createMyPlatformChannel);
```

2. Add a barrel import in `src/channels/index.ts`:

```typescript
import './myplatform.js';
```

3. On inbound messages, call `opts.onMessage(jid, msg)` with a `NewMessage` object.
   Use a JID format unique to your platform (e.g., `mp:<chat_id>`).

4. Add credentials to `.env.example` with a comment explaining where to get them.

5. Add a setup skill at `.claude/skills/add-myplatform/SKILL.md` documenting the auth flow.

---

## How to add a new session command

Session commands are slash commands that users send from a messaging channel to control the agent (e.g., `/compact`, `/reset`, `/help`). They are intercepted before the agent runs.

**File:** `src/session-commands.ts`

**Steps:**

1. Find the `handleSessionCommand` function. Add your command to the `switch` or `if` chain:

```typescript
// In session-commands.ts, inside handleSessionCommand:
if (command === '/mycommand') {
  await deps.sendMessage('Processing...');
  // do the thing
  await deps.advanceCursor(cmdMsg.timestamp);
  return { handled: true, success: true };
}
```

2. Constraints:
   - Always call `deps.advanceCursor(cmdMsg.timestamp)` when the command succeeds, so the DB cursor advances past this message.
   - Return `{ handled: true, success: false }` if the command fails and the message should be retried.
   - If the command needs to run the agent, use `deps.runAgent(prompt, onOutput)` — do not call `runContainerAgent` directly.
   - Authorization: commands from non-control-group senders are already blocked by `isSessionCommandAllowed` before `handleSessionCommand` is called.

3. Add tests in `src/session-commands.test.ts`.

---

## How to add a new IPC message type

IPC messages are JSON files that the container agent writes to `/workspace/ipc/messages/` to request host-side actions (register a group, schedule a task, send a message, etc.).

**File:** `src/ipc.ts`

**Steps:**

1. Add a new type to the discriminated union in `ipc.ts`:

```typescript
// In the IpcMessage union:
| { type: 'my_action'; param1: string; param2?: number }
```

2. Add a handler in the `processIpcMessage` function:

```typescript
case 'my_action': {
  // validate required fields
  if (!msg.param1) {
    logger.warn('my_action: missing param1');
    return;
  }
  // do the action
  break;
}
```

3. Update the container-side type definitions in `container/agent-runner/src/ipc-types.ts` (kept in sync manually — see the SYNC-REQUIRED comment at the top of that file).

4. Add tests in `src/ipc.integration.test.ts`.

---

## How to add a new startup check

Startup checks run before the service accepts messages. They validate prerequisites and print warnings or fatals.

**File:** `src/startup-gate.ts`

**Steps:**

1. Implement the check function in `src/checks.ts` if it needs system inspection (credentials, files, processes). Return a typed result.

2. Register the check at the bottom of `src/startup-gate.ts`:

```typescript
registerStartupCheck({
  name: 'My check',
  level: 'warn',        // 'fatal' blocks startup; 'warn' allows degraded startup; 'suggest' is informational
  run: () => ({
    name: 'My check',
    level: 'warn',
    ok: myCheckFunction(),
    hint: 'What to do if this fails.',
  }),
});
```

3. Add tests in `src/startup-gate.test.ts`.

**Before changing `startup-gate.ts` or `checks.ts`:** read `docs/decisions/INDEX.md` — there are non-obvious constraints documented there.

---

## Architecture map

```
index.ts            — thin startup: DB init, channel connect, subsystem start
message-orchestrator.ts — poll loop, trigger detection, cursor management, agent dispatch
router-state.ts     — mutable router state (lastTimestamp, sessions, registeredGroups)
container-mounter.ts — volume mount assembly (security-critical, tested independently)
container-runner.ts — container spawn, stdout streaming, evolution logging, snapshots
container-runtime.ts — runtime abstraction (docker/podman binary, host gateway)
channels/           — messaging channel implementations (WhatsApp, Telegram, ...)
ipc.ts              — host-side IPC: handles agent → host requests
session-commands.ts — slash command interception (/compact, /reset, /help, ...)
startup-gate.ts     — prerequisite checks with check registry
db.ts               — SQLite persistence (messages, groups, sessions, tasks)
group-queue.ts      — per-group serialization queue + container lifecycle tracking
task-scheduler.ts   — cron/interval/once task scheduling loop
remote-control.ts   — Claude Code remote control session management
```

### Key invariants

- **`isControlGroup`**: A group with `isControlGroup: true` has no trigger requirement and can see all groups, all tasks, and the project root. There should be exactly one per deployment.
- **Cursor management**: `lastAgentTimestamp[jid]` tracks the last message sent to the agent. It is advanced before the agent runs and rolled back on error (unless output was already sent). Never advance the cursor without running the agent.
- **Session IDs**: Each group has an isolated Claude Code session stored in `sessions[folder]`. Session IDs are persisted to DB on every update.
- **IPC isolation**: Each group's IPC directory (`/workspace/ipc`) is namespaced by `group.folder`. Containers cannot read other groups' IPC files.

---

## Commit and PR conventions

```
feat(scope): add X        — new capability
fix(scope): fix Y         — bug fix
refactor(scope): extract Z — code restructure, no behavior change
test(scope): add tests for W
docs(scope): update D
```

Scope is the module name (e.g., `channels`, `ipc`, `startup-gate`, `container`, `orchestration`).

**Pre-PR checklist:**
- [ ] `npm run build` passes
- [ ] `npm test` passes (all 569+ tests)
- [ ] Cross-platform rules followed (see [CROSS_PLATFORM.md](CROSS_PLATFORM.md))
- [ ] ADR index consulted for changed modules (see [decisions/INDEX.md](decisions/INDEX.md))
- [ ] New credentials added to `.env.example` with comments (never in code)
