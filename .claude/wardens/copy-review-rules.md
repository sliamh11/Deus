# Copy Review Rules — Wardens/copy-writer (document-prose)

> Rules the document-prose `copy-writer` agent checks against scope docs, PRDs, ADRs, READMEs,
> technical plans, and proposals — for how clearly and consistently they are WRITTEN, not
> whether the underlying decisions are good.
> Add a new rule by appending a section. No agent edit needed.
>
> Format per rule: `Severity`, `Applies when`, `Check`, `Rule`.
> Severity: `blocking` (must fix before SHIP) · `warning` (should address) · `informational`
> (author's awareness).
>
> Not to be confused with `ui-copy-rules.md`, which governs the separate `ui-copy-writer`
> agent (UI microcopy — error messages, button labels, status indicators). This file is for
> document-length prose review.

## terminology-consistency
**Severity:** blocking
**Applies when:** Document uses more than one term/phrase for what appears to be the same concept, or uses the same term for what turn out to be different concepts.
**Check:** Is each concept named consistently after its first definition? Does any single term carry two or more distinct meanings across the document?
**Rule:** A term meaning different things in different places, or a single concept named three or more different ways, is a blocking finding — terminology drift compounds across a document's life and confuses every future reader.

## structural-cross-reference-accuracy
**Severity:** warning
**Applies when:** Document contains internal references ("see Section X", section numbers) or a stated count followed by an enumerated list.
**Check:** Do the internal references actually point at the right place? Does a stated count ("three pillars") match what follows (a list of four)?
**Rule:** Verify every structural claim against the actual document rather than assuming it's right. A broken cross-reference or count mismatch is a warning finding.

## first-use-definition
**Severity:** warning
**Applies when:** Document uses an acronym or jargon term.
**Check:** Is it defined before or at first use, or covered in a glossary if one exists?
**Rule:** An undefined acronym or jargon term at first use is a warning finding.

## clarity-pass
**Severity:** informational
**Applies when:** Any sentence in the document.
**Check:** Would a first-time reader need to re-read the sentence to parse it?
**Rule:** Flag sentences that require a re-read as informational, unless the ambiguity changes the document's actual meaning — in that case, escalate to warning.

## tone-consistency
**Severity:** informational
**Applies when:** Document has multiple sections, was revised over time, or shows signs of multiple authorship.
**Check:** Is the tone plain and professional throughout, or does it drift into jargon, hype, or vagueness anywhere?
**Rule:** Flag tone drift as informational.

## repetition-check
**Severity:** informational
**Applies when:** Document has multiple paragraphs addressing a related topic.
**Check:** Does any paragraph restate an earlier point without adding new information?
**Rule:** Flag pure repetition as informational — it's a candidate for trimming, not a defect.

## mechanics
**Severity:** informational
**Applies when:** Any document.
**Check:** Typos, awkward phrasing, or inconsistent capitalization of a defined term (e.g. a role name like "Admin" versus the common noun "administrator").
**Rule:** Flag mechanical issues as informational unless the inconsistency creates real ambiguity about meaning — in that case, escalate to warning.

---

## Remediation Details

### terminology-consistency
**Cite:** `copy-writer` (document-prose) agent's "Consistency" check
**Remediation:** Pick one term per concept and replace every other variant with it throughout the document. If two genuinely different concepts share a name, rename one of them.

### structural-cross-reference-accuracy
**Cite:** `copy-writer` (document-prose) agent's "Structural cross-checks" check
**Remediation:** Fix the section reference to point at the correct location, or correct the stated count to match the actual enumerated list.

### first-use-definition
**Cite:** `copy-writer` (document-prose) agent's "Undefined terms" check
**Remediation:** Expand the acronym/term at its first use in the document, or add it to a glossary section if the document has one.

### clarity-pass
**Cite:** `copy-writer` (document-prose) agent's "Clarity" check
**Remediation:** Split the sentence, or reorder its clauses so the reader doesn't need a second pass.

### tone-consistency
**Cite:** `copy-writer` (document-prose) agent's "Tone" check
**Remediation:** Rewrite the drifting passage to match the document's dominant register.

### repetition-check
**Cite:** `copy-writer` (document-prose) agent's "Repetition" check
**Remediation:** Cut the repeated paragraph, or merge it into the earlier statement of the same point.

### mechanics
**Cite:** `copy-writer` (document-prose) agent's "Mechanics" check
**Remediation:** Fix the typo/capitalization directly; if a role-name vs. common-noun distinction is ambiguous, define both terms once near first use.
