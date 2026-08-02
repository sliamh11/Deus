# ADR: Round-count circuit breaker for the plan-review REVISE loop

**Date:** 2026-08-02
**Status:** Accepted
**Reversibility:** REVERSIBLE
**Scope:** `.claude/rules/core-behavioral-rules.md` (§ Execution Gates), `.claude/wardens/plan-review-rules.md`, `docs/decisions/standards-pack-priority.md` (quoted-excerpt sync only)

## Context

`core-behavioral-rules.md` § Execution Gates states: "REVISE from any warden means re-run after
fixes until SHIP. Never touch markers, commit, or proceed on REVISE -- no exceptions, no 'close
enough,' no time-pressure rationalization." This rule's only termination condition is SHIP. It
guarantees nothing unreviewed ships; it does not guarantee SHIP is reachable.

The 2026-08-02 session retrospective (`$VAULT/Retrospectives/2026-08-02-retrospective.md`)
documents a clean natural experiment: a Confluence Phase 2 backtest plan accumulated 45
adversarial plan-review rounds against a single 1800-line growing document, with zero
implementation, before the user manually stopped it ("stop, 45 is too much... something's wrong
here"). The session's own post-mortem: "iterative adversarial plan-review against a single
accumulating document, with no periodic holistic re-read, does not reliably converge... each fix
reliably introduces one new adjacent bug the same reviewer then finds next round." Concrete
non-convergence markers: 6+ rounds spent on one look-ahead defect, the same `repricedCandidate`
scope-visibility bug found independently in two separate rounds, and a foundational
UTC-vs-New_York timezone bug that silently undermined ~15 rounds of logic built on top of it.

The counterfactual: the same feature, re-scoped fresh the next day as a small v1 instead of
patched onto the 1800-line document, reached dual-backend SHIP in 6 rounds with the same
reviewer pair.

Two other efforts in the same window (an 11-round codex-exec security review, a 7-round
hardening cycle) did converge normally and produced real fixes -- so round count alone is not
the pathology. The distinguishing variable, isolated by the counterfactual, is whether the
artifact is periodically re-scoped/re-read holistically or only ever patched incrementally.

Cost of inaction: the rule as currently written provides no mechanism to detect or interrupt
this failure mode short of a human manually noticing and stopping it -- which happened here, but
an unprompted check-in at round 40 had already received "keep going" and did not help.

### A second, independent mechanism: finite-attention sampling

The moving-target mechanism above explains a *growing* artifact's non-convergence, but doesn't
fully explain why even a small, static artifact can take more than one review round. That second
mechanism was confirmed empirically, in this same working session (2026-08-02), on a case
unrelated to the Confluence effort: a personal-memory procedure-node draft (not part of this
repo, not part of any PR diff) went through a `result-skeptic` relevancy pass, returned REVISE
with real findings, was fixed, and then -- going beyond `/learn-procedure`'s standard single-pass
step, specifically because the first pass found genuine issues -- got a second, independently
dispatched `result-skeptic` pass on the revised draft. That second pass found a *different*,
unrelated problem: a factual claim about a CLI capability (`unified_exec`) that was present in
the draft since before round 1 and directly contradicted by the underlying source evidence --
not something round 1's own fix introduced.

This is a distinct failure mode from the moving target: verifying every embedded claim against
primary sources is expensive, so any single review pass -- human or AI -- implicitly samples a
subset of an artifact's claims rather than achieving full coverage. A second, independent pass
over the *same, unchanged* snapshot can still find something the first pass simply never checked.
Re-scoping to a smaller artifact (the fix for the moving-target mechanism) reduces how much a
sampling gap costs per round, but doesn't eliminate the gap itself -- a small artifact can still
need 2 rounds for this reason, as the case above shows.

## Decision

We add a round-count circuit breaker to the plan-review loop for the moving-target mechanism, and
a reviewer-coverage-diversity recommendation for the sampling mechanism -- two different levers
for two different causes, both landed as real rule changes (not proposals).

**Applied rule** (the actual text now live in `.claude/rules/core-behavioral-rules.md` §
Execution Gates, verbatim):

> REVISE from any warden means re-run after fixes until SHIP, against a bounded artifact
> (bounding is the round-count-circuit-breaker re-scope checkpoint in `plan-review-rules.md` --
> never a self-declared exemption) -- no exceptions, no "close enough," no time-pressure
> rationalization. Never touch markers, commit, or proceed on REVISE.
>
> Two distinct review-loop non-convergence mechanisms, with different fixes, are detailed in
> `plan-review-rules.md`'s `round-count-circuit-breaker` (moving-target: a growing/patched
> artifact -- re-scope after 5 unconverged rounds) and `reviewer-coverage-diversity` (sampling: a
> single pass doesn't achieve full claim coverage even on a static artifact -- prefer parallel
> independent reviewers over more serial rounds when findings aren't traceable to prior fixes).
> Evidence and rationale: `docs/decisions/review-round-circuit-breaker-proposal.md`.

**Applied rules** (the two new sections now live in `.claude/wardens/plan-review-rules.md`,
condensed here -- see that file for the full `Severity`/`Applies when`/`Check`/`Rule` text):

- `round-count-circuit-breaker` (**Severity: informational**, not blocking -- see Consequences
  below for why): after 5 unconverged rounds against the same artifact, when that round count is
  directly evidenced (not assumed), flag it and recommend a holistic re-read + explicit re-scope
  decision before another patch round.
- `reviewer-coverage-diversity` (**Severity: informational**): when 2+ rounds from the same
  reviewer role find issues NOT traceable to the previous round's own fixes, prefer 2-3
  independent reviewer instances on the same snapshot in one round, unioning findings, over more
  serial rounds.

This does NOT weaken the REVISE rule. Nothing ships without SHIP. It bounds the *artifact* the
rule applies to, and recommends *how* subsequent rounds are structured -- it does not touch the
SHIP requirement itself.

## Alternatives Considered

| Alternative | Reason Rejected |
|-------------|----------------|
| Leave the rule as-is; rely on the human to notice and intervene | Already failed once at round 40 (an explicit check-in received "keep going"); the retrospective's whole point is that this is not a reliable backstop |
| Hard cap total rounds (e.g. reject/escalate at round 10, no re-scope option) | Would have blocked the legitimate 11-round and 7-round efforts in the same window that converged normally and produced real security fixes -- round count alone is not the pathology per the counterfactual |
| Require re-scope after every single REVISE (round 2 onward) | Too aggressive -- would fight normal incremental fix-and-recheck cycles that converge quickly (both the 11-round and 6-round efforts show 2-3 rounds of ordinary fix-recheck before any hypothetical re-scope trigger) |
| Cap rounds per REVISE-finding-category instead of total rounds | More complex to implement and track; the retrospective's evidence (same bug found twice, 15 rounds built on one undetected foundational bug) points at document-accumulation as the mechanism, not category exhaustion |
| Ship `round-count-circuit-breaker` at `Severity: blocking` | Rejected on this same PR's own plan-review: no automated per-artifact round-count tracker exists (verified -- `verdict_store.py` stores only the latest verdict per role/backend, `plan-reviewer.md` is stateless and freshly dispatched each time). A blocking rule with no real detection mechanism either never fires or fires on unverifiable self-report -- worse than the "checkbox compliance" risk already named below. Shipped as `informational` instead; promoting to `blocking` is a real follow-up requiring a persisted per-artifact counter first. |

## Rationale

The circuit breaker targets the specific mechanism the counterfactual isolated: a single
accumulating document lets each round's fix add adjacent surface area the same reviewer then
finds next round, so SHIP is not a fixed point of the patch-only process. Forcing a holistic
re-read at a fixed checkpoint interrupts that dynamic without weakening the review-until-SHIP
guarantee itself -- the artifact resets, the requirement to reach SHIP does not. The threshold
of 5 is chosen conservatively: both convergent efforts in the evidence window (11 rounds, 7
rounds) would still get one holistic-re-read checkpoint under this rule, and the pathological
effort would have been interrupted at round 5 instead of round 45.

The reviewer-coverage-diversity recommendation targets the separate sampling mechanism: it does
not claim re-scoping alone guarantees single-round convergence, because the item-4 case shows
even a small, static artifact can need a second pass. Parallel independent review is a different
lever from round-capping -- it increases coverage per unit of review effort rather than bounding
worst-case iteration count.

## Consequences

**Positive:**
- Bounds worst-case review-round waste to ~5 rounds before a forced sanity check, rather than
  unbounded.
- Makes re-scoping an explicitly sanctioned, first-class response to non-convergence instead of
  something that requires overriding "no exceptions" language.
- Preserves the REVISE-until-SHIP guarantee completely -- no unreviewed work can ship under this
  change.
- Names a second, previously-uncredited cause of multi-round review (sampling) with its own
  targeted lever, rather than treating every multi-round review as the same moving-target
  problem.

**Negative:**
- Adds one more procedural checkpoint to the review loop, with associated overhead (the holistic
  re-read + written answer) even for plans that are legitimately complex and would have
  converged with a few more ordinary rounds.
- "Is this still the smallest thing that satisfies the user's ask?" is a judgment call that could
  itself be answered wrong under time pressure, in the same way the original rule's "no
  exceptions" clause was designed to prevent rationalization.
- Both new rules ship at `informational` severity, not `blocking` -- they inform and recommend but
  do not themselves gate a REVISE/SHIP decision. This is a deliberate, evidence-based choice (see
  Alternatives Considered), not an oversight, but it means neither rule is self-enforcing yet.

**Risks:**
- A false-positive re-scope on a plan that was actually converging normally could discard useful
  accumulated review context.
- Without a persisted per-artifact round counter, an agent (or reviewer) could genuinely lose
  track of "this is round 6" without any structural prompt -- the `round-count-circuit-breaker`
  rule depends on the round count being visible in the moment (resumed context, a cross-review
  memo, or an explicit dispatch-prompt statement), which is not guaranteed.

## Exit Path

Fully reversible: remove the added clauses from `core-behavioral-rules.md` and the two new rule
sections from `plan-review-rules.md`, and revert the quoted-excerpt sync in
`standards-pack-priority.md`. No code or tooling depends on these rules; they are prose-only
guidance for the same warden loop that exists today.
