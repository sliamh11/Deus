# CLI Mode Data Management: Research & Design

How Deus should handle memory, privacy, and data when operating in external project directories via CLI.

**Date:** 2026-03-31
**Status:** Implemented (merged 2026-03-31)
**Author:** Research collaboration (Liam + Claude)

---

## Table of Contents

1. [How Other Tools Handle This](#1-how-other-tools-handle-this)
2. [The Three-Tier Memory Model](#2-the-three-tier-memory-model)
3. [What Deus Should Remember vs Not](#3-what-deus-should-remember-vs-not)
4. [Privacy and Compliance Concerns](#4-privacy-and-compliance-concerns)
5. [Design: Project Onboarding + Memory Levels](#5-design-project-onboarding--memory-levels)
6. [Implementation Plan](#6-implementation-plan)

---

## 1. How Other Tools Handle This

### Claude Code (our foundation)

Claude Code already provides per-project memory isolation. When running in any directory:
- **Auto-memory** saved to `~/.claude/projects/<path-encoded>/memory/` — local, editable, project-scoped
- **CLAUDE.md** in the repo is loaded automatically — project instructions
- **Session transcripts** stored as `.jsonl` files in the project directory
- **Three scopes**: user-level (`~/.claude/CLAUDE.md`), project-level (repo root), folder-level (nested)
- **No cross-project leakage** — sessions from different projects never mix

**Key insight:** When `deus` runs in `~/projects/foo`, Claude Code automatically creates isolated memory at `~/.claude/projects/-Users-liam10play-projects-foo/memory/`. This native isolation is our starting point — Deus doesn't need to build a separate memory system.

### Cursor

- `.cursorrules` and `.cursor/rules/` for project-specific instructions (committed to repo)
- Native "Memories" feature was introduced mid-2025 then **removed in v2.1.x** — partly due to user concerns about uncontrolled accumulation
- No built-in cross-session memory; community uses "Memory Bank" markdown files or MCP-based external memory
- Privacy Mode available (disables telemetry, no server-side code storage)

### Windsurf (Codeium)

- Auto-generated memories created during conversations, stored at `~/.codeium/windsurf/memories/`
- **Workspace-scoped** — memories from one workspace are unavailable in another
- Memories Panel UI for viewing, editing, deleting
- Rules can be global or workspace-specific

### GitHub Copilot

- Custom instructions via `.github/copilot-instructions.md` (committed to repo)
- **Agentic Memory** (public preview) — auto-generates memories per repository
- **Memories auto-expire after 28 days** — time-bounded decay prevents accumulation
- Enterprise customers can disable features, control data retention, opt out of training

### Devin

- Cloud-hosted session state, playbooks, and knowledge
- Enterprise tier offers VPC deployment (data stays in customer-controlled environment)
- SOC 2 posture (details behind NDA)
- Per-workspace/team isolation

### aider

- Chat history (`.aider.chat.history.md`), input history, optional LLM log — all in project directory
- Configuration via `.aider.conf.yml` (project) or `~/.aider.conf.yml` (global)
- **Git commits as the primary persistence** — code changes tracked as commits, not memory
- Fully local, no vendor-hosted state, no telemetry by default

### Continue.dev

- `.continuerules` for project instructions
- No built-in cross-session memory yet; MCP-based memory is opt-in
- Open-source, runs locally, user chooses LLM provider

---

## 2. The Three-Tier Memory Model

The industry is converging on three tiers:

| Tier | Scope | Lifetime | What's in it |
|------|-------|----------|-------------|
| **Global** | All projects | Permanent | User identity, coding style, behavioral rules, tool preferences |
| **Project** | Single repo/workspace | Project lifetime | Architecture decisions, conventions, build commands, team info |
| **Session** | Single conversation | Ephemeral | Current task context, working memory, file contents |

Claude Code's existing system already maps to this:
- **Global**: `~/.claude/CLAUDE.md` (user-level instructions)
- **Project**: `<repo>/CLAUDE.md` + `~/.claude/projects/<path>/memory/` (auto-memory)
- **Session**: Conversation context (lost on exit unless compacted)

Deus adds a **fourth layer** on top: the Deus vault (user preferences, behavioral rules, session logs, semantic memory). This is cross-project knowledge about the USER, not about any specific project.

### Copilot's 28-Day TTL

GitHub Copilot's approach of auto-expiring memories after 28 days is notable. It prevents unbounded accumulation while keeping recent context fresh. Worth considering for Deus's project-level memory.

---

## 3. What Deus Should Remember vs Not

Based on the "team member" analogy: a new colleague naturally accumulates project understanding, but would never carry proprietary code verbatim from one client to another.

### Always Remember (Global Tier — Deus Vault)

These are about the USER, not the project:
- Coding style preferences ("functional over OOP", "always use feature branches")
- Tool preferences ("use pnpm not npm", "prefer vitest over jest")
- Communication style ("be concise", "no emojis")
- Behavioral rules ("always wait for approval before executing")
- Study/personal context (it's the same Deus everywhere)

### Remember Per-Project (Project Tier — Local)

These are about THIS project and stay isolated:
- Architecture decisions ("we chose Postgres because X")
- Team info ("Bob is the PM, Alice handles frontend")
- Client context ("fintech domain, regulatory constraints apply")
- What was tried and why ("migrated from REST to GraphQL on 2026-03-15")
- Build/test/deploy commands
- Project conventions

### Never Remember (Ephemeral Only)

These should NOT persist beyond the session:
- Specific code contents (the code IS the source of truth — read it fresh)
- File paths and line numbers (they change constantly)
- Debugging session details (stack traces, error logs)
- Specific variable names, function signatures (grep is better than memory)
- Credentials, tokens, API keys, environment variables

### The Gray Area

Some things could go either way depending on user preference:
- Session summaries ("today we refactored the auth module") — useful for continuity but could leak project details
- Code patterns and idioms specific to this project — useful but project-specific
- Bug reports and issue references — useful but tied to the project

---

## 4. Privacy and Compliance Concerns

### What Enterprises Worry About

- **Code leakage to training**: Most vendors promise "no training on your code" but this is policy, not technical guarantee
- **Cross-client contamination**: AI remembers Client A's architecture and surfaces it while working on Client B — compliance nightmare
- **Persistent artifact risk**: Memories, embeddings, indexes that persist after disengagement could be discoverable
- **SOC 2 / GDPR**: Require defined data retention policies, access controls, right to deletion, data minimization
- **Audit trail**: What does the AI "know" and when did it learn it?

### Deus-Specific Risks

1. **Vault leakage**: The Deus vault is injected into every session. If the vault contains details about Project A, those details are now in Project B's context. **Mitigation**: The vault contains USER preferences, not project details. Project-specific data stays in project-local memory.

2. **Session log leakage**: If `/compress` saves session summaries to the Deus vault, summaries from Client A's project could surface in Client B's session. **Mitigation**: Session logs from external projects should either (a) not be saved to the vault, or (b) be tagged with the project and filtered when loading context for a different project.

3. **Auto-memory cross-pollination**: Claude Code's auto-memory is already project-isolated. Deus doesn't break this — it only adds vault context (user-level) via system prompt. No risk here.

4. **Memory index pollution**: The Deus memory indexer (`scripts/memory_indexer.py`) indexes session logs for semantic search. If external project sessions are indexed, they could surface in unrelated contexts. **Mitigation**: Either don't index external project sessions, or tag them with the project path and filter at query time.

### The Key Realization

**Deus's vault is about the USER. Project memory is about the PROJECT. These are different systems with different lifetimes and isolation boundaries.**

As long as we maintain this separation, privacy is preserved:
- Vault (global): user identity, preferences, behavioral rules — safe to inject everywhere
- Project memory (local): architecture, decisions, team info — stays in `~/.claude/projects/<path>/`
- Session (ephemeral): code details, debugging — gone when session ends

The only bridge between them is: **should Deus save a session summary from an external project to the vault?** This is the user's choice.

---

## 5. Design: Project Onboarding + Memory Levels

### First-Run Questionnaire

When Deus detects it's running in a new external directory for the first time, it should run a brief onboarding:

```
Welcome to ~/projects/client-api! First time here.

Quick setup — tell me how to handle this project's data:

1. Memory level:
   [F] Full — Remember everything. Best for personal projects.
   [S] Standard — Remember decisions and architecture, skip code details.
       Best for team/work projects. (default)
   [R] Restricted — Nothing persists between sessions. 
       Best for NDA/client projects or one-off tasks.

2. Save session summaries to your Deus vault? (topic + decisions, never code)
   [Y] Yes  [N] No  (default: Yes for Full/Standard, No for Restricted)

You can change this anytime with /project-settings.
```

### Memory Levels Explained

| Level | Claude Auto-Memory | Session Summaries to Vault | Memory Indexer | When to Use |
|-------|-------------------|---------------------------|----------------|-------------|
| **Full** | Enabled (default Claude behavior) | Yes — full summaries | Indexed | Personal projects, open-source, your own repos |
| **Standard** | Enabled with guardrails | Yes — redacted summaries (decisions only, no code) | Indexed (summaries only) | Work projects, team repos, internal tools |
| **Restricted** | Disabled (`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`) | No | Not indexed | Client projects, NDA work, compliance-sensitive repos |

### How Each Level Works

**Full**: Identical to home mode. Claude Code auto-memory enabled, session summaries saved to vault, sessions indexed for semantic search. The project is treated as part of Deus's core memory. Best for repos the user owns or contributes to long-term.

**Standard** (default): Claude Code auto-memory is enabled but guided. A system prompt instruction tells Claude to remember only decisions, architecture, and team context — not code patterns or implementation details. Session summaries saved to vault but automatically redacted (topic + decisions only, no code references, no file paths, no function names). Indexed for semantic search but with the project tag, so they only surface when relevant.

**Restricted**: Claude Code auto-memory is disabled entirely (`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`). No session summaries saved to vault. No indexing. Each session starts completely fresh (except for Deus's global user context). The only persistence is what Claude Code stores natively in its session transcript (`.jsonl` files in `~/.claude/projects/`).

### Config Storage

Project settings stored at `~/.config/deus/projects/<path-hash>.json`:

```json
{
  "path": "/Users/liam/projects/client-api",
  "name": "client-api",
  "memory_level": "standard",
  "save_summaries": true,
  "created_at": "2026-03-31T10:00:00Z",
  "last_accessed": "2026-03-31T10:00:00Z"
}
```

This is outside the project directory (no pollution) and outside the Deus repo (no cross-user leakage).

### /project-settings Command

A slash command available in external project mode to adjust settings mid-session:
- `/project-settings` — show current settings
- `/project-settings memory full|standard|restricted` — change memory level
- `/project-settings summaries on|off` — toggle session summaries
- `/project-settings reset` — reset to defaults
- `/project-settings delete` — delete all Deus data for this project

### What Changes at Runtime

The `deus` CLI script reads the project config (or triggers onboarding) and adjusts:

1. **System prompt additions** based on memory level:
   - Full: no restrictions
   - Standard: "Remember architectural decisions and team context. Do not memorize specific code, file paths, or implementation details."
   - Restricted: "This is a restricted project. Do not save any project-specific information to memory."

2. **Environment variables**:
   - Restricted: `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`

3. **Post-session hooks** (if save_summaries is true):
   - On session end, offer to save a summary to vault
   - Standard: auto-redact code references before saving
   - Restricted: skip entirely

---

## 6. Implementation Plan

### Phase 1: First-Run Onboarding + Memory Levels (this commit)

1. **Project config system** (~50 lines, shell functions in `deus-cmd.sh`)
   - `~/.config/deus/projects/` directory
   - Read/write project config JSON
   - Path-based lookup (hash of absolute path as filename)

2. **First-run detection + questionnaire** (~30 lines in `deus-cmd.sh`)
   - Check if config exists for current directory
   - If not, run interactive onboarding
   - Save config

3. **Memory level enforcement** (~20 lines in `deus-cmd.sh`)
   - Read memory level from config
   - Set env vars (`CLAUDE_CODE_DISABLE_AUTO_MEMORY` for restricted)
   - Inject memory-level-specific instructions into system prompt

4. **Full context loading** (already implemented)
   - Load vault + sessions + semantic search (identical to home mode)
   - Add project-mode startup instruction

### Phase 2: /project-settings command (follow-up)

- Container skill that reads/writes the project config
- Available in external project mode sessions

### Phase 3: Session summary saving (follow-up)

- Pre-compact hook that extracts a summary
- Auto-redaction for standard mode (strip code references)
- Save to vault with project tag
- Memory indexer integration with project filtering

### What's NOT Needed

- **Separate memory system** — Claude Code's native auto-memory handles project-level persistence
- **Code indexing/embedding** — Claude Code reads files on demand; Deus doesn't need to duplicate this
- **Project-specific reflections/evolution** — The evolution loop is a messaging-channel feature; CLI mode doesn't need it yet

---

## References

### Tool Research
- [Claude Code Memory Docs](https://code.claude.com/docs/en/memory) — CLAUDE.md tiers, auto-memory, project isolation
- [Anatomy of the .claude/ Folder](https://blog.dailydoseofds.com/p/anatomy-of-the-claude-folder) — internal storage structure
- [Cursor Memory Across Conversations](https://www.blockchain-council.org/ai/cursor-ai-track-memory-across-conversations/) — memory feature added then removed
- [Windsurf Cascade Memories](https://docs.windsurf.com/windsurf/cascade/memories) — workspace-scoped memories
- [GitHub Copilot Agentic Memory](https://docs.github.com/en/copilot/concepts/agents/copilot-memory) — 28-day TTL
- [Aider Configuration](https://aider.chat/docs/config/aider_conf.html) — plain-text, git-native persistence
- [SOC 2 Ready AI Coding Tools](https://www.augmentcode.com/guides/7-soc-2-ready-ai-coding-tools-for-enterprise-security) — enterprise compliance
- [AI Coding Compliance Gap](https://pinklime.io/blog/ai-coding-compliance-gdpr-soc2-hipaa) — GDPR, HIPAA considerations
- [Top AI Memory Products 2026](https://medium.com/@bumurzaqov2/top-10-ai-memory-products-2026-09d7900b5ab1) — Mem0, Zep, Letta frameworks
