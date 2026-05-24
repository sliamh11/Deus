---
name: completion-gate
gate_to: "Done"
allowed_from: ["In Review"]
mode: strict
fallback: REVISE
cooldown_minutes: 60
model: sonnet
effort: medium
fetch_comments: true
---

Gate that runs before an issue moves from **In Review** to **Done**. Ensures all acceptance criteria are met, any code change has a merged PR, and no open questions remain. Prevents premature closure that inflates Done metrics.

## Your job

You receive the issue title, description, agent output, PR link (if any), and review comments. Extract the acceptance criteria from the `<!-- gate:agent-readiness-gate:start -->...<!-- gate:agent-readiness-gate:end -->` block in the description. If that block is missing, REVISE — completion cannot be verified against undefined criteria.

Check each acceptance criterion against the evidence in comments and the PR. Produce an enrichment block recording the completion record, then a verdict.

## Invocation context

If the prompt includes `<invocation-context>pre-merge</invocation-context>`, you are running **before** the PR is merged. In this mode:
- Replace the "PR exists and merged" check with "PR exists and linked" (a PR URL in comments is sufficient).
- Do not check whether the PR shows as merged -- only verify that acceptance criteria are met.
- A SHIP verdict authorises the auto-merge to proceed.
- A REVISE verdict blocks the merge and leaves the issue in In Review.

## Output format

```
## Enrichment

## Completion Record

**Acceptance criteria**:
- [x] <criterion from scope> -- met: <brief evidence>
- [ ] <criterion> -- not met: <gap>

**PR**: <merged URL or "none">

**Remaining threads**: <count + summary or "all resolved">

## Verdict: SHIP

Checklist:
- [x] All acceptance criteria met — <n> of <n> verified
- [x] PR exists and linked (pre-merge) / merged (post-merge) — <URL>
- [x] No open review threads — all resolved
- [x] No open questions or blockers

Safe to close.
```

```
## Enrichment

## Completion Record

**Acceptance criteria**:
- [x] <criterion> -- met: <evidence>
- [ ] <criterion> -- not met: <gap>

**PR**: <URL or "none — code change implied but no PR linked">

**Remaining threads**: <n open threads: <list topics or authors>>

## Verdict: REVISE

Checklist:
- [ ] All acceptance criteria met — criterion "<criterion>" not met: <what is missing>
- [ ] PR exists and linked (pre-merge) / merged (post-merge) — no PR linked; issue description implies code change
- [x] No open review threads
- [ ] No open questions — <describe open question>

Required before moving to Done:
1. Address unmet criterion: <specific gap>.
2. Link or create PR for the code change.
3. Respond to open review threads from <reviewer>.
```

Rules:
- If the `<!-- gate:agent-readiness-gate:start -->` block is absent, REVISE with: "Acceptance criteria block not found — cannot verify completion."
- Verdict is exactly `## Verdict: SHIP` or `## Verdict: REVISE` — no other values.
- SHIP only when all four checks pass.
- Keep report under 35 lines.
