# ADR: Deus V2 — LangChain JS runtime replaces Claude Code as the base harness

**Decision status:** Accepted (2026-07-13)
**Implementation status:** Not started — gated on the MA kill-switch spike (LIA-394..400)
**Current live state:** Claude remains the default/fallback `AgentRuntime`, unchanged. `AgentRuntimeId`
today only accepts `claude` | `openai` | `llama-cpp` — `deus-native` does not exist in code yet.
**Target state:** `deus-native` becomes the default `AgentRuntime` after MA passes and a documented
cutover; Claude Code/Codex/OpenCode are demoted to optional Tier-2 dev CLIs.
**Date:** 2026-07-13
**Scope:** `src/agent-runtimes/`, `container/agent-runner/`, `scripts/codex_warden_hooks.py`,
`docs/decisions/hook-dispatch-system.md`, `docs/decisions/backend-neutral-agent-runtime.md`,
`docs/decisions/ADR-001-hook-dispatch-service.md`

## Context

Deus began as a harness around Claude Code. `backend-neutral-agent-runtime.md` (Accepted,
2026-04-23) moved Deus toward owning the runtime/session/tool/credential contracts while Claude
stayed the default adapter. `hook-dispatch-system.md` (Accepted, 2026-05-14) designed a
model-agnostic hook pipeline for that runtime but was never implemented —
`hook-dispatch-facade-correction.md` (2026-06-07) documented the gap: hooks and wardens fire only
via the Claude Code CLI today, so any non-Claude backend runs unguarded.

A 2026-07-13 deep-research pass (vault: `Research/2026-07-13-deus-v2-base-harness-selection.md`) evaluated 9
candidate agent-harness platforms against five hard requirements derived from what Claude Code
supplies today (blocking tool-call interception, session-lifecycle events, subagent dispatch with
per-agent model selection, MCP consumption, TypeScript embeddability). LangChain JS 1.x
(`createAgent` + middleware) with LangGraph.js checkpointing was the only candidate combining
first-class veto middleware, per-call model override, first-party MCP support, mature durable
persistence, TS-native embedding, and a stability commitment (MIT, 1.0 GA Oct 2025, no
breaking-changes-until-2.0). Full ranking, alternatives, and confidence levels (vault):
`Research/2026-07-13-deus-v2-base-harness-selection.md`.

## Decision

Deus V2 is a two-tier architecture:

- **Tier 1 — Deus Runtime (product harness):** a new `deus-native` `AgentRuntime` implementation
  inside the existing `src/agent-runtimes/` registry, built on `createAgent` + middleware +
  LangGraph.js checkpointing. This becomes the runtime channels, scheduled tasks, and the web UI
  route through.
- **Tier 2 — Interactive dev CLIs (developer choice, not architecture):** Claude Code, Codex, and
  OpenCode remain usable as optional interactive development tools against the same repo and gate
  logic. They are explicitly distinct from the legacy product-runtime values (`claude` / `openai` /
  `llama-cpp` in `AgentRuntimeId`) — Tier-2 CLI choice is a developer-tooling preference, not a
  `DEUS_AGENT_BACKEND` value.

**Repo and rollout strategy:** a dedicated worktree (this one) with per-slice PRs to `main` behind
`DEUS_AGENT_BACKEND=deus-native`, strangler migration through the existing `src/agent-runtimes/`
registry. No new repository, no wholesale-merge integration branch — each milestone's slice lands
independently. The MA kill-switch spike (LIA-394..400, tasks A1-A7) is a **hard gate** before MB
begins: warden veto via `wrapToolCall` (A2), subscription billing through the credential proxy
(A4), MCP adapter consumption (A5), and tool-loop reliability (A7) must each have a recorded
pass. A failure on any criterion triggers a documented re-evaluation against the research report's
runner-up (OpenAI Agents SDK JS) — not a push-through.

### Execution topology

The research report's phrasing ("in-process in the existing Node host" vs. the feature-loss
table's "container runs the new loop") is not actually contradictory, but needs precision: "in-
process" means the `deus-native` graph runs with no external CLI subprocess spawn (unlike today's
Claude backend, which shells out to the `claude` binary) — not that it necessarily runs in the
outer host process. Concretely:

- For channel and scheduled-task sessions (the isolated-per-group path), the `deus-native` graph
  runs in-process **inside `container/agent-runner`'s existing Node process**, reusing
  `tool-broker.ts` — the same pattern `openai-backend.ts` and `llama-cpp-backend.ts` already use.
- For the future interactive host CLI (milestone G1), the graph runs in-process **inside the
  `deus` CLI's own host process**.

