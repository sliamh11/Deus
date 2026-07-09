# Technical Review Rules — Wardens/technical-manager

> Rules the `technical-manager` agent checks against a technical plan/architecture doc/ADR
> BEFORE it's ratified or infrastructure work starts against it.
> Add a new rule by appending a section. No agent edit needed.
>
> Format per rule: `Severity`, `Applies when`, `Check`, `Rule`.
> Severity: `critical` (drives a REVISE verdict on its own) · `moderate` (SHIP-with-findings;
> two or more compounding into the same underlying gap escalates to REVISE) · `minor` (polish).

## boundary-justification
**Severity:** moderate
**Applies when:** Document proposes splitting work into services, workstreams, or modules, or introduces a new data-modeling boundary.
**Check:** Is each proposed split justified by a real trust boundary or rate-of-change boundary, or is it arbitrary? Does new data modeling respect single-responsibility, or is something awkwardly overloaded onto an existing entity?
**Rule:** An unjustified architectural split is a moderate finding. If the split (or lack of one) creates a genuine correctness or security risk, escalate to critical.

## principle-self-consistency
**Severity:** critical
**Applies when:** Document explicitly states an architectural or process principle it claims to follow (e.g. "guarantees before features," a named isolation or audit model).
**Check:** Does every part of the document actually follow that stated principle, or does some section quietly violate it?
**Rule:** A principle the document itself claims to follow, violated anywhere else in the same document, is a critical finding — self-contradiction undermines the whole document's credibility.

## risk-register-completeness
**Severity:** moderate
**Applies when:** Document includes, or should include, a risk register for new scope.
**Check:** Are there obvious technical risks the stated risk register misses — recompute/staleness semantics, partial-failure handling, an under-specified field that blocks real implementation?
**Rule:** A missing obvious risk is a moderate finding. If the missing risk would block correct implementation once discovered, escalate to critical.

## roadmap-sequencing
**Severity:** moderate
**Applies when:** Document has phased or sequenced work with dependencies between phases.
**Check:** Do phase dependencies make sense? Does the plan avoid re-touching already-ratified content it has no stated reason to reopen?
**Rule:** Illogical phase sequencing, or reopening ratified work without a stated reason, is a moderate finding.

## buildability-specificity
**Severity:** critical
**Applies when:** Document describes a field, lifecycle, or interface a team would need to implement against.
**Check:** Is anything hand-waved that a team would actually need specified before writing code — an untyped/unranged field, a lifecycle with no defined trigger?
**Rule:** A hand-waved implementation detail that blocks a team from actually building against the document is a critical finding, not a style note.

## gap-disclosure-vs-schedule
**Severity:** critical
**Applies when:** Document candidly admits an unresolved gap AND simultaneously schedules dependent work with a BUILD/ship verdict.
**Check:** Is there a scheduled resolution for the gap before the dependent work is meant to start?
**Rule:** Candidly disclosing a gap does not offset the risk of scheduling dependent work without a resolution plan for it — this is critical regardless of how honestly the gap was flagged. The candor is good; the missing resolution schedule is still a real defect.

## diagram-prose-consistency
**Severity:** critical
**Applies when:** Document includes a diagram or table alongside descriptive prose about the same subject.
**Check:** Does the diagram/table actually match the prose, especially after any change the prose describes making?
**Rule:** A diagram or table that contradicts the prose it's meant to visualize is critical, not cosmetic — implementers build from whichever one they happen to read first.

---

## Remediation Details

### boundary-justification
**Cite:** `technical-manager` agent's "Architectural soundness" check
**Remediation:** Either state the specific trust/rate-of-change boundary that justifies the split, or merge the pieces and remove the unjustified boundary.

### principle-self-consistency
**Cite:** `technical-manager` agent's "Self-consistency with the plan's own stated principles" check
**Remediation:** Fix the violating section to align with the stated principle, or revise the principle statement itself if it was aspirational and the document should be honest about the exception.

### risk-register-completeness
**Cite:** `technical-manager` agent's "Risk-register completeness" check
**Remediation:** Add the missing risk to the register with an owner and mitigation, or an explicit "accepted risk" note if it's being deliberately deferred.

### roadmap-sequencing
**Cite:** `technical-manager` agent's "Roadmap sequencing" check
**Remediation:** Reorder phases to resolve the dependency issue, or add a stated reason for reopening previously-ratified content.

### buildability-specificity
**Cite:** `technical-manager` agent's "Buildability" check
**Remediation:** Add the missing type/range/direction/trigger specification directly to the field or lifecycle description in question.

### gap-disclosure-vs-schedule
**Cite:** `technical-manager` agent's Rules of engagement — "admits a gap... but still schedules dependent work to BUILD... is a CRITICAL finding, not a MINOR one"
**Remediation:** Either delay the dependent work's BUILD verdict until the gap has a scheduled resolution, or explicitly scope the dependent work to not require the unresolved part.

### diagram-prose-consistency
**Cite:** `technical-manager` agent's Rules of engagement — diagram/prose contradiction guidance
**Remediation:** Regenerate the diagram/table from the corrected prose (or vice versa) before shipping — never leave the two disagreeing.
