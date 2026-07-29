# EP-001: Skip memory-retrieval on synthetic task-notification prompts

**Status:** active
**Branch:** worktree-fluttering-twirling-parasol
**ADRs consulted:** threshold-calibration-sweep, benchmark-regression-gate, memory-tree, standards-pack-priority, procedure-memory-default-on (docs/decisions/INDEX.md)
**Opened:** 2026-07-28
**Closed:** --

## Goal

`scripts/memory_retrieval_hook.py` (a `UserPromptSubmit` hook) runs full semantic retrieval + injection on synthetic, system-generated prompts (background-agent completion notifications), which don't need memory context. Independently reconfirmed across 4 separate review passes: 158/226/608 (varying by scan scope, never reproduced exactly) marker-bearing user messages found across real transcripts, the large majority anchored-exact (case-sensitive, no attributes, at the very start of the prompt), and the small non-anchored minority consistently session-summarizer/autonomous-loop prompts quoting transcript text mid-message -- precisely the false-positive class this fix's anchored-match design must not trip on (and doesn't). Three earlier attempts to pin an exact injection-percentage target (23.6% / 44.5% / 57.5%) did not reproduce each other -- the qualitative finding (non-trivial, worth fixing) is solid and independently reconfirmed at every scale tried; no single precise percentage is asserted as a target here.

This fix has a SECOND, related effect: the early return also skips `session_concepts.extract_terms`/`update_concepts` (`memory_retrieval_hook.py:37-38`), which today accumulates notification-prompt tokens into a session-scoped, monotonically-growing top-20 concept set appended to the FTS query for EVERY subsequent prompt in that session (`memory_tree.py:1544`). Expected direction: an improvement (noise no longer crowds out real query terms) -- but UNMEASURED by anything in this plan's validation, called out explicitly rather than left implicit.

Done = the fix is implemented at the exact placement specified, `scripts/tests/test_memory_retrieval_hook.py` covers all 5 cases in Validation, `tests/test_session_type_contract.py` still passes, and the live measurements described in Validation are recorded in this EP's decision log once available.

**Explicitly out of scope, split to follow-on EPs:** excluding already-statically-preloaded vault files (originally "Fix 3" -- false premise found, `DEUS_VAULT_PRELOADED=1` is set even when the preload block is skipped in restricted-mode sessions -- needs a `deus-cmd.sh` redesign, deserves its own dedicated plan); the duplicate hook-registration double-firing problem (originally "Fix 2" -- neither registration can be safely deleted, one gives cross-machine coverage, the other ships with the public repo); the CLAUDE.md-oversized-node dedup problem (originally "Fix 4" -- real position-dependent correctness bug found, near-zero residual benefit); consolidating this fix's marker-matching with the repo's other prompt/text-pattern classifiers (`evolution/cc_backfill.py`, `evolution/mine_implicit_feedback.py`, `evolution/trec_atom_benchmark.py`) -- deferred to EP-002 (not yet filed, next available EP slot); a formal context-residency-tier system; chunking CLAUDE.md; receipt/pointer-based injection.

## Origin

This EP began as a 4-fix plan from a session-long investigation into per-turn memory-retrieval token cost (two independent AI brainstorming passes, Claude/Fable + GPT 5.6 Sol). Across 9 rounds of plan-review on both the Claude backend and the GPT co-gate, 3 of the original 4 fixes were dropped after real, structurally-distinct defects were found in every design attempted for them (see decision log below for the condensed history). Only Fix 1 (this EP) survived every round without a design defect -- later rounds found and fixed real precision issues: a broken test oracle (the file's own `_load_hook` fixture pre-populates the exact modules the oracle needed absent), an unexecutable acceptance check (wrong hash length, no positive control, a confounding second log writer), and a documented second effect on session-concept pollution. All resolved; final verdict SHIP on both backends against the exact content below.

## Alternatives considered

| Approach | Tradeoff | Why rejected |
|----------|----------|--------------|
| Idempotency marker/lock in `memory_retrieval_hook.py` to survive dual registration (2 design iterations tried) | Would eliminate double-firing cost specifically inside ~/deus sessions | Found broken on 2 separate grounds across 2 rounds (TTL-window ambiguity; lock-overlap assumption) |
| Delete one of the two hook registrations (host-scope, then reconsidered as project-scope) | Zero new code either way | Both directions break coverage somewhere: host-scope deletion breaks retrieval in every other project on this machine; project-scope deletion breaks retrieval for every other Deus repo user. No safe deletion target exists |
| Fix the oversized-node dedup problem (original Fix 4) in this pass | Would make CLAUDE.md dedupable in the sessions Fix 3 doesn't cover | Two independent, structurally different correctness bugs found (position-dependent slice key; missing full-content identity in the key); tiny residual benefit; touches the highest-risk shared code (`recall()`'s LIA-355 mark-only-what-survives arithmetic, used by MCP + container bridge, with a TypeScript twin in `container/agent-runner/src/memory-dedup.ts` that would silently diverge). Deferred to a dedicated future pass |
| New env var (`DEUS_VAULT_PRELOADED_FILES`) exported by `deus-cmd.sh` for Fix 3 | Explicit | Would become a THIRD independent resolver of the preloaded-file set -- `feedback_no_duplication`. Deferred along with the rest of Fix 3 |
| Lower `MAX_CONTEXT_CHARS` (4096 cap) instead of fixing allocation | Cheap one-line change | Anti-pattern per both brainstorms: budget already saturated with low-value bytes; shrinking gives less of the same low-value content |

## Chosen approach

**Fix 1 -- skip retrieval on synthetic prompts, placed before the deferred imports.** In `memory_retrieval_hook.py`, immediately after the existing `if not prompt or len(prompt) < MIN_PROMPT_LEN: return` block and BEFORE the deferred `import session_concepts as sc` / `import memory_query as mq` lines (`:32-33`), add: if the prompt, left-stripped, STARTS WITH the literal, CASE-SENSITIVE marker `<task-notification>` (anchored match, not a substring search -- a user prompt that merely quotes/discusses a notification must not match), return immediately. Placement here is load-bearing for two reasons: it preserves the ~200ms saving the deferred-import comment at `:31` exists to protect, and it is what actually stops the concept-store pollution described in the Goal (a later placement would still import and run `session_concepts.update_concepts` before returning, missing that benefit entirely).

Case-sensitive, anchored-only: confirmed via review that all real anchored occurrences observed are lowercase-exact with no attributes, and that substring matching would false-positive on real quoted-transcript prompts (multiple non-anchored cases found in production data across several independent scans).

**Accepted, bounded risk (named explicitly, not left implicit):** a human prompt that itself begins with the literal text `<task-notification>` (e.g. pasting one verbatim to ask about it) will also skip retrieval for that one turn. This is a real, if narrow, exception to the "fail-open: on doubt, retrieve" principle used elsewhere in this hook. Accepted because the failure mode is bounded and self-recovering (one missed injection; the next turn's prompt won't match and retrieval resumes normally) -- not because it's impossible.

**Side effect, not fixed here, no mitigation needed:** a task-notification prompt no longer produces a `_log_retrieval` entry in `~/.deus/memory_retrieval_log.jsonl` (that write only happens inside `recall()`, which this early return never reaches) -- confirmed no code anywhere in the repo reads THIS SPECIFIC log for analysis (writers only). Separately, this early return also suppresses a write to a SECOND, different log: `memory_tree.py`'s `_log_query` writes to `~/.deus/memory_tree_queries.jsonl`, which unlike the log above DOES have a real reader (`scripts/trec_atom_benchmark.py:74`). No mitigation needed there either: that reader already explicitly skips `<task-notification>`-prefixed entries on its own (`trec_atom_benchmark.py:83`), so this fix's effect on that second log is consistent with what its one real consumer already expects. The replacement observability for both logs is the unit tests (below) plus the two-part hash-based live check (below), not a permanent log entry in either.

## Validation

Unit tests, extending `scripts/tests/test_memory_retrieval_hook.py`:
1. A prompt starting with `<task-notification>` (after left-strip) returns immediately with empty stdout (matching the existing empty-stdout precedent at `:124-132`), and neither `memory_query` nor `session_concepts` end up in `sys.modules` as a result of this call. VERIFIED CONSTRAINT: the test file's existing `_load_hook` fixture (`:77-105`) re-inserts BOTH modules into `sys.modules` via `monkeypatch.setitem` at `:96-97` as part of its own setup -- using it for this test would make the absence-assertion fail regardless of correctness (a captured-oracle bug). This test must NOT use `_load_hook`; instead load the hook module fresh under its own name with no stub modules pre-inserted, and use `monkeypatch.delitem(sys.modules, name, raising=False)` for both `memory_query` and `session_concepts` immediately before invoking it, then assert both remain absent from `sys.modules` after.
2. A prompt that merely CONTAINS `<task-notification>` later in its text (not at the start, e.g. quoting a notification while asking a question) is NOT skipped -- the concrete anchored-vs-substring regression test, using a real example shape found in review.
3. A prompt with LEADING WHITESPACE/NEWLINES before the marker is still correctly matched after stripping (explicit case, not assumed).
4. The exact marker text with different casing (e.g. `<Task-Notification>`) is NOT matched -- confirms case-sensitivity explicitly, matching the observed real-data shape.
5. A marker-only prompt (`<task-notification>`, verified 19 chars) clears the pre-existing `MIN_PROMPT_LEN` check (verified `MIN_PROMPT_LEN = 10` at `memory_retrieval_hook.py:14`; 19 > 10) and reaches the new early-return logic -- explicit ordering test grounded in the verified constant value, not an assumed length.

**Regression check:** run `tests/test_session_type_contract.py` -- unaffected by this change (hook-local, no settings/contract changes), included as a regression check.

**`deus sweep` (calibrate-sweep):** run as a cheap no-regression sanity check ONLY. Confirmed on review that this fix does not touch `retrieve()` or the concept-store input in a way `calibrate_sweep` can observe (`memory_tree.py:2643` runs directly on the tree DB, with no session-concept store) -- `docs/decisions/procedure-memory-default-on.md` already documents this exact blind spot for a related class of change ("`deus sweep` sweeps `retrieve()`, not `memory_recall`"), directly supporting treating the sweep as sanity-only here too.

## Pre-change baseline

Captured via `python3 scripts/memory_tree.py calibrate-sweep scripts/tests/fixtures/memory_tree_queries.jsonl --json` at current defaults, before any code change in this EP:

| Metric | Value |
|---|---|
| recall | 0.676 |
| mrr | 0.507 |
| abstain_accuracy | 0.636 |
| current defaults | abstain_threshold=0.31, gap_threshold=0.06, coverage_threshold=0.5, content_cap=0.35, min_entity_overlap=1 |

Note: the sweep's own `min_recall_constraint` is 0.7 and reports `feasible_count: 0` across all 1440 combos tried -- i.e. no parameter combination in the sweep's search space currently clears 0.7 recall, including the shipped defaults (0.676). This is PRE-EXISTING and unrelated to this EP's changes (this fix does not touch scoring/threshold logic) -- recorded here only so the post-change comparison isn't misread as a regression this EP introduced.

**Live measurement 1 (retrieval-log absence, with a positive control, source-scoped):** the log stores `sha256(query).hexdigest()[:16]` (first 16 hex chars, verified `memory_query.py:149`). Two-part check: (a) POSITIVE CONTROL -- a PRE-merge task-notification prompt's hash confirmed PRESENT in the existing log (executed live during review: 30/30, later reconfirmed 607/607, all with `source == "repo-hook"`). (b) a POST-merge task-notification prompt's hash, once one fires live after this fix ships, confirmed NOT present in any new log line WITH `source == "repo-hook"` specifically -- scoping required since the log has a second, independent writer (`codex_warden_hooks.py:2792`'s `run_memory_retrieval`) that applies no filtering and never sets `source` (confirmed live: 243/2015 sampled entries have no `source` field), which could otherwise confound an unscoped absence-check.

**Live measurement 2 (exploratory, not pass/fail):** dump the session concept-store file before/after a live session with notifications, check whether notification-shaped tokens are present before this fix and absent after. Record the observation in the decision log once available.

## Progress checklist

- [x] Capture pre-change `deus sweep` baseline
- [x] plan-reviewer SHIP, Claude backend (9 rounds total across the full investigation)
- [x] plan-reviewer SHIP, GPT co-gate (9 rounds total)
- [x] Fix 1 implemented + unit tests (all 5 cases) — 13/13 pass in `scripts/tests/test_memory_retrieval_hook.py`. Red-green confirmed: code-reviewer's second-lens mutation (early return disabled) made 3 of the 5 new tests fail; source restored byte-for-byte and re-verified green
- [x] `tests/test_session_type_contract.py` still passes unmodified — 63/63 pass
- [x] Post-change `deus sweep` run, compared against baseline — recall 0.676 and abstain_accuracy 0.636 bit-identical to the pre-change baseline; mrr 0.5 vs baseline's 0.507 is a trivial, run-to-run-stable wobble mechanically unrelated to this fix (confirmed: `calibrate_sweep()`/`benchmark()` never invoke `memory_retrieval_hook.py`, only `memory_tree.py`'s own scoring path -- consistent with this EP's own "sanity-only" framing). `min_recall_constraint=0.7` still infeasible at every combo (pre-existing, unrelated)
- [x] Post-change retrieval-log check (live measurement 1) — fired hook with a synthetic `<task-notification>` prompt, confirmed 0 matching `source=repo-hook` lines before and after (hash `9239a118d6084780`). Also ran a REAL Claude Code session smoke test: an isolated scratch project (`.claude/settings.json` UserPromptSubmit hook pointed at this worktree's fixed script, not the shared host-registered copy) received a live `<task-notification>` prompt via `claude -p`; a temporary debug marker (removed after) confirmed the early-return branch actually executed through the real harness. Session IDs: `5e9403ed-a4fc-46c7-aac7-0f4aa53f6511`, `6695a594-f4f1-44bc-813a-7548ff27d209` (transcripts under `~/.claude/projects/-Users-liamsteiner--claude-jobs-a1376e26-tmp-ep001-smoke-test/`)
- [x] code-reviewer SHIP — both backends: GPT co-gate SHIP round 1; Claude SHIP round 2 (round 1 REVISE on 3 blocking issues, all fixed and independently re-verified)
- [x] Commit message proposed, user approval obtained — commit `1d110ac`
- [x] PR opened on the fork remote, not merged without explicit approval — PR #38 (draft), https://github.com/liam-cyberpro/Deus/pull/38

## Decision log

| Date | Decision | Reasoning |
|------|----------|-----------|
| 2026-07-28 | Fix 2 (double-firing) dropped from this pass entirely, after 3 rounds of review each finding a structurally different bug | TTL-marker semantics wrong (round 1), lock-overlap assumption wrong (round 2), and no safe deletion target exists for either registration since each covers a different real audience (round 3) |
| 2026-07-28 | Original Fix 4 (oversized-node dedup) dropped from this pass entirely | Two independent, structurally different correctness bugs found on review; near-zero residual benefit once Fix 3 ships; highest-risk change in the set |
| 2026-07-28 | Fix 3 (exclude preloaded vault files) dropped from this pass, deferred to future EP | Core premise found false: `DEUS_VAULT_PRELOADED=1` does not imply the preload actually ran (restricted-mode sessions skip the preload block but still set the flag) -- real retrieval-channel data-loss risk as designed |
| 2026-07-28 | Fix 1's test 1 oracle redesigned to avoid `_load_hook` | That fixture re-inserts the exact modules the test needed absent, making the original oracle self-defeating regardless of implementation correctness |
| 2026-07-28 | Fix 1's live measurement 1 redesigned with a positive control + source scoping | Original design was unexecutable (wrong hash length assumed) and confoundable (a second, unfiltered log writer with no `source` field) |
| 2026-07-28 | Dropped the `[SYSTEM NOTIFICATION` marker, kept only `<task-notification>` | Zero confirmed occurrences anywhere in the repo or any sampled transcript across 2 independent checks |
| 2026-07-28 | Did not commit to a single frozen injection-percentage target | Multiple independent measurement attempts did not reproduce each other despite each using a real method on real data -- the qualitative finding is solid; the precise number is not |