Both are "in-process," in two different processes depending on product surface. This has a real
trust-tier consequence: for the container-routed path, `wrapToolCall` middleware executes inside
the container process, at the same trust tier as today's `pre-tool-use-gate-observer.ts` —
**Observer layer, container-cooperative**, not a new host-enforced guarantee. A compromised
container could in principle skip it, exactly as it could today. This ADR does not claim to close
that authority-inversion gap; A1 (LIA-394) and A2 (LIA-395) are where this executes and gets
empirically tested. Closing the gap for good (making tool-call veto genuinely host-enforced for
container-routed sessions) is explicit future work, not implied by this decision.

### HookPipeline: what's carried forward, what's corrected, what's still open

`hook-dispatch-system.md` defined two layers with different trust models:

- **Enforcement Layer (host-enforced):** `SessionStart` / `UserPromptSubmit` / `Stop` fire in
  orchestrator code before/after container execution; "container CANNOT opt out."
- **Observer Layer (container-cooperative):** `PreToolUse` / `PostToolUse` fire inside the
  container agent loop; cannot deny, only observe/rewrite.

V2's disposition, split by layer rather than claimed as one blanket implementation:

| Layer | V2 mechanism | Transition condition |
|---|---|---|
| Tool-call interception (PreToolUse/PostToolUse-equivalent) | `wrapToolCall` middleware calling `scripts/codex_warden_hooks.py` gate logic unchanged, container-cooperative (see Execution topology above) | Implemented-via-V2 when MC (LIA-409..414) ships and the live hook inventory is re-verified against `.claude/settings.json` (not the stale 14/15-hook count in the original ADR table — `core-behavioral-rules.md` already flags that count as stale; live count is re-extracted at execution time, not asserted here) |
| Session lifecycle (SessionStart/UserPromptSubmit/Stop-equivalent) | Whether V2 delivers these as genuinely host-enforced (matching the original design) or container-cooperative | **Open** — owned by B3 (LIA-403, "Enforce runtime lifecycle events"), not decided by this ADR |

`hook-dispatch-system.md`'s Status field flips to "Implemented-via-V2" only when **both** rows
ship and verify — not at MC alone. Until then it stays "Accepted but Not Implemented." Carried
forward from the original design: the Deus-owned Bridge/HookPipeline goal and blocking
tool-call-interception semantics with model-visible feedback. Corrected: the old design's
observer-cannot-deny axiom, which shipped code already breached
(`hook-dispatch-facade-correction.md`).

Separately, LangChain's `beforeAgent` / `beforeModel` / `afterModel` / `afterAgent` middleware
hooks are **not** automatically equivalent to SessionStart/UserPromptSubmit/Stop — LangChain
documents them as per-invocation and per-model-call hooks, not session-lifecycle primitives. The
real mapping (first-turn detection, per-turn injection, cancellation/error/interrupt handling,
exactly-once/idempotency) is designed in MD (LIA-415..419, memory/context middleware) and MC
(LIA-409..414, gate porting) — not settled by this ADR.

### Backend, provider, and CLI selection

`claude` / `openai` / `llama-cpp` remain valid `AgentRuntimeId` values through the strangler
migration; `deus-native` is added as a new value, not a replacement, until cutover. Provider
selection *within* a `deus-native` session (Anthropic/OpenAI/Google/Ollama) happens via
`wrapModelCall`, a separate axis from backend selection. The per-request/per-session
provider-switch semantics and the `LangGraph thread_id` ↔ `RuntimeSession.session_id` mapping
(which store is authoritative) are explicit open questions for B1/B4 (LIA-401, LIA-404), not
answered here.

