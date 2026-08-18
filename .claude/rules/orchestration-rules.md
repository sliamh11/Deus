# Orchestration Rules
# Applies to all agent dispatch, pipeline automation, and task orchestration.
# Covers: issue creation, gate discipline, state management, MCP tool hygiene,
# warden co-gate verdict marking, session-start freshness, and pipeline co-authorship.
# Separated from core-behavioral-rules.md because these are pipeline-specific
# (not general coding/commit rules) and were triggered by observed failures:
# auth-error fallbacks labeled as "Scoped", agents working on stale requirements
# after mid-flight description changes, and double-escaped MCP tool output.
#
# CONTEXT BUDGET: every file in .claude/rules/ is injected into the system prompt on
# EVERY turn. Task-specific detail therefore does NOT belong here — it lives in
# docs/gotchas/ (see docs/gotchas/INDEX.md) and is routed per task type by
# .mex/ROUTER.md. Keep this file to rules that must fire unprompted. When capturing a
# new gotcha, append it to the owning docs/gotchas/ file, not to this one.

## Issue Creation
- Always assign issues to the correct project. Never leave issues floating without a project.
- Use actual newlines in descriptions — never `\n` escape sequences. MCP tools double-escape them, producing literal `\\n` in rendered output.

## Pipeline State Integrity
- If an issue's scope or description changes after entering the pipeline, move it back to the relevant step. Scope changed → back to the scoping step so the gate re-evaluates. Never leave an agent working on stale requirements.
- An issue may only advance past a gate when the gate agent ran successfully and approved. Any other outcome (error, timeout, crash, auth failure) is not approval — it's a failure that needs investigation or retry.
- The "Scoped" label may only be applied when the readiness gate produces a real scope block with enrichment. Fallback verdicts from failed gates are not scoping.
- **A Linear ticket showing `Done` means its PR merged — it does not mean the ticket's full scope is implemented.** Confirmed 2026-08-08 (LIA-519 roadmap): LIA-531 (credential-separation) merged and moved to `Done`, but its own PR body said "Design-only, implementation deferred to a future ticket" — a downstream session (LIA-536) nearly read "LIA-531 Done" as "activation-ready" for a dependent ruleset before catching the discrepancy by reading the merged PR's actual body, not just its Linear state. Before treating any `Done` ticket as clearing a real dependency, read the merged PR's own description for scope caveats ("design-only," "implementation deferred," "deliberately not touched") — the state field alone is not sufficient evidence of what shipped.

