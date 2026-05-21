# Verification Rules — Wardens/verification-gate

> Rules the `verification-gate` agent checks against BEFORE any completion claim.
> Adapted from Superpowers' verification-before-completion. Zero tolerance for unverified claims.
>
> Format per rule: `Severity`, `Applies when`, `Check`, `Rule`, `Cite`.
> Severity: `blocking` (must verify) · `warning` (should verify) · `informational` (note).

## fresh-evidence
**Severity:** blocking
**Applies when:** Any claim that code works, tests pass, build succeeds, or bug is fixed.
**Check:** Was the relevant verification command run in this turn (not a previous turn)?
**Rule:** Every success claim requires fresh command output from the current turn. Prior runs are stale.
**Cite:** Superpowers verification-before-completion; "If you haven't run the command in this message, you cannot claim it passes."
**Remediation:** Run the relevant command now (`npm test`, `npm run build`, etc.) and paste its full stdout/stderr output. Do not claim success based on output from a previous turn.

## full-command
**Severity:** blocking
**Applies when:** Verification command is run.
**Check:** Was the FULL command run (e.g., `cargo test` not `cargo test one_test`), and was the exit code checked?
**Rule:** Partial verification proves nothing. Run the full suite. Check the exit code, not just the output text.
**Cite:** Superpowers verification-before-completion
**Remediation:** Re-run the full test suite without filters (e.g., `npm test` not `npm test -- --grep "foo"`) and confirm the exit code is 0. Paste the full output including the summary line.

## no-hedging
**Severity:** blocking
**Applies when:** Completion claim contains hedging language.
**Check:** Does the claim use "should", "probably", "seems to", "looks correct", or "I'm confident"?
**Rule:** Hedging language = unverified claim. Replace with evidence or state "not yet verified."
**Cite:** Superpowers verification-before-completion rationalization table
**Remediation:** Remove the hedging phrase and replace it with the actual command output that proves the claim, or explicitly state "not yet verified — will run `<command>` next."

## agent-distrust
**Severity:** warning
**Applies when:** A subagent reports success.
**Check:** Was the subagent's claim independently verified (e.g., checking VCS diff, running tests)?
**Rule:** Don't trust agent success reports. Verify independently — agents hallucinate completion.
**Cite:** Superpowers "Agent said success → Verify independently"

## regression-check
**Severity:** warning
**Applies when:** Bug fix is claimed.
**Check:** Was a regression test added? Was the red-green cycle verified (test fails without fix, passes with fix)?
**Rule:** Bug fixes without regression tests are incomplete. The red-green cycle proves the test actually tests the bug.
**Cite:** Superpowers TDD red-green pattern

## requirements-checklist
**Severity:** warning
**Applies when:** Task or phase completion is claimed.
**Check:** Were requirements re-read and checked line-by-line against the implementation?
**Rule:** "Tests pass" ≠ "requirements met." Re-read the spec and verify each requirement individually.
**Cite:** Superpowers "Requirements: Re-read plan → Create checklist → Verify each → Report gaps"
