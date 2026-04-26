# Architecture Decision Records (ADR Index)

One line per decision. Load the full file only when the topic is directly relevant to what you're about to change.

| File | Topic | One-line ruling |
|------|-------|-----------------|
| [eval-ipc-file-output.md](eval-ipc-file-output.md) | eval / Docker / IPC | Results via shared-volume files, not stdout — Docker pipe buffering is permanent, **do not revert** |
| [eval-no-disk-cache.md](eval-no-disk-cache.md) | eval / caching | In-memory cache only — disk cache silently masks regressions across builds |
| [eval-selective-warmup.md](eval-selective-warmup.md) | eval / warmup / concurrency | Warm only active test datasets — saves ~3× time, avoids API rate saturation |
| [startup-gate.md](startup-gate.md) | startup / onboarding / memory | Check registry pattern; channels optional; memory system is the priority; vault path configurable |
| [platform-abstraction-layer.md](platform-abstraction-layer.md) | cross-platform / architecture | All OS-sensitive code in `src/platform.ts` only — ESLint enforced, **do not scatter platform checks** |
| [evolution-db-split.md](evolution-db-split.md) | evolution / storage / memory | Evolution uses `~/.deus/evolution.db`, memory indexer uses `~/.deus/memory.db` — **never share a database file between subsystems** |
| [pattern-verification-system.md](pattern-verification-system.md) | patterns / verification / drift | 7-mode check system for pattern files — static + LLM-behavioral verification catching content gaps, routing errors, and cross-pattern contradictions |
| [pattern-verification-deferred.md](pattern-verification-deferred.md) | patterns / deferred work | Implementation plans for 5 verification gaps deferred from `pattern-verification-system.md` — router tightening, cross-pattern contradictions, code-only rules, mtime noise, docs-vs-code |
| [kb-phase2-graph.md](kb-phase2-graph.md) | memory / graph / contradiction | Entity graph is text-keyed + rebuildable; contradictions invalidate never delete; detection is best-effort, max 5 LLM calls |
| [no-db-deletion.md](no-db-deletion.md) | data integrity / all DB ops | Never DELETE/DROP rows — use `orphaned_at`/`expired_at` soft-delete flags; rebuild = mark stale + re-verify, **do not revert** |
| [memory-tree.md](memory-tree.md) | memory / cold-start / retrieval | Collapsed-flat retrieval over ULID-keyed nodes + `see_also` edges; Ollama `embeddinggemma` embeds; own DB at `~/.deus/memory_tree.db`; thresholds **must** be calibrated, not hardcoded |
| [error-discipline.md](error-discipline.md) | errors / async / reliability | 4-class taxonomy (`RetryableError`/`UserError`/`FatalError`/`DeusError`) + bootstrap harness + async helpers — **every throw picks the class that tells the caller what to do** |
| [embedding-model-selection.md](embedding-model-selection.md) | memory / embeddings / models | Ollama `embeddinggemma` for memory tree; Gemini as fallback; provider-aware thresholds |
| [llama-cpp-optional-integration.md](llama-cpp-optional-integration.md) | llama.cpp / local models / embeddings | `llama.cpp` is optional via `/add-llama-cpp`; Ollama remains default for memory embeddings and local judge until a benchmark-gated provider switch |
| [benchmark-regression-gate.md](benchmark-regression-gate.md) | memory / retrieval / CI | CI label validation + local snapshot gate for memory_tree benchmark; score-gap guard uses `abstain_threshold + gap_threshold` — **do not revert to `low_threshold`** |
| [backend-neutral-agent-runtime.md](backend-neutral-agent-runtime.md) | agent runtime / backends / tools | Deus owns runtime/session/tool/context contracts; Claude is default adapter, OpenAI/Codex is opt-in; never resume sessions across backend mismatch |
