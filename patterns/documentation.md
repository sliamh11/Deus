---
governs:
  - docs/
last_verified: "2026-05-02" # auto-bump
test_tasks:
  - "Add a new ADR to docs/decisions/ explaining an architectural change"
  - "Update ARCHITECTURE.md after a major refactor"
  - "Add a research doc under docs/research/ for a design investigation"
---
# Pattern: documentation

## ADR format

New architecture decision records go in `docs/decisions/` and must be listed in
`docs/decisions/INDEX.md`. Use the existing ADR format: title, status, context,
decision, consequences.

## Research docs

Exploratory write-ups go in `docs/research/`. These are living documents — mark
status clearly (`draft`, `active`, `archived`).

## Cross-references

When a doc references code paths, link to the source file, not line numbers
(lines shift). When a doc references another doc, use relative links.

## Keep docs current

If a PR changes behavior documented in `docs/`, update the relevant doc in the
same PR. Stale docs are worse than no docs.
