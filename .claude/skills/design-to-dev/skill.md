---
name: design-to-dev
description: >
  Orchestrate frontend implementation from design wireframes. Extracts tokens,
  creates Linear issues with full specs, drives phased parallel development
  in git worktrees, and validates output against the original design.
user_invocable: true
triggers:
  - design.?to.?dev
  - implement.+(design|wireframe|mockup|figma)
  - wireframe.+(implement|build|develop|ship)
requires:
  - mcp-linear (Linear MCP server for issue creation/management)
  - mcp-chrome (optional — falls back to manual screenshots if unavailable)
---

# /design-to-dev

Orchestrate frontend implementation from design wireframes. Takes wireframe
screenshots, extracts design tokens, creates Linear issues with full specs,
and drives phased parallel development in git worktrees.

**Patterns:** Mediator (wave orchestration — agents communicate via Linear
comments and git PRs, not with each other), State Machine (design/state.json),
Structural Isolation (visual judge never receives source code).

## Arguments

- `/design-to-dev <path1> <path2> ...` — wireframe screenshot paths
- `/design-to-dev resume` — resume from paused state (reads `design/state.json`)

---

## Phase 0 — Preflight

### Step 1: Detect project

Verify cwd is a git repository. If not, fail with:
"This skill must be run from within a git repository."

### Step 2: Detect stack

Scan `package.json` for React, Tailwind CSS, and shadcn/ui.

If any are missing:

```
AskUserQuestion:
  question: "The project is missing required dependencies. How should we proceed?"
  options:
    - label: "Scaffold missing deps in Wave 0"
      description: "The foundation wave will install and configure the missing dependencies before any component work begins."
    - label: "Abort"
      description: "Set up the project manually first, then re-run /design-to-dev."
```

On abort: exit. On scaffold: note missing deps for Wave 0 issue creation.

### Step 3: Accept wireframe files

If args contain file paths, use those. Otherwise:

```
AskUserQuestion:
  question: "Provide the paths to your wireframe/design screenshots."
  options:
    - label: "I'll paste paths now"
      description: "Enter file paths in the chat (space or newline separated)."
    - label: "They're already in design/wireframes/"
      description: "Skip — files are already in the project."
```

Copy wireframe files to `design/wireframes/` in the project root. Create the
directory if it doesn't exist. Verify each file exists and is a readable image.

Add `design/` to `.gitignore` if not already present. Design assets are
temporary build artifacts — they must be removed after implementation completes.

### Step 4: Linear project setup

Discover available teams via `mcp__linear__linear_getTeams`.

```
AskUserQuestion:
  question: "Which Linear project should issues go into?"
  options:
    - label: "Create new project"
      description: "Create a new Linear project named after this repo."
    - label: "Use existing project"
      description: "Select from existing projects."
```

On "Create new": use `mcp__linear__linear_createProject` with the repo name.
On "Use existing": list projects via `mcp__linear__linear_getProjects` and let
the user choose.

Store the project ID and team ID for issue creation.

---

## Phase 1 — Design Analysis

### Step 5: Extract design tokens

For each wireframe screenshot in `design/wireframes/`:

Read the image via the Read tool (multimodal). Analyze it to extract:
- Color palette: all hex values with semantic names (primary, secondary, muted,
  destructive, background, foreground, border, accent, sidebar colors)
- Typography: font family, weights used, sizes for each text level
  (h1-h4, body, caption, label, table cell)
- Spacing scale: padding, margin, and gap values observed
- Border radius values (cards, buttons, inputs, avatars)
- Shadow values
- Component states visible (hover highlights, active states, disabled elements)

Write the structured output to `design/tokens.json`:
```json
{
  "colors": { "primary": "#2563EB", "background": "#FFFFFF" },
  "typography": { "fontFamily": "Inter", "sizes": { "h1": "24px" } },
  "spacing": [4, 8, 12, 16, 24, 32, 48],
  "radius": { "sm": "4px", "md": "6px", "lg": "8px", "full": "9999px" },
  "shadows": { "sm": "0 1px 2px rgba(0,0,0,0.05)" }
}
```

### Step 6: Generate Tailwind theme

From `design/tokens.json`, generate a Tailwind v4 `@theme` CSS block and write
it to `design/tokens.css`. This file will be imported into the project's
`src/index.css` during Wave 0.

Annotate each token with its wireframe source as a CSS comment.

### Step 7: Component inventory

Analyze each wireframe to identify distinct UI regions. For each region, note:
- Candidate component name (e.g., "Sidebar", "UserTable", "StatCard")
- Which wireframe it appears in
- Whether it's shared across wireframes (shared = Wave 0) or page-specific
- Visible states (if data is shown, assume loading/empty/error states exist)
- Candidate shadcn/ui primitives it maps to (Button, Card, Table, Input, etc.)

