# Copy Rules — Wardens/ui-copy-writer

> Rules the `ui-copy-writer` agent checks against AFTER implementation.
> Add a new rule by appending a section. No agent edit needed.
>
> Format per rule: `Severity`, `Applies when`, `Check`, `Rule`, `Cite`.
> Severity: `critical` (user will be confused or make a wrong decision) · `major` (noticeable friction or inconsistency) · `minor` (polish).

## error-message-anatomy
**Severity:** critical
**Applies when:** Change adds or modifies an error message, catch block output, or failure notification shown to the user.
**Check:** Does the error message contain all three parts: (1) what happened, (2) why it happened, and (3) what the user should do next? Are raw error codes, stack traces, or internal class names hidden from the user?
**Rule:** Every user-facing error message must explain what happened, why, and what to do next. Never show raw exceptions, internal identifiers, or stack traces without a human-readable wrapper.
**Cite:** Nielsen H9 (Help users recognize, diagnose, and recover from errors); Microsoft Writing Style Guide — error messages

## help-text-scannable
**Severity:** major
**Applies when:** Change adds or modifies help text, documentation strings, command descriptions, or instructional copy.
**Check:** Is the help text structured for scanning — bullet points, numbered steps, or short paragraphs? Can a user find the answer to their question without reading everything?
**Rule:** Help text must be scannable: bullet points over paragraphs, action verbs at the start of each item, most important information first. If it takes more than 5 seconds to find the key point, restructure.
**Cite:** Inverted pyramid writing style; plain language guidelines

## system-message-voice
**Severity:** major
**Applies when:** Change adds or modifies system messages, status updates, notifications, or automated responses the user sees.
**Check:** Does the message sound like it was written by a human? Is it consistent with the voice established in existing system messages? Does it avoid robotic patterns ("Operation completed successfully", "An error has occurred")?
**Rule:** System messages must use consistent, human voice. Avoid passive voice, bureaucratic phrasing, and the word "successfully" (if it succeeded, say what happened). Match the existing product tone.
**Cite:** Product voice consistency; Deus personality as a personal assistant

## status-indicator-clarity
**Severity:** major
**Applies when:** Change adds or modifies status indicators, progress messages, loading states, or state-change notifications.
**Check:** Can the user understand the current state without referring to documentation? Does the indicator explain what's happening AND set expectations for what comes next?
**Rule:** Status indicators must be self-explanatory without docs. Include what's happening now and what the user should expect next. "Processing..." is insufficient — "Sending message to agent (usually takes 5-10s)..." is clear.
**Cite:** Nielsen H1 (Visibility of system status)

## onboarding-first-use
**Severity:** major
**Applies when:** Change introduces a new feature, command, panel, or interaction that a user will encounter for the first time.
**Check:** Is there introductory copy for first-time users? Does it explain what this is, what it does, and how to start? Does it avoid assuming prior knowledge of the system?
**Rule:** New features must include onboarding copy that works for first-time users. Assume zero prior context. If a feature has no natural place for intro text, add a hint or placeholder on first encounter.
**Cite:** Nielsen H10 (Help and documentation); first-run experience patterns

## no-jargon-leak
**Severity:** critical
**Applies when:** Any user-facing string is added or modified.
**Check:** Does the text contain internal terms, class names, enum values, config keys, or technical jargon that a non-developer user wouldn't understand? Examples: "ChatState::Streaming", "ECONNREFUSED", "container exited with code 137", "webhook payload", "MCP server".
**Rule:** User-facing text must never expose internal identifiers, class names, or developer jargon. Translate technical concepts into user terms. "Connection lost" not "ECONNREFUSED". "The agent stopped unexpectedly" not "container exited with code 137".
**Cite:** Plain language guidelines; Deus user-facing quality standard

## placeholder-honesty
**Severity:** major
**Applies when:** Change adds placeholder, stub, or not-yet-implemented feature indicators visible to the user.
**Check:** Does the placeholder tell the user when the feature is expected, or does it just say "coming soon"? Is it clear that this is intentionally incomplete, not broken?
**Rule:** Placeholder text for unfinished features must set expectations: what it will do and when it's expected. "Coming soon" alone is insufficient — add context. If no timeline exists, say "planned" and describe what it will do.
**Cite:** User trust principles; progressive disclosure

## hebrew-typography
**Severity:** minor
**Applies when:** Change includes Hebrew text in artifacts, templates, or locale-specific output.
**Check:** Does the Hebrew text use proper typography? Correct quotation marks (״ ״ not " "), proper geresh/gershayim for abbreviations, correct punctuation placement at BiDi boundaries, no broken word order in mixed Hebrew+English strings?
**Rule:** Hebrew text in artifacts must use proper Hebrew typography: correct quotation marks, geresh/gershayim for abbreviations, and correct punctuation at script boundaries. Mixed Hebrew+English must render in correct visual order.
**Cite:** Academy of the Hebrew Language typography standards; Deus user profile (Hebrew speaker)
