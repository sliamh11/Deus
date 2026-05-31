# ADR: Activate DSPy Prompt Self-Optimization (Wire the Optimizer Arm Live)

# Status

Accepted

**Date:** 2026-05-30
**Scope:** The DSPy optimizer arm — its GEPA quality metric and artifact activation gate (`evolution/optimizer/dspy_optimizer.py`, `evolution/optimizer/artifacts.py`, `evolution/storage/` artifact ops, `evolution/config.py`), plus the host-side prompt-injection consumer (`evolution/cli.py`, `evolution/mcp_server.py`, `evolution-client.ts`, `container-runner.ts`). Phase 1 fixed the metric + added the ship-if-better gate (PR #651, merged). Phase 2 wires the consumer dark behind `EVOLUTION_OPTIMIZED_PROMPTS` (default OFF) and lands the LIA-152 sanitization prerequisite.

# Context

Deus's evolution loop has two independent self-improvement arms:

1. **Reflexion arm (live).** Low-scoring interactions generate a natural-language
   "lesson" which is stored and retrieved for similar future queries. This arm is
   fully wired: `evolution-client.ts:getReflections()` runs every dispatch and
   prepends a `<reflections>` block to the agent prompt via `appendToSystemPrompt`
   in `message-orchestrator.ts`. It has its own damping (dedup at
   `reflexion/store.py:17`, 30-day decay, helpful-counters).

2. **DSPy optimizer arm (built, never connected).** `optimizer/dspy_optimizer.py`
   runs GEPA over logged interactions, produces a compiled prompt artifact,
   `save_artifact()` writes `{module}-latest.json` and marks it "active", and
   `mcp_server.py:get_active_prompt_tool` exposes it over the live `deus-evolution`
   MCP server. **Nothing consumes it.** Repo-wide grep finds zero callers of
   `get_active_prompt` / `latest.json` outside the producer; git pickaxe confirms
   a consumer never existed in history. The sole host bridge,
   `evolution-client.ts`, calls only `get_reflections` and `log_interaction`.

A review of the optimizer arm surfaced three issues, in priority order:

- **Degenerate optimization target.** The GEPA metric in
  `dspy_optimizer.py:99-108` is `len(pred.strip()) > 20` — it optimizes prompts
  toward "produce text longer than 20 characters," not toward the real
  `JudgeResult` (quality / safety / tool_use / personalization). The reported
  `baseline` (real mean judge score) and `optimized_score` (this length pass-rate)
  are different metrics on different scales, so the printed delta is meaningless.

- **No ship-if-better gate.** `save_artifact()` activates the new artifact
  unconditionally (`dspy_optimizer.py:228`), even on a negative delta. A control
  loop with no validation gate can regress itself.

- **No consumer.** The optimized prompt is never injected into a live agent.

The arm auto-triggers: `cli.py` runs `optimize()` for every module once
`scored_since >= AUTO_OPTIMIZE_THRESHOLD` (default **15**, not disabled), floored at
`DSPY_MIN_SAMPLES` (20) usable examples. In practice it is currently a no-op:
`dspy` is not installed in the runtime Python (`EVOLUTION_PYTHON`, default
`python3`), so `optimize()` raises `ImportError` at `_require_dspy()` which is
swallowed by the `except` at `cli.py:174` — no artifacts are produced now. It DID
run historically (one `qa` artifact, 2026-03-30, recorded `optimized_score=1.0` —
the tell-tale length-metric footprint), so the machinery is real, not theoretical.
The self-improvement / evolution loop is a stated product differentiator, so we make
the second arm correct (real judge metric + ship-if-better gate) for when `dspy` is
present, rather than delete it.

# Decision

Finish and activate the DSPy prompt-optimization arm end-to-end, with the safety
mechanisms a live self-modifying loop requires. Four pieces ship together — none
alone is safe to enable:

1. **Real metric.** Replace the length heuristic with the actual judge. The GEPA
   objective scores candidate prompts via `make_runtime_judge()` so optimization
   targets the same quality signal (quality/safety/tool_use/personalization) the
   rest of the loop uses. `baseline` and `optimized_score` become the same metric
   on the same scale, making the delta meaningful.

2. **Ship-if-better gate + holdout.** `save_artifact()` activates a new artifact
   only when its score beats the current active artifact's score on a held-out
   evaluation set by a configurable minimum margin. Otherwise the artifact is
   persisted but NOT activated (kept for audit). This is the damping the control
   loop is missing.

3. **Host consumer.** Add a `getActivePrompt(module)` path in `evolution-client.ts`
   and inject the active optimized prompt at the same site where reflections are
   injected today — composing with, not replacing, the reflections block.

   **Implementation note (injection site).** An earlier draft of this ADR named
   `message-orchestrator.ts` as the reflections/injection site. That is stale:
   the live reflexion arm prepends its block to the **user prompt** at
   `container-runner.ts:~251` (`runContainerAgent`), not via
   `appendToSystemPrompt` in `message-orchestrator.ts`. Phase 2 injects the
   optimized prompt at that **same** `container-runner.ts` seam, immediately after
   the reflections prepend. Rationale, recorded so it is not re-litigated: both
   self-improvement arms then compose at one well-tested, fail-safe point — a
   single place that reasons about prompt assembly, where both arms degrade to the
   base prompt identically when their source is empty. This supersedes the
   `message-orchestrator.ts` reference.

   **Trust boundary (LIA-152).** An optimized artifact is untrusted LLM output, and
   it is injected into the **user-prompt** channel. So the consumer never injects
   raw artifact content: a single helper (`artifacts.get_active_prompt_block`)
   extracts only `_predict.signature.instructions`, rejects the trivial
   auto-generated default (no learned signal), length-caps it
   (`OPTIMIZED_PROMPT_MAX_CHARS`), and wraps it in
   `<stored-output source="dspy-artifact" module="…">` boundary tags that are
   injected verbatim to demarcate stored content. Both the Node bridge and the
   `get_active_prompt_tool` MCP surface route through this one helper, so neither
   can leak raw, unbounded content. This sanitization is a **hard prerequisite**
   for enabling injection.

4. **Kill switch + observability.** A single env flag (`EVOLUTION_OPTIMIZED_PROMPTS`,
   default off) gates injection so the arm can be disabled instantly without a
   deploy. The gate is checked **before** the Python subprocess spawns, so the
   default-off path adds zero dispatch latency. Every activation logs baseline →
   new score, delta, sample count, and artifact id. Rollback = revert to the
   previous active artifact.

Rollout: activation defaults OFF. The arm runs in shadow (produces + validates
artifacts, logs deltas) until shadow data shows consistent positive deltas, then
the flag is flipped per-module.

**Wired ≠ validated.** Phase 2 ships the *mechanism*. The `qa` module's instruction
is tuned by GEPA against a `dspy.Predict` signature (`query/context/reflections →
answer`), but the container agent is a full Claude Code agent, not a DSPy Predict —
so even a rich optimized instruction was tuned against a different harness. Shadow
deltas validate *transfer* (does the injected instruction actually help the real
agent), not the wiring. Do not read "wired" as "proven useful."

**Pre-flip checklist** (all required before setting `EVOLUTION_OPTIMIZED_PROMPTS=1`
for any module — the dark ship deliberately defers these):

1. **Verify the key path.** Confirm `_predict.signature.instructions` against a real
   GEPA-compiled `dump_state()` once `dspy` is installed (the consumer was verified
   only against a non-GEPA artifact). The whole trust boundary is correct only if
   this key reaches real content (`artifacts.py` TODO).
2. **Move the block to a session-stable channel.** The injected block is static
   (hardcoded `qa`, no per-query variation), so on the per-turn user prompt it
   re-bills its ~tokens every turn of a resumed session. Move it to the
   session-stable system-prompt channel (the same reasoning the project-hint uses
   at `container-runner.ts`) so it is sent once.
3. **Parallelize retrieval.** `getReflections` and `getActivePrompt` are independent;
   `Promise.all` them so two ~3 s subprocesses don't serialize to a 6 s pre-dispatch
   ceiling.
4. **Make the shadow delta a live signal.** Today the logged delta is the offline
   GEPA holdout delta from artifact metadata, not a live A/B. Join pre-injection vs
   post-dispatch judge scores under the same `artifactId` so "does it transfer" is
   actually falsifiable.
5. **Fix the `tool_selection` judge objective** (LIA-151).

# Consequences

**Positive**
- The optimizer arm becomes a genuine second self-improvement mechanism instead
  of dead weight, supporting the evolution-loop differentiator.
- Optimization targets real quality, not text length — the existing GEPA/artifact
  infrastructure starts producing meaningful prompts.
- The ship-if-better gate makes the loop monotonic-by-construction: it cannot
  activate a worse prompt, which directly answers the "can bad scores swing the
  active prompt" stability concern.
- Kill switch + per-module rollout means risk is bounded and instantly reversible.

**Negative / risk**
- Introduces a live self-modifying loop — the highest-risk class of change in the
  system. Mitigated by: ship-if-better gate, holdout validation, shadow rollout,
  default-off flag, per-module activation, and full activation logging.
- Judge-as-metric makes optimization cost and latency higher than the length
  heuristic, and inherits judge noise (bench: quality bounds swing at low n).
  Mitigated by the minimum-sample floor and minimum-margin threshold.
- More moving parts in the prompt-assembly path; the consumer must compose
  cleanly with the reflections block and fail safe (no optimized prompt → fall
  back to base, exactly as reflections already do).

# Alternatives Considered

- **Delete the arm.** Lowest effort, aligns with "no features without a real
  caller." Rejected: discards real infrastructure and a piece of the
  evolution-loop story the product leans on.
- **Fix-and-shelf** (fix the metric + add the gate, keep dormant). Cheapest
  correct step and lowest risk. Rejected in favor of Finish because the goal is a
  working second arm, not just a non-broken dormant one — though its safety
  mechanisms (real metric, ship-if-better gate) are subsumed as the first,
  safe-by-themselves phase of Finish.

# References

- `evolution/optimizer/dspy_optimizer.py` (GEPA metric `:99-108`, unconditional
  activation `:228`, sample floor `:168`)
- `evolution/optimizer/artifacts.py` (`save_artifact` / `get_active`)
- `evolution/mcp_server.py:104` (`get_active_prompt_tool` — exposed, uncalled)
- `src/evolution-client.ts` (host bridge; `getReflections` live, no `getActivePrompt`)
- `src/message-orchestrator.ts` (reflections injection site = consumer slot)
- `docs/decisions/evolution-db-split.md` (evolution storage architecture)
