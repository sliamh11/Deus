# Codegraph-First Exploration (opt-in module)

> Install this module only if the project is indexed for code-intelligence tools (a `codegraph`
> graph and/or a semantic `search_code` index). Without those tools, this rule does not apply.

## The three-stage protocol

When exploring code to answer "where is X" / "how does X work" / "what would changing X break":

1. **Semantic first** — `search_code` (or `codegraph_context`) to find candidate areas by meaning.
2. **Structural next** — `codegraph_callers` / `codegraph_callees` / `codegraph_impact` to map what
   connects to the candidates.
3. **Exact last** — `grep` / `Read` to confirm specific lines.

Skip stages only when the answer is already known.

## Why

A repo-wide `grep -r` or `find -name` as the *first* move repeats work the index already did, and
misses connections that text search can't follow (dynamic dispatch, callbacks). Semantic search
identifies the landscape; structural queries map the connections; exact search confirms specifics.

## Before modifying a function

Query the callers first. A change with 2 callers is not the same risk as a change with 50 — know
the blast radius before you touch it.
