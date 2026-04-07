# Architecture Decision Records (ADR Index)

One line per decision. Load the full file only when the topic is directly relevant to what you're about to change.

| File | Topic | One-line ruling |
|------|-------|-----------------|
| [eval-ipc-file-output.md](eval-ipc-file-output.md) | eval / Docker / IPC | Results via shared-volume files, not stdout — Docker pipe buffering is permanent, **do not revert** |
| [eval-no-disk-cache.md](eval-no-disk-cache.md) | eval / caching | In-memory cache only — disk cache silently masks regressions across builds |
| [eval-selective-warmup.md](eval-selective-warmup.md) | eval / warmup / concurrency | Warm only active test datasets — saves ~3× time, avoids API rate saturation |
| [startup-gate.md](startup-gate.md) | startup / onboarding / memory | Check registry pattern; channels optional; memory system is the priority; vault path configurable |
| [platform-abstraction-layer.md](platform-abstraction-layer.md) | cross-platform / architecture | All OS-sensitive code in `src/platform.ts` only — ESLint enforced, **do not scatter platform checks** |