This inventory drives issue creation in Phase 2.

---

## Phase 2 — Issue Breakdown

### Step 8: Create Linear issues

For each component/page identified in step 7, create a Linear issue via
`mcp__linear__linear_createIssue`. Use the team ID and project ID from step 4.

**Every issue MUST contain all of the following** (hard rule — no exceptions):

**Title format:** `[Wave N] Component/Page Name`

**Description** (markdown) must include these sections:

**## Overview** — What this component/page does and where it appears.

**## Wireframe Reference** — `design/wireframes/<filename>.png` with description
of which region of the wireframe this issue covers.

**## Design Tokens** — Every token this component uses, with both CSS variable
and Tailwind class name: `var(--color-primary)` / `bg-primary`.

**## Style Specification** — Exact layout, sizing, spacing, colors, typography
for every element. Reference design tokens, never raw values. Example:
`Container: flex flex-col gap-4 p-6 rounded-lg bg-card`.

**## Behavior & Interactions** — Click, hover, focus behavior for each
interactive element. Navigation targets. Form validation rules. Loading and
transition behaviors.

**## State Matrix** — Table with columns: State, Description, Mock Data Shape.
Required states for data components: loading, empty, error, single-item,
full-page, overflow. Required states for forms: pristine, validating,
field-errors, submit-loading, success, server-error. Static components:
default only.

**## shadcn/ui Components** — List the shadcn primitives to use.

**## Definition of Done**
- Builds clean (`npm run build` passes)
- Matches wireframe region (see Wireframe Reference)
- One Storybook story per state in the State Matrix
- Uses only design token utility classes (no raw hex/px values)
- Code review passes

### Step 9: Wave assignment

Assign each issue a wave number in the title `[Wave N]`:

- **Wave 0 (Foundation):** Layout shell (sidebar + top bar + content area),
  routing skeleton with placeholder routes, design token integration
  (`design/tokens.css` into `src/index.css`), shared types/interfaces, any
  missing dependency scaffolding from step 2.

- **Wave 1+:** Group issues so no two issues in the same wave touch the same
  files. Issues depending on components from a prior wave go in a later wave.
  Shared components before the pages that use them.

### Step 10: User approval gate (MANDATORY — never skip)

Print a summary table of all created issues:

```
| Wave | Issue ID | Title | States |
|------|----------|-------|--------|
| 0    | LIA-XXX  | Layout Shell | 1 |
| 1    | LIA-XXX  | Dashboard Stat Cards | 4 |
```

```
AskUserQuestion:
  question: "Issues created. Review them in Linear and confirm when ready."
  options:
    - label: "Proceed to development"
      description: "Issues are correct. Start Wave 0."
    - label: "I'm editing issues first"
      description: "Skill pauses. Run /design-to-dev resume when ready."
```

On "editing first": write state to `design/state.json` with status `paused`,
current wave `0`, all issue IDs per wave, Linear project ID. Exit.

---

## Phase 3 — Phased Development

*This phase repeats for each wave until all waves are complete.*

### Step 11: Pre-wave checks

If resuming (`/design-to-dev resume`), read `design/state.json` and re-fetch
all Linear issues for the current wave to pick up any user modifications.

Scan all issues in the current wave for `[BLOCKING]` comments via
`mcp__linear__linear_getComments`. If any found:

```
AskUserQuestion:
  question: "[BLOCKING] flag found on LIA-XXX: <comment text>"
  options:
    - label: "Halt wave"
      description: "Stop and investigate. Run /design-to-dev resume after resolving."
    - label: "Override and proceed"
      description: "Ignore the blocking flag and continue."
```

### Step 12: Dispatch subagents

For each issue in the current wave, dispatch a Sonnet subagent in a git
worktree. Launch ALL agents for the wave in a single message (parallel).

Each agent's prompt:

```
You are implementing a frontend component. Work in your git worktree.

## Your Issue
[full Linear issue description pasted here]

## Design System
[contents of design/SYSTEM_MANIFEST.md if it exists, otherwise design/tokens.json]

## Rules
- Use ONLY design token utility classes. Never raw hex colors or pixel values.
- Create one Storybook story per state in the State Matrix.
- Import shadcn/ui components from @/components/ui/.
- Follow existing code patterns in the project.
- Show commit message before committing.
- When done, create a PR against main.
- Post your result as a comment on the Linear issue:
  - Success: "SHIP: PR #XX created. <brief summary>"
  - Concern: "[WARNING]: <description>"
  - Blocker: "[BLOCKING]: <description — halts the wave>"
```

