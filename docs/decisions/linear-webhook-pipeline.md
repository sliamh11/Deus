# ADR: Linear Webhook Pipeline -- Dispatch + Warden Gates

**Status:** Accepted
**Date:** 2026-05-22
**Scope:** `src/linear-dispatcher.ts`, `src/linear-webhook.ts`, `src/linear-gate-specs.ts`, `.claude/agents/wardens/`, `src/db.ts`
**Wardens consulted:** Architecture Snapshot, Plan Agent

## Context

Linear is used as the command center for autonomous agent work. Issues move through a Kanban board; when an issue reaches "Ready for Agent" the agent should pick it up without a human in the loop. Two problems arise immediately:

1. **Dispatch gap** - Linear has no native webhook for "move this issue to an agent." Something must poll and translate board state into agent runs.
2. **Quality gap** - Autonomous agents can produce empty output, mis-scope issues, or mark work done prematurely. Human reviewers waste time on bad handoffs. Warden gates are the fix: short-lived agent runs that evaluate each state transition and post a verdict before (or instead of) allowing the move.

This ADR covers both systems and how they share `LinearContext` and `executeAgentRun`.

## Decision

Run two concurrent subsystems off a single `LinearContext`:

- **Polling dispatcher** - polls Linear every 30 s (configurable), picks up "Ready for Agent" issues, dispatches them to the agent runtime, and advances the board.
- **Webhook gate server** - listens on `:3005` for Linear webhooks, runs a warden agent on every configured state transition, posts a rolling comment with the verdict, and optionally reverts bad transitions in strict mode.

Gate specs are markdown files in `.claude/agents/wardens/` with YAML frontmatter. Adding a gate requires only a new file; no code change is needed.

## Architecture

### Full Kanban Flow

```mermaid
flowchart LR
    BL[Backlog] --> TD[Todo]
    TD -- "gate: agent-readiness-gate\n(advise)" --> RFA[Ready for Agent]
    RFA -- "dispatcher poll" --> AW[Agent Working]
    AW -- "gate: output-quality-gate\n(advise)" --> IR[In Review]
    IR -- "gate: completion-gate\n(advise)" --> DN[Done]

    AW -- "agent error" --> BL
```

Gates fire on transitions into: **Ready for Agent**, **In Review**, **Done**. The transition into **Agent Working** is made by the dispatcher itself (bot actor) and is skipped by the webhook handler.

### End-to-End Request Flow

```mermaid
sequenceDiagram
    participant LUI as Linear UI / API
    participant WH as Webhook :3005
    participant DB as SQLite
    participant GA as Gate Agent
    participant LAPI as Linear API

    LUI->>WH: POST /webhook (state transition)
    WH->>WH: verify HMAC signature
    WH->>DB: INSERT OR IGNORE linear_webhook_events (dedup)
    WH->>WH: check warden:skip label
    WH->>WH: check allowedFrom (revert if illegal)
    WH->>DB: check cooldown (getLastCompletedGateRun)
    WH->>WH: check inFlightGate set
    WH->>DB: updateWebhookEventStatus → running
    WH->>GA: executeAgentRun(gateSpec prompt)
    GA-->>WH: text output (## Enrichment + ## Verdict)
    WH->>LAPI: updateIssue description (merge enrichment block)
    WH->>LAPI: createComment or updateComment (rolling verdict)
    alt strict mode AND verdict != SHIP
        WH->>LAPI: updateIssue stateId → fromState (revert)
    end
    WH->>DB: updateWebhookEventStatus → done / error
```

### Polling Dispatcher Flow

```mermaid
sequenceDiagram
    participant PD as Dispatcher (30 s timer)
    participant LAPI as Linear API
    participant ART as Agent Runtime
    participant DB as GroupQueue

    PD->>LAPI: issues(filter: state = Ready for Agent)
    Note over PD: sort by sortOrder ASC (column drag position)
    Note over PD: cap dispatch to availableSlots()
    loop each issue (up to available slots)
        PD->>LAPI: issue.labels() -- find agent:* or Scoped label
        PD->>DB: enqueueTask(chatJid, issueId, runIssue)
    end
    Note over ART: issue stays in "Ready for Agent" until run starts
    ART->>LAPI: updateIssue → Agent Working (best-effort)
    Note over ART: proceeds even if state update fails
    ART->>ART: executeAgentRun
    ART-->>PD: output text
    alt success
        PD->>LAPI: createComment(output)
        PD->>LAPI: updateIssue → In Review
    else error
        PD->>LAPI: createComment(error)
        PD->>LAPI: updateIssue → Backlog
    end
```

