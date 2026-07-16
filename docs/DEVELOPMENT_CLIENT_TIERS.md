# Development Client Tiers

Deus distinguishes between the **product runtime** that executes Deus itself
and the **interactive development clients** contributors may use to work on
this repository. This document defines that boundary, explains how the
optional guardrails kit applies to each development client, and states what
each client does and does not guarantee.

## 1. Two-tier model

### Tier 1 — Deus Runtime (the product execution boundary)

Tier 1 is the Deus product runtime: the Deus-owned execution path that serves
real user traffic. Its centerpiece is `deus-native`, the Deus-owned
`AgentRuntime` (`src/agent-runtimes/deus-native-backend.ts`). Tier 1 owns:

- **Middleware-enforced permissions** — a declarative allow/deny rule
  evaluator with named profiles (`default`, `read-only`) at the outermost
  `wrapToolCall` layer. See
  [`deus-v2-permission-rules.md`](./decisions/deus-v2-permission-rules.md).
- **Checkpointing** of agent state.
- **Tool exposure** and the tool-surface allowlist.
- **Sessions** (backend-scoped; never resumed across backend mismatch).
- **Model selection** for product execution.

These guarantees apply to product execution through channels (WhatsApp,
Telegram, Slack, Discord, Gmail), isolated containers, the web UI, and
scheduled tasks. The product contract for all of this is defined in
[`AGENTS.md`](../AGENTS.md).

Warden review gates are **not** a Tier 1 product-runtime guarantee. The gate
*logic* (`scripts/codex_warden_hooks.py`) is backend-neutral, but its
*invocation* is coupled to a development CLI's hook engine — nothing in the
product runtime fires it. This distinction is recorded in
[`hook-dispatch-facade-correction.md`](./decisions/hook-dispatch-facade-correction.md):
enforcement is logic-decoupled but trigger-coupled, and that ADR is the
source of truth for the gap.

### Tier 2 — Interactive development clients (optional)

Tier 2 is the set of optional local clients a developer may use to edit this
repository: **Claude Code**, the **Codex CLI**, and **OpenCode**.

> "Codex" is overloaded in this repository. "OpenAI/Codex backend" means the
> Tier 1 product-runtime backend that runs in-container and can power channel
> messages; "Codex CLI" means the Tier 2 local development client used to
> edit the repository. The Codex CLI is not the OpenAI/Codex runtime backend
> and does not replace `deus-native`.

The qualified terms **OpenAI/Codex backend** and **Codex CLI** are used
throughout the rest of this document.

## 2. Clients, not runtime dependencies

Claude Code, the Codex CLI, and OpenCode are interchangeable developer-facing
clients that operate *on* the repository. None of them is a component the
Deus product runtime requires: Deus starts, routes channel messages, runs
containers, and executes scheduled tasks whether or not any Tier 2 client is
installed. A contributor can use any of them — or none — without affecting
what Deus ships or how it behaves in production.

Related but distinct topics live in their own guides:

- Runtime **backend selection** (which model provider powers a Deus group or
  task, including the OpenAI/Codex backend) is covered in
  [`MULTI_BACKEND.md`](./MULTI_BACKEND.md).
- **Editor/MCP composition** (using Deus's memory and tools from an editor)
  follows the "compose, don't port" model in
  [`EDITOR_INTEGRATION.md`](./EDITOR_INTEGRATION.md).

## 3. Guardrails kit: setup and supported enforcement

The development-process guardrails (plan-review, code-review, and
verification gates) are installed by the
[`add-guardrails` skill](../.claude/skills/add-guardrails/), which is the
setup source of truth. Its public workflow:

- **Default install** places the self-contained kit into the target repo:
  gate agents, rule books, a vendored gate script, a process-rules doc, and
  hook wiring merged into `.claude/settings.json`.
- **`--dry-run`** previews all planned actions before applying.
- **`--update`** refreshes kit-owned files that have drifted from the
  templates.
- Re-running is **idempotent**: existing settings and docs are merged, never
  replaced; an unchanged re-run is a no-op.
- Optional modules: **`design-logs`** (ADR-style decision records) and
  **`codegraph-first`** (semantic-search-before-grep exploration rule, for
  code-intelligence-indexed repos only).

The review contract the kit enforces is the same regardless of client:

- **Plan review** must pass before editing files.
- **Code review** and **verification** must pass before `git commit`.
- A **REVISE** verdict requires fixing and re-running the review until SHIP —
  it is never bypassed.
- Review markers reset per session.

Codex-CLI-specific installation, verification, and removal
(`codex_warden_hooks.py install` / `check` / `uninstall`) are documented in
the **Codex CLI Warden hooks** section of
[`MULTI_BACKEND.md`](./MULTI_BACKEND.md); this document does not repeat those
commands.

## 4. Guarantees and limitations by client

| Client | Enforcement path | Guarantee level | Known limitations |
|---|---|---|---|
| **Claude Code** | Native `.claude/settings.json` hook invocation | The kit's supported blocking path: gates fire on the CLI's own hook events | Gates exist only inside Claude Code sessions; they are not a product-runtime boundary (see [`hook-dispatch-facade-correction.md`](./decisions/hook-dispatch-facade-correction.md)) |
| **Codex CLI** | Bridged via `scripts/codex_warden_hooks.py`, which mirrors the Claude Code hooks as closely as Codex CLI hook events allow | Supported, but parity is not absolute | Detailed and evolving parity gaps are tracked in [`agent-agnostic-debt.md`](./agent-agnostic-debt.md) (see `AAG-010`) |
| **OpenCode** | Deny-only pre-tool blocking is possible in principle | **No Deus guardrail enforcement is currently supported or guaranteed** | Deus has no OpenCode adapter, no hook-mirroring script, and no `add-guardrails` wiring for it |

For every client, the trigger-coupling limitation applies: the gates run only
because that client's hook engine invokes them. See
[`hook-dispatch-facade-correction.md`](./decisions/hook-dispatch-facade-correction.md)
rather than any per-client gap list duplicated here.

## 5. Product-runtime boundary

Tier 2 clients are local development surfaces only. They do not execute Deus
channel traffic, replace `deus-native`, provide container isolation, or
become the runtime for the web UI or scheduled tasks.

Choosing a different Tier 2 client (or none) cannot alter the backend-neutral
experience contract in [`AGENTS.md`](../AGENTS.md): identity, memory, chat
commands, tool semantics, security boundaries, and scheduled-task behavior
are owned by the Tier 1 runtime and remain stable regardless of which
development client edited the code.
