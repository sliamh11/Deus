# Quality Grades

Per-subsystem health at a glance. Updated manually or by the doc-gardening agent.

**Scale:** A (clean, well-tested) | B (functional, minor gaps) | C (functional, known gaps) | D (fragile, active issues)

| Subsystem | Grade | Last audited | Key gaps | Debt/ADR refs |
|-----------|-------|-------------|----------|---------------|
| Memory tree | B | 2026-05-16 | Atom fallback disabled pending benchmark | [atom-fallback-evaluation](decisions/atom-fallback-evaluation.md) |
| Scheduler/cron | A | 2026-05-16 | -- | -- |
| Warden gates | B | 2026-05-16 | No remediation instructions | -- |
| Backends (Claude/OpenAI) | B | 2026-05-16 | Codex hook parity open | [AAG-010](agent-agnostic-debt.md) |
| Eval/benchmarks | A | 2026-05-16 | -- | [benchmark-regression-gate](decisions/benchmark-regression-gate.md) |
| Channel layer | B | 2026-05-16 | Agent-native migration WIP | -- |
| TUI/CLI | C | 2026-05-16 | Visual verification deferred 6+ times | [tui-agent-orchestration](decisions/tui-agent-orchestration.md) |
| Pattern verification | C | 2026-05-16 | 4/5 gaps open | [pattern-verification-deferred](decisions/pattern-verification-deferred.md) |