## Key Components

### LinearContext

Shared state object threaded through both subsystems:

| Field | Type | Purpose |
|---|---|---|
| `client` | `LinearClient` | Authenticated Linear SDK client |
| `stateByName` | `Map<string, WorkflowState>` | Name-keyed state lookup |
| `stateById` | `Map<string, WorkflowState>` | ID-keyed state lookup (webhook payloads carry IDs) |
| `botUserId` | `string` | Used to skip bot-triggered webhook events |
| `dispatchGroup` | `RegisteredGroup` | Virtual group folder for agent runs |
| `inFlightDispatch` | `Set<string>` | Issue IDs currently being dispatched (prevents double-dispatch) |
| `inFlightGate` | `Set<string>` | Issue IDs currently under gate evaluation (prevents concurrent gate runs per issue) |

`initLinearContext` discovers team ID and workflow states at startup. Missing required states (`Ready for Agent`, `Agent Working`, `In Review`, `Backlog`) are a `FatalError` -- the subsystem goes dormant rather than silently misbehave.

### executeAgentRun

Single function used by both the dispatcher and the webhook gate handler. Resolves the configured backend from `RuntimeRegistry`, runs a turn, and returns `{ text, error }`. Gate agents and dispatch agents share the same backend resolution path.

### Gate Specs

Each `.md` file in `.claude/agents/wardens/` with a `gate_to` frontmatter key is a gate spec. `loadGateSpecs` reads them at startup. The spec body is the full warden prompt injected into the agent run.

**Frontmatter schema:**

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | string | filename | Gate display name |
| `gate_to` | string | required | Workflow state that triggers this gate |
| `allowed_from` | string[] | `[]` | Legal source states (empty = any) |
| `mode` | `advise` \| `strict` | `advise` | Whether to revert on non-SHIP verdict |
| `fallback` | `SHIP` \| `REVISE` | `SHIP` | Verdict used when the agent errors |
| `cooldown_minutes` | number | 60 | Minimum minutes between gate runs per issue |
| `effort` | `low` \| `medium` \| `high` | unset | Passed to `RunContext.effort` |
| `fetch_comments` | boolean | `false` | Whether to include issue comments in prompt |

### Webhook Handler

`startLinearWebhookServer` binds to `0.0.0.0:3005`. Every inbound webhook is verified via Linear's HMAC signature before any processing. The handler filters to `action = update` events with a `stateId` change, then runs the gate pipeline.

**Health endpoint:** `GET /health` returns `{ status: "ok", gates: [...] }`.

### SQLite Event Log

Two tables in the shared `db.ts` database:

**`linear_webhook_events`** - one row per webhook event, keyed by `event_key = "{issueId}:{fromStateId}:{toStateId}:{webhookTimestamp}"`. Serves as the dedup key and the audit trail. Status lifecycle: `pending` -> `running` -> `done` / `error`.

**`linear_gate_comments`** - maps `(issue_id, gate_to)` to the comment ID of the last gate verdict comment. Enables rolling update: the same comment is edited on every re-run rather than a new one being created.

## Role Specs (Dispatcher)

Agent role specs live in `.claude/agents/*.md`. Any file with a `linear_label` frontmatter key is loaded as a role spec. The dispatcher matches the issue's `agent:*` label to the role spec, then builds the prompt:

```
<role>   -- role spec body
<issue>  -- title, identifier, description
<comments> -- issue comments (always fetched for dispatch)
```

## Enrichment Gates -- Living Document Pattern

Gate agents can emit an `## Enrichment` section above `## Verdict`. The webhook handler:

1. Extracts the enrichment body.
2. Calls `mergeEnrichment(currentDesc, gateName, body)`, which wraps it in HTML sentinel markers (`<!-- gate:{name}:start -->` / `<!-- gate:{name}:end -->`).
3. Patches the existing sentinel block in-place on re-runs, or appends if absent.
4. Writes the updated description back to Linear.

This means the issue description accumulates structured context from each gate as the issue progresses. The `completion-gate` reads the `<!-- gate:agent-readiness-gate:start -->` block to verify acceptance criteria -- gates downstream depend on enrichment written by gates upstream.

## Active Gate Specs

| Gate | Triggers On | Allowed From | Mode | Effort | Fetch Comments |
|---|---|---|---|---|---|
| `agent-readiness-gate` | Ready for Agent | Todo | advise | high | no |
| `output-quality-gate` | In Review | Agent Working | advise | medium | yes |
| `completion-gate` | Done | In Review | advise | medium | yes |

## Triggers and Loop-Break Mechanisms

