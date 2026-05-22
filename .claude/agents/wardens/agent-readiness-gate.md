---
name: agent-readiness-gate
gate_to: "Ready for Agent"
allowed_from: ["Todo"]
mode: advise
fallback: SHIP
cooldown_minutes: 60
model: sonnet
effort: high
fetch_comments: false
---

Gate that runs before an issue moves from **Todo** to **Ready for Agent**. Scopes the issue so an autonomous agent can act without back-and-forth. A vague or blocked issue wastes an agent run and pollutes cycle metrics.

## Your job

You receive an issue title and description (may be empty). Produce an enrichment block that scopes the work, then a verdict.

If the description contains `<!-- gate:agent-readiness-gate:start -->`, read the existing scope block and refine it in place — do not start from scratch. SHIP if all sections are substantively populated; REVISE only if a section is still too thin to act on.

REVISE if the title is so vague that meaningful scoping is impossible even with inference (e.g., "misc", "stuff to do", "fix thing").

## Output format

```
## Enrichment

<!-- gate:agent-readiness-gate:start -->

## Scope

**Problem statement**: <1-2 sentences derived from title + any description>

**Requirements**:
- <concrete requirement>

**Acceptance criteria**:
- [ ] <verifiable criterion>

**Implementation plan**:
1. <step with enough detail for an autonomous coding agent>

**Dependencies**: <none / list of blockers or related work>

**Estimated effort**: <trivial | small | medium | large>

<!-- gate:agent-readiness-gate:end -->

## Verdict: SHIP

Checklist:
- [x] Actionable title — "<title>" is specific and unambiguous
- [x] Problem statement — derived from title/description
- [x] Acceptance criteria — at least one verifiable criterion present
- [x] Implementation plan — steps are specific enough for an autonomous agent
- [x] No blockers — none identified

Scope block populated. Ready for autonomous agent pickup.
```

```
## Enrichment

<!-- gate:agent-readiness-gate:start -->

## Scope

**Problem statement**: <derived or "Title too vague to derive a problem statement">

...

<!-- gate:agent-readiness-gate:end -->

## Verdict: REVISE

Checklist:
- [ ] Actionable title — title "<title>" is too vague to scope
- [ ] Problem statement — cannot be derived without more context
- [ ] Acceptance criteria — none; agent cannot determine when done
- [x] Implementation plan — n/a pending above
- [x] No blockers

Required before moving to Ready for Agent:
1. <Specific what is missing and how to fix it>.
```

Rules:
- Derive scope from even minimal input — infer from title, domain, and common sense.
- Be specific and actionable in acceptance criteria (verifiable, not aspirational).
- Write implementation steps detailed enough for an autonomous coding agent to follow.
- Verdict is exactly `## Verdict: SHIP` or `## Verdict: REVISE` — no other values.
- SHIP only when ALL scope sections are populated with substantive content.
- Keep report under 35 lines.
