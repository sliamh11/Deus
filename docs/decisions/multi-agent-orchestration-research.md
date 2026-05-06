# Multi-Agent Orchestration & Agent Quality Research

**Status:** Accepted
**Date:** 2026-05-06
**Scope:** `src/agent-backends/`, `src/message-orchestrator.ts`, `container/agent-runner/`, `.claude/wardens/`, memory atoms
**Related:**
- `backend-neutral-agent-runtime.md` — defines the backend interface contract this ADR proposes renaming; that ADR already deferred agents-js adoption ("Full OpenAI Agents SDK sessions, handoffs, and tracing remain parity work") — this ADR formalizes that deferral as a skip with architectural justification
- `parallel-agent-orchestration.md` — covers TUI-level parallel agent sessions (independent CLI sessions). This ADR covers container-level multi-agent coordination (subagents within a single request), a different scope
- `tui-agent-orchestration.md` — covers TUI subprocess lifecycle. Not superseded

## Context

Investigated enhancing Deus's agent performance across three dimensions: input security (prompt injection), multi-agent coordination (parallel subagents, handoffs), and self-improvement loops (structured lesson capture, warden learning). Evaluated external tools and frameworks to determine what to adopt, adapt, or skip.

## Research Conducted

### Repos Evaluated

| Repo | Verdict | Reasoning |
|------|---------|-----------|
| `msitarzewski/agency-agents` | **Skip** | Prompt library (147 markdown persona files, zero runtime). No tools, no API. Deus already has real memory and structured skills. |
| `hesreallyhim/awesome-claude-code` | **Mine selectively** | Curated list of 226 entries. Notable: parry-guard, HCOM, Context Engineering Kit, Trail of Bits security skills. |
| `obra/superpowers` | **Cherry-pick patterns** | High quality (179k stars) but heavy overlap with existing wardens/patterns/router. Adopt: subagent-driven-development prompt templates, systematic-debugging 4-phase process. |
| `EveryInc/compound-engineering-plugin` | **Adopt schema** | Structured lesson capture: bug-track (Symptoms / What Didn't Work / Solution / Prevention) and knowledge-track. Complementary to evolution/eval — eval measures outcomes, CE captures qualitative lessons. |
| `openai/openai-agents-js` | **Adopt concepts only** | v0.9.0, 2.9k stars. Clean guardrail/tracing interfaces. Runner expects to own LLM calls — incompatible with container model (see below). |
| `openai/openai-agents-python` | **Reference only** | More mature (25.9k stars, SQLite sessions, multi-provider, sandbox). Patterns worth back-porting: prefix-based provider dispatch, model-level retry advice. Wrong language for the real-time dispatch path. |
| `openai/evals` | **Skip** | Effectively dormant (last real commit Sep 2024). Existing eval system (DSPy + OllamaJudge + Reflexion) is stronger. Optionally mine their dataset registry for additional test cases. |
| `reporails/cli` | **Run diagnostic** | Lints AI instruction files against 92+ rules. Worth running once. BUSL-1.1 license (can use CLI, cannot fork/embed). Separate pending task. |
| `vaporif/parry-guard` | **Adopt Layer 2 only** | Substantive Rust scanner (DeBERTa ML, tree-sitter AST exfil detection, 16 languages). Critical gap: `UserPromptSubmit` does NOT scan user prompt text — only audits config directory. Useful for host Claude Code sessions (Layer 2), not for container pre-ingestion (Layer 1). |

### Architectural Finding: Runner vs Container Model

The `@openai/agents-js` Runner assumes it controls LLM calls via its `Model` interface. Deus runs LLM calls **inside isolated containers** (via Claude Agent SDK or OpenAI Responses API). Three integration options evaluated:

| Option | Description | Verdict |
|--------|-------------|---------|
| Runner on host, LLM on host | Containers become tool sandboxes. Loses Claude Agent SDK autonomous loop. | **Rejected** — massive rewrite, loses core strength. |
| Runner on host, LLM in container (bridged) | `Model.getResponse()` wraps entire container run. | **Rejected** — impedance mismatch. Runner cannot orchestrate individual turns inside a container. |
| Runner inside container | Multi-agent within a single container only. | **Partial** — useful for intra-container subagents, not host-level orchestration. |

**Conclusion:** agents-js Runner does not fit the container-based architecture. Adopt concepts (guardrail interfaces, tracing patterns, handoff semantics, structured return contracts) and build a thin custom orchestrator on top of the existing runtime interface. This is consistent with `backend-neutral-agent-runtime.md`'s prior deferral.

### Multi-Agent Prompt Engineering Patterns

Research across Superpowers, CrewAI, AutoGen, and agents-js yielded four adoptable patterns:

1. **Persona scaffold** — role/goal/backstory triple per subagent
2. **Structured return contract** — `DONE | DONE_WITH_CONCERNS | BLOCKED | NEEDS_CONTEXT` status enum [^1]

[^1]: Implementation note (Phase 6): `NEEDS_CONTEXT` was folded into `BLOCKED` with a dependency reason — if a dependency is available, the orchestrator injects it before launch; if unavailable, the task cannot run and is `BLOCKED`.
3. **Adversarial review framing** — reviewer told to assume implementer's work is incomplete
4. **Explicit context graph** — each agent declares which prior outputs it needs, not everything

**Key constraint:** Parallel fan-out is for reads (research/retrieval), not writes (implementation). Implementation and review run sequentially.

## Decision

### Adopt

1. **Interface rename:** `AgentBackend` → `AgentRuntime`, `BackendSessionRef` → `RuntimeSession`, `BackendCapabilities` → `RuntimeCapabilities`, `AgentBackendName` → `AgentRuntimeId`. Three-layer naming: Transport (LLM call) → Runtime (session lifecycle) → Runner (orchestration). Code change tracked as its own PR.
2. **Pre-ingestion scanner:** Guardrail in the message orchestrator that scans untrusted channel messages before container dispatch. Pattern matching (Aho-Corasick injection phrases) + optional ML classifier.
3. **Parry-guard on host:** Layer 2 defense for host Claude Code sessions via PreToolUse/PostToolUse hooks.
4. **Thin multi-agent orchestrator:** Dispatches parallel container runs via the runtime interface. Curated isolation (bespoke context per subagent). Structured return contracts.
5. **Structured lesson capture:** Compound-Engineering-style atoms with bug-track/knowledge-track schema. Auto-surfaced via memory tree during agent context assembly.
6. **Generalized warden learning:** Extend code-review dismissal→reflection pattern to plan-reviewer and threat-modeler.
7. **Prompt template system:** Role-specific prompts for subagents using persona scaffold + adversarial review patterns.

### Skip

- Full `@openai/agents-js` framework adoption (Runner incompatible with container model)
- Full `@openai/agents-python` adoption (wrong language for real-time dispatch path)
- OpenAI evals framework (dormant, existing system is stronger)
- agency-agents prompt library (no runtime, no value over existing system)
- Installing Superpowers as a plugin (fights existing wardens/patterns)

## Implementation Order

Separate PRs by context, infrastructure first:

| PR | Scope | Dependencies |
|----|-------|--------------|
| 1 | Interface rename (`AgentBackend` → `AgentRuntime`, etc.) | None — pure refactor |
| 2 | Pre-ingestion scanner (guardrail in orchestrator) | None |
| 3 | Parry-guard host installation | None |
| 4 | Structured lesson capture (atom schema + auto-detection) | None |
| 5 | Generalized warden learning loop | PR 4 (uses same atom schema) |
| 6 | Thin multi-agent orchestrator + prompt templates | PR 1 (uses renamed interfaces) |

## Consequences

- Container model preserved — LLM calls stay inside containers. No architectural migration.
- The thin orchestrator adds a lightweight coordination layer to the host, not a framework dependency.
- Pre-ingestion scanner covers all channels at the orchestrator level.
- Structured lessons compound over time as memory atoms, surfaced by existing retrieval.
- Warden learning extends an already-proven pattern (code-review dismissal→reflection).

## Verification

- Interface rename: `npm run typecheck` + all existing tests pass. grep confirms zero remaining old names.
- Pre-ingestion scanner: test with known injection payloads (parry-guard's Aho-Corasick phrase list as test corpus).
- Multi-agent orchestrator: eval bench expanded with multi-step tasks measuring subagent coordination quality.
- Lesson capture: verify memory tree surfaces solution atoms during relevant context assembly.
- Warden learning: verify reflection injection reduces false positive rate over 10+ review cycles.

## Rollback

Each PR is independently revertible. The interface rename is a mechanical find-replace — rollback is the same operation in reverse. The multi-agent orchestrator ships behind an env flag (`DEUS_MULTI_AGENT=1`) until validated.
