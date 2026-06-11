## Development guardrails

This repo enforces a three-stage review discipline through Claude Code hooks. The full contract is
in `.claude/rules/dev-process.md`; the short version:

- **Before editing** — draft a plan, run the `plan-reviewer` agent, get SHIP, then
  `python3 .claude/hooks/warden-gate.py mark plan-reviewed SHIP "reason"`.
- **Before `git commit`** — run the `code-reviewer` agent on the diff and the `verification-gate`
  agent (build/test/lint), get SHIP from each, then mark `code-reviewed` and `verified`.
- **Always** — feature branch (never the default branch), one concern per branch, show the commit
  message for approval first, and verify before claiming done.

A REVISE/BLOCK verdict means fix and re-run the agent until SHIP — never bypass the gate. Markers
reset each session and when a new plan begins, so every change is reviewed fresh.
