# Spike: warden veto through LangChain's `wrapToolCall` middleware (LIA-395 / A2)

**Date:** 2026-07-14 · **Verdict: PASS (framework-mechanism)** — blocking
enforcement, allow-once semantics, and model-visible feedback all proven
deterministically against the real, unmodified `codex_warden_hooks.py` gate
script. No piece of this proof needed a live model credential. Real-provider
serialization stays unverified — see Scope below.

## Question

Can LangChain JS's real `wrapToolCall` middleware contract — not a Decorator
around `.invoke()`, the actual `createMiddleware({ wrapToolCall })` hook
`createAgent` calls into on every tool invocation — enforce an **unmodified**
Deus warden gate (`scripts/codex_warden_hooks.py run plan-review-gate`)? This
is A2 of the Linear-tracked MA milestone (LIA-394..400), continuing from A1
(LIA-394, PR #1031, still open/unmerged). Three things must hold:

1. A denied tool call never reaches the wrapped tool's real execute function.
2. An allowed tool call reaches it exactly once.
3. The model sees the gate's actual denial text as a `ToolMessage`, and the
   agent loop continues using that feedback.

## Method

### Reuse target — unmodified

`scripts/codex_warden_hooks.py run plan-review-gate --repo-root <path>` is
the exact mechanism that gated every `Edit`/`Write` in the session that
produced this spike. Nothing in this file modifies it; `invokeWardenGate`
below only spawns it as a subprocess and interprets its stdout/exit code.

### `invokeWardenGate` — subprocess wrapper, fail-closed for subprocess/protocol failures

Spawns `<PYTHON_BIN> <WARDEN_SCRIPT> run <gateName> --repo-root <repoRoot>`
(both `PYTHON_BIN` and the absolute `WARDEN_SCRIPT` path are resolved via
`src/platform.ts` and `import.meta.url` respectively — no hardcoded
`python3`, no relative script path that would break once a scratch repo's
`cwd` differs from this file's own location). Writes the constructed hook
event as JSON to stdin, waits for the child to `close`, and interprets:

- Nonzero exit, spawn failure, a signal-terminated child, or a timeout → a
  **hard error** (throw), never treated as allow.
- Exit 0 with empty/whitespace stdout → `{ decision: 'allow' }`.
- Exit 0 with non-empty stdout that parses as the gate's real deny JSON
  shape (`hookSpecificOutput.hookEventName === 'PreToolUse'`,
  `permissionDecision === 'deny'`, a non-empty string
  `permissionDecisionReason`) → `{ decision: 'deny', reason }`.
- Any other shape (malformed JSON, missing fields, empty reason) → a **hard
  error**, never a silent allow.

**Honest scope limitation, not a bug in this wrapper:** the *unmodified* gate
script itself has pre-existing fail-open paths — malformed stdin silently
becomes `{}`, and a failed `git` invocation returns `None` and is
subsequently treated permissively. Both produce a normal exit-0-empty-stdout
"allow" indistinguishable from a legitimate one at the subprocess boundary.
This wrapper cannot and does not paper over that; fixing it would be new
scope against a script this spike's whole premise is to reuse unmodified.

### `createWardenGateMiddleware` — real `wrapToolCall`, not a Decorator

```ts
createMiddleware({
  name: 'WardenGate',
  wrapToolCall: async (request, handler) => {
    const toolCallId = request.toolCall.id;
    if (!toolCallId?.trim()) throw new Error('Warden-denied tool call has no tool-call ID');
    const decision = await invokeGate(gateName, toEvent(request), repoRoot);
    if (decision.decision === 'deny') {
      return new ToolMessage({ content: decision.reason, tool_call_id: toolCallId,
        name: request.toolCall.name, status: 'error' });
    }
    return handler(request);
  },
})
```

This is passed to `createAgent({ middleware: [...] })` — the same contract
`createAgent` uses for every tool call, not a wrapper applied around one
specific tool instance. `invokeGate` is an injectable seam (default: the
real `invokeWardenGate`) so the "gate is never called" assertion on the
missing-ID path uses a real mock, not a `vi.spyOn` against a module-local
binding that Vitest's ESM transform doesn't reliably intercept.

### Synthetic tool — no path argument, no redirection surface

`makeScratchEditTool(scratchFilePath)` has schema `{ content: string }`
only — closes over the write target at construction time. Empirically
confirmed (not assumed): LangChain's own schema validation
(`additionalProperties: false`) rejects a call carrying a forged extra
`path`/`file_path` property *before* it ever reaches the execute function —
a stronger guarantee than "the code happens not to read it." Scratch repo is
a throwaway `git init`'d `mkdtemp` directory, torn down in every test's
`afterEach`; never the real project worktree.

## What was built

- `scripts/spikes/lia395_warden_veto_walking_skeleton.ts` — `invokeWardenGate`,
  `createWardenGateMiddleware`, `makeScratchEditTool`, scratch-repo helpers,
  and a `main()` demo (blocked path + allowed path, printed transcript).
- `scripts/spikes/lia395_warden_veto_walking_skeleton.test.ts` — 13 tests, all
  passing on first real run against the actual gate script:
  - Real-gate blocked/allowed paths (subprocess, no mocking of the gate).
  - 6 subprocess/protocol fail-closed cases: nonzero exit, spawn failure,
    signal termination, timeout, malformed stdout, deny-with-empty-reason —
    each via an injected `spawnFn` seam, since the real gate script can't be
    made to time out or die by signal on demand.
  - Middleware blocked/allowed paths through a real `createAgent` +
    `FakeToolCallingModel` loop (execute-spy call counts, deny/allow content).
  - The fixed-path negative test (forged extra argument rejected by schema).
  - Missing/empty tool-call-ID ordering (gate mock + handler both zero calls).
  - Model-visible-feedback: a spy on `FakeToolCallingModel.prototype._generate`
    proves the model's **second invocation** actually received the denied
    `ToolMessage` (matching content, `tool_call_id`, `status: 'error'`) — not
    just that the agent's final state contains one.
- `package.json`/`vitest.config.ts`/`package-lock.json` — same
  `langchain`/`@langchain/core`/`@langchain/anthropic` devDependencies and
  spike-test-discovery glob A1 added, independently, since `origin/main`
  doesn't yet have A1's unmerged additions and A2 forks fresh off
  `origin/main` per the established per-slice convention (no branch stacking).

## Verdict

All three kill-switch criterion-1 requirements are proven deterministically,
with zero dependency on a live model credential:

1. **Blocking**: the wrapped tool's execute function is asserted **never
   called** on a denied call (via both a direct spy and, separately, the
   `FakeToolCallingModel` prototype spy) — proven against the real,
   unmodified gate script, not a stub.
2. **Allow-once**: the wrapped tool's execute function is asserted **called
   exactly once** on an allowed call, and the scratch file's actual written
   content is checked, not just a call count.
3. **Model-visible feedback**: the fake model's second invocation is proven
   to have actually received the denied `ToolMessage` — the full
   `createAgent → wrapToolCall → ToolMessage → next model call` transport,
   not merely that the final agent state happens to contain one.

**Scope of this PASS:** it proves LangChain's own middleware transport
mechanism deterministically. It does **not** prove real-provider
serialization or how an actual Anthropic model reacts to the feedback text —
`ANTHROPIC_API_KEY` remains genuinely absent in this environment (same as
A1), so the optional live smoke test did not run. Nothing in this spike's
core claim depends on that smoke test, unlike A1's split — but a future
session with real credentials (or the subscription-billing route documented
in `Research/2026-07-14-deus-v2-subscription-billing-credential-proxy.md`)
should still run it before treating real-provider behavior as verified.

## Scope of this spike

Read-only proof-of-concept; nothing wired into production. No modifications
to `scripts/codex_warden_hooks.py` or any other reused mechanism. No
`.plan-reviewed` marker or `.warden-verdicts.json` was ever written into the
real `deus-v2` worktree by any test — confirmed via marker-mtime comparison
before/after the test run. Not merged to `main` — pushed as a PR for review
only.
