---
name: to-tickets
description: Decompose a plan into dependency-ordered tracer-bullet tickets and publish them to whatever tracker this project actually uses (Linear, Asana, GitHub Issues, or local files) - releasing into the autonomous dispatch pipeline where one exists.
user_invocable: true
---

# /to-tickets

Turn a plan or PRD into a set of **thin, vertical, independently-shippable slices**, each published as a
ticket in dependency order, on whichever tracker this project is actually using.

This is the missing front-end to Deus's Linear pipeline when Linear is the tracker: the webhook gates
(`enrichment-gate` → `bouncer-gate` → dispatcher) describe how an issue *flows through* the board, but not
how a good, independently-grabbable issue gets *created*. This skill creates them — and, for trackers that
don't have that pipeline (everything except Linear today), creates tickets that are simply ready for a
human or agent to pick up manually.

Creating tickets is a deliberate action with real side effects. **Always preview and get explicit approval
before creating anything.**

## Step 0: resolve the tracker

Don't assume Linear. Different projects use different trackers — this repo alone may have several Linear
MCP servers connected at once (workspace-scoped names, not a single fixed `linear` prefix), and some
projects use Asana instead.

1. `ToolSearch` for tracker MCP tools actually available right now (e.g. query `"linear"`, `"asana"`,
   `"github issue"`).
2. If exactly one tracker family resolves, use it.
3. If more than one plausible tracker is available (multiple Linear servers, or Linear + Asana both
   connected), `AskUserQuestion` — never guess silently.
4. If none resolve, fall back to the **local-file** tracker (see below).

The rest of this skill is the same regardless of tracker — only Step 6 (publish) and the pipeline-release
behavior branch by tracker.

## Step 1: get the plan

Take the plan from one of, in priority order:
- An explicit argument (a file path, or pasted text).
- The current conversation (a plan just drafted/approved here).
- A parent tracker issue whose body is the plan to decompose.

If no plan is identifiable, ask the user for one. Do not invent scope.

## Step 2: explore the codebase (optional but recommended)

If you haven't already, explore the codebase to understand its current state. Ticket titles and
descriptions should use the project's domain glossary vocabulary, and respect ADRs in the area you're
touching. Look for prefactoring opportunities — "make the change easy, then make the easy change."

## Step 3: decompose into tracer-bullet vertical slices

Break the plan into the smallest set of **vertical slices**.

<vertical-slice-rules>

- Each slice cuts a narrow but COMPLETE path through every layer needed to be **independently
  demoable** — a thin end-to-end thread, not a horizontal layer like "all the types" or "the whole API."
- A completed slice delivers one tangible increment of value on its own.
- Each slice is sized to fit in a single fresh context window.
- Put **prefactoring first** — if a slice needs a seam that doesn't exist yet, make the prefactor its own
  earlier slice.

</vertical-slice-rules>

**Wide refactors are the exception to vertical slicing.** A **wide refactor** is one mechanical change —
rename a column, retype a shared symbol — whose **blast radius** fans across the whole codebase, so a
single edit breaks thousands of call sites at once and no vertical slice can land green. Don't force it
into a tracer bullet; sequence it as **expand–contract**. First expand: add the new form beside the old so
nothing breaks. Then migrate the call sites over in batches sized by blast radius (per package, per
directory), each batch its own ticket blocked by the expand, keeping CI green batch to batch because the
old form still exists. Finally contract: delete the old form once no caller remains, in a ticket blocked
by every migrate batch. When even the batches can't stay green alone, keep the sequence but let them share
an integration branch that all block a final integrate-and-verify ticket — green is promised only there.

**Use codegraph before estimating blast-radius** (a skill body inherits no exploration hook, so this is on
you): for each slice that touches existing code, run `codegraph_impact` / `codegraph_callers` on the
symbols it changes to size the real blast-radius and surface prefactor opportunities. Do not guess from
filenames. Fall back to `search_code` then grep only to confirm.

## Step 4: build the dependency graph

Determine the `Blocked by` edges between slices (prefactors block the features that need them; a shared
scaffold blocks its consumers). Keep it a DAG. The **unblocked (root)** slices are the ones that can start
immediately.

## Step 5: quiz the user

Present the proposed breakdown as a numbered list. For each ticket, show:

- **Title**: short descriptive name
- **What to build**: the end-to-end behaviour this ticket makes work, from the user's perspective — not a
  layer-by-layer implementation list
- **Blocked by**: which other tickets (if any) must complete first
- **Effort**: trivial/small/medium/large

Ask the user:
- Does the granularity feel right? (too coarse / too fine)
- Are the blocking edges correct — does each ticket only depend on tickets that genuinely gate it?
- Should any tickets be merged or split further?

Present the dependency graph (ASCII or a simple table) alongside the list, and the target project/tracker.

