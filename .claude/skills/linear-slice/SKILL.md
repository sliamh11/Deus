---
name: linear-slice
description: Decompose a plan into dependency-ordered Linear issues (tracer-bullet vertical slices) and release them into the autonomous dispatch pipeline.
user_invocable: true
---

# /linear-slice

Turn a plan or PRD into a set of **thin, vertical, independently-shippable slices**, each created
as a Linear issue in dependency order, then released into Deus's autonomous dispatch pipeline.

This is the missing front-end to the Linear pipeline: the webhook gates (`enrichment-gate` →
`bouncer-gate` → dispatcher) describe how an issue *flows through* the board, but not how a good,
independently-grabbable issue gets *created*. This skill creates them.

Creating issues is a deliberate action with real side effects. **Always preview and get explicit
approval before creating anything.**

## How the pipeline works (the contract you must honor)

The board is `Backlog → Todo → Ready for Agent → Agent Working → In Review → Done`. Gates fire on
**transitions into** a state, never on issue creation:

- **Backlog → Todo** fires the **enrichment gate**, a *mutating* controller that writes the scope
  block into the issue description itself (wrapped in `<!-- gate:enrichment-gate:start -->` …
  `<!-- gate:enrichment-gate:end -->`).
- **Todo → Ready for Agent** fires the **bouncer gate**, which validates that scope block.
- The dispatcher then picks up `Ready for Agent` issues autonomously.

Consequences for this skill:

- **Create issues in Backlog.** A freshly created issue fires no gate, so it sits inert until moved.
- **Never pre-write the enrichment scope block.** It is gate-owned. Write a normal human
  description; the enrichment gate generates the `## Scope` block on the Backlog→Todo transition.
- **Never apply a `Scoped` label.** The dispatcher matches `Scoped`; applying it to a non-enriched
  issue would make the dispatcher pick up unscoped work. The gate manages that label.
- **Releasing = moving Backlog → Todo.** That is how you start the pipeline on a slice.

## Steps

### 1. Get the plan

Take the plan from one of, in priority order:
- An explicit argument (a file path, or pasted text).
- The current conversation (a plan just drafted/approved here).
- A parent Linear issue (`mcp__linear__linear_getIssueById`) whose body is the plan to decompose.

If no plan is identifiable, ask the user for one. Do not invent scope.

### 2. Decompose into tracer-bullet vertical slices

Break the plan into the smallest set of **vertical slices**. Each slice must:
- Cut through every layer needed to be **independently demoable** (a thin end-to-end thread, not a
  horizontal layer like "all the types" or "the whole API").
- Deliver one tangible increment of value on its own.
- Put **prefactoring first** — if a slice needs a seam that doesn't exist yet, make the
  prefactor its own earlier slice.

**Use codegraph before estimating blast-radius** (a skill body inherits no exploration hook, so
this is on you): for each slice that touches existing code, run `codegraph_impact` /
`codegraph_callers` on the symbols it changes to size the real blast-radius and surface prefactor
opportunities. Do not guess from filenames. Fall back to `search_code` then grep only to confirm.

### 3. Build the dependency graph

Determine the `Blocked by` edges between slices (prefactors block the features that need them; a
shared scaffold blocks its consumers). Keep it a DAG. The **unblocked (root)** slices are the ones
that can start immediately.

### 4. Resolve Linear context

(If the plan came from a parent Linear issue in Step 1, you already used
`mcp__linear__linear_getIssueById` to read it.)

- `mcp__linear__linear_getTeams` → pick the team (single team `LIA` in this workspace).
- `mcp__linear__linear_getProjects` → confirm the target project with the user. **Every issue must
  be assigned to a project — never leave an issue floating** (orchestration rule).
- `mcp__linear__linear_getWorkflowStates` (with `teamId`) → resolve the **Backlog** and **Todo**
  state IDs. The call returns `[{id, name, type, position, …}]`. **Match by exact `.name`**
  (`"Backlog"`, `"Todo"`) — **never by `.type`**: there are two `type:"backlog"` states ("Icebox"
  and "Backlog"), so a type match is ambiguous and can land issues in Icebox. Resolve IDs at
  runtime; never hardcode them.

### 5. Preview and get approval

Present, and then **stop and wait** for explicit approval:
- A slice table: `# | Title | One-line scope | Blocked by | Effort (trivial/small/medium/large)`.
- The dependency graph (ASCII or mermaid).
- The target project, and which slices will be auto-released to Todo (the unblocked ones).

Do not create anything until the user approves. If they want changes, revise and re-preview.

### 6. Create the issues (on approval)

For each slice, `mcp__linear__linear_createIssue`:
- `teamId`, `projectId` (mandatory), `title` = `[Slice N] <concise name>`.
- `description` written with **actual newlines, never `\n` escape sequences** (MCP double-escapes
  them into literal `\\n`). Use these GENERAL sections (this is not a frontend skill — do not use
  design-to-dev's Wireframe/Design-Tokens/shadcn sections):

  ```markdown
  ## Overview
  What this slice delivers and why it is independently shippable.

  ## Acceptance criteria
  - Observable, checkable outcomes that mean this slice is done.

  ## Dependencies
  Blocked by: [Slice K] <name> (and the Linear relation below). "None" if a root slice.

  ## Definition of Done
  - Builds/tests pass; criteria met; reviewed.
  ```
- Land in **Backlog** (set `stateId` to the resolved Backlog id, or leave unset — Backlog is the
  default). Do **not** write the enrichment scope block. Do **not** add a `Scoped` label. **Omit
  `priority` and `estimate`** — let the enrichment gate or the user set them; do not guess.

### 7. Link the dependencies

For each `Blocked by` edge, `mcp__linear__linear_createIssueRelation` (type `blocks`/`blocked_by`)
between the two created issue IDs, so the board reflects the DAG.

### 8. Release the unblocked slices

For each **root (unblocked)** slice, `mcp__linear__linear_updateIssue` to set `stateId` → the
resolved **Todo** id. This fires the enrichment gate and starts the autonomous pipeline. Leave
blocked slices in Backlog. Do **not** auto-release subsequent waves: tell the user which slices are
now blocked and on what, and let them re-approve (or manually release) each later wave. A blocked
slice advancing without fresh approval would push unreviewed work into the pipeline.

### 9. Cap handling — never silent-partial

If `mcp__linear__linear_createIssue` fails with `exceeded the free issue limit` (Linear free-plan
cap), **STOP immediately**. Report exactly which slices were created (with IDs) and which were not,
and which relations/releases did or didn't happen. Offer to archive old issues and resume, or to
resume later. Never leave a half-created graph unreported.

A gate or API failure is an **error, not an approval** — never proceed as if a failed step
succeeded.

### 10. Summary

Report: created issue IDs + URLs, the dependency graph, and which slices were released to Todo
(now in the pipeline) vs left in Backlog.

## Notes

- Preview-first is mandatory; this skill mutates a shared system of record.
- A change to a slice's scope after creation must move that issue back per the orchestration rules
  (scope change → back to the relevant step so the gate re-evaluates).
- Companion to `/design-to-dev` (which creates issues from *design wireframes*); this is the
  general-plan analog.
