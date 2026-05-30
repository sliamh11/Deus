# ADR: Activate DSPy Prompt Self-Optimization (Wire the Optimizer Arm Live)

# Status

Proposed

**Date:** 2026-05-30
**Scope:** The DSPy optimizer arm — its GEPA quality metric and artifact activation gate (`evolution/optimizer/dspy_optimizer.py`, `evolution/optimizer/artifacts.py`, `evolution/storage/` artifact ops, `evolution/config.py`). Phase 1 fixes the metric + adds the ship-if-better gate; prompt injection (`evolution-client.ts`, `container-runner.ts`) is Phase 2+.

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
   and inject the active optimized prompt at the same site
   (`message-orchestrator.ts`) where reflections are injected today — composing
   with, not replacing, the reflections block.

4. **Kill switch + observability.** A single env flag (`EVOLUTION_OPTIMIZED_PROMPTS`,
   default off) gates injection so the arm can be disabled instantly without a
   deploy. Every activation logs baseline → new score, delta, sample count, and
   artifact id. Rollback = revert to the previous active artifact.

Rollout: activation defaults OFF. The arm runs in shadow (produces + validates
artifacts, logs deltas) until shadow data shows consistent positive deltas, then
the flag is flipped per-module.

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
