# ADR-001: Hook Dispatch Service — Port :3002 + Observer Layer

> **⚠️ SCOPE CLARIFICATION (2026-06-07).** This `:3002` service is real but
> narrower than it reads: **container-only, `HOOK_DISPATCH_ENABLED` default-false,
> still triggered by Claude Code's own hooks** — not the host-side model-agnostic
> pipeline that `hook-dispatch-system.md` envisioned (that pipeline is unbuilt).
> Contradiction to flag: `hook-dispatch-system.md` says the Observer Layer
> `PreToolUse` cannot `deny`, but the shipped `pre-tool-use-hook.ts` (L54–63)
> DOES forward `decision: "block"` — the implementation chose blocking. See
> [hook-dispatch-facade-correction.md](hook-dispatch-facade-correction.md).

**Status:** Accepted
**Date:** 2026-05-23
**Scope:** `container/agent-runner/src/hook-dispatch-service.ts`, `container/agent-runner/src/pre-tool-use-hook.ts`, `container/agent-runner/src/post-tool-use-observer.ts`, `container/agent-runner/src/index.ts`
**Deciders:** Deus Engineering
**Relates to:** LIA-42 (Phase 2), LIA-41 / PR #456 (Phase 1 — Enforcement Layer)

---

## Context

Phase 1 (PR #456) shipped the Enforcement Layer: inline hook handlers in
`doom-loop-detector.ts`, `tool-audit.ts`, and `index.ts` (lines 813-830). All
existing hooks fire synchronously inside the agent process and are
co-located with the runner. There is no registered `PreToolUse` hook and no
surface external to the container that observers can subscribe to without
modifying the runner itself.

The credential proxy already occupies **port 3001** (`CREDENTIAL_PROXY_PORT`
in `memory-retrieval-hook.ts`). A new port is required for the hook dispatch
surface.

As Patterson notes in the observer pattern literature, the core problem with
tightly coupled event consumers is that each new observer forces a change at
the event source [1]. Phase 2 breaks hooks outward so external tooling (audit
daemons, security scanners, workflow engines) can subscribe to `PreToolUse`
and `PostToolUse` events over plain HTTP without forking or patching the agent
runner container.

---

## Decision

### 1. `HookDispatchService` on port `:3002`

A dedicated HTTP server (`src/hook-dispatch-service.ts`) listens on
`:3002` (env `HOOK_DISPATCH_PORT`, default `3002`) and exposes:

```
POST /hooks/:event
```

The service uses an internal observer registry (`registerObserver(event, cb)`)
and fans out every inbound payload to all registered callbacks via
`Promise.allSettled`, ensuring a single failing observer cannot block others.

**Port rationale — `:3001` / `:3002` split**

| Port | Service | Env var |
|------|---------|---------|
| 3001 | Credential proxy (memory bridge, token auth) | `CREDENTIAL_PROXY_PORT` |
| 3002 | Hook dispatch surface (pre/post tool events) | `HOOK_DISPATCH_PORT` |

Keeping the ports separate avoids routing ambiguity and allows each service to
evolve its auth, schema, and timeout policy independently.

### 2. `PreToolUse` hook — blocking, 4 s timeout

`src/pre-tool-use-hook.ts` uses the same fetch + `AbortController` timeout
pattern as `memory-retrieval-hook.ts` (line 12, `BRIDGE_TIMEOUT_MS = 4000`).
If the dispatch service returns `{ decision: "block" }` the hook forwards that
decision to the SDK, causing the tool call to be blocked before execution.
Any error or timeout degrades silently (returns `{}`).

### 3. `PostToolUse` observer — non-blocking fire-and-forget

`src/post-tool-use-observer.ts` initiates a `fetch` but does **not** await it.
The promise rejection is caught with `console.warn`. This mirrors the
non-blocking logging pattern already used for `tool-sizes.jsonl` (index.ts
lines 343-372).

**Why non-blocking for PostToolUse?**  
`PostToolUse` events carry the tool response — information the model has
already received. There is no value in blocking the agent turn to wait for
external observers to acknowledge receipt. Keeping it fire-and-forget guarantees
< 20 ms additional latency (measurable via existing `tool-sizes.jsonl` data).

`PreToolUse` **is** blocking because it is the only point where an external
observer can interject before the tool call reaches the model context.

### 4. Cold-start safety gate

`HOOK_DISPATCH_ENABLED` (default `false`) prevents the HTTP server from
binding during normal deployments. The service only starts when explicitly
opted in, eliminating port-conflict risk in environments that do not need
external hook observers.

### 5. Fan-out aggregation

When multiple observers are registered for the same event:
- `additionalContext` strings are concatenated with newlines.
- Other top-level response fields are merged (last-writer-wins for scalars).
- Rejected observer promises are logged as warnings and skipped.

This matches the SDK's `hookSpecificOutput` contract: multiple `additionalContext`
contributions are safe to merge because they are injected as additional context
into the model's next turn, not as structured data.

---

## Consequences

### Positive
- External tooling can subscribe to every tool call without touching the runner.
- `PreToolUse` blocking enables policy enforcement at the hook boundary (e.g.
  DLP scanners, secret-detection gates).
- `PostToolUse` fire-and-forget preserves turn latency.
- Degrade-silently pattern keeps the agent reliable even when `:3002` is down.

### Negative / Trade-offs
- A second open port per container increases the network attack surface.
- Fire-and-forget `PostToolUse` dispatch means observers may miss events if
  the container exits before the in-flight fetch completes. Acceptable for
  observability use cases; not acceptable for write-ahead audit requirements
  (those should use the existing `tool-audit.ts` synchronous path).
- `Promise.allSettled` fan-out means all observers are called sequentially
  within the await — for high-cardinality observer lists, latency accumulates.
  Mitigated by the 4 s hard timeout on `PreToolUse` hooks and by keeping the
  observer list short in practice.

---

## Alternatives Considered

### A. WebSocket subscription endpoint
Rejected: adds stateful connection management and reconnect complexity.
Plain HTTP POST is sufficient for the current observer use-cases.

### B. In-process callback registry only (no HTTP server)
Rejected: observers living inside the runner container defeats the purpose of
decoupling; they would require redeployment of the runner on every change.

### C. Shared message queue (Redis / NATS)
Rejected: out-of-scope dependency for Phase 2. The HTTP fan-out approach is
sufficient and keeps the service self-contained. A queue-backed transport can
be added in a future phase if throughput requires it.

---

## References

[1] Patterson, R. "Observer" in *Game Programming Patterns*. 2014.
    Discusses the observer pattern's role in decoupling event sources from
    consumers and the cost of tightly-coupled inline notification.
    URL: https://gameprogrammingpatterns.com/observer.html
    *(Note: The specific Patterson article referenced in issue LIA-42 should
    be confirmed against the issue thread; this citation is the closest match
    found during implementation.)*

[2] Deus ADR process — `docs/decisions/` convention.

[3] `src/memory-retrieval-hook.ts` — canonical silent-degradation + 4 s
    timeout pattern used throughout Phase 2 hooks.

[4] `src/doom-loop-detector.ts` lines 73-129 — reference PostToolUse hook
    implementation (Phase 1 Enforcement Layer).
