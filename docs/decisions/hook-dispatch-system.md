# ADR: Model-Agnostic Hook Dispatch System

**Status:** Accepted
**Date:** 2026-05-14
**Scope:** `src/message-orchestrator.ts`, `.claude/hooks/`, `scripts/codex_warden_hooks.py`, CLI (asymmetric by model)
**Wardens consulted:** Brainstormer (2 rounds), Architecture Snapshot, Threat Modeler (BLOCK resolved), Plan Agent, Advisor

## Context

Deus's 15+ hooks (quality gates, memory retrieval, safety checks, context injection) only work with Claude Code sessions. When users run Deus with OpenAI/Codex backend, local models, or any future backend, zero hooks fire. The hooks need to become model-agnostic for both container agents and CLI sessions.

**CLI strategy (asymmetric by model):** Claude CLI sessions stay on `claude` interactive (subscription-covered, unlimited, CC hooks). Non-Claude CLI sessions (OpenAI, Ollama, Qwen, future) run the Agent SDK on host with Deus HookPipeline (no subscription concern -- these use API keys or run locally). This gives every model hooks without touching the Claude subscription.

Note: Codex CLI has the same subscription/API split as Claude (ChatGPT auth = subscription allocation, API key = pay-per-token). But unlike Claude, Codex CLI has ZERO hook support, making the SDK path strictly better -- you gain hooks without losing anything. Claude is the only model where subscription economics justify keeping the CLI spawning approach.

**Auth defaults by backend:**
- Claude: defaults to subscription (interactive CLI). Fallback: API key.
- Codex: defaults to API key (SDK with Deus hooks). Fallback: subscription (degraded -- no hooks, warn user).
- Ollama/local: no auth needed.
- Other API models: API key only.

When a user configures Codex, Deus prompts for an OpenAI API key by default. If declined, falls back to subscription but displays a warning about degraded experience (no guardrails, no memory retrieval, no quality gates).

## Decision

Implement a Bridge-pattern hook system where Deus owns the hook contract and backends are dumb executors. Two layers with different trust models. For CLI, Claude stays on `claude` interactive (CC hooks); non-Claude CLIs run the Agent SDK on host with the same HookPipeline.

## Architecture

### Enforcement + Observer Layers

**Enforcement Layer (Host-Enforced, in orchestrator):**
- Fires in `message-orchestrator.ts` before/after container execution
- Events: `SessionStart`, `UserPromptSubmit`, `Stop`
- Container CANNOT opt out -- hooks run on the host before the container exists
- ALL security-critical hooks live here
- The host decides whether the container turn proceeds

**Observer Layer (Container-Cooperative, in agent loop):**
- Fires inside the container around tool execution
- Events: `PreToolUse` (observational + context), `PostToolUse` (telemetry)
- Container calls the HookDispatchService via HTTP bridge
- NOT for security enforcement -- a compromised container could skip the call
- Used for: memory retrieval, tool-size logging, tool audit, context injection
- `PreToolUse` CAN return `updatedInput` (rewrite tool args) but NOT `deny`

**Why this split:** The threat model identified an authority inversion -- if containers initiate security hook dispatch, a compromised container can skip it. The Enforcement Layer eliminates this by making security decisions host-initiated. The Observer Layer remains useful for non-security hooks where the container cooperates in good faith.

### Discriminated HookResult

```typescript
// Enforcement Layer results: can deny operations
interface EnforcementHookResult {
  continue: boolean;           // false = block the operation
  stopReason?: string;
  additionalContext?: string;  // injected into the prompt
}

// Observer Layer results: context and observation only
interface ObserverHookResult {
  additionalContext?: string;
  updatedInput?: Record<string, unknown>;  // rewrite tool args
}
```

No `permissionDecision` field anywhere. Enforcement uses `continue=false` to deny. Observer cannot deny. This eliminates the anti-pattern of "symmetric permission enforcement across backends."

### HookDispatchService (:3002)

Dedicated HTTP service, completely isolated from credential proxy (:3001). Transport is pluggable (HTTP first, interface-based so UDS/gRPC can replace later).

**What it handles:**
- Hook script execution for Observer Layer events dispatched from containers
- Memory queries (migrated from credential proxy)
- No access to API keys, OAuth tokens, or credential files

**Security controls (resolving threat model BLOCK):**

1. **Static hook allowlist:** Only hooks declared in config can be dispatched. Container requests reference hooks by event type, not by script path. The service determines which scripts fire.

2. **Input schema validation:** Per-event schemas enforced. Container CANNOT include arbitrary fields.

3. **Output sanitization:** `$HOME`, `$PROJECT_ROOT`, absolute host paths stripped from hook output before returning to container. Only structured fields pass through.

4. **Auth:** Reuses `DEUS_PROXY_TOKEN` per-group token, validated by `group-tokens.ts`.

5. **Rate limiting:** Per-group concurrency cap (max 5 concurrent dispatches). Excess gets 429. Keyed by validated group token.

6. **Port binding:** Binds to `PROXY_BIND_HOST` only. No external exposure.

### Hook Classification

