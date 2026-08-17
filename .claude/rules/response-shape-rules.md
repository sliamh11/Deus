# Response Shape (hard rule, Claude Code CLI only)
# Always loaded, always active — no toggle. Top-priority shaping rule for
# every Claude Code CLI response in this repo. Adapted from
# github.com/ayghri/i-have-adhd (MIT) — see
# docs/decisions/adhd-output-shaping-adoption.md for full provenance and
# what differs from upstream.

## Scope

- Governs my conversational/task responses in Claude Code CLI sessions in
  this repo — not commit messages, PR descriptions, or session-log entries,
  which follow their own format conventions (Conventional Commits, PR
  template, etc.) and are not reshaped by this rule.
- Does NOT govern text Deus generates for end users on
  WhatsApp/Telegram/Slack/Discord/Gmail — those remain governed by
  `copy-writer`/`ux-reviewer` conventions, unchanged by this rule.
- Does NOT reach the Codex backend — `.claude/rules/*.md` is a
  Claude-Code-specific loading mechanism; Codex sessions read their own
  `hooks.json`/config and get no equivalent shaping today. This is a
  deliberate, named gap, not an oversight — Codex parity is a separate,
  unscoped follow-up if ever wanted.

## Rules

- Lead with the next action. First line is something to do, not context.
- Number multi-step tasks. Fewest steps that still work; fold trivial ones in.
- End with one concrete next action if anything is left open.
- Suppress tangents. Answer a side issue yourself if you can; otherwise surface it once, at the end, as a separate question.
- Restate state every turn. Use the task/plan tool for multi-step work — the checklist restates, don't also narrate the plan in prose.
- Give specific time estimates, not vague ones.
- Make completed work visible in concrete terms — don't bury it in a recap.
- Matter-of-fact tone for errors: state cause and fix, never "uh oh" or "there seems to be a problem."
- Cap lists at 5 items. Beyond that, split into do-now vs. later.
- No preamble ("Great question," "Sure!"), no recap, no closing pleasantries ("Hope this helps," "Let me know if you need anything else"). Exception: a brief tool-call announcement ("Let me read the file.") is not preamble — see carve-out 1 below.

## What this does not override

This list, and this file's own structural content generally, is exempt from
the Cap-lists-at-5 rule above — never trim an item here to satisfy it.

None of the above changes these — they are procedural/mechanical obligations, not stylistic choices:

1. Tool-call announcements before tool calls (unqualified — not limited to any specific tool).
2. Harness-level, execution-mode-specific session conventions (e.g. a background job's structured completion signaling). These are not documented in this repo — they come from the harness's own per-session instructions for that mode — so this rule can't name them specifically; it only states that when such a convention applies, it isn't overridden by the style preferences above.
3. Execution Gate confirmations before destructive/irreversible actions.
4. Plan-review-gate sequencing (SHIP before edits).
5. English-only chat responses.
6. Structured report/output formats prescribed by other rules files — a warden's fixed report template (e.g. `plan-reviewer`'s Verdict/Blocking-Issues/Questions structure, `code-reviewer`'s `ReportFindings` schema) is unaffected. This rule governs conversational/task responses, not warden-report structures owned by their own instruction files.
