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
| [vault-autoload.md](vault-autoload.md) | startup / vault / tokens | `vault_autoload` in config.json controls which vault files load at startup; default `["CLAUDE.md"]` — all others on-demand; **do not hardcode vault file lists in the launcher** |
| [backend-strategy-trait.md](backend-strategy-trait.md) | backends / providers / TUI | Provider integrations implement a `Backend` trait (strategy pattern) — adding a provider is 1 file + 2 lines in `backend/mod.rs`; **do not inline provider logic in app.rs** |
| [parallel-agent-orchestration.md](parallel-agent-orchestration.md) | TUI / parallel agents / sessions | Strategy+Mediator: `HashMap<SessionId, Session>` with per-session `Box<dyn Backend>` and `sync_channel`; sessions backend-scoped 1:1; **manual spawn first, auto-spawn deferred** |
| [tui-agent-orchestration.md](tui-agent-orchestration.md) | TUI / agents / communication | Independent subprocesses with mpsc channels; per-session lifecycle, timeout, auto-GC; Backend trait strategy for multi-provider; 50ms poll loop |
| [parry-guard-installation.md](parry-guard-installation.md) | security / prompt injection / host | parry-guard as Layer 2 host Claude Code defense — DeBERTa ML + tree-sitter AST via PreToolUse/PostToolUse hooks; containers use separate Layer 1 scanner |
| [tui-permission-bridge.md](tui-permission-bridge.md) | TUI / permissions / hooks | File-based IPC between PreToolUse hook and TUI for interactive permission approval in piped mode; `--settings` per-invocation; Claude-only; **120s timeout with deny fallback** |
| [multi-agent-orchestration-research.md](multi-agent-orchestration-research.md) | multi-agent / orchestration / containers | Thin custom orchestrator over agents-js Runner — container LLM model preserved; topological task sort with parallel fan-out for reads; **Patterns 1-2 only, defer 3-4** |
| [threshold-calibration-sweep.md](threshold-calibration-sweep.md) | memory / retrieval / calibration | `deus sweep` grid-searches retrieval thresholds using Pareto selection (recall ≥ floor, maximize abstain_acc); **run after any retrieval pipeline change** |
| [atom-fallback-evaluation.md](atom-fallback-evaluation.md) | memory / atoms / retrieval | Atom fallback disabled by default — `embeddinggemma` L2 distances too compressed for atom-length text; re-enable via `DEUS_ATOM_DIST` only after benchmark-validated improvement |
| [atom-retrieval-pipeline.md](atom-retrieval-pipeline.md) | memory / atoms / retrieval | Multi-stage atom retrieval: approach angles (HyPE) + cross-encoder reranker + BM25 rescue + entity boost. Recall 0.500 to 0.900. Post-merge: run `--backfill-atom-angles` |
| [standards-pack-priority.md](standards-pack-priority.md) | standards_pack / priority / atoms | `priority: high\|med\|low` frontmatter on `kind: standard` atoms; sort by `(priority_rank, filename)`; CRITICAL stderr + `dropped_high` cache field on high-priority drop. `priority: high` requires `.claude/rules/core-behavioral-rules.md` citation. Inclusion is NOT unconditionally guaranteed — see ADR |
| [printing-press-adoption.md](printing-press-adoption.md) | agent-native / CLI / MCP / token-efficiency | Typed exit codes + compact/select for Python CLIs and MCP servers; 4-phase roadmap: protocol → tool proxy → SQLite cache → codegen. Extends error-discipline.md taxonomy to subprocess callers |
| [hook-dispatch-system.md](hook-dispatch-system.md) | hooks / dispatch / backends | Deus-owned hook contract with Bridge pattern; host-enforced Layer 1 + container-cooperative Layer 2; HookDispatchService on :3002 |
| [judge-lora-specialization.md](judge-lora-specialization.md) | eval / judge / LoRA | Negative result on Gemma-3n-E4B Q4 (Adapter mean Pearson 0.368 vs Base 0.390) — do NOT adopt run `20260518T071842Z-1614baf-dirty`; keep base Q4 as local judge. **Future judge-quality work begins with cross-stack truth bench + replace-base / logit-mean / frozen-head alternatives BEFORE any retune; retune requires regression bar mean Pearson ≥ Base + 0.05** |

## Related documentation

| File | Purpose |
|------|---------|
| [../exec-plans/README.md](../exec-plans/README.md) | Execution plan convention — versioned artifacts capturing why/how decisions are made during multi-step implementations (EP-NNN scheme) |
| [../QUALITY_GRADES.md](../QUALITY_GRADES.md) | Per-subsystem health grades — at-a-glance A/B/C/D ratings with last-audit dates and debt refs |
