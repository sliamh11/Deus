# ADR: In-memory agent response cache (no disk cache)

**Status:** Accepted
**Date:** 2026-03-29
**Scope:** `eval/`

## Context

Each eval test case invokes the agent in a Docker container (~300–500MB RAM, 10–60s latency). Multiple DeepEval metrics can target the same prompt, which would naively re-spawn a container per metric per test.

A cache is needed. Two options were considered:

1. **Disk cache** — serialize responses to a file keyed by prompt hash; survives process restart.
2. **In-memory cache** — a session-scoped dict in the pytest fixture; lives only for the test run.

## Decision

In-memory cache via a session-scoped `agent` pytest fixture (`conftest.py`). Responses are stored in a `dict[str, AgentResponse]` with a double-checked lock for thread safety during parallel pre-warm.

## Reasons disk cache was rejected

- **Staleness risk.** A cached response from a previous build reflects old agent behavior. Disk cache would mask regressions silently.
- **No meaningful speedup across runs.** Eval suite is typically run after a code change; reusing old responses defeats the purpose.
- **Complexity.** Cache invalidation requires hashing the container image, group config, and prompt — significant complexity for no benefit.

## Consequences

- Each `pytest` session runs fresh containers; no stale results across builds.
- Multiple metrics on the same prompt within one session share one container invocation.
- Full suite (~40 prompts) always starts cold — plan for ~10 min runtime on first run of the day.
