# Deus V2 Archival

**Status:** Accepted
**Date:** 2026-08-15
**Scope:** docs/decisions/deus-v2-archival.md
**Governs no code in this repository.** This decision applies to the external
`sliamh11/deus-v2` fork, the local `~/deus-v2-mvp` checkout, and its
`com.deus-v2.*` launchd jobs. Nothing in `~/deus` changes as a result of it. The
Scope field above names this record's own path because `scripts/drift_check.py`
requires a non-empty Scope — not because edits under `docs/decisions/` should
trigger a re-read of this ADR.

## Context

Deus V2 is not a separate product line. It is a fork of this repository that
diverged at `38aa2470` on 2026-07-14 (LIA-397). Everything before that date —
including the entire backend-neutral agent runtime — exists in **both** repos.

The fork's premise was to stop depending on one AI provider. That premise was
already substantially delivered *inside this repository* before the fork existed:
`src/index.ts:161-163` registers three production backends (`claude`, `openai`,
`llama-cpp`) through `RuntimeRegistry`. What the fork added on that axis was one
more backend — `deus-native`, an in-house LangChain runtime that removes the
Claude Code CLI dependency entirely — plus an Ink TUI, a hand-rolled
CLI-subprocess transport, and a Hermes Agent integration.

Two separate things had been conflated under "provider control", and separating
them is what makes this decision tractable:

| Mechanism | What it controls | Claude Code CLI still required? |
|---|---|---|
| `deus connect` (this repo) | **Which model** answers in a CLI coding session | **Yes** — runs the unmodified `claude` binary |
| `openai` / `llama-cpp` backends (both repos) | Which model serves the **container/chat** agent | No |
| `deus-native` (V2 only) | **Who owns the agent loop** — permissions, compaction, checkpointing | No |

