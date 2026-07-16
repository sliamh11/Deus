# deus-native CLI chat: thin client over a daemon-owned loopback endpoint

- **Status:** accepted
- **Date:** 2026-07-15
- **Scope:** `src/cli/deus-native-chat.ts`, `src/cli/deus-native-chat-server.ts`, `src/cli/deus-native-chat-client.ts`, `src/index.ts` (server startup/shutdown wiring), `src/odysseus-server.ts` (exports two existing auth helpers for reuse), `deus-cmd.sh`, `deus-cmd.ps1`
- **Ticket:** LIA-428 (G1)
- **Related:** `deus-v2-langchain-runtime.md`, `deus-v2-replay-safety.md`,
  `backend-neutral-agent-runtime.md`

## Decision

`deus chat` opens an interactive terminal conversation on the registered
`deus-native` `AgentRuntime` through a **thin client**: the short-lived CLI
process (`src/cli/deus-native-chat-client.ts`) sends authenticated loopback
HTTP requests to a small chat endpoint that the **daemon** starts
(`src/cli/deus-native-chat-server.ts`, wired in `src/index.ts` after the
runtime registry is initialized). The runtime, credential proxy, group-token
state, and SQLite session store all stay in the daemon process.

## Why a second in-process CLI runtime is invalid

Instantiating `DeusNativeRuntime` (plus `startCredentialProxy`/
`startToolProxy`) inside the CLI process was rejected because:

- group tokens are **process-local** (`src/group-tokens.ts`): a token minted
  by a second process would be rejected by the daemon's credential proxy;
- the credential proxy owns fixed port 3001 — a second proxy collides with
  the daemon;
- booting the full host just to chat would duplicate channels, schedulers,
  webhooks, and IPC watchers.

## Transport and credential boundary

- The daemon listens on `127.0.0.1` with an **OS-assigned ephemeral port**
  (no new fixed-port/config surface) and generates a fresh
  cryptographically random bearer token per daemon start.
- Every route requires a constant-time bearer match (the exact pattern of
  `src/odysseus-server.ts`; the two helpers are shared, not duplicated).
  Loopback alone is insufficient — any local process can connect, and this
  endpoint can drive the host-side agent loop. The auth boundary is pinned
  by `src/cli/deus-native-chat-server.oracle.test.ts` (`@oracle`, derived
  from the plan spec; must not be weakened by implementers).
- Discovery: the server atomically writes
  `~/.config/deus/native-chat.json` (`{version, pid, host, port, token}`,
  user-only `0600`) **after** it is listening, and on shutdown removes only
  a record it still owns (a successor daemon's record survives). On
  Windows the profile-directory ACL plus the random token is the boundary;
  if review ever finds that insufficient, replace the transport/discovery
  implementation with a same-user named-pipe ACL without touching the
  controller/client seam.
- A missing/stale/version-mismatched record or failed auth fails **closed**
  with an actionable "rebuild/restart the service" message. There is
  deliberately no fallback that starts another proxy or reads provider
  credentials in the client.

## Fixed synthetic session identity

The controller (`src/cli/deus-native-chat.ts`) uses
`CLI_CHAT_GROUP_FOLDER = "deus-native-cli"` / `CLI_CHAT_JID = "cli:deus-native"`:
an unregistered synthetic group, so `resolveGroup` returns `undefined`
(safe non-publicIngress path in `deus-native-backend.ts`) and the CLI
session can never overwrite a channel group's backend-scoped session row.
`isControlGroup` is `false`. One resumable CLI thread exists by design;
named/multiple sessions are future UX (G-series) decisions. Sessions are
read/written **backend-scoped** (`db.getSession(folder, 'deus-native')`);
`/exit` never calls `clearSession` — resume depends on the stored row.

This deliberately does **not** reuse the Odysseus `/v1/chat/completions`
semantics: that endpoint uses fresh non-persisted sessions and a control
group, which conflicts with the persisted synthetic CLI session.

## Rollback

Remove the `chat` arms in `deus-cmd.sh`/`deus-cmd.ps1`, the
`startNativeChatServer` call + shutdown close in `src/index.ts`, and the
`src/cli/deus-native-chat*` modules; delete any leftover
`~/.config/deus/native-chat.json`. No schema or session-row migration is
involved.
