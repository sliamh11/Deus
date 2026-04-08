---
name: preserve
description: Scan this conversation and silently save anything worth permanent memory
user_invocable: true
---

# /preserve

Context-aware memory preservation. Behavior adapts to home mode vs external project mode.

## Detect mode

Check if the current working directory is the Deus home directory (`~/deus`). If it is → **Home Mode**. Otherwise → **External Project Mode**.

## Resolve vault path

Read `~/.config/deus/config.json` and use the `vault_path` value. If the env var `DEUS_VAULT_PATH` is set, use that instead. All paths below use `$VAULT` to mean this resolved path.

## Check memory level (External Project Mode only)

Compute MD5 hash of the current working directory and read `~/.config/deus/projects/<hash>.json`.
- If memory level is **restricted**: tell the user "Memory preservation is disabled for restricted projects." and stop.
- If memory level is **standard** or **full**: proceed, but with different scopes (see below).
- Home mode: always proceed.

## What to preserve

Scan the conversation for:
- Preferences or habits the user revealed
- Decisions made with lasting effect
- Things the user corrected or clarified
- Facts worth knowing in future sessions

Do not preserve one-off requests or temporary context.

**External Project Mode — standard:**
Only preserve USER preferences and behavioral corrections — things that are about the user, not the project. Skip project-specific architecture decisions, code patterns, or team info (those belong in Claude Code's auto-memory, not the vault).

**External Project Mode — full:**
Preserve both user preferences AND project-relevant decisions. Include project context where it helps future sessions.

## Where to save

Save findings to: $VAULT/CLAUDE.md

Add findings using the same compact key:value format as the file — no prose bullets.
One line per insight.

If CLAUDE.md exceeds 200 lines, archive old content to:
$VAULT/CLAUDE-Archive.md

Confirm briefly what was added, or say nothing was worth preserving if nothing qualified.