## Gate Discipline
- Gate fallbacks are errors, not approvals. If a gate agent fails, the verdict must be ERROR with a visible error label — never SHIP. Fallback-SHIP silently bypasses quality gates and produces false labels on unreviewed work.
- Never auto-advance an issue past a gate that didn't actually run. Silence is not consent.
- REVISE handling follows core-behavioral-rules.md: re-run after fixes until SHIP, no exceptions.
- Different warden roles/backends can reach opposite verdicts on the identical residual-risk judgment call — not a bucket/routing bug, a genuine disagreement (confirmed 2026-08-13, LIA-550/cliproxy credential fix: `threat-modeler` SHIPped a cleanup script's concurrency guard after 5 rounds, explicitly accepting the residual TOCTOU window as irreducible; the `code-reviewer` co-gate's GPT backend independently and repeatedly REVISE'd the identical window across 3 separate rounds, refusing to accept it). Treat the stricter verdict as controlling — do not override a specific, well-understood REVISE just because a different role/backend SHIPped the same risk. The correct resolution is to re-scope (split the disputed component out, ship the rest) rather than loop indefinitely trying to satisfy both, and rather than picking whichever verdict is more convenient. This is the same round-count-circuit-breaker instinct (`plan-review-rules.md`) applied to a cross-role disagreement instead of a single role's non-convergence.
- When a pipeline loop is detected, stabilize first: move the issue to a safe state (Manual Review Required or Backlog) before investigating. Never debug a live loop.
- The commit-gate string-matches `git commit` anywhere in a Bash command and blocks the ENTIRE command before any of it runs — this isn't limited to warden marks chained before a commit (a previously-documented case, in personal vault memory, not this repo); it also silently drops a leading `git add` in the same call, e.g. `git add -A && git status && git commit -m ...` never stages anything (confirmed 2026-08-08, checklist.design-integration PR #1133 — a code-reviewer pass caught the staged diff missing a fix that was genuinely present in the working tree). Always issue `git commit` as its own isolated Bash call, with `git add`/staging done in a prior, separate call — never chained with `&&` before it.

## Agent Dispatch
- Dispatched agents must work against the current issue state. If the issue was modified after dispatch, the agent's output is suspect — re-evaluate before accepting.
- Agent output that doesn't match the issue's acceptance criteria should not auto-merge, even if CI passes. The output-quality-gate exists for this.
- Failed dispatches (auth errors, container failures, timeouts) must be surfaced with clear error state — not silently swallowed.
- **`subagent_type: "general-purpose"` is denied in this repo** (2026-08-09, confirmed via `.claude/settings.json:4`: `"deny": ["Agent(Explore)", "Agent(general-purpose)", "Agent(Plan)"]`). Use `"general"` instead — it's the equivalent catch-all agent type actually permitted here. A denied dispatch fails immediately with a permission-rule error, not silently, but it still wastes a round trip; default to `"general"` for open-ended delegated research/tasks in this repo.

## Tool Hygiene
- When creating or updating issues via MCP tools, verify the rendered output matches intent. Double-escaped markdown, broken formatting, and missing fields are bugs, not cosmetic issues — they degrade agent scoping and human review.

## Warden Co-Gate Verdict Marking
- Match the verdict bucket to the **committing session's cwd**, and prefer `scripts/cogate.py` over a direct `mark` call — a wrong-bucket mark prints success, exits 0, and still blocks the next edit.
- Full detail — mechanism, evidence and cost for every rule in this section — is in [`docs/gotchas/warden-co-gate.md`](../../docs/gotchas/warden-co-gate.md); read it when you are marking warden verdicts, running `scripts/cogate.py`, or a plan-review / commit gate has blocked you.

## Gate Invocation Preflight (RETRO-2026-07-13-02)
- Before invoking ANY warden/gate command (GPT `codex_warden`, edit-gate, verification-gate, or any Warden Co-Gate mark above), run a one-line preflight: `git rev-parse --show-toplevel` (confirms which repo/worktree you're actually in) + `git status --short` (confirms any new files are `git add`ed, so the diff isn't empty). Eyeball both before the gate call, not after.
- This exists because every gate/mark decision above is cwd- and worktree-sensitive - the GPT backend reads its diff from cwd, the co-gate bucket is chosen by cwd - and the recurring failure mode isn't a wrong rule, it's the agent's mental model of "where am I running from" drifting across a `cd`/EnterWorktree boundary. A correctly-written fail-closed gate that silently no-ops on an empty diff (abstain) reads identical to "nothing to review" - the one case where silence means the protection is off.
- Concretely: ran the GPT warden twice from the main-repo cwd by mistake (empty-change abstain) instead of from inside the worktree; a `cd` moved the hook event's cwd mid-session without the agent noticing, splitting marks across the flat and worktree-sha buckets. The preflight above catches both before the gate call, not after a confusing abstain or bucket-mismatch message.

## Session-Start State Freshness
- Before treating local/worktree state as ground truth or implementing anything, run `git fetch origin` then `git --no-pager diff --stat HEAD origin/main`. Worktrees are pinned at creation and `origin/main` merges continuously (autonomous pipeline + work-fork), so a local checkout can be days behind.
- For any task that "needs implementing," first confirm it is not already on `origin/main` — a stale start otherwise reconstructs work that already shipped. Verify-don't-trust catches bad code before it lands, but the rediscovery cost is paid regardless; the fetch is the cheap defense.
- Merged is not deployed: a daemon keeps serving whatever it loaded at process start, and a plist on disk is not a loaded job — check `launchctl list`, and verify the running process actually picked up the change.
- Full deploy-state detail (OPA daemon staleness, plist-not-loaded, the `service` step not reloading) is in [`docs/gotchas/deploy-state.md`](../../docs/gotchas/deploy-state.md); read it when you are deploying, restarting a daemon, or about to trust a live test against local infra.

## Autonomous Pipeline Co-Authorship
- The autonomous pipeline and work-fork are routine co-authors of `origin/main` and may merge work mid-session/overnight. Before building on or deploying their merged work: (i) re-check merge state via `mergeCommit`/`mergedAt` (not a stale "OPEN" read), (ii) for load-bearing surfaces (memory heart, gates) re-verify the controlling invariant first-hand, (iii) chain a distinct session log via `continues` — never overwrite the pipeline's logs on `/compress`.
- A pipeline merge is a hypothesis until verified first-hand, same as any delegated verdict (core-behavioral-rules.md § Verification & Honesty). Don't race the pipeline on its own PRs.

## Concurrent Session Collision
- Two Claude Code sessions can independently pick up the same stuck-job/handoff and start editing the same worktree's files concurrently, with no warning beyond the preflight hook's generic "N other live sessions" notice — this caused real, reproducible file-content oscillation during a review round on 2026-08-08 (codegraph-gate-retire/PR #1131: 3 dispatched review agents each independently confirmed a fix disappearing and reappearing from the same file). Don't assume single-writer just because you're the one in a worktree. When a review agent reports content that contradicts what you just read/verified yourself, check `ListAgents` and the preflight warning before assuming your own mistake or a flaky tool — a live peer session editing the same files by absolute path is a real, confirmed cause. Resolve via direct `SendMessage` coordination (ask, don't guess) rather than continuing to dispatch reviews against a moving target.

## CI Verification Discipline
- Never hand-roll a `gh pr checks --watch` or `--json statusCheckRollup` polling loop — run `scripts/ci/wait_for_checks.py <pr>` with cwd inside the target repo, and read its printed output, not just an exit code.
- Full detail — mechanism, evidence and cost for every rule in this section — is in [`docs/gotchas/ci-verification.md`](../../docs/gotchas/ci-verification.md); read it when you are waiting on CI, reading PR checks, or gating a merge on green — which is every PR, not only deployment work.

## Documentation & Markdown Gotchas
- Before shipping a long markdown doc, scan for inline code spans broken by a line wrap (they render with a literal space), and keep an ADR's `**Scope:**` inside its first 20 header lines or the drift check fails the push.
- Full detail — mechanism, evidence and cost for every rule in this section — is in [`docs/gotchas/markdown-docs.md`](../../docs/gotchas/markdown-docs.md); read it when you are authoring a long markdown doc or an ADR.

## Admin-Merge Branch Cleanup
- After any `--admin --delete-branch` merge, verify BOTH that the merge landed and that the remote branch is actually gone — gh's local cleanup step errors out when `main` is checked out in another worktree, and the remote branch is then silently left behind despite the flag.
- Full detail — mechanism, evidence and cost for every rule in this section — is in [`docs/gotchas/admin-merge.md`](../../docs/gotchas/admin-merge.md); read it when you are running `gh pr merge --admin --delete-branch`.

## Cross-Repo Worktree Handling
- No hook in this repo gates a cross-repo commit correctly — for any repo other than the launch repo, self-impose the full review discipline manually and never read an absent block as "review happened".
- Full detail — mechanism, evidence and cost for every rule in this section — is in [`docs/gotchas/cross-repo-worktrees.md`](../../docs/gotchas/cross-repo-worktrees.md); read it when you are working in any repo other than this session's launch repo.

## Multi-Thread Work Mapping (Wayfinder-Inspired)
- For a multi-thread effort, keep ONE index issue tracking done-criteria, resolved decisions, still-fuzzy open questions, and what is out of scope.
- Full detail — mechanism, evidence and cost for every rule in this section — is in [`docs/gotchas/multi-thread-mapping.md`](../../docs/gotchas/multi-thread-mapping.md); read it when you are planning a multi-thread effort too big for one session.
