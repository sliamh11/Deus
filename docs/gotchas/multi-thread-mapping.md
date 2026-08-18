# Multi-Thread Work Mapping (Wayfinder-Inspired)

**Read this when you are planning a multi-thread effort too big for one session.**

Moved verbatim out of `.claude/rules/orchestration-rules.md` (which is
always-loaded, and was over Claude Code's 40.8k per-file limit) so it loads
only when the task calls for it. No rule below has been reworded.
Routed by [`.mex/ROUTER.md`](../../.mex/ROUTER.md); index at
[`docs/gotchas/INDEX.md`](INDEX.md).

- For a loose idea too big for one session — multiple dependent-but-distinct threads branching off a single ask (e.g. "ship deus-v2's UI": research → TUI spike → TUI follow-up → web spec → infra follow-ups) — don't let related Linear issues stay scattered with only loose links between them. Adapted from `mattpocock/skills`' `wayfinder` skill (evaluated 2026-07-21, pattern borrowed rather than the skill installed — see the session log for the full comparison against this repo's existing Linear pipeline).

- Create one index issue (or Linear project doc) per multi-thread effort that tracks, in one place: what "done" looks like, decisions already resolved (one-line summaries + links to their issues), open questions still too fuzzy to ticket ("fog" — explicitly named as unresolved, not silently assumed), and what's consciously out of scope. Update it as threads resolve or new ones branch off — this is an index that gists and links, it never restates what's already recorded in the linked issues/session logs.

- Distinguish a **decision to make** from **work to execute** before ticketing it. A ticket should be a sharply-defined question ("should X use profile A or B") sized for one session, not a vague slice of the destination. This is the same discipline `core-behavioral-rules.md`'s "don't solve problems that don't exist yet" already asks for, applied to planning granularity specifically.

- This composes with `linear-slice` (tracer-bullet vertical slices) rather than replacing it — `linear-slice` handles decomposing an already-scoped plan into dependency-ordered execution issues; this section is about the earlier step of keeping the still-fuzzy, multi-thread *planning* state visible and current instead of scattered across chat history and disconnected issues.
