---
name: add-guardrails
description: Install a self-contained development-process guardrail kit (plan-review, code-review, and verification gates) into the current repository, so any developer's Claude Code follows the same review discipline on clone. Triggers on "add guardrails", "install guardrails", "set up quality gates", "warden gates for this repo".
user_invocable: true
---

# /add-guardrails

Installs a portable warden quality-gate kit into the **current repo**. After it runs and you commit
the changes, every developer who clones the repo gets three review gates enforced by Claude Code
hooks — with no dependency on any external checkout:

- **plan-review** before any Edit / Write / MultiEdit
- **code-review** before `git commit`
- **verification** before `git commit`

It ships self-contained agents (`.claude/agents/`), their rule books (`.claude/wardens/`), a small
vendored gate (`.claude/hooks/warden-gate.py`), a process-rules doc (`.claude/rules/`), the hook
wiring (merged into `.claude/settings.json`), and a short section appended to `CLAUDE.md`.

## How to run it

1. **Confirm the target.** This installs into the current working directory, which must be a git
   repository. If the intended target is ambiguous, confirm it with the user first.

2. **Locate the installer** (it ships alongside this skill):
   ```bash
   # The second path assumes a Deus checkout at ~/deus; adjust if yours is elsewhere.
   INSTALLER=$(find ~/.claude/skills ~/deus/.claude/skills -maxdepth 2 -name add_guardrails.py 2>/dev/null | head -1)
   ```
   If that finds nothing, ask the user where their Deus checkout lives and look under its
   `.claude/skills/add-guardrails/`.

3. **Choose opt-in modules.** Use AskUserQuestion to ask which optional modules to include beyond
   the always-installed baseline (plan/code/verification gates, the process-rules doc, the gate
   script, and the hook wiring):
   - `design-logs` — ADR-style decision records under `docs/decisions/`. Good for any repo that
     wants a durable trail of design decisions.
   - `codegraph-first` — a semantic-search-before-grep exploration rule. Include **only** if the
     repo is indexed for code-intelligence tools.

   Confirm the available modules with `python3 "$INSTALLER" --list-modules`.

4. **Preview, then apply.** Always dry-run first and show the user the planned actions:
   ```bash
   python3 "$INSTALLER" --target "$(pwd)" --modules <chosen,comma,separated> --dry-run
   ```
   On approval, re-run the same command without `--dry-run`.

5. **Review and commit.** Tell the user to inspect `git diff` and commit the new files so the gates
   travel with the repo. The gates take effect in new Claude Code sessions opened in that repo.

## Updating / re-running

Re-running is safe and idempotent: existing `settings.json` hooks and `CLAUDE.md` content are
merged, never replaced, and an unchanged re-run is a no-op. Pass `--update` to refresh kit files
that have drifted from the templates.

## Notes

- **macOS / Linux only in v1.** The hook wiring uses a `bash` wrapper, so on Windows the gates are
  inert (they fail open — no blocking, no errors). Cross-platform support is a follow-up.
- **Fail-open by design.** If a teammate's machine has no `python3`, the gates allow all actions
  rather than blocking work.
- **Distinct from `/onboard`.** `/onboard` indexes a repo for code-intelligence search; this skill
  installs the review-process gates. They are independent — a repo can have either or both.
- **Availability in other repos.** This skill must be discoverable from the session you run it in.
  If it only appears inside the Deus repo, make it global by symlinking it into
  `~/.claude/skills/add-guardrails/` so `/add-guardrails` works from any repo.
