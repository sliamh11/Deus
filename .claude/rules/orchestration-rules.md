# Orchestration Rules
# Applies to all agent dispatch, pipeline automation, and task orchestration.
# Covers: issue creation, gate discipline, state management, and MCP tool hygiene.
# Separated from core-behavioral-rules.md because these are pipeline-specific
# (not general coding/commit rules) and were triggered by observed failures:
# auth-error fallbacks labeled as "Scoped", agents working on stale requirements
# after mid-flight description changes, and double-escaped MCP tool output.

## Issue Creation
- Always assign issues to the correct project. Never leave issues floating without a project.
- Use actual newlines in descriptions — never `\n` escape sequences. MCP tools double-escape them, producing literal `\\n` in rendered output.

## Pipeline State Integrity
- If an issue's scope or description changes after entering the pipeline, move it back to the relevant step. Scope changed → back to the scoping step so the gate re-evaluates. Never leave an agent working on stale requirements.
- An issue may only advance past a gate when the gate agent ran successfully and approved. Any other outcome (error, timeout, crash, auth failure) is not approval — it's a failure that needs investigation or retry.
- The "Scoped" label may only be applied when the readiness gate produces a real scope block with enrichment. Fallback verdicts from failed gates are not scoping.

## Gate Discipline
- Gate fallbacks are errors, not approvals. If a gate agent fails, the verdict must be ERROR with a visible error label — never SHIP. Fallback-SHIP silently bypasses quality gates and produces false labels on unreviewed work.
- Never auto-advance an issue past a gate that didn't actually run. Silence is not consent.
- REVISE handling follows core-behavioral-rules.md: re-run after fixes until SHIP, no exceptions.

## Agent Dispatch
- Dispatched agents must work against the current issue state. If the issue was modified after dispatch, the agent's output is suspect — re-evaluate before accepting.
- Agent output that doesn't match the issue's acceptance criteria should not auto-merge, even if CI passes. The output-quality-gate exists for this.
- Failed dispatches (auth errors, container failures, timeouts) must be surfaced with clear error state — not silently swallowed.

## Tool Hygiene
- When creating or updating issues via MCP tools, verify the rendered output matches intent. Double-escaped markdown, broken formatting, and missing fields are bugs, not cosmetic issues — they degrade agent scoping and human review.
