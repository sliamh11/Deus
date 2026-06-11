# Design Logs (opt-in module)

> Lightweight architecture decision records. Install this module when the project wants a durable
> trail of why non-trivial decisions were made.

## When to write one

Write a short design log when a change involves a decision that a future reader would otherwise
have to reverse-engineer:

- choosing one approach over viable alternatives (a library, a pattern, a storage layout)
- a trade-off with non-obvious consequences
- anything you'd want to explain in a PR review and not repeat in six months

Skip it for routine changes — bug fixes, renames, dependency bumps.

## Format

One markdown file per decision under `docs/decisions/` (create the directory if absent), named
`NNNN-short-title.md`:

```
# <Decision title>

- **Date:** <YYYY-MM-DD>
- **Status:** proposed | accepted | superseded by NNNN

## Context
What problem forced a decision? What constraints applied?

## Decision
What we chose, stated plainly.

## Alternatives considered
Each option and why it lost.

## Consequences
What this makes easy, what it makes harder, and how to reverse it.
```

## How it interacts with the gates

The `plan-reviewer`'s `prior-decisions` rule checks `docs/decisions/` for records that overlap a
new plan. Keeping this log current means the gate catches plans that silently contradict a past
decision — and an explicit superseding record is the sanctioned way to reverse one.
