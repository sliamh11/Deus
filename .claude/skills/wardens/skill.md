---
name: wardens
description: View, toggle, and configure wardens — the quality gates that review plans, code, and security
user_invocable: true
---

If the user passed arguments after `/wardens` (e.g. `/wardens disable plan-reviewer`), run `python3 scripts/wardens.py <args>` via Bash and show the output.

If no arguments, tell the user to run the interactive TUI directly:

> Run `! python3 scripts/wardens.py` to open the interactive wardens panel.

The `!` prefix is required so the TUI gets a real terminal (arrow keys, toggle with space).

For `customize <name>`, don't launch `claude -p` — you're already in a session. Instead, read the current `custom_instructions` from `.claude/wardens/config.json` for the named warden, ask the user what behavior they want to customize, help them write it, then save it back to `config.json`.
