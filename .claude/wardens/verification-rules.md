# Verification Rules — Wardens/verification-gate

> Rules the `verification-gate` agent checks against BEFORE any completion claim.
> Adapted from Superpowers' verification-before-completion. Zero tolerance for unverified claims.
>
> Format per rule: `Severity`, `Applies when`, `Check`, `Rule`, `Cite`.
> Severity: `blocking` (must verify) · `warning` (should verify) · `informational` (note).

## fresh-evidence
**Severity:** blocking
**Applies when:** Any claim that code works, tests pass, build succeeds, or bug is fixed — or where a plan committed an expected output to diff against.
**Check:** Was the relevant verification command run in this turn (not a previous turn)? AND, where the plan committed an expected output (plan-review `verification-strategy`), does the fresh output match that frozen prediction?
**Rule:** Every success claim requires fresh command output from the current turn — prior runs are stale. Where the plan predicted a concrete output, the fresh output must be diffed against that frozen prediction. Where that prediction's oracle is a test, it must be red-green proven — shown to fail without the change — so we know it can actually reject a wrong result; a never-red test is an unvalidated oracle that may simply pass on everything (cf. `regression-check`). A mismatch is a finding to investigate, not noise: if the implementation is wrong, fix the code; if the implementation is correct and the prediction was a mistaken mental model, the prediction may be corrected — but only with an explicit written reason for why the original was wrong. Never silently rewrite the prediction to fit the output; that converts the check into theater.
**Cite:** Superpowers verification-before-completion; "If you haven't run the command in this message, you cannot claim it passes."; predict→execute→diff / reward-hacking guard.
**Remediation:** Run the relevant command now (`npm test`, `npm run build`, etc.) and paste its full stdout/stderr output. Where a prediction was committed, show the diff of actual vs predicted; if they differ, state which was wrong (code or mental model) and why. For a test oracle, include the pre-change run showing it fails (red-green proof that the test can actually reject). Do not claim success based on stale output, and do not amend the prediction to match the output without justification.

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
**Check:** Were requirements re-read and checked line-by-line against the implementation — and, where the plan committed an expected output, was the actual output confirmed to match it (the diff itself is enforced by `fresh-evidence`)?
**Rule:** "Tests pass" ≠ "requirements met." Re-read the spec and verify each requirement individually, and confirm any committed output-prediction was diffed (see `fresh-evidence`). Report gaps explicitly rather than asserting completion.
**Cite:** Superpowers "Requirements: Re-read plan → Create checklist → Verify each → Report gaps"; predict→execute→diff guard.

## wire-reachability
**Severity:** warning
**Applies when:** Claiming a new feature, flag-gated behavior, or integration module is "done" or "working".
**Check:** Was the new capability exercised through its live runtime path — a non-test caller, a live registration, or the flag set on a real path — not only its unit tests? If the wire is deferred, is the tracking issue cited?
**Rule:** "Unit tests pass" ≠ "reachable in production." Confirm the producer→consumer wire before claiming done, or state the wire is deferred and cite the tracking issue.
**Note:** Advisory only — `verification-gate` has no `tools` binding in `config.json`, so it is not commit-enforced. The commit-time enforcement of this concern lives in `code-review-rules.md` (`connectivity-wiring`); this is its claim-time companion, not a duplicate. See `docs/decisions/facade-prevention-mechanism.md`.
**Cite:** LIA-133 facade audit; cross-references `code-review-rules.md` `connectivity-wiring`.
**Remediation:** Demonstrate the live wire (a caller, a registration, or a runtime flag read), or cite the deferred-wire Linear issue. Do not claim completion on unit-test evidence alone.