### Step 13: Wait and collect

Wait for all subagents to complete. Collect PR URLs from Linear comments.

### Step 14: Handle failures

For agents that failed (error, crash, timeout):
- Post `[WARNING]: re-queued from wave N due to agent failure` on the issue
- Move the issue to the next wave in `design/state.json`
- Do NOT block the wave — successful PRs proceed

### Step 15: Code review and merge

For each successful PR, dispatch a code-review subagent (Sonnet). All reviews
in parallel.

On SHIP verdict: merge the PR.
On REVISE: the review agent fixes issues and re-requests. Loop until SHIP.

### Step 16: Post-wave — manifest and coherence

After all PRs merge:

**Regenerate manifest:** Analyze merged main branch. Write
`design/SYSTEM_MANIFEST.md` (~800-1200 tokens):
- Component inventory: name, file path, props, slot interface
- Design token coverage: tokens used, any raw values detected
- Route map: routes and their components
- Storybook story count vs state matrix total

This manifest is the coherence sweep's data source — it defines what
"consistent" means so the sweep agent doesn't need to scan the full codebase.

**Coherence sweep:** Dispatch a Sonnet agent that:
1. Reads `design/SYSTEM_MANIFEST.md`
2. Detects dev server command from `package.json` scripts
3. Starts the dev server
4. Screenshots every route at desktop viewport (1440px) via chrome tools
   (if chrome unavailable, ask user for screenshots via AskUserQuestion)
5. Evaluates cross-page consistency: spacing rhythm, typography hierarchy,
   color usage, interactive patterns, navigation continuity
6. Outputs a coherence report

If coherence issues found: create fix-up issues for the current wave, merge
fixes, re-run coherence until clean.

### Step 17: Wave transition

On clean coherence:
1. Run `/handoff` for the wave — captures what was built, deviations, warnings
2. Update `design/state.json`: advance `current_wave`, status `running`
3. Return to step 11 for the next wave

If no more waves remain, proceed to Phase 4.

---

## Phase 4 — Visual Quality Gate

### Step 18: Start dev server

Detect dev server command from `package.json` `scripts.dev` field. Start in
the background. Wait for it to be reachable.

### Step 19: Adversarial visual judge

Dispatch a Sonnet visual-judge agent. This agent is STRUCTURALLY ISOLATED:

**Receives ONLY:**
- Wireframe screenshot paths from `design/wireframes/`
- Live app screenshots via chrome tools (fallback: ask user for screenshots)
- `design/tokens.json` for reference values

**NEVER receives:**
- Source code paths or file contents
- Component names or implementation details

**Layer 1 — Deterministic:**
- Navigate to each route
- Run `getComputedStyle` on key elements via chrome JavaScript tool
- Assert CSS values match design tokens (colors, font sizes, spacing)
- Check heading hierarchy via accessibility tree
- Count Storybook stories vs state matrix rows

**Layer 2 — Vision:**
- For each page: view wireframe and screenshot at same viewport
- Evaluate: layout fidelity, spacing, colors, typography, element positioning
- Output per-page: SHIP or REVISE with specific region callouts

### Step 20: Fix loop

On REVISE:
1. Create fix-up Linear issues per delta, assigned to next wave
2. Re-enter Phase 3 (step 11) for that wave
3. After fixes merge, re-run Phase 4 (step 18)
4. Repeat until judge returns SHIP for all pages

On SHIP: proceed to Phase 5.

---

## Phase 5 — Cleanup

### Step 21: Remove design assets

```
AskUserQuestion:
  question: "Implementation complete and visually verified. Remove design assets?"
  options:
    - label: "Remove design/ folder"
      description: "Wireframes and tokens served their purpose. Clean up."
    - label: "Keep design/ folder"
      description: "Preserve for future reference."
```

On remove: delete `design/` directory, remove `.gitignore` entry if added by
this skill, run `git worktree prune`. Commit the cleanup.

---

## State Machine

`design/state.json` (gitignored, project-scoped):

```json
{
  "status": "running",
  "current_wave": 1,
  "total_waves": 4,
  "linear_project_id": "...",
  "linear_team_id": "...",
  "waves": {
    "0": { "issues": ["LIA-101", "LIA-102"], "status": "complete" },
    "1": { "issues": ["LIA-103", "LIA-104"], "status": "running" },
    "2": { "issues": ["LIA-106", "LIA-107"], "status": "pending" }
  }
}
```

**Valid transitions:**
`preflight → analyzing → issues_created → paused | wave_N → visual_gate → complete`

**Invalid** (skill rejects):
- Skipping waves (`paused → wave_2` when wave_1 incomplete)
- Resuming from `complete` (terminal state)
