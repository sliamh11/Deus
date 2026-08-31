# dsh port: running Deus's Claude Code setup on DeepSeek Harness

Generates a [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
(`dsh`) config patch from a live Claude Code setup, so the hooks, MCP servers,
skills and wardens already on the host keep working under a different harness.

**This is not a Deus backend adapter.** `docs/decisions/backend-neutral-agent-runtime.md`
governs Deus's *container agent-runtime*, where Deus owns the tool and credential
contract and backends route through its broker. This directory is about the
personal top-level CLI harness a developer drives interactively — the same
carve-out `deus connect` sits in. Nothing here changes the container runtime.

## Usage

```sh
python3 integrations/dsh/generate_dsh_config.py           # write generated/
python3 integrations/dsh/generate_dsh_config.py --check   # non-zero if any matcher is dead
```

Then point dsh at the patch:

```sh
dsh --profile headless --patch integrations/dsh/generated/deus-dsh.patch.yml --dump-config
```

### Seed pi-ai subscription grants

The shipped `llm-pi-ai` adapter can use and refresh the OAuth grant shapes
already held by Claude Code and Codex. Seed either or both into dsh's private
credential store with:

```sh
python3 integrations/dsh/seed_pi_ai_oauth.py --dry-run
python3 integrations/dsh/seed_pi_ai_oauth.py

# Or seed only one provider:
python3 integrations/dsh/seed_pi_ai_oauth.py --provider anthropic
python3 integrations/dsh/seed_pi_ai_oauth.py --provider openai-codex
```

The helper reads `~/.claude/.credentials.json` and `~/.codex/auth.json`, merges
`llm-pi-ai/anthropic` and/or `llm-pi-ai/openai-codex` records into
`~/.dsh/.credentials.yaml`, and backs up an existing destination before an
atomic mode-`0600` write. It never prints credential values and never modifies
the source files. PyYAML is required (`python3 -m pip install pyyaml`).

This deliberately uses pi-ai's provider implementation. It does not copy
jcode's raw request implementation, freeze a client version, or add request
fingerprint/header spoofing. Subscription providers can restrict where their
grants may be used; review the applicable provider terms before seeding them.

Output lands in `generated/`, which is **gitignored**: it is built from the
host's own configuration and embeds personal hook commands, MCP server rows
(Linear workspaces, Outlook, Asana) and warden bodies. Only the generator and
this README are tracked.

The generator is **read-only** with respect to `~/.claude` and `~/.claude.json`.
The port is additive — dsh reads its own generated files, and rolling back means
not launching dsh. No Claude Code config is modified.

## Why a translation step is needed at all

dsh ships `@deepseek-ai/dsh-hooks-claude-code`, a bridge that runs an existing
Claude Code `hooks.json` unchanged. It reproduces Claude Code's matcher
semantics faithfully — including the part that silently breaks a naive port:

```ts
// packages/hooks/hook-protocol/src/matcher.ts
if (mode === 'claude-code' && CLAUDE_LITERAL.test(pattern)) {
  return pattern.split('|').includes(query)
}
```

That is an exact, case-sensitive membership test. dsh's tools are snake_case
(`bash`, `write`, `edit`, `exit_plan_mode`, `subagent`), so a Claude Code matcher
of `Bash` never matches `bash`, so such a hook would register and then never
be selected. (Stated from the matcher's semantics and the tool catalog, not
from an observed run — see "What has NOT been verified" below.)

Measured on this host, control and treatment graded by one identical checker
against dsh's real tool catalog:

| | Untranslated (control) | Translated (treatment) |
|---|---|---|
| Matcher groups | 10 live / **11 dead** | 21 live / **0 dead** |
| Handlers | 13 live / **28 dead** | 41 live / **0 dead** |
| Checker exit | 1 | 0 |

Every dead group is a `PreToolUse` or `PostToolUse` gate. The 10 that survive
untranslated are the matcher-less `SessionStart` / `UserPromptSubmit` events
plus one MCP regex — they never compare a tool name, so nothing can mismatch.

A dead handler raises no error. That is the whole reason `tool_name_map.py`
separates equivalences from approximations from drops, and why the generator
reports each one instead of resolving it quietly.

## Design

- **Lookup registry** (`tool_name_map.py`): three structurally distinct dicts —
  `TOOL_MAP` (direct equivalents), `APPROXIMATED` (widenings that are *not*
  equivalent, e.g. `MultiEdit` → `str_replace_editor`), `UNMAPPED` (no dsh
  counterpart). Separate containers mean an approximation cannot later be read
  as an equivalence; the distinction lives in the structure, not a comment.
- **Strategy** (`map_matcher`): branches on the bridge's own literal-vs-regex
  discriminator. Regex matchers pass through verbatim — MCP tool names keep
  their `mcp__<server>__<tool>` shape in dsh. Literal matchers translate
  per-alternative. It returns an explicit **status**
  (`match-all` / `unchanged` / `translated` / `dead`) because a bare `None`
  return once meant both "no matcher, fires for everything" and "nothing maps,
  fires for nothing" — opposite meanings behind one value, which dropped every
  match-all group on the first run.
- **Emitters**: `emit_hooks`, `emit_mcp_rows`, `emit_skill_row`,
  `emit_subagents` are independent pure functions returning `(rows, report)`.
  One writer composes them. No shared mutable state.

## What it emits

| Source | dsh row | Count on this host |
|---|---|---|
| Both `settings.json` scopes' `hooks` | `dsh-hooks-claude-code` | 1 row, 38-39 effective handlers |
| `~/.claude.json` `mcpServers` | `dsh-mcp-client` | 10 rows (all stdio) |
| `~/.claude/skills/` | `dsh-skill-filesystem` | 1 row, 84 skills |
| `~/.claude/agents/*.md` | `dsh-tool-subagent` | 33 rows |

The project-scope path is derived from this file's own location, so the handler
count follows **whichever branch is checked out in the tree you run from** —
running from a worktree off `origin/main` picks up a `Stop` hook that a branch
removing it does not have. That is intended: the generator describes the tree
it is run in, not a fixed path.

Agent frontmatter maps as `name:` → `toolName`, body → `persona`, `model:` →
`agentOptions.model`, `tools:` → `toolFilter.allow`.

## Verification

Four checks. Read what each one actually establishes — they are not
interchangeable, and the difference between checks 2 and 3 has already hidden a
fatal defect once.

1. `generate_dsh_config.py --check` — non-zero exit if any matcher group is dead.
2. `dsh --profile headless --patch generated/deus-dsh.patch.yml --dump-config` —
   expect exit 0 and zero stderr. **This only proves the tree COMPOSES.** It
   never instantiates a plugin, so a row that parses perfectly and then refuses
   to load passes it cleanly. A duplicate `dsh-skill-filesystem` provider name
   did exactly that: clean dump, fatal at load.
3. **Drive a real boot** — `dsh --profile headless --patch … "say only OK"` —
   and confirm it gets past plugin load. Success looks like reaching
   `MISSING_CREDENTIAL: llm-deepseek`, which is the credential wall below,
   not a plugin-tree error. This is the check that matters; #2 is a cheap
   pre-filter for it.
4. Re-execute dsh's matcher algorithm over `generated/hooks-merged.json`
   against dsh's tool catalog, asserting every group can fire. **This is a
   Python re-implementation of `matcher.ts`, i.e. a model of dsh, not dsh.** It
   is transcribed from source and graded against a control, but it is not the
   real engine. The totals vary with the tree (see the table above); the
   assertion that matters is **0 dead**, which `--check` enforces.

The OAuth seeder has a separate secret-free unit suite:

```sh
python3 integrations/dsh/test_seed_pi_ai_oauth.py
```

### Live hook verification

Verified on 2026-08-31 with API-key environment variables unset and the seeded
pi-ai Anthropic grant: a real `claude-opus-4-5` turn requested
`rm /private/tmp/dsh-hook-probe-do-not-create`. The session log recorded the
`PreToolUse` hook outcomes, dsh requested approval, and the tool returned
`requires approval, but no approval channel is available`; the command never
executed. This closes the previous bridge-firing gap.

## Known gaps

These are accepted and named, not oversights.

- **7 of Claude Code's 30 hook events** are supported by the bridge. Notably
  **`PreCompact` is not**, and is explicitly out of scope in dsh's own
  interception design — so a blocking pre-compaction hook cannot live here yet.
- **One `configPath`, read once at startup.** dsh performs no user/project scope
  merge and no live reload, which is why this generator merges both scopes into
  a single file.
- **`updatedInput` is not honoured** on `PreToolUse` (logged and warned). dsh
  seals tool arguments before policy runs, because history, audit and UI all
  read them.
- **Claude Code `if:` is not a bridge feature.** This host's four conditioned
  rows call the same consolidated gate, which already parses and filters the
  pending Bash command itself; the generator therefore collapses them to one
  self-filtering invocation. Any other conditioned handler is left intact and
  reported as a capability loss rather than silently widened.
- **`disable-model-invocation` is preserved.** dsh's filesystem loader parses
  the key, excludes those skills from model-facing catalogs/loaders, and keeps
  explicit `/name` invocation as their only entry point.
- **MCP resources and prompts are not bridged** — tools only.
- **Per-agent `hooks:` blocks have no dsh equivalent.** Two wardens
  (`code-explorer`, `general`) carry one; the generator reports each
  as a capability loss.
- **`MultiEdit` and `apply_patch` have no real counterpart.** They are widened
  onto `str_replace_editor`, which is reported on every run.

## Provenance of the numbers

The "28 dead handlers" and "84 skills parse" figures are **self-measured** by the
tooling in this directory, not independently warden-verified. The dsh behaviours
they rest on were read from source at `deepseek-ai/deepseek-harness@0a53fb55be`
(v0.1.2-alpha.2) and confirmed by execution where noted.
