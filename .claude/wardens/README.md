# Wardens

Two specialized review agents that guard the Deus codebase. Both run on **Sonnet 4.6** (structured rule-matching + multi-doc reads; ~15× cheaper than Opus with negligible quality hit).

| Warden | Phase | Rules file | Invocation |
|--------|-------|------------|------------|
| **plan-reviewer** | BEFORE implementation | `plan-review-rules.md` | Gated by PreToolUse hook (auto-required for Edit/Write in `~/deus/`) |
| **code-reviewer** | AFTER implementation, BEFORE commit | `code-review-rules.md` | Manual: `Agent(subagent_type="code-reviewer", prompt="review my changes")` |

## Directory

```
~/deus/.claude/agents/
  plan-reviewer.md       ← agent definition (read by Claude Code)
  code-reviewer.md       ← agent definition
~/deus/.claude/wardens/
  README.md              ← this file
  plan-review-rules.md   ← rules loaded by plan-reviewer at invocation
  code-review-rules.md   ← rules loaded by code-reviewer at invocation
```

## How the plan-review gate works

Before any Edit/Write/MultiEdit in `~/deus/`, the hook `~/.claude/hooks/plan-review-gate.sh` checks for the marker `~/deus/.claude/.plan-reviewed`. If missing, the edit is blocked with instructions to invoke `plan-reviewer`.

**Marker lifecycle (event-based, no timer):**

- **Invalidated** by:
  - `SessionStart` hook (`~/.claude/hooks/plan-mode-session-init.sh`) — every new conversation starts clean.
  - PreToolUse `ExitPlanMode` — submitting a new plan via `/plan` or Shift-Tab plan mode.
  - PreToolUse `Agent`/`Task` with `subagent_type=Plan` — invoking the built-in `Plan` subagent.
- **Refreshed** ONLY by:
  - `plan-reviewer` returning `VERDICT: SHIP` → author runs `touch ~/deus/.claude/.plan-reviewed`.
  - Trivial-change bypass (typos, comments, single-line renames): same `touch` command, with the judgment call stated aloud in the response so it's visible.

## Invoking the Wardens

**plan-reviewer (required via gate):**
```
Agent(subagent_type="plan-reviewer", prompt="<your plan: what, why, files>")
```
Then on `VERDICT: SHIP`:
```
touch ~/deus/.claude/.plan-reviewed
```

**code-reviewer (manual, post-implementation):**
```
Agent(subagent_type="code-reviewer", prompt="review my changes for <task>")
```
The agent runs `git diff` + `git diff --cached` and reviews both.

## Adding or editing rules

Rules files are the single source of truth — agents read them at invocation. Add a new rule by appending a section to the relevant file. Agents pick it up immediately on next invocation; no agent-file edit needed.

**Format per rule:**
```
## <rule-id>
**Severity:** blocking | warning | informational
**Applies when:** <precondition — when this rule is relevant>
**Check:** <what the agent looks for to decide violation>
**Rule:** <the rule, one sentence>
**Cite:** <source — memory file, doc path, or system-prompt reference>
```

Keep rules concise. Total file size matters — every rule adds context cost per invocation. If the file exceeds ~300 lines, split by category and index from the main file.

> **Note on `Cite:` fields:** Rules may cite filenames like `feedback_public_repo_generic` or `project_error_discipline_plan.md` — these refer to the maintainer's auto-memory system (Claude Code's per-project memory). Rules themselves are self-contained; citations are for traceability only and safe to ignore if you don't have the same memory setup.

## What's NOT a Warden

- `.claude/skills/code-review/` — a DIFFERENT concept (multi-agent review skill with false-positive learning loop). Not part of this system.
- Built-in `Plan` subagent — CREATES plans; doesn't review them. Wardens critique, Plan drafts.
- `general-purpose`, `Explore` — no Deus-specific rule knowledge.

Wardens are specifically the two rule-enforcing reviewers in this directory.