| Hook | Target Layer | Rationale |
|------|-------------|-----------|
| plan-review-gate.sh | Enforcement | Security gate: must be host-enforced |
| code-review-gate.sh | Enforcement | Security gate: must be host-enforced |
| threat-model-gate.sh | Enforcement | Security gate: must be host-enforced |
| injection-scanner.ts | Enforcement | Already in the right place |
| path-leak-detector.sh | Enforcement | Safety check, host-only |
| code-review-invalidator.sh | Enforcement | Marker management, host-only |
| plan-mode-invalidator.sh | Enforcement | Marker management, host-only |
| plan-mode-session-init.sh | Enforcement | Session lifecycle, host-only |
| vault-context-hook.py | Enforcement | Context injection, host-only |
| standards-pack.py | Enforcement | Context injection, host-only |
| catchup-freshness.sh | Enforcement | Context injection, host-only |
| memory-cite.sh | Enforcement | Host filesystem check |
| memory-cite-seed.sh | Enforcement | Session lifecycle, host-only |
| orchestrator-preflight.sh | Enforcement | Already orchestrator-scoped |
| plan-revise-logger.sh | Enforcement | Logging, host-only |
| memory-retrieval.sh | **Observer** | Needs per-turn firing inside agent loop |
| sonnet-default-reminder.sh | **DROP** | Claude-specific, not model-agnostic |

**Result:** 14 of 15 hooks become Enforcement (host-enforced). Only memory-retrieval stays Observer. 1 dropped.

### HookPipeline Interface (Bridge Pattern)

```typescript
interface HookPipeline {
  // Enforcement Layer: fires in orchestrator, can deny
  enforce(
    event: 'SessionStart' | 'UserPromptSubmit' | 'Stop',
    context: HookContext,
    payload: Record<string, unknown>,
  ): Promise<EnforcementHookResult>;

  // Observer Layer: fires in container via HTTP bridge, cannot deny
  observe(
    event: 'PreToolUse' | 'PostToolUse',
    context: HookContext,
    payload: Record<string, unknown>,
  ): Promise<ObserverHookResult>;
}
```

Method names make the trust difference explicit. Part of `AgentRuntime` contract.

### Bridge Pattern in Code

```
Abstraction (Deus-owned)         Implementation (Backend-specific)
========================         =================================
HookPipeline interface    <----> Claude: SDK HookCallback adapter (~5 lines)
                          <----> OpenAI: executeBrokerTool() wrapper (~5 lines)
                          <----> Ollama/future: tool loop wrapper (~5 lines)

HookDispatcher (host)     <----> ShellHookAdapter (runs existing scripts)

HookDispatchService       <----> HookBridge (container HTTP client)
(host :3002)                     (container-side, fail-open)
```

Per-backend work is ~5 lines at the tool call chokepoint, not a full implementation. The hook logic (which scripts fire, how results are processed) is shared.

### Double-Enforcement (Claude Code sessions)

When using Claude backend, Claude Code's native hooks still fire inside the SDK. Enforcement Layer hooks ALSO fire in the orchestrator. This is defense-in-depth:
- Enforcement Layer catches issues before the container runs (definitive)
- Claude CC native hooks catch inside the agent loop (backup)
- Non-Claude backends: only Enforcement Layer fires (still fully protected)

### Default-Off

No `hooks.json` = zero hooks fire, zero overhead. Non-technical users are unaffected. The HookDispatchService still starts (serves memory queries) but hook dispatch returns empty results.

### CLI/Agent View (Asymmetric by Model)

**Claude CLI:** Spawns `claude` interactive. Subscription-covered (unlimited). Uses Claude Code's native hook system (`~/.claude/hooks/`) for per-tool-call enforcement. Migrating to SDK would shift to credit pool ($100-200/month cap) -- not acceptable.

**Non-Claude CLI (OpenAI, Ollama, Qwen, future):** Runs the Agent SDK directly on the host with Deus HookPipeline. No subscription concern (API key pay-per-token or local/free). Full hook control via the same `enforce()` + `observe()` interface used by container agents.

This means every model gets hooks in CLI:
- Claude: via CC hooks (existing, proven, subscription-friendly)
- Everything else: via Deus hooks running SDK on host (full control, no subscription constraint)

Codex CLI specifically has zero hook support, so the SDK path is strictly better -- you gain hooks without losing anything.

### Incremental Deployment

**Phase 1:** Interface + Enforcement Layer hooks in orchestrator. All 14 Enforcement hooks become model-agnostic for container agents. No container changes.

**Phase 2:** HookDispatchService + Observer Layer bridge. Memory retrieval works for OpenAI backend. Credential proxy loses memory route (reduced attack surface).

**Phase 3:** Configuration (per-group enable/disable), migration guide, documentation.

## Consequences

- All container agent hooks work across all backends (Claude, OpenAI, Ollama, future)
- Security hooks are host-enforced (cannot be bypassed by container)
- Credential proxy attack surface reduced
- Non-technical users see zero overhead
- Future backends get hooks by implementing ~5 lines at their tool call chokepoint
- CLI: Claude stays on CC hooks (subscription-friendly), non-Claude runs SDK with Deus hooks (full control)
