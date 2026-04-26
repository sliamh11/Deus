# ADR: llama.cpp as Optional Integration, Not Default Replacement

**Date:** 2026-04-26
**Status:** Accepted
**Scope:** `.claude/skills/add-llama-cpp/`, memory embeddings, local generation/judging

## Context

Deus evaluated `llama.cpp` as a possible replacement for Ollama after a local
GGUF setup and an isolated embedding parity benchmark. The benchmark used a
temporary memory-tree environment and did not modify production embedding
databases.

The result was mixed:

- `llama.cpp` embedding calls were much faster than Ollama.
- The `llama.cpp` and Ollama embedding vector spaces were incompatible despite
  both returning 768-dimensional vectors.
- Existing Ollama-backed vectors cannot be reused by a `llama.cpp` embedding
  provider.
- After provider-specific threshold calibration, `llama.cpp` embeddings were
  viable but not clearly safer than the existing Ollama default.

Generation and judge use may still be valuable, but that is a separate
decision from memory embeddings.

## Decision

Keep Ollama as the default memory embedding and local judge path.

Ship `llama.cpp` only as an optional host integration skill:
`/add-llama-cpp`.

The skill may install and verify a local `llama-server` endpoint and document
how to connect it to Deus if the current checkout contains optional
`llama_cpp` provider wiring. It must not silently change the default memory,
judge, or generation providers.

## Required Guardrails

- A `llama.cpp` embedding switch requires a full provider-specific re-embed.
- The switch must include calibrated thresholds for that provider and model.
- The switch must include a benchmark snapshot showing recall, MRR, OOD
  abstain, wrong-confident rate, and latency.
- Runtime/provider source changes must be shipped separately from the skill PR.
- Existing Ollama vectors must be treated as provider-specific data, not as a
  portable embedding cache.

## Alternatives Considered

**Replace Ollama with llama.cpp by default.** Rejected. The benchmark did not
justify a product-wide default change, and embedding-vector incompatibility
would make this a migration with data consequences.

**Drop llama.cpp entirely.** Rejected. Local GGUF serving is still useful for
experiments and may become a better generation or judge path later.

**Merge all provider wiring now.** Rejected for this change. Optional provider
code should be evaluated independently and should not be bundled with the
install skill.

## Consequences

- Deus remains stable for existing users and keeps Ollama-backed memory
  behavior unchanged.
- Users who want local GGUF experiments can opt in through `/add-llama-cpp`.
- Future `llama.cpp` provider work must be explicit about which surface it
  changes: generation, judging, or embeddings.
- Memory embedding changes remain benchmark-gated and provider-aware.
