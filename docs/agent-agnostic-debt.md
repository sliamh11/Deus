# Agent-Agnostic Technical Debt

This file tracks the remaining open-ended work between today's
backend-neutral foundation and a fully agent-agnostic Deus.

Rules:

- Every row needs a stable debt ID.
- Every row must name the affected surface and user-visible risk.
- Every row must have explicit exit criteria.
- If a change intentionally leaves parity or onboarding incomplete, update this
  file in the same diff.

## Active Debt

| ID | Surface | Current gap | User-visible risk | Exit criteria |
|---|---|---|---|---|
| `AAG-001` | Live backend parity | OpenAI/Codex parity is still mostly verified by unit and targeted integration tests, not rebuilt live containers with real credentials. | Backend swaps can still fail in real container startup, provider auth, or mounted-project flows despite passing local tests. | Rebuild the agent container, run both Claude and OpenAI backends end-to-end with real credentials, and document the verified parity checklist in the runtime ADR or a linked test record. |
| `AAG-002` | OpenAI/Codex adapter depth | OpenAI is opt-in and still lacks parity-certified handoffs/tracing and other provider-native session features. | Deus behavior is mostly aligned, but backend-specific debugging and advanced orchestration can still diverge. | Decide whether to adopt OpenAI Agents SDK features or keep the current Responses-based adapter; then document the chosen scope and verify the parity matrix for it. |
| `AAG-003` | Backend coverage | The runtime contract exists, but only `claude` and `openai` are implemented. | Deus is backend-neutral in architecture but not yet broadly backend-portable. | Add at least one more adapter or explicitly document the supported backend boundary as a deliberate product scope. |
| `AAG-004` | Compatibility migration | `CLAUDE.md` is still required as a compatibility surface while `AGENTS.md` is canonical. | Some interfaces still enter through legacy naming, so the onboarding story is canonical in source-of-truth terms but not fully single-surface in every runtime. | Migration policy changes and the repo no longer requires `CLAUDE.md` as a live compatibility file, or the compatibility mirror is formally frozen and documented as permanent. |
| `AAG-005` | Dynamic skill parity | OpenAI now bridges shared MCP surfaces, but live parity still depends on skills exposing their tools through the shared `deus` server or equivalent MCP bridges. | Backend changes can still expose or hide niche capabilities if a skill only works on one side of the shared tool plane. | Audit agent-side skills that add runtime capabilities and verify they are reachable through the shared backend-neutral tool plane. |
| `AAG-006` | Qodo discovery parity | `get-qodo-rules` now checks env vars and common config paths, but the actual Claude-side Qodo discovery mechanism on Liam's machine is still unverified from this runtime. | Claude may load Qodo rules successfully while Codex still cannot reproduce the same lookup until the real host-side source is identified and normalized. | Reproduce a successful Qodo rules load on both Claude and Codex, capture the exact source path/env chain used, and document one canonical discovery order that both runtimes follow. |

| `AAG-007` | User-facing backend setup | No `/add-openai` skill exists. Users must manually edit `.env` to configure a non-Claude backend. | Non-technical users cannot switch backends. No validation, no guided setup, no parity warnings during setup. | `/add-openai` skill that interactively asks for API key, writes `.env`, validates the connection, and warns about parity gaps. |
| ~~`AAG-008`~~ | ~~CLI backend management~~ | Resolved | -- | `deus backend [show\|set\|model\|list]` implemented in both `deus-cmd.sh` and `deus-cmd.ps1`. |
| `AAG-009` | `runTurn()` dispatch | Orchestrator and scheduler use the registry for name resolution but still call `runContainerAgent()` directly, bypassing `backend.runTurn()`. | No functional impact today (all backends are container-based), but blocks non-container backends. | Orchestrator and scheduler call `backend.runTurn()` instead of `runContainerAgent()`. |

## Recently Closed

| ID | Closed by | Resolution |
|---|---|---|
| `AAG-C001` | `feat/agent-backend-abstraction` worktree | `AGENTS.md` became the canonical onboarding source, `CLAUDE.md` was reduced to a compatibility mirror, and runtime-loaded `AGENT_DEUS_101.md` onboarding was removed from the always-load context path. |
| `AAG-C002` | `feat/agent-backend-abstraction` worktree | `deus backend [show\|set\|model\|list]` CLI command added to both `deus-cmd.sh` and `deus-cmd.ps1`. Persists to both `~/.config/deus/config.json` and `.env`. |
