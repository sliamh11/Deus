# Product Review Rules — Wardens/product-manager

> Rules the `product-manager` agent checks against a product scope/PRD/business-requirements
> document BEFORE it's greenlit for stakeholder approval.
> Add a new rule by appending a section. No agent edit needed.
>
> Format per rule: `Severity`, `Applies when`, `Check`, `Rule`.
> Severity: `critical` (drives a REVISE verdict on its own) · `moderate` (SHIP-with-findings;
> two or more compounding into the same underlying gap escalates to REVISE) · `minor` (polish).

## internal-consistency
**Severity:** critical
**Applies when:** Document has multiple sections describing the same scope, capability, or commitment (e.g. an executive summary and a detailed scope section).
**Check:** Does every section agree with every other section — does the executive summary's framing match what the actual MVP-scope section commits to; do acceptance criteria map to every capability claimed elsewhere?
**Rule:** A mismatch between how the document frames its scope in one place and what it actually commits to in another is a critical finding — it will get read differently by different stakeholders and cause disputes later.

## scope-boundary-clarity
**Severity:** moderate
**Applies when:** Document labels items "MVP" / "in scope" versus "future work" / "out of scope".
**Check:** Does anything labeled MVP/in-scope secretly depend on something in future-work to deliver its claimed value, or vice versa?
**Rule:** MVP-labeled scope must be able to deliver its stated value on its own. A hidden dependency on a future-work item is moderate; if the MVP's entire value proposition is unreachable without it, escalate to critical.

## decision-completeness
**Severity:** critical
**Applies when:** Document presents a scope or plan that implies a business-critical choice (pricing, eligibility, prioritization, a build-vs-buy call, etc.).
**Check:** Does the document force an explicit decision on that choice, or does it gloss over it / leave it implicit?
**Rule:** Every business-critical fork must be an explicit, stated decision in the document — not an assumption the reader has to infer.

## business-case-soundness
**Severity:** critical
**Applies when:** Document states a value proposition and defines acceptance criteria meant to validate that the scope delivers it.
**Check:** Do the acceptance criteria actually test the stated value proposition, or only a weaker, easier-to-measure proxy (e.g. "the feature is visible" instead of "the feature was acted upon / delivered the claimed value")?
**Rule:** Acceptance criteria that test only a proxy of the real value proposition are a critical finding. Name the specific criterion that's a proxy and what a real test of the value prop would actually require.

## precondition-explicitness
**Severity:** moderate
**Applies when:** The document's success depends on some fact or condition already being true (e.g. "the pilot customer already uses both integration platforms").
**Check:** Is that dependency captured as an explicit, checkable selection or entry criterion, or left implicit in the narrative?
**Rule:** Any precondition the plan's success silently depends on must become an explicit, checkable criterion — not something only discovered when the precondition turns out false.

## executive-scrutiny
**Severity:** minor
**Applies when:** Document makes a claim a sharp executive reader would immediately question or push back on.
**Check:** Does the document anticipate the obvious follow-up question and answer it nearby, or leave it hanging?
**Rule:** Flag any claim that invites an immediate "wait, what about X" that the document doesn't address near that claim. Escalate to moderate if the unanswered question bears on whether the scope is actually sound.

---

## Remediation Details

### internal-consistency
**Cite:** `product-manager` agent's "Internal consistency" check
**Remediation:** Reconcile the conflicting sections — either narrow the summary framing to match the committed scope, or expand the scope section to match what the summary promises. State which section is authoritative.

### scope-boundary-clarity
**Cite:** `product-manager` agent's "Scope creep or scope confusion" check
**Remediation:** Either move the dependency into the MVP's own scope, or explicitly note in the MVP section that its value is partial/conditional pending the future-work item.

### decision-completeness
**Cite:** `product-manager` agent's "Missing decisions" check
**Remediation:** Add an explicit decision statement (who decided, what was decided, and why) for each business-critical fork identified.

### business-case-soundness
**Cite:** `product-manager` agent's "Business-case soundness" check and Rules of engagement (proxy-criterion guidance)
**Remediation:** Rewrite the acceptance criterion to test the actual claimed value, or add a second criterion that closes the gap between the proxy and the real outcome.

### precondition-explicitness
**Cite:** `product-manager` agent's "Precondition gaps" check
**Remediation:** Add the precondition as a named entry/selection criterion in the relevant section (e.g. a pilot-selection or launch-readiness checklist).

### executive-scrutiny
**Cite:** `product-manager` agent's "Anything a sharp executive would immediately push back on" check
**Remediation:** Add a sentence or short subsection addressing the anticipated objection directly, near the claim that invites it.