**Do not create anything until the user approves.** If they want changes, revise and re-preview.

## Step 6: publish the tickets (per-tracker)

Once approved, publish in dependency order (blockers first) so blocking edges can reference real
identifiers.

### Linear

Today's exact behavior — **the only branch that enters Deus's autonomous dispatch pipeline**:

- The board is `Backlog → Todo → Ready for Agent → Agent Working → In Review → Done`. Gates fire on
  **transitions into** a state, never on issue creation.
- **Create issues in Backlog.** A freshly created issue fires no gate, so it sits inert until moved.
- **Never pre-write the enrichment scope block.** It is gate-owned. Write a normal human description; the
  enrichment gate generates the `## Scope` block on the Backlog→Todo transition.
- **Never apply a `Scoped` label.** The dispatcher matches `Scoped`; applying it to a non-enriched issue
  would make the dispatcher pick up unscoped work. The gate manages that label.
- Resolve tracker context: `linear_getTeams` → pick the team. `linear_getProjects` → confirm the target
  project with the user (every issue must be assigned to a project — never leave an issue floating).
  `linear_getWorkflowStates` (with `teamId`) → resolve the **Backlog** and **Todo** state IDs, matching by
  exact `.name` (`"Backlog"`, `"Todo"`) — **never by `.type`**: there are two `type:"backlog"` states
  ("Icebox" and "Backlog"), so a type match is ambiguous and can land issues in Icebox. Resolve IDs at
  runtime; never hardcode them.
- Create each issue: `teamId`, `projectId` (mandatory), `title` = `[Slice N] <concise name>`, description
  with **actual newlines, never `\n` escape sequences** (MCP double-escapes them into literal `\\n`). Land
  in Backlog (default, or set `stateId` explicitly). Omit `priority`/`estimate` — let the gate or user set
  them.
- Link dependencies (`blocks`/`blocked_by`) between the created issue IDs.
- **Releasing = moving Backlog → Todo.** For each **root (unblocked)** slice, update `stateId` → the
  resolved Todo id. This fires the enrichment gate and starts the autonomous pipeline. Leave blocked
  slices in Backlog. Do **not** auto-release subsequent waves: tell the user which slices are now
  blocked and on what, and let them re-approve (or manually release) each later wave.
- **Cap handling — never silent-partial.** If issue creation fails with `exceeded the free issue limit`
  (Linear free-plan cap), **STOP immediately**. Report exactly which slices were created (with IDs) and
  which were not, and which relations/releases did or didn't happen. Offer to archive old issues and
  resume, or resume later. Never leave a half-created graph unreported. A gate or API failure is an
  **error, not an approval** — never proceed as if a failed step succeeded.

### Asana

Create tasks in the appropriate project/section, with dependency links where the Asana MCP supports them.
**No autonomous dispatch pipeline exists for Asana in this codebase** — a ticket created here is ready for
a human or agent to pick up manually, nothing auto-releases it. State this plainly in the summary so
nobody assumes Linear-parity.

### GitHub Issues

One labeled issue per ticket, created in dependency order. GitHub has no native blocking-relation API
without Projects v2, so express dependencies via a `## Blocked by` section in the issue body (reference by
issue number, or "None — can start immediately"). No pipeline-release claim here either.

### Local fallback (no tracker configured)

Write one file per ticket under `.scratch/<feature-slug>/issues/<NN>-<slug>.md`, numbered from `01` in
dependency order (blockers first):

<local-ticket-template>

# <NN> — <Ticket title>

**What to build:** the end-to-end behaviour this ticket makes work, from the user's perspective — not a
layer-by-layer implementation list.

**Blocked by:** the numbers/titles of the tickets that gate this one, or "None — can start immediately".

**Status:** ready-for-agent

- [ ] Acceptance criterion 1
- [ ] Acceptance criterion 2

</local-ticket-template>

In any tracker form, avoid specific file paths or code snippets in ticket bodies — they go stale fast.
Exception: if a prototype produced a snippet that encodes a decision more precisely than prose can (state
machine, reducer, schema, type shape), inline it and note briefly that it came from a prototype.

## Step 7: summary

Report: created issue IDs + URLs (or local file paths), the dependency graph, and — for Linear only —
which slices were released to Todo (now in the pipeline) vs left in Backlog. For every other tracker,
state plainly that nothing auto-releases; the tickets are simply ready.

## Notes

- Preview-first is mandatory; this skill mutates a shared system of record.
- A change to a slice's scope after creation must move that issue back per the orchestration rules (scope
  change → back to the relevant step so the gate re-evaluates) — applies to the Linear branch specifically,
  since that's the only one with gates to re-evaluate.
- Companion to `/design-to-dev` (which creates issues from *design wireframes*); this is the general-plan
  analog.
- Work the frontier one ticket at a time with `/implement`, clearing context between tickets.
