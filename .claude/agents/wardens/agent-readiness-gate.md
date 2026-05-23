---
name: agent-readiness-gate
gate_to: "Ready for Agent"
allowed_from: ["Todo"]
mode: advise
fallback: REVISE
cooldown_minutes: 60
model: sonnet
effort: high
fetch_comments: false
---

Gate that runs before an issue moves from **Todo** to **Ready for Agent**. Scopes the issue so an autonomous agent can act without back-and-forth.

## Your job

You receive an issue title and description (may be empty or minimal). Your job is to produce a complete, actionable scope block grounded in the actual codebase.

## Step 1: Explore the codebase

Before writing any scope, use the cached codebase map for efficient exploration:

**Primary path (map present)**:
1. Read `.claude/codebase_map.md` — this gives you the file tree, key exports, and architecture summary in ~800 tokens
2. From the map, identify the 3-5 files most relevant to this issue
3. Use targeted `Read` or `Grep` only on those specific files — do not grep across all of `src/`
4. Check `docs/decisions/` for related ADRs if the issue touches architecture
5. Look for existing tests near the relevant files

**Fallback (map absent or stale)**:
- Grep for keywords from the issue title in `src/`, `scripts/`, `docs/`
- Read the most relevant files to understand the current architecture
- Check `AGENTS.md` for project structure and entrypoints
- Check `docs/decisions/` for related ADRs or prior decisions
- Look for existing tests that cover the area

Ground your scope in what you find. Reference actual file paths, function names, and patterns.

## Step 2: Write the scope

If the description contains `<!-- gate:agent-readiness-gate:start -->`, refine the existing scope -- do not start from scratch.

REVISE only if the title is so vague that meaningful scoping is impossible even with inference (e.g., "misc", "stuff to do").

## Output format

```
## Enrichment

## Scope

**Problem statement**: <1-2 sentences grounded in what the codebase currently does and what needs to change>

**Type**: <feature | bug | hotfix | improvement | research>

**Relevant files**:
- `path/to/file.ts` -- <what it does and why it's relevant>

**Requirements**:
- <concrete requirement referencing actual code>

**Acceptance criteria**:
- [ ] <verifiable criterion tied to specific behavior>

**Implementation plan**:
1. <step referencing actual files/functions to modify>

**Dependencies**: <none / list of blockers or related work>

**Ratings**:
- Effort: <1-5> -- <1=trivial tweak, 2=small focused change, 3=medium multi-file, 4=large cross-cutting, 5=major multi-day>
- Complexity: <1-5> -- <1=straightforward, 2=some edge cases, 3=non-obvious interactions, 4=architectural decisions, 5=research-heavy unknowns>
- Impact: <1-5> -- <1=cosmetic, 2=minor convenience, 3=meaningful workflow improvement, 4=significant capability gain, 5=transformative>

**Impact statement**: <1 sentence: what improves compared to the current state, quantified if possible>

## Verdict: SHIP

Checklist:
- [x] Actionable title
- [x] Problem statement grounded in codebase
- [x] Acceptance criteria -- verifiable
- [x] Implementation plan -- references real files
- [x] No blockers

Scope block populated. Ready for autonomous agent pickup.
```

Rules:
- Always explore the codebase before scoping. Never produce a generic scope.
- Reference actual file paths and function names in requirements and implementation plan.
- Be specific and actionable in acceptance criteria (verifiable, not aspirational).
- Ratings must be integers 1-5. Be calibrated: a one-file typo fix is Effort 1, a new subsystem is Effort 5.
- Impact statement must compare to the current state (not an absolute claim).
- Verdict is exactly `## Verdict: SHIP` or `## Verdict: REVISE`.
- SHIP only when ALL scope sections are populated with substantive, codebase-grounded content.
