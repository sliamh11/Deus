# Solution Atom Schema

Solution atoms capture operational learnings — bug fixes, dead ends, discovered
patterns — so future agent runs benefit from past experience. They follow the
existing memory atom format (YAML frontmatter + markdown body) and live in the
vault's `solutions/` directory.

## Frontmatter

```yaml
---
id: <UUID>
type: solution                    # atom type identifier
title: "<short description>"
tags: [module, category, ...]
problem_type: bug | knowledge | pattern
module: <affected module path>    # optional
severity: low | medium | high
updated: <YYYY-MM-DD>
---
```

## Body — Bug / Pattern type

```markdown
## Symptoms
What the user/agent observed.

## What Didn't Work
Dead ends — often the most valuable section. What was tried
that turned out to be wrong and why.

## Solution
What actually fixed the problem.

## Prevention
How to avoid hitting this in the future.
```

## Body — Knowledge type

For learnings that are not bug fixes but operational knowledge:

```markdown
## Context
When this knowledge applies.

## Guidance
What to do — the actionable instruction.

## When to Apply
Triggers or conditions that should prompt recall of this knowledge.
```

## Storage

- **Location**: `<vault>/solutions/` directory
- **Filename**: `<slugified-title>-<id-prefix>.md`
- **Append-only**: once written, never deleted or overwritten
- **Encoding**: UTF-8 markdown with YAML frontmatter

## Discovery

Solutions are discoverable through:

1. **Text/tag search**: `searchSolutions(query, tags?)` — substring matching
2. **Context injection**: The container context registry loads the 3 most
   recent solutions and injects them into agent context
3. **CLI**: `deus solution list|search|add` subcommands

### Memory Indexer Compatibility

Solution files use the same markdown-with-frontmatter format as session logs
and atoms. However, the memory indexer's `--add-dir` walks `.md` files
under the given directory, so solutions can be indexed via:

```bash
python3 scripts/memory_indexer.py --add <vault>/solutions/my-fix.md
```

**Note**: The indexer's `--rebuild` command does not automatically walk the
`solutions/` directory (it walks `Session-Logs/` and `Atoms/`). A future PR
should add `solutions/` to the rebuild walk list so solutions are included in
full re-indexes. The memory tree will surface solutions once they are indexed.

## Example

```yaml
---
id: 3f8a1b2c-4d5e-6f7a-8b9c-0d1e2f3a4b5c
type: solution
title: "OAuth token frozen in .env causes login loop"
tags: [auth, oauth, credentials]
problem_type: bug
module: src/credential-proxy.ts
severity: high
updated: 2026-05-05
---

## Symptoms
Container agent enters a login loop after ~1 hour. The OAuth access token
in the environment is stale but the CLI keeps using it instead of refreshing
from credentials.json.

## What Didn't Work
- Increasing IDLE_TIMEOUT — unrelated to the token lifecycle
- Clearing .env and restarting — worked temporarily but recurred
- Setting DEUS_PROXY_AUTH=0 — masked the symptom, broke auth entirely

## Solution
The `deus auth` command was writing the access token to `.env`, freezing it.
The credential proxy's `getDynamicOAuthToken()` reads credentials.json with a
5-minute cache and auto-refreshes. Removed the `.env` write from the auth
command so the proxy always reads the live token.

## Prevention
Never write OAuth tokens to .env or export them as env vars. The credential
proxy reads credentials.json directly — env vars freeze the token and bypass
the refresh mechanism.
```
