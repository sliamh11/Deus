# Oracle Rules — Wardens/oracle-author

> Rules defining a good independent oracle (a discriminating red-green test).
> Used by the `oracle-author` agent when authoring, and by any reviewer (human
> or warden) evaluating oracle quality — independently of which agent produced it.
> The agent spec governs runtime behavior; this file is the reviewable checklist.
>
> Format per rule: `Severity`, `Applies when`, `Check`, `Rule`.
> Severity: `blocking` (oracle is invalid without it) · `warning` (should hold) · `informational`.

## independence
**Severity:** blocking
**Applies when:** Any oracle is authored or reviewed.
**Check:** Was the oracle derived from the spec/contract, with its author not having written (or seen) the implementation under test?
**Rule:** The oracle's value is that its errors are uncorrelated with the implementation's. A test authored by the implementer, or derived from the implementation, is a captured oracle — it agrees with whatever the code does, including its bugs. Author from the spec, blind to the new code.

## red-green-able
**Severity:** blocking
**Applies when:** Any oracle is authored or reviewed.
**Check:** Does the test FAIL against the current/absent implementation before the change (red), and would it pass once the contract is met (green)?
**Rule:** A test that cannot fail proves nothing — it may simply pass on everything (a tautology). Red-green proves the oracle can discriminate right from wrong. Show the red run.

## spec-sourced
**Severity:** blocking
**Applies when:** The oracle asserts a concrete expected value, output, or state.
**Check:** Does each expected value trace to the requirement/acceptance criteria or a reference — not to the chosen implementation?
**Rule:** Expected values retrofitted from the implementation make the test a mirror. Trace every assertion to the spec.

## contract-level
**Severity:** warning
**Applies when:** The oracle asserts behavior.
**Check:** Does it assert the observable contract (inputs→outputs, state, side effects, errors) rather than internal code shape (private fields, call order, structure)?
**Rule:** Assert behavior, not structure — so a correct re-implementation still passes and a wrong one still fails. Structural assertions break on refactors and miss behavioral bugs.

## falsifiable
**Severity:** warning
**Applies when:** Any oracle is authored or reviewed.
**Check:** Does the oracle name the specific wrong behavior(s) each test would catch?
**Rule:** An oracle you cannot describe a failure for is not discriminating. State what it falsifies.

## executable-preferred
**Severity:** warning
**Applies when:** Choosing the oracle's form.
**Check:** Is the oracle an executable test (settled by running it)? If a judge/human check is used instead, is the reason stated?
**Rule:** Prefer an executable oracle — execution overrides belief and is independent of the proposer. A judge is the fallback only where no command can settle the claim, and it is the weaker check (cf. `verification-rules.md` `fresh-evidence`, `code-review-rules.md` `expected-output-confirmed`).

## oracle-tagged
**Severity:** warning
**Applies when:** An oracle test is written.
**Check:** Is each oracle test marked with an `@oracle` comment (e.g. `// @oracle: <spec reference>`)?
**Rule:** The tag is the checkable signal the commit-side `oracle-integrity` rule keys on to protect the oracle from being silently weakened. Untagged oracle tests are invisible to that gate.
