---
name: wardens
description: View, toggle, and configure wardens — the quality gates that review plans, code, and security
user_invocable: true
---

If no arguments, run `~/deus/tui/target/release/deus-tui` via Bash (piped mode) and show the full dashboard output directly in chat. If the binary doesn't exist, build it first with `cargo build --release` in `~/deus/tui/`.

If the user passed arguments after `/wardens` (e.g. `/wardens disable plan-reviewer`), run `python3 scripts/wardens.py <args>` via Bash and show the output.

For `customize <name>`, don't launch `claude -p` — you're already in a session. Instead, read the current `custom_instructions` from `.claude/wardens/config.json` for the named warden, ask the user what behavior they want to customize, help them write it, then save it back to `config.json`.
