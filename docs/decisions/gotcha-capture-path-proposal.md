# ADR: Lighter-weight capture path for single-fact gotchas

**Date:** 2026-08-02
**Status:** Accepted (Option A)
**Reversibility:** REVERSIBLE
**Scope:** `.claude/skills/compress/SKILL.md` (Option A, applied). Option B not pursued, per this doc's own "do not do both" guidance.

## Context

The 2026-08-02 session retrospective found nine distinct tool/infra gotchas discovered at real
cost and recorded only in session-log prose across a two-day window, while exactly one new
`/learn-procedure` node was captured in the same window (store: 49 -> 50 nodes). One of the
nine -- the `--repo-root` warden-marking gotcha -- explicitly named by its own discovering
session as "worth folding into `orchestration-rules.md`" as a pending follow-up -- was
independently verified still absent from that file when the 2026-08-02 retrospective ran, weeks
after being flagged. (The specific `scripts/cogate.py`-based mitigation lands as a companion fix
in this same change set, appended to the pre-existing "Warden Co-Gate Verdict Marking" section
-- which itself predates this retrospective, landing separately via #868/#869 -- but was not yet
captured at the time the retrospective's finding was made.) This rules out simple ignorance of
the destination and points at friction/shape mismatch instead.

`/learn-procedure`'s own SKILL.md requires: a trigger, a negative scope, and an ordered
multi-step list (Steps 1-9 for the one node that WAS captured this window,
`judge-model-ab-fixture.md`), plus a dual-warden pre-review (result-skeptic + two ai-eng-warden
backends) before the human approval gate. This is well-suited to genuine multi-step procedures
but is a heavyweight, multi-tool-call process for a single sentence like "pass --repo-root when
marking from a worktree cwd." The discovery moment is also typically mid-debugging, competing
directly with finishing the task at hand -- the worst moment to context-switch into a 6-step
capture workflow.

Cost of inaction: this is the second consecutive retrospective naming this exact theme. The
retrospective's own framing: "Tool gotchas recorded in prose only. Second consecutive retro, 8
-> 9 gotchas, still ~1 node captured per window." (The store's raw node count grew 17 -> 50 in
the prior window via a 33-node one-off bulk batch unrelated to gotcha capture, and 49 -> 50 --
one node, a genuine multi-step procedure, not a gotcha -- in this window; "~1 node captured per
window" describes the gotcha-capture rate specifically, not the store's total growth.)
Repeating "capture more" as a recommendation has not moved the number across multiple retros --
the retrospective's own conclusion is that the capture *mechanism*, not operator diligence, is
the untried variable.

## Decision

We will NOT prescribe a specific mechanism in this ADR -- we propose the two lightest candidate
options below and recommend picking exactly one to build, since the retrospective's evidence
cannot yet distinguish which one works (neither has been tried).

**Option A: `/compress`-time prompt.** Extend `/compress` (session-save skill) to ask once per
session, only if the session log contains gotcha-shaped content: "any single-fact gotcha from
this session belongs in a rules file or a knowledge node?" Destination is pre-resolved from the
session's own `project_path`, since in the observed cases the author already knew where the fact
belonged and only lacked the trigger to act on it in the moment.

**Option B: Explicit routing rule, no new tooling.** Accept that gotchas live in rules-file prose
(the status quo), but make the routing obligation explicit in `feedback_rule_extraction_standard.md`:
a gotcha discovered mid-session gets appended to its owning rules file in the SAME session it was
found, not deferred as a "pending task" note. No new skill or prompt -- just a stated behavioral
rule closing the gap between "knew the destination" and "wrote it there."

Do not implement both -- pick one, since running two capture paths for the same gotcha class
would itself become the kind of redundant process this retrospective's parent themes (review-round
inefficiency, tool-shape mismatch) already warn against.

**Outcome:** Option A was chosen and implemented as `.claude/skills/compress/SKILL.md`'s new
"Step 0.5 -- Flag capturable gotchas" section. Option B was not pursued -- its edit target
(`feedback_rule_extraction_standard.md`) is a personal-memory file outside this repo, not
something this ADR can apply directly, and per the guidance above only one path should exist.

## Alternatives Considered

| Alternative | Reason Rejected |
|-------------|----------------|
| Lower `/learn-procedure`'s bar to accept single-fact captures | Would blur the procedure/gotcha distinction and risk flooding the procedure store (already only reviewed at recall@3 for genuine multi-step procedures) with one-liners it wasn't measured against |
| Build a new `/learn-gotcha` skill with its own dual-warden pre-review | Heaviest option; the retrospective's own root-cause hypothesis is that discovery-moment friction (not review rigor) is the blocker, so adding another full skill with its own review ceremony doesn't address the actual mechanism identified |
| Do nothing; keep re-flagging in future retrospectives | Already tried across at least two consecutive retros with no measured improvement (8 -> 9 gotchas, ~1 node per window each time) |

## Rationale

Both surviving options are deliberately minimal because the retrospective's confidence in the
root-cause diagnosis (Medium-High, not High) is explicitly capped by not yet knowing whether the
blocker is "wrong tool shape" or "no slack in a dense window" -- an expensive new mechanism is
not justified until a cheap one is tried and measured. Option A adds a single low-cost prompt at
a moment (session close) that is not mid-task, addressing the "worst moment to context-switch"
friction directly. Option B adds zero tooling and instead closes the gap the evidence most
directly demonstrates: knowing the destination but not writing to it in the same session.

## Consequences

**Positive:**
- Either option is cheap to build/adopt and cheap to reverse if it doesn't move the capture rate.
- Directly targets the retrospective's own testable prediction: the next window's retro should
  find at least 3 of its discovered gotchas already captured at retro time, with the prose-only
  theme dropping below 5 occurrences.

**Negative:**
- Option A adds a small amount of friction to every `/compress` invocation, even sessions with no
  gotcha to capture.
- Option B relies on self-discipline in the moment with no structural enforcement, which is the
  same class of dependency the original problem (knew the destination, didn't write it there)
  already demonstrated can fail.

**Risks:**
- Neither option has been tried, so this ADR cannot predict which one actually improves the
  capture rate -- the next retrospective is the real test.
- A rushed same-session append (Option B) risks lower-quality rule-file prose than a more
  considered `/learn-procedure`-style pass would produce.

## Exit Path

Fully reversible either way: Option A is a prompt addition to one skill file, removable in one
edit. Option B is a rules-file clause, removable in one edit. Neither touches code, schemas, or
stored data.
