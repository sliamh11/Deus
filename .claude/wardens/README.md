# Wardens

Specialized review agents that guard the codebase. Validator wardens check correctness against rules; generator wardens produce artifacts.

| Warden | Type | Model | Rules/Schema file | Invocation |
|--------|------|-------|-------------------|------------|
| **plan-reviewer** | Validator | Opus | `plan-review-rules.md` | Gated by PreToolUse hook (auto-required for Edit/Write in `~/deus/`) |
| **code-reviewer** | Validator | Sonnet | `code-review-rules.md` | Manual: `Agent(subagent_type="code-reviewer", prompt="review my changes")` |
| **threat-modeler** | Validator | Opus | `threat-modeling-rules.md` | Manual: invoke when plan touches auth, credentials, external APIs, or trust boundaries |
| **architecture-snapshot** | Generator | Sonnet | `architecture-schema.md` | Manual: `Agent(subagent_type="architecture-snapshot", prompt="snapshot the architecture")` |
| **session-retrospective** | Generator | Opus | `retrospective-schema.md` | Manual: `Agent(subagent_type="session-retrospective", prompt="retrospective for last 20 sessions")` |

## Directory

```
~/deus/.claude/agents/
  plan-reviewer.md          ← agent definition (read by Claude Code)
  code-reviewer.md          ← agent definition
  threat-modeler.md         ← agent definition
  architecture-snapshot.md   ← agent definition
  session-retrospective.md  ← agent definition
~/deus/.claude/wardens/
  README.md                 ← this file
  plan-review-rules.md      ← rules loaded by plan-reviewer
  code-review-rules.md      ← rules loaded by code-reviewer
  threat-modeling-rules.md  ← rules loaded by threat-modeler
  architecture-schema.md    ← output schema for architecture-snapshot
  retrospective-schema.md   ← output schema for session-retrospective
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

Wardens are specifically the rule-enforcing reviewers and generators in this directory.

## Agent discovery

Claude Code discovers `.claude/agents/*.md` files at **session start**. Agents added mid-session won't appear in the `subagent_type` registry until the next session. To invoke a newly created agent in the current session, use a general-purpose agent with a prompt that reads the agent definition:

```
Agent(prompt="You are the <warden-name> warden. Read your agent definition at
~/deus/.claude/agents/<warden-name>.md and follow its instructions exactly. <task>")
```