`deus connect` (#1171) is explicitly *not* a backend adapter. Its own amendment to
`backend-neutral-agent-runtime.md` states it is "a CLI-level model redirect — it
launches the real, unmodified `claude` binary with only the upstream model
swapped via `ANTHROPIC_BASE_URL`". Commit `63eebc24` touched `deus-cmd.sh`,
`connectors/`, `scripts/`, docs and skills — and **zero files** under `src/` or
`container/`.

So: model choice is solved. Runtime ownership is not, and that is the only thing
V2 uniquely buys.

## Decision

**Archive Deus V2. Do not continue development, and do not backport from it.**

The `com.deus-v2.*` launchd jobs are disabled and unloaded. The repository, its
branches, and its ADRs are kept intact as a frozen reference — this is an
archival, not a deletion.

## Evidence

Measured 2026-08-15, `origin/main` vs `deus-v2-origin/main`:

- **Development has stopped.** V2's last commit is 2026-08-02. Since the fork,
  this repo has 121 commits to V2's 103, and essentially all of V2's arrived
  before August. (All three figures have since moved — see
  [Amendment — 2026-08-16](#amendment--2026-08-16-commit-count-evidence-re-measured).)
- **It serves no traffic.** V2's `store/messages.db` was last written 2026-07-18.
  Its running daemon's entire recent log was credential-proxy OAuth refresh
  churn — some of it failing — on behalf of no conversations.
- **`deus-native` is not production-ready.** Its own parity matrix carries two
  open **release blockers** (container tool-broker parity has no test coverage at
  all; no end-to-end `deus-native` scheduled-task test exists) plus known gaps in
  shell, filesystem, multimodal, and tool streaming. `AAG-017` records that its
  host-side tool surface still exposes only `web_search` and `web_fetch` — no
  `apply_patch`, no commit-capable `Bash`.
- **Divergence is large and compounding.** 659 files, ~88,800 insertions and
  ~50,289 deletions separate the two trees, growing with every merge here.

## Backport assessment

Three candidates were considered and all three were declined:

- **Ink TUI** (`packages/tui`, LIA-471/473) — declined by the user.
- **Hermes integration + `deus_memory_client`** (LIA-499/500/501) — declined by
  the user. `deus_memory_client` is plumbing rather than capability: it adds no
  new memory ability, only makes the existing `memory_recall` and
  `log_interaction` MCP tools reachable from a foreign process. Its README names
  exactly one consumer, the Hermes adapter. Without Hermes it has zero consumers.
  The underlying *pattern* is worth remembering; see "Idea worth keeping" below.
- **`waitForMcpReady`** (LIA-461) — **not applicable to this repository**, on the
  evidence below. This is the one candidate ruled out on technical grounds rather
  than preference.

`waitForMcpReady` fixes a race created by V2's own architecture. V2 hand-rolled a
session pool around `claude --print --input-format stream-json` that spawns the
process and writes turns to its stdin *later*, opening a window in which the first
turn can fire before the stdio MCP server has initialized. That produced the
roughly 43% reliability gap seen in V2's A7 tool-loop benchmark. The whole
mechanism lives in `src/agent-runtimes/cli-subprocess/`, a module this repository
does not have.

This repository never opened that window, confirmed two independent ways:

1. `src/container-runner.ts:535-536` writes one payload and immediately calls
   `stdin.end()` — one-shot per run, with no persistent turn-piping.
2. `container/agent-runner/src/index.ts:942` calls `query({...})` from the
   official `@anthropic-ai/claude-agent-sdk`, passing the prompt *into* the call.
   The SDK owns MCP initialization and will not dispatch the turn until it
   completes.

(`closeStdin` in `src/group-queue.ts:188` is a misnomer — it writes a `_close`
sentinel file into an IPC directory rather than writing to stdin, which further
confirms there is no live stdin session to race.)

V2 replaced the SDK's startup sequencing with its own and inherited a race the
SDK already handled. There is nothing here to port.

With all three declined, the backport list is empty — which makes the archive
cleaner, since no long-lived backport branch needs maintaining.

## Alternatives Considered

- **Continue V2 to completion.** Rejected: it needs the two release blockers
  closed plus four known gaps filled, against a fork that has served no traffic
  since July and whose divergence grows with every merge here. The cost is paid
  now; the benefit is insurance against a risk that has not materialized.
- **Merge V2 back into V1.** Rejected: 659 files of divergence to reconcile in
  order to acquire one incomplete backend, an unwanted TUI, and an integration
  that has been declined.
- **Delete the V2 repository.** Rejected: the ADRs and the `deus-native`
  groundwork are the cheapest possible form of insurance, and keeping a dormant
  repository costs nothing once its daemons are unloaded.
- **Leave V2 running but unattended.** Rejected: it was consuming OAuth refresh
  cycles for zero traffic, and a running parallel daemon invites the
  wrong-instance debugging confusion that a stopped one cannot.

## Consequences

- This repository is the single line of development. Backend neutrality for the
  chat/container path continues through the existing `claude`/`openai`/
  `llama-cpp` adapters; model choice for CLI sessions continues through
  `deus connect`.
- The Claude Code CLI dependency is **accepted, not solved**. This is the real
  cost of the decision and should not be described any other way.
- `deus-native` remains unfinished. Reviving it means resuming from two release
  blockers, not from a working system.
- The seven `com.deus-v2.*` launchd jobs are `disable`d as well as `bootout`ed.
  `bootout` alone would have silently reloaded them at the next login.

## Revival condition

One specific scenario justifies reopening this decision.

`deus connect` works by pointing Claude Code at CLIProxyAPI, which reuses a
ChatGPT/Codex OAuth subscription. That is a fragile and arguably ToS-grey
dependency — plausibly *more* fragile than the Anthropic CLI dependency it routes
around. If that path is closed off, or if the Claude Code CLI itself becomes a
genuine liability (pricing, hook removal, rate limiting, licence terms), then
runtime ownership stops being speculative hardening and becomes necessary.

Until then the insurance is worth **keeping the option, not paying rent on it**.

## Idea worth keeping

`deus_memory_client`'s pattern outlives its one consumer and is worth recalling
if an external process ever needs Deus's memory again: **reach the memory layer
by speaking MCP to it, never by importing Deus code.** A foreign process gets
`recall()` and `log_interaction()` with only the `mcp` library installed, no
Deus dependency tree, and one place to instrument for cross-cutting concerns such
as privacy labelling. The failure mode it avoids is importing `evolution/` or
`scripts/` into a process that cannot support them.

This is recorded here as a design note only. Nothing in this repository
implements it today, and nothing should until a real second consumer exists.

## Verification

Performed 2026-08-15:

- `git rev-list --count` and `git diff --shortstat` against both remotes for the
  commit and divergence figures.
- Direct reads of `src/index.ts:161-163`, `src/container-runner.ts:535-536`,
  `container/agent-runner/src/index.ts:942`, and `src/group-queue.ts:188` for the
  runtime and MCP-race claims.
- The `deus connect` scope claim confirmed two ways: the ADR amendment text in
  `backend-neutral-agent-runtime.md`, and the file list of commit `63eebc24`.
- `launchctl list` before and after: seven `com.deus-v2.*` jobs removed, all
  sixteen `com.deus*` jobs still loaded, no orphaned processes.
- Independently re-derived by a `plan-reviewer` pass, which re-ran every
  quantitative claim above and reported exact matches.

## Rollback

Restore the V2 daemons by re-enabling and bootstrapping each of the seven
`com.deus-v2.*` labels listed above from `~/Library/LaunchAgents` — `launchctl
enable` followed by `launchctl bootstrap` for each, in the current user's GUI
domain. The plists are untouched on disk and the repository is unmodified, so
this decision is fully reversible.

## Amendment — 2026-08-16: Commit-count evidence re-measured

**Trigger**: the "Development has stopped" bullet under Evidence names V2's last
commit as 2026-08-02. One commit landed on V2 after that date — on the same day
this decision was accepted — so the figures are restated here rather than left
to read as current.

Both original figures were accurate as measured on 2026-08-15 and are kept above
as what was measured. This section records a re-measurement, not a rewrite.

**Measurement** (2026-08-16):

| figure | measured 2026-08-15 | re-measured 2026-08-16 | command |
|---|---|---|---|
| this repo, commits since the fork | 121 | **147** | `git rev-list --count 38aa2470..origin/main` |
| V2, commits since the fork | 103 | **104** | `gh api repos/sliamh11/deus-v2/compare/38aa2470...main --jq .total_commits` |
| V2, last commit | 2026-08-02 | **2026-08-15** | `gh api repos/sliamh11/deus-v2/commits --jq '.[0].commit.author.date'` |

V2's `+1` is [#86](https://github.com/sliamh11/deus-v2/pull/86) (`91b9e047`),
merged 2026-08-15T21:14:54Z — the only V2 commit after the date the bullet
names. It was opened at 21:09:47Z, 4h40m after this decision's own PR
[#1193](https://github.com/sliamh11/Deus/pull/1193) merged at 16:29:30Z, and it
carried the delivery half of an unrelated ticket (LIA-552). Nothing in this
repository would have surfaced the archival at that push: no gate here
meaningfully covers a cross-repo commit, as
`.claude/rules/orchestration-rules.md` § Cross-Repo Worktree Handling records.
The evidence is consistent with the decision not having been in view; it does
not establish what was or was not read.

This repo's `+26` is ordinary drift on an active repository and carries no
signal on its own. The direction is what matters: the gap **widened**, 121:103
to 147:104.

The two halves of the original bullet therefore moved in opposite directions,
and only one is strengthened. The **ratio** case is stronger than when it was
written. The **recency** case is weaker: V2's last commit is not 13 days
stale but same-day as this decision. The conclusion still holds on the ratio,
on the traffic evidence below, and on the fact that the one commit is
attributable and unrelated — not because V2 had gone quiet by the day this was
accepted.

**The decision is unchanged.** Status stays Accepted and the Backport assessment
is untouched. The `com.deus-v2.*` jobs remain disabled and unloaded — re-verified
2026-08-16, `launchctl list com.deus-v2.morning-report` exits 113.