**What causes a gate to fire:** a Linear webhook `Issue` event with `action = update` and a `stateId` change pointing to a state that has a registered gate spec.

**Loop-break mechanisms (evaluated in order):**

1. **Bot actor skip** - if `payload.actor.id === ctx.botUserId`, the event is skipped. This prevents the dispatcher's own state writes from triggering gates.
2. **`warden:skip` label** - if the issue carries this label, all gate evaluation is skipped for that event.
3. **Dedup** - `INSERT OR IGNORE` on `linear_webhook_events` using `event_key`. Duplicate webhook deliveries are silently dropped.
4. **`allowedFrom` check** - illegal transitions (e.g., Backlog directly to Done) are immediately reverted without running a gate agent.
5. **Cooldown** - if a gate ran for this issue within `cooldown_minutes`, the previous verdict is reused and no agent is spawned.
6. **`inFlightGate`** - if a gate is already running for this issue, the new event is dropped. One gate run per issue at a time.

## UX -- Advise vs Strict Mode

**Advise mode (default):** The gate runs, posts a verdict comment, and the transition stands regardless of verdict. Human is informed but not blocked.

**Strict mode:** On a non-SHIP verdict, the issue is reverted to its source state. The human must address the gate feedback before re-attempting the move.

Strict mode can be activated per-issue by adding the `warden:strict` label. This overrides the spec-level `mode` field at evaluation time:

```typescript
const effectiveMode = data.labels.some((l) => l.name === 'warden:strict')
  ? 'strict'
  : gateSpec.mode;
```

**Rolling comments:** The gate comment for each `(issue, gate_to)` pair is updated in-place on re-runs (via `linear_gate_comments` tracking). Re-running a gate does not spam the issue with new comments.

**Comment format:**

```
**Warden: {gateName}** - {SHIP|REVISE|BLOCK}

{agent output (verdict section only, enrichment stripped)}

---
*Gate: {gateName} | Mode: {advise|strict} | {ISO timestamp}*
```

## Configuration

**Required environment variables:**

| Variable | Description |
|---|---|
| `LINEAR_API_TOKEN` | Linear personal API token (also accepted as `LINEAR_API_KEY`) |
| `LINEAR_WEBHOOK_SECRET` | HMAC secret registered with the Linear webhook |

**Optional environment variables:**

| Variable | Default | Description |
|---|---|---|
| `LINEAR_POLL_INTERVAL_MS` | 30000 | How often the dispatcher polls |
| `LINEAR_TEAM_ID` | auto-discovered | Override if account has multiple teams |
| `LINEAR_WEBHOOK_PORT` | 3005 | Port the webhook server binds to |
| `LINEAR_BOT_USER_ID` | authenticated viewer ID | Override to match a dedicated bot account |

**Adding a gate:** create `.claude/agents/wardens/{name}.md` with `gate_to` frontmatter pointing to the target workflow state. Restart Deus. No code change required.

**Removing a gate:** delete the file and restart. The SQLite tables retain history but no new gate runs fire.

## Infrastructure

### Local Development (ngrok)

Linear webhooks require a publicly reachable URL. For local development:

```bash
ngrok http 3005
```

Register the resulting HTTPS URL in the Linear workspace webhook settings. Set `LINEAR_WEBHOOK_SECRET` to the value from Linear's webhook configuration panel.

### Webhook Registration

Linear webhooks must be configured in the workspace settings (Settings > API > Webhooks). Required event: `Issue`. The HMAC secret is set once; rotating it requires updating `LINEAR_WEBHOOK_SECRET` and restarting.

### Credential Proxy Dependency

Gate agents and dispatch agents run through the same agent runtime as all other Deus agents. They use the `linear-dispatch` group folder, which is a virtual group registered at startup. The agent runtime's credential proxy is required to be running -- Linear-sourced agent runs are not exempt from the proxy dependency.

## Consequences

- Linear becomes a first-class command interface for autonomous agent work without custom tooling beyond Deus itself.
- Gate specs are plain markdown files -- adding, tuning, or removing a gate requires no TypeScript changes.
- The living document pattern means each issue accumulates verifiable scope and completion records inline, auditable in Linear's native UI.
- Advise-mode gates give signal without blocking velocity; `warden:strict` escalates enforcement per-issue on demand.
- Cooldown and dedup mechanisms prevent runaway agent spend on flapping issues.
- The SQLite event log provides a complete audit trail of every gate evaluation without external infrastructure.
- ngrok (or equivalent tunnel) is a hard dependency for local development; production deployments need a stable public endpoint.
