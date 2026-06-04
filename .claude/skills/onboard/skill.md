---
name: onboard
description: Onboard the current project into Deus code intelligence — index it for codegraph + code_search and register it so memory and search work here.
user_invocable: true
---

# /onboard

Make Deus's code-intelligence engines work in the **current project**. This
indexes the repo with both engines and registers it with Deus:

- **codegraph** — a per-repo `.codegraph/` knowledge graph (symbols, callers,
  callees, impact). Powers the `codegraph_*` MCP tools.
- **code_search** — a per-project semantic index under
  `~/.config/deus/projects/<id>/code_search.db` (sqlite-vec + Ollama). Powers
  the `search_code` MCP tool. Per-project, so onboarding one project never
  clobbers another's index.
- **registration** — writes `~/.config/deus/projects/<id>.json` so the project
  appears in Deus with its own memory settings.

## How to run it

This is a thin wrapper over the `deus init` CLI. Run it in the project you want
to onboard:

```bash
deus init
```

`onboard` is an alias — `deus onboard` is identical. Onboard a different
directory by passing a path: `deus init /path/to/project`.

After it finishes, relay the summary to the user: which engines indexed, the
DB location, and whether the project was newly registered or already known
(existing memory settings are preserved on re-run, never overwritten).

## Safety gate

`deus init` refuses to index unless the target is a git repository, and refuses
git repos with more than 5000 tracked files. This guards against accidentally
indexing a home directory or an unbounded tree. If the user genuinely wants to
onboard a non-git folder or a very large repo, re-run with `--force`:

```bash
deus init --force
```

Onboarding a non-git folder with `--force` drops a small `.deus/` marker in it
so its index stays isolated (not merged into the shared legacy DB).

## Idempotent

Re-running `deus init` in an already-onboarded project just refreshes the
indexes (codegraph `sync`, code_search reindex) and updates the last-accessed
timestamp. It never overwrites the project's memory settings.

## Notes

- macOS/Linux only (bash + git + codegraph), consistent with the rest of the
  `deus` CLI.
- If `codegraph` or the code_search prerequisites (python3 + Ollama) are
  missing, that engine is skipped with a warning — onboarding still completes
  for whatever is available. Run `/setup` to register the code-intelligence
  MCP servers if they are missing.
