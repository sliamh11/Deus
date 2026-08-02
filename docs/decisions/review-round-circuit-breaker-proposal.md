# ADR: Round-count circuit breaker for the plan-review REVISE loop

**Date:** 2026-08-02
**Status:** Proposed
**Reversibility:** REVERSIBLE
**Scope:** `.claude/wardens/plan-review-rules.md`, `.claude/rules/core-behavioral-rules.md` (§ Execution Gates) — proposed, not yet applied to either file

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

## Decision

We will add a round-count circuit breaker to the plan-review loop instead of leaving "re-run
until SHIP" unbounded.

**Proposed rule** (to be added to `.claude/wardens/plan-review-rules.md`, with a corresponding
clause added to `core-behavioral-rules.md` § Execution Gates):

> After 5 review rounds on the same plan artifact, the next action is not a 6th round patching
> the same document. It is a mandatory holistic re-read of the whole accumulated artifact, and a
> written answer to one question: "is this still the smallest thing that satisfies the user's
> actual ask?"
> - If the answer is no: re-scope and restart the round count against the new, smaller artifact.
> - If the answer is yes: record why, and continue -- the round count resets after this
>   checkpoint, not before.
>
> This does NOT weaken the REVISE rule. Nothing ships without SHIP. It bounds the *artifact* the
> rule applies to, not the gate itself. The "re-run until SHIP" clause in
> `core-behavioral-rules.md` should be amended to read: "...applies to a bounded artifact; when a
> plan fails to converge after 5 rounds, re-scoping is the sanctioned response to
> non-convergence, not a rationalization for stopping review."

## Alternatives Considered

| Alternative | Reason Rejected |
|-------------|----------------|
| Leave the rule as-is; rely on the human to notice and intervene | Already failed once at round 40 (an explicit check-in received "keep going"); the retrospective's whole point is that this is not a reliable backstop |
| Hard cap total rounds (e.g. reject/escalate at round 10, no re-scope option) | Would have blocked the legitimate 11-round and 7-round efforts in the same window that converged normally and produced real security fixes -- round count alone is not the pathology per the counterfactual |
| Require re-scope after every single REVISE (round 2 onward) | Too aggressive -- would fight normal incremental fix-and-recheck cycles that converge quickly (both the 11-round and 6-round efforts show 2-3 rounds of ordinary fix-recheck before any hypothetical re-scope trigger) |
| Cap rounds per REVISE-finding-category instead of total rounds | More complex to implement and track; the retrospective's evidence (same bug found twice, 15 rounds built on one undetected foundational bug) points at document-accumulation as the mechanism, not category exhaustion |

## Rationale

The circuit breaker targets the specific mechanism the counterfactual isolated: a single
accumulating document lets each round's fix add adjacent surface area the same reviewer then
finds next round, so SHIP is not a fixed point of the patch-only process. Forcing a holistic
re-read at a fixed checkpoint interrupts that dynamic without weakening the review-until-SHIP
guarantee itself -- the artifact resets, the requirement to reach SHIP does not. The threshold
of 5 is chosen conservatively: both convergent efforts in the evidence window (11 rounds, 7
rounds) would still get one holistic-re-read checkpoint under this rule, and the pathological
effort would have been interrupted at round 5 instead of round 45.

## Consequences

**Positive:**
- Bounds worst-case review-round waste to ~5 rounds before a forced sanity check, rather than
  unbounded.
- Makes re-scoping an explicitly sanctioned, first-class response to non-convergence instead of
  something that requires overriding "no exceptions" language.
- Preserves the REVISE-until-SHIP guarantee completely -- no unreviewed work can ship under this
  proposal.

**Negative:**
- Adds one more procedural checkpoint to the review loop, with associated overhead (the holistic
  re-read + written answer) even for plans that are legitimately complex and would have
  converged with a few more ordinary rounds.
- "Is this still the smallest thing that satisfies the user's ask?" is a judgment call that could
  itself be answered wrong under time pressure, in the same way the original rule's "no
  exceptions" clause was designed to prevent rationalization.

**Risks:**
- A false-positive re-scope on a plan that was actually converging normally could discard useful
  accumulated review context.
- Without a mechanism to detect it, an agent could pattern-match "5 rounds happened" without
  doing a genuine holistic re-read (checkbox compliance rather than the intended reflection).

## Exit Path

Fully reversible: remove the added clause from `core-behavioral-rules.md` and the corresponding
rule from `plan-review-rules.md`. No code or tooling depends on this threshold; it is prose-only
guidance for the same warden loop that exists today.
