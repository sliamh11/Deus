---
name: implement
description: Drive the coding phase of an approved spec or ticket - red-green-refactor via /tdd at agreed seams, gated by the code-reviewer Warden before commit. Use once plan-reviewer has SHIPed a plan with a concrete spec/ticket to implement against.
---

Implement the work described by the user in the spec or tickets.

Use /tdd where possible, at pre-agreed seams.

Run typechecking regularly, single test files regularly, and the full test suite once at the end.

Once done, invoke the `code-reviewer` Warden (`Agent(subagent_type="code-reviewer")`) — Deus's mandatory pre-commit gate, which internally also runs `/code-review` as a second lens. Wait for SHIP before proceeding.

Once the Warden returns SHIP, present the commit message and wait for the user's explicit approval before committing (see `core-behavioral-rules.md` Execution Gates — never commit without approval).
