# Orchestration Rules
# Applies to all agent dispatch, pipeline automation, and task orchestration.
# Covers: issue creation, gate discipline, state management, MCP tool hygiene,
# warden co-gate verdict marking, session-start freshness, and pipeline co-authorship.
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
- When a pipeline loop is detected, stabilize first: move the issue to a safe state (Manual Review Required or Backlog) before investigating. Never debug a live loop.

## Agent Dispatch
- Dispatched agents must work against the current issue state. If the issue was modified after dispatch, the agent's output is suspect — re-evaluate before accepting.
- Agent output that doesn't match the issue's acceptance criteria should not auto-merge, even if CI passes. The output-quality-gate exists for this.
- Failed dispatches (auth errors, container failures, timeouts) must be surfaced with clear error state — not silently swallowed.

## Tool Hygiene
- When creating or updating issues via MCP tools, verify the rendered output matches intent. Double-escaped markdown, broken formatting, and missing fields are bugs, not cosmetic issues — they degrade agent scoping and human review.

## Warden Co-Gate Verdict Marking
- The plan-review (edit) and code-review/verification/ai-eng (commit) gates decide on the verdict STORE (`.warden-verdicts.json`), not the marker files. The `run()` hook dispatcher resolves the bucket from the **hook EVENT's cwd** (`event["cwd"]`, which equals the committing session's working directory) and pins it via `worktree_override` for every gate runner — deliberately NOT the hook process's `os.getcwd()`, since the two can differ. Mark verdicts (both claude + gpt backends) into the bucket that matches that cwd, or the gate won't see them.
- cwd is the MAIN repo (`~/deus`, editing a worktree's files via `git -C <wt>`): the gate reads the FLAT `.claude/.warden-verdicts.json`. Mark from cwd=`~/deus` with NO `--worktree-root`.
- cwd is INSIDE a linked worktree (`EnterWorktree`, or Claude launched there): the gate reads the per-worktree `.claude/worktree-markers/<sha1(worktree_abspath)[:12]>/.warden-verdicts.json`. Mark from that cwd with NO `--worktree-root`, or from any cwd with `--worktree-root <wt>`. A flat mark is IGNORED here.
- Rule of thumb: match the bucket to the committing session's cwd — not "always flat" nor "always worktree". `--worktree-root` is only for an out-of-band writer whose cwd is not the gate's worktree (e.g. the gpt driver, or marking for a worktree you are not cwd'd into). If `mark`/`record-verdict` prints "cwd is not inside a worktree ... using the main-repo (flat) bucket", your mark went to the flat bucket — intended only for case 1.
- Mechanism (source of truth): `codex_warden_hooks.py` `run` (event-cwd → `worktree_override` for every gate runner), `_claude_marker_dir` (bucket resolution), `_current_worktree`/`_worktree_for_cwd` (cwd→worktree), `_with_cli_worktree` (CLI writer side), `run_warden_backends_gate` (gate read); `warden_hooks/verdict_store.py` `_verdicts_path`.
- SUPERSEDES RETRO-2026-06-17-02's "always mark from inside the worktree with `--worktree-root`": that is correct only for case 2, and post-#868/#869 the cwd-dependent rule above is canonical.
- Canonical recipe (avoids the bucket split): keep the committing session's cwd ON the worktree the commit targets — enter it via `EnterWorktree` (or commit from inside it) so the edit gate, the review agents, the marks, and the commit gate ALL resolve the same bucket from cwd, and mark with NO `--worktree-root`. Reserve `--worktree-root` for marking a worktree you are not cwd'd into. A `git -C <wt>`/`cd <wt> &&` commit from a main-repo session does NOT move the hook's cwd, so the gate reads the FLAT bucket while a `--worktree-root` mark lands in the worktree-sha bucket — the classic mismatch.
- Diagnostic: when the backends-gate blocks a model backend as "not run yet" but a SHIP for it exists in a different bucket, the block message now prints `co-gate bucket mismatch: a SHIP for <role>@<backend> exists in <dir>; this gate reads <dir> (worktree=...)` with the corrective re-mark — so a wrong-bucket mark is one clear message, not a silent retry loop (`_buckets_with_ship`, display-only, never changes acceptance).
- Agent-captured verdicts: the harness's Agent PostToolUse event carries the LAUNCH-dir cwd, not the session's EnterWorktree'd cwd, so the tracker routes by the worktree path named in the dispatch prompt (LIA-376, `_worktree_from_prompt`) and writes ONLY to that worktree's bucket. Name exactly ONE worktree path in every warden dispatch prompt, and make it the review target — an incidentally-named worktree receives the verdict (text routing cannot know intent). Zero or multiple named worktrees fall back to the event-cwd bucket.
- Re-review rounds: verdicts from a RESUMED agent (SendMessage) arrive as task-notifications no hook can see — the tracker keeps the stale round-1 verdict. Always dispatch a FRESH warden agent for each re-review round; a manual mark citing a resumed-agent SHIP is the exception, not the flow.

## Session-Start State Freshness
- Before treating local/worktree state as ground truth or implementing anything, run `git fetch origin` then `git --no-pager diff --stat HEAD origin/main`. Worktrees are pinned at creation and `origin/main` merges continuously (autonomous pipeline + work-fork), so a local checkout can be days behind.
- For any task that "needs implementing," first confirm it is not already on `origin/main` — a stale start otherwise reconstructs work that already shipped. Verify-don't-trust catches bad code before it lands, but the rediscovery cost is paid regardless; the fetch is the cheap defense.

## Autonomous Pipeline Co-Authorship
- The autonomous pipeline and work-fork are routine co-authors of `origin/main` and may merge work mid-session/overnight. Before building on or deploying their merged work: (i) re-check merge state via `mergeCommit`/`mergedAt` (not a stale "OPEN" read), (ii) for load-bearing surfaces (memory heart, gates) re-verify the controlling invariant first-hand, (iii) chain a distinct session log via `continues` — never overwrite the pipeline's logs on `/compress`.
- A pipeline merge is a hypothesis until verified first-hand, same as any delegated verdict (core-behavioral-rules.md § Verification & Honesty). Don't race the pipeline on its own PRs.