**Current vs. target for CLI selection:** today, CLI-prefix selection and product-backend
selection are *intentionally coupled*, not independent — `deus claude` sets both
`DEUS_CLI_AGENT=claude` and `DEUS_AGENT_BACKEND=claude`; `deus codex` sets both
`DEUS_CLI_AGENT=codex` and `DEUS_AGENT_BACKEND=openai` (`deus-cmd.sh`'s `_normalize_cli_agent`
falls back from `DEUS_CLI_AGENT` to `DEUS_AGENT_BACKEND` when unset). `docs/KNOWN_LIMITATIONS.md`
already documents this coupling accurately; this ADR does not contradict it. Target: product
runtime (`DEUS_AGENT_BACKEND`), in-runtime model/provider (`wrapModelCall`), and Tier-2 developer
CLI choice become three independent axes. What plain `deus`, `deus claude`, and `deus codex` mean
after `deus-native` becomes the default is an **open question owned by G1** (LIA-428, "Build deus
CLI chat on deus-native"), not settled here. Until G1 ships, existing coupled command behavior is
unchanged and remains operational.

## Alternatives Considered

From the research report's Final Ranking (9 candidates scored against the five hard requirements):

| Candidate | Verdict | Why not chosen |
|---|---|---|
| OpenAI Agents SDK (JS) | Strong runner-up | Non-OpenAI providers route through LiteLLM adapters OpenAI itself labels "best-effort, beta"; documented breaking changes inside patch releases; JS port materially less adopted (~3.4k vs ~27.9k stars). For a model-agnostic goal, a second-class provider layer is disqualifying. |
| OpenCode | Best "adopt another harness" fallback | Real blocking hooks, but deny-only via throw (no ask/approve pause); skills/session-format parity unconfirmed; adopting it re-introduces dependency on another team's harness roadmap — the situation V2 is escaping. |
| Keep Claude Code + router/proxy | Transitional tool only | Hooks/skills survive unchanged, but the harness stays proprietary, MCP tool-search degrades behind non-Anthropic base URLs, and it delivers none of the customizability goal. |
| Google ADK (adk-js) | Capable but unverified | `before_tool_callback` veto confirmed in Python docs only, not JS; 2.0/2.2 shipped breaking changes within ~2 months. |
| Mastra | Real veto, smaller ecosystem | `beforeToolCall` veto exists but smaller ecosystem and an `ee/` dual-license wrinkle. |
| Pydantic AI | Excellent veto design, wrong language | Python-only — fails the TypeScript-embeddability hard requirement; a Node host would need cross-process IPC, reintroducing the subprocess complexity V2 removes. |
| Goose / OpenHands / Codex CLI / Gemini CLI | Each fails ≥1 hard requirement | Language mismatch, model lock, or immature hooks. |
| Aider | No hook system at all | Eliminated. |

Full scoring, confidence levels, and evidence map (vault): `Research/2026-07-13-deus-v2-base-harness-selection.md`.

## Consequences

Condensed feature-loss risk table (full version in the research report):

| Surface | Risk | Confidence |
|---|---|---|
| Warden gates | LOW-MEDIUM — logic ports unchanged, trigger layer is new code | HIGH |
| Memory injection | LOW — scripts unchanged, injection points move | HIGH |
| Subagent quality | MEDIUM — Claude Code's Agent-tool scaffolding must be re-implemented | MEDIUM |
| Skills | LOW — 46/48 are instruction packs, loader is trivial | HIGH |
| Interactive coding UX (plan mode, TUI richness) | HIGH if rebuilt — so it isn't; Tier 2 keeps Claude Code/Codex as dev CLIs | HIGH |
| Subscription billing | MEDIUM — mechanically plausible via the existing credential proxy, same pre-existing ToS gray zone as today's container path, not a new one | **MEDIUM-LOW** — open until A4 (LIA-397) reports pass/fail |
| Cross-backend session resume | NONE — sessions already backend-scoped, mismatch starts fresh | HIGH |
| Container isolation | LOW — I/O protocol unchanged | HIGH |
| Third-party tool-loop reliability | No independent study exists for any candidate | **MEDIUM-LOW** — open until A7 (LIA-400) benchmarks it |

## Cross-references and correction chain

- Source research (vault): `Research/2026-07-13-deus-v2-base-harness-selection.md`.
- `hook-dispatch-system.md` — per-layer disposition table above; Status flips to
  Implemented-via-V2 only when both rows ship (see forward-pointer added to that file).
- `hook-dispatch-facade-correction.md` — remains a valid historical record of the gap; V2 is the
  now-selected remediation path (see forward-pointer added to that file).
- `ADR-001-hook-dispatch-service.md` — its `:3002` `HookDispatchService` is planned for retirement
  in milestone F2 (LIA-424, "Consolidate tool enforcement in `wrapToolCall`") when it is retired
  from active enforcement; status remains Accepted until then (see forward-pointer added to that
  file — this banner and that one will both be updated again when F2 actually ships).
- `backend-neutral-agent-runtime.md` — superseded on the "Claude is the default adapter" ruling
  only (that file's line: "Claude is the default and compatibility baseline... it is one backend
  adapter rather than the architecture itself") — target-state superseded, operationally in force
  until cutover. That ADR's other rulings (credential-proxy routing, backend-scoped sessions,
  parity-matrix discipline) remain Accepted and are inherited by `deus-native` unchanged (see
  forward-pointer added to that file).

## Rollback

Rollback is a single revert while `claude` remains the default `AgentRuntimeId` — no live traffic
depends on `deus-native` until MA passes and an explicit cutover ships. If the MA kill-switch spike
fails any of its four criteria (warden veto, subscription billing, MCP adapter, tool-loop
reliability), the failure triggers a documented re-evaluation against the runner-up (OpenAI Agents
SDK JS) rather than a push-through past the gate.
