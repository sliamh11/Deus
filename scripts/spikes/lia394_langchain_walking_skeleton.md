# Spike: LangChain `createAgent` walking skeleton reusing tool-broker.ts (LIA-394 / A1)

**Date:** 2026-07-14 · **Verdict: adapter + enforcement logic implemented and
unit-tested; live end-to-end multi-tool-call proof BLOCKED on missing
`ANTHROPIC_API_KEY` credential in this environment** — needs a future
session with real credentials to complete.

## Question

Can LangChain JS's `createAgent` actually drive a multi-turn tool-calling
loop using Deus's existing `container/agent-runner/src/tool-broker.ts` tool
definitions and execution logic, **unmodified**? This is A1 of the
Linear-tracked MA milestone (LIA-394..400) of the "Deus V2 — Base Harness
Migration" project — a walking-skeleton proof, not a production integration.

## Method

### Why relocated out of `container/agent-runner/`

`container/Dockerfile:46` does `COPY container/agent-runner/ ./` — anything
placed there ships into the real container image, where a "no raw secrets"
credential-boundary invariant applies per
`docs/decisions/backend-neutral-agent-runtime.md` (Accepted ADR: "Backend
adapters must not read raw host secrets. They receive placeholder
credentials and route through the credential proxy"). This spike
deliberately runs host-side-only, outside that ADR's declared scope
(`src/agent-runtimes/`, `container/agent-runner/`, `deus-cmd.sh/ps1`,
`AGENTS.md`, `AI_AGENT_GUIDELINES.md`), using a personal `ANTHROPIC_API_KEY`
(already a documented opt-in var, commented out at `.env.example:34`) rather
than the credential-proxy route — proxy-routed billing is a distinct, later
concern (A4/LIA-397).

### Relationship to prior ADRs

`docs/decisions/multi-agent-orchestration-research.md` already evaluated and
**REJECTED** the "Runner on host, LLM on host" shape for `@openai/agents-js`
as a production architecture: *"Containers become tool sandboxes. Loses
Claude Agent SDK autonomous loop. **Rejected** — massive rewrite, loses core
strength"* (line 37), and also rejected the bridged variant: *"`Runner on
host, LLM in container (bridged)` ... **Rejected** — impedance mismatch"*
(line 38).

This spike deliberately revisits that same architectural shape (an
LLM-driving Runner living on the host) with a different library (LangChain
JS's `createAgent` instead of `@openai/agents-js`) and a narrower goal: this
is a **bounded, non-production research spike** gathering fresh evidence
under the Deus V2 migration's new constraints — it is not a silent reversal
of that rejection. It answers "can `createAgent` drive tool-calling through
`tool-broker.ts` unmodified at all?", not "should Deus's production agent
loop move host-side?" Nothing here is wired into the container image, the
production agent-runtime dispatch (`src/agent-runtimes/`), or any
user-facing path. If this spike's findings ever motivate a production
proposal to move the Runner host-side, that proposal requires its own ADR
explicitly superseding or reconciling with
`multi-agent-orchestration-research.md`'s rejection — this document is
evidence-gathering only, not that ADR.

### Why scoped to web_search + web_fetch only, not the full toolset

`resolveWorkspacePath` (`container/agent-runner/src/tool-broker.ts:200-219`)
hardcodes containment to five absolute container-only mount roots
(`/workspace/group`, `/workspace/project`, `/workspace/extra`,
`/workspace/vault`, `/workspace/global`) — every filesystem tool
(`read_file`, `write_file`, `edit_file`, `glob_files`/`grep_files` with
`base_path`) calls it unconditionally and throws `Path escapes the mounted
workspace` for any real host path (confirmed: `/` is read-only on the dev
machine, so satisfying the allowlist would need sudo/root-fs changes —
inappropriate for a spike). Separately, `runCommand` (backing `bash_exec`,
`tool-broker.ts:92-119`) spawns `/bin/bash -lc` with the full inherited
`process.env` and zero sandboxing — safe inside the container (the
container IS the sandbox) but unrestricted host command execution if wired
into a host-side LLM tool loop. `web_search`/`web_fetch` are the only two
tools that bypass `resolveWorkspacePath` entirely (verified: they call
`fetchPublicText()` → `resolvePublicWebTarget()` only, no path resolution)
and don't touch the filesystem or spawn commands — the only pair safe to
expose host-side without modifying `tool-broker.ts` itself.

### Adapter design

`toolBrokerToLangChainTools(ctx)` maps every `getOpenAIToolDefinitions()`
entry to a LangChain `tool()` call. LangChain's `tool()` (from
`@langchain/core/tools`, re-exported by `langchain`) has an overload that
accepts a raw JSON-Schema-7 object directly as the `schema` field — verified
by reading the installed package's `.d.ts` (`langchain@1.5.3`,
`@langchain/core@1.2.2`). Since `getOpenAIToolDefinitions()`'s `parameters`
field is already a JSON-Schema-7-shaped object (`{type: 'object',
properties, required, additionalProperties: false}`), it is passed through
**unchanged** — no Zod conversion needed. The tool's execute function calls
`executeBrokerTool(name, args, ctx)` unmodified. Pure adapter, zero tool
behavior redefined, zero logic duplication.

### Host-allowlist enforcement (Decorator)

