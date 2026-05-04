# ADR: Config-driven vault auto-loading

**Status:** Accepted
**Date:** 2026-05-02
**Scope:** `deus-cmd.sh` startup, `~/.config/deus/config.json`

## Context

The deus launcher (`deus-cmd.sh`) injects vault files into the system prompt
at startup as `=== VAULT: <file> ===` blocks. Originally this list was
hardcoded — CLAUDE.md, STUDY.md, INFRA.md were always loaded. This caused two
problems:

1. **Token waste.** Files like STUDY.md and INFRA.md are only relevant to
   specific session types (study sessions, infra work) but were loaded every
   session. The memory-retrieval hook and `/resume` skill already load them
   on-demand when semantically relevant.

2. **Intent-vs-code drift.** The vault CLAUDE.md description stated "only
   CLAUDE.md auto-loads every turn" but the launcher contradicted this by
   loading all three files. The drift went undetected because no automated
   check enforced the documented contract.

## Decision

The list of auto-loaded vault files is **config-driven** via the
`vault_autoload` key in `~/.config/deus/config.json`:

```json
{
  "vault_path": "/path/to/vault",
  "vault_autoload": ["CLAUDE.md"]
}
```

The launcher reads this array and loops over it. If the key is missing, it
defaults to `["CLAUDE.md"]`.

### Rules

- **Only CLAUDE.md should be in the default.** It contains identity, rules,
  and the index to everything else — it's the one file that's always relevant.
- Other vault files (STUDY.md, INFRA.md, Persona/INDEX.md) are
  **on-demand** — loaded by `/resume`, the memory-retrieval hook, or explicit
  Read when the session topic warrants it.
- Users can add files to `vault_autoload` if they want them pre-loaded. This
  is a user choice, not a system default.
- The memory-cite hook (`~/.claude/hooks/memory-cite.sh`) seeds auto-loaded
  files as "already read" at session start, so redundant Read calls are
  soft-blocked.

### Why not a drift-guard script?

A drift guard that hardcodes disallowed filenames is fragile and
user-specific. Making the loader data-driven eliminates the drift category
entirely — the config IS the source of truth, so code can't diverge from
intent.

## Consequences

- Startup token cost drops by ~300-700 tokens (STUDY.md + INFRA.md no longer
  loaded unconditionally).
- On-demand loading via hooks/skills means the files still appear when
  relevant — no loss of context quality.
- New vault files can be auto-loaded by adding them to the config array,
  no code change needed.
