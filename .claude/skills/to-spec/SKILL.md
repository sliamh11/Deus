---
name: to-spec
description: Turn the current conversation into a spec and publish it to the project issue tracker — no interview, just synthesis of what you've already discussed.
disable-model-invocation: true
user_invocable: true
---

This skill takes the current conversation context and codebase understanding and produces a spec (you may know this document as a PRD). Do NOT interview the user to gather spec content — synthesize what you already know. (One exception, Step 2 below: confirming the seam choice is a decision check, not content-gathering.)

The issue tracker and triage label vocabulary is inferred dynamically by `/to-tickets`'s tracker-detection step; if ambiguous it will ask.

## Process

1. Explore the repo to understand the current state of the codebase, if you haven't already. Use the project's domain glossary vocabulary throughout the spec, and respect any ADRs in the area you're touching.

2. Sketch out the seams at which you're going to test the feature. Existing seams should be preferred to new ones. Use the highest seam possible. If new seams are needed, propose them at the highest point you can. The fewer seams across the codebase, the better - the ideal number is one.

Check with the user that these seams match their expectations.

3. Write the spec using the template below, then publish it to the project issue tracker, using the same tracker resolved by `/to-tickets`'s tracker-detection step. Publication is tracker-specific — there is no single `ready-for-agent` label that fits every tracker:
   - **Linear**: create the spec as an issue in **Backlog**, same as `/to-tickets` does — no scope block, no `Scoped` label (both are gate-owned; a manually-applied `ready-for-agent`-style label doesn't exist in this workflow and moving straight to a "ready" state would bypass the enrichment/bouncer gates). The user moves it to Todo when ready to start the pipeline.
   - **Asana / GitHub Issues**: create the task/issue as `/to-tickets` does for these trackers — no autonomous pipeline exists for either, so the spec is simply ready for a human or agent to pick up manually. No additional triage or label is required.
   - **Local files**: apply the `ready-for-agent` status, matching `/to-tickets`'s local-file ticket template convention.

<spec-template>

## Problem Statement

The problem that the user is facing, from the user's perspective.

## Solution

The solution to the problem, from the user's perspective.

## User Stories

A LONG, numbered list of user stories. Each user story should be in the format of:

1. As an <actor>, I want a <feature>, so that <benefit>

<user-story-example>
1. As a mobile bank customer, I want to see balance on my accounts, so that I can make better informed decisions about my spending
</user-story-example>

This list of user stories should be extremely extensive and cover all aspects of the feature.

## Implementation Decisions

A list of implementation decisions that were made. This can include:

- The modules that will be built/modified
- The interfaces of those modules that will be modified
- Technical clarifications from the developer
- Architectural decisions
- Schema changes
- API contracts
- Specific interactions

Do NOT include specific file paths or code snippets. They may end up being outdated very quickly.

Exception: if a prototype produced a snippet that encodes a decision more precisely than prose can (state machine, reducer, schema, type shape), inline it within the relevant decision and note briefly that it came from a prototype. Trim to the decision-rich parts — not a working demo, just the important bits.

## Testing Decisions

A list of testing decisions that were made. Include:

- A description of what makes a good test (only test external behavior, not implementation details)
- Which modules will be tested
- Prior art for the tests (i.e. similar types of tests in the codebase)

## Out of Scope

A description of the things that are out of scope for this spec.

## Further Notes

Any further notes about the feature.

</spec-template>