`withHostAllowlist(tool, allowedHosts)` is a **Decorator**: it preserves the
wrapped tool's `name`/`description`/`schema` unchanged, intercepts
`.invoke()`, and delegates to the original tool only after a passing
hostname check. This mitigates a known residual risk:
`resolvePublicWebTarget`'s SSRF guard (`isPrivateIp`,
`tool-broker.ts:221-232`) checks `10.x`/`127.x`/`172.16-31.x`/`192.168.x`/
`169.254.x` but omits `100.64.0.0/10` (CGNAT, used by Tailscale — a real gap
for a host-side run on a machine that may have Tailscale active). Fixing
`tool-broker.ts` itself is out of scope for A1 (unmodified per this spike's
constraint, pre-existing and unrelated). Instead, the wrapper enforces an
argument-level host allowlist on `web_fetch` specifically, **before**
delegating to the real tool: it parses `args.url` with `new URL(...)`,
compares `url.hostname` **exactly** against a hardcoded literal list
(`['npmjs.com', 'www.npmjs.com']` — not derived from model output, user
input, or any runtime-configurable value), and rejects malformed URLs and
all other hostnames with a structured tool-error result returned to the
model — **never** an unhandled exception. This is real enforcement (code,
not a prompt instruction) — nothing stops the model from ignoring prompt
guidance, but the wrapper's reject path never calls the wrapped tool's real
execute function, so a disallowed or malformed URL provably never reaches
`executeBrokerTool`.

The allowlist check is **exact hostname string match only**
(`allowedHosts.includes(parsed.hostname)`) — deliberately no subdomain or
wildcard matching, so a future reader shouldn't assume `.includes()`
substring semantics apply.

`web_search` is intentionally left unwrapped: its only network target
(`executeBrokerTool`'s `web_search` case) is a hardcoded DuckDuckGo URL
template (`https://duckduckgo.com/html/?q=${encodeURIComponent(query)}`)
with the model supplying only the query string, never a URL or hostname —
there is no attacker/model-controlled hostname-selection surface to gate,
unlike `web_fetch` where `args.url` is fully model-supplied.

### Cross-platform

The spike uses only standard Node.js `URL`, LangChain's HTTP-based model
client, and no shell-outs or OS-specific paths — it runs identically on
macOS/Linux/Windows Node 20+.

### What was built

- `scripts/spikes/lia394_langchain_walking_skeleton.ts` — the adapter
  (`toolBrokerToLangChainTools`), the Decorator (`withHostAllowlist`), and a
  main script that builds the filtered/wrapped tool list, checks
  `ANTHROPIC_API_KEY`, and (if present) runs `createAgent({model: new
  ChatAnthropic({apiKey, model: 'claude-opus-4-8'}), tools})` against a
  prompt requiring both `web_search` and `web_fetch`, printing the full
  transcript.
- `scripts/spikes/lia394_langchain_walking_skeleton.test.ts` — unit tests
  covering the adapter mapping, the negative host-allowlist check (proves
  `executeBrokerTool` is never called for a disallowed host), the positive
  allowlist pass-through, and the malformed-URL non-throwing path.
- Root `package.json` dependencies: `langchain@^1.5.3`,
  `@langchain/core@^1.2.2`, `@langchain/anthropic@^1.5.1` (host-only script;
  NOT added to `container/agent-runner/package.json`, which never ships this
  code into the container). Verified zod compatibility:
  `@langchain/anthropic` requires `zod@^3.25.76 || ^4`, satisfied by root's
  existing `zod@^4.4.3` — no peer-dependency conflict.
- `vitest.config.ts` — added `'scripts/spikes/**/*.test.ts'` to
  `test.include` (previously only `src/**` and `setup/**`), a narrowly
  scoped addition so `npm test` discovers the new spike's unit tests.

## Verdict

`ANTHROPIC_API_KEY` is confirmed genuinely absent in this dev environment
(`echo $ANTHROPIC_API_KEY` empty; no `.env` file exists; `.env.example:34`
has it commented out — this environment uses subscription-based OAuth
billing, not a raw API key). Everything that CAN be verified without a live
key was verified:

- **Adapter correctness**: unit-tested — `toolBrokerToLangChainTools` maps
  every `getOpenAIToolDefinitions()` entry to a `StructuredTool` with
  matching name/description, and a mapped tool's execute function calls
  `executeBrokerTool` with the right arguments (confirmed via a real
  `web_search` invocation against a stubbed context).
- **Host-allowlist enforcement is real**: unit-tested — a disallowed
  hostname (`https://evil.example.com`) is rejected and **never** reaches
  `executeBrokerTool` (proven via a mock/spy on `executeBrokerTool` that
  records zero calls), while `https://npmjs.com/...` and
  `https://www.npmjs.com/...` pass through to the real tool. A malformed
  URL is rejected with a structured error, never an unhandled exception.
- **Fail-fast without a key**: running the script directly with no
  `ANTHROPIC_API_KEY` set prints the documented message and exits with code
  `3` — no crash, no raw stack trace.

**Not verified — blocked**: the live end-to-end proof that `createAgent`
actually drives a real multi-turn tool-calling loop against the Anthropic
API through the unmodified `tool-broker.ts` execution path. This requires a
real `ANTHROPIC_API_KEY`, which is not available in this environment. A
future session with real credentials should run
`ANTHROPIC_API_KEY=sk-... npx tsx scripts/spikes/lia394_langchain_walking_skeleton.ts`
and capture the transcript to complete this spike's VIABLE/NOT VIABLE
determination.

## Scope of this spike

Read-only proof-of-concept; nothing wired into production. No modifications
to `container/agent-runner/src/tool-broker.ts`, `container/Dockerfile`,
`src/agent-runtimes/`, or any credential-proxy path. Not merged to `main` —
pushed as a PR for review only.
