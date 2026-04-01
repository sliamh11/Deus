# External Environment Mode: Research & Design

How Deus can work on external projects beyond its own `~/deus` folder.

**Date:** 2026-03-31
**Status:** Implemented (merged 2026-03-31)
**Author:** Research collaboration (Liam + Claude)

---

## Table of Contents

1. [Current State Analysis](#1-current-state-analysis)
2. [Approaches to External Project Support](#2-approaches-to-external-project-support)
3. [Key Technical Challenges](#3-key-technical-challenges)
4. [What Other Tools Do](#4-what-other-tools-do)
5. [Recommended Approach](#5-recommended-approach)
6. [Non-Programmer Environments](#6-non-programmer-environments)

---

## 1. Current State Analysis

### How Deus Currently Handles Working Directories

The container working directory is always `/workspace/group`, which maps to `groups/<folder_name>/` on the host. The agent reads and writes files here -- conversation archives, notes, user-created files all live in this group folder.

The project root (`~/deus`) is mounted read-only at `/workspace/project` for the **main group only**. Non-main groups get no access to the Deus codebase at all -- they only see their own group folder and `groups/global/` (read-only).

Key code paths:

- `src/container-runner.ts:buildVolumeMounts()` -- builds the mount list per group
- `src/config.ts` -- `GROUPS_DIR = path.resolve(PROJECT_ROOT, 'groups')` -- groups are always relative to CWD
- `src/group-folder.ts` -- validates and resolves group folder names (alphanumeric only, within `groups/`)
- `container/Dockerfile` -- creates `/workspace/group`, `/workspace/global`, `/workspace/extra`, `/workspace/ipc`

### What Gets Mounted Into Containers and Why

| Mount | Container Path | Access | Purpose |
|-------|---------------|--------|---------|
| Project root (main only) | `/workspace/project` | Read-only | Self-modification capability (main group can read its own codebase) |
| `.env` shadow (main only) | `/workspace/project/.env` | `/dev/null` | Prevents agent from reading secrets |
| Group folder | `/workspace/group` | Read-write | Working directory, conversation archives, user files |
| Global folder (non-main) | `/workspace/global` | Read-only | Shared `CLAUDE.md` persona/instructions |
| `.claude/` sessions | `/home/node/.claude` | Read-write | Claude SDK session state, skills, settings |
| IPC directory | `/workspace/ipc` | Read-write | Message exchange, task snapshots |
| Agent runner source | `/app/src` | Read-only | Per-group customizable agent code |
| Additional mounts | `/workspace/extra/<name>` | Configurable | User-specified directories via mount allowlist |

### Assumptions About ~/deus Being the Working Directory

1. **`process.cwd()` is the project root** -- `config.ts` derives `GROUPS_DIR`, `DATA_DIR`, `STORE_DIR` all from `process.cwd()`, which is expected to be the Deus install directory.
2. **Groups live inside the project** -- `groups/` is a subdirectory of the project root. Group folders are restricted to simple names within this directory.
3. **Container image is built locally** -- `container/build.sh` and the Dockerfile assume they are run from the Deus project root.
4. **Skills are synced from `container/skills/`** -- hard-coded path relative to `process.cwd()`.
5. **Evolution CLI lives at `evolution/cli.py`** -- hard-coded relative to `process.cwd()`.
6. **Presets live at `presets/`** -- hard-coded relative to `process.cwd()`.

### How Group-Specific CLAUDE.md Files Work

Each group gets a `groups/<name>/CLAUDE.md` that persists across sessions. The Claude Agent SDK automatically loads `CLAUDE.md` from the working directory (`/workspace/group`). For non-main groups, `groups/global/CLAUDE.md` is injected as a system prompt append (not via CLAUDE.md discovery -- it is read by the agent runner and passed as `systemPrompt.append`).

The SDK setting `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1` is enabled, which means any directory listed in `additionalDirectories` also has its `CLAUDE.md` loaded. This is already used for `/workspace/extra/*` mounts.

### Existing Mechanism for External Paths

The `additionalMounts` system already exists and is the closest thing to external project support:

```typescript
// In RegisteredGroup.containerConfig
additionalMounts?: AdditionalMount[];

// AdditionalMount
{
  hostPath: string;       // Absolute path on host (supports ~)
  containerPath?: string; // Defaults to basename, mounted at /workspace/extra/{value}
  readonly?: boolean;     // Default: true
}
```

These mounts are validated by `mount-security.ts` against `~/.config/deus/mount-allowlist.json`:

```json
{
  "allowedRoots": [
    { "path": "~/projects", "allowReadWrite": true, "description": "Dev projects" }
  ],
  "blockedPatterns": ["password", "secret", "token"],
  "nonMainReadOnly": true
}
```

The agent runner already discovers extra mounts and passes them to the SDK:

```typescript
// container/agent-runner/src/index.ts
const extraDirs: string[] = [];
const extraBase = '/workspace/extra';
if (fs.existsSync(extraBase)) {
  for (const entry of fs.readdirSync(extraBase)) {
    const fullPath = path.join(extraBase, entry);
    if (fs.statSync(fullPath).isDirectory()) {
      extraDirs.push(fullPath);
    }
  }
}
// Passed to query() as additionalDirectories -- SDK loads their CLAUDE.md files
```

**Conclusion:** The infrastructure for mounting external directories is already built. What is missing is (a) a user-facing way to register projects, (b) making the external project the *primary* workspace rather than an ancillary mount, and (c) project-type detection and context injection.

---

## 2. Approaches to External Project Support

### Approach A: Project Registration + Container Mount Override

**Concept:** User registers a project path. When the user talks about that project (or from a project-specific group), the container mounts that path as the primary workspace instead of the default group folder.

#### How It Works

1. User registers: `@Deus project add ~/client-repo` or via main group
2. Deus stores project config in SQLite (path, name, type, read-write flag)
3. When a group is associated with a project, `buildVolumeMounts()` changes:
   - External project mounts at `/workspace/project` (read-write or read-only)
   - Group folder still mounts at `/workspace/group` (for Deus-specific files, conversation archives)
   - Agent `cwd` set to `/workspace/project` instead of `/workspace/group`
4. Project's own `CLAUDE.md` is automatically loaded by the SDK (since cwd is the project)
5. Deus injects a project-context system prompt with detected project type, conventions, etc.

#### Implementation Changes

| File | Change |
|------|--------|
| `src/types.ts` | Add `ProjectConfig` type, add `projectId?` to `RegisteredGroup` |
| `src/db.ts` | Add `projects` table (id, name, path, type, created_at, config) |
| `src/container-runner.ts` | Modify `buildVolumeMounts()` to handle project-associated groups |
| `container/agent-runner/src/index.ts` | Set `cwd` to `/workspace/project` when project mount exists |
| `src/mount-security.ts` | Reuse existing validation (already supports this) |
| `container/Dockerfile` | Add `/workspace/project` directory (already exists for main) |
| New: `src/project-registry.ts` | Project CRUD, type detection, config management |
| New: IPC command | `register_project` command for container-initiated registration |

#### Detailed Analysis

**Implementation complexity:** Medium. Most infrastructure exists (mount system, mount security, additional directories). Main work is the project registry, modifying `buildVolumeMounts`, and adding project-aware cwd switching in the agent runner. Estimated ~300-500 lines of new code.

**User experience:** Natural for messaging-first users. "Register a project, talk about it." Works well from WhatsApp/Telegram -- "hey Deus, look at the tests in client-repo and fix the failing ones." The agent knows which project you mean because the group is associated with it.

**Security:** Leverages the existing mount-allowlist system. Projects must be under an allowed root. Read-write access controlled per-root. The `.env` shadow pattern can be extended to shadow sensitive files in external projects too. Risk: if the user grants read-write access to a project root and the agent misbehaves, it can corrupt the project. Mitigation: git integration (always work on branches, never force-push to main).

**Memory/context management:** Per-project memory lives in the group folder (separate from the project itself). The project's own CLAUDE.md is the project's instructions; the group's CLAUDE.md stores Deus-specific memory about working with that project. Clean separation.

**Evolution loop:** Works naturally -- interactions are already tagged by group. Project-associated groups get domain detection from the project type. Reflections are per-group (and therefore per-project when 1:1 mapping).

**Scalability:** Each project gets its own group, so multiple projects work concurrently (one container per group, existing queue system). No new concurrency challenges.

**Channel compatibility:** Works perfectly -- any channel can send messages to a project-associated group. WhatsApp user asks about code, Telegram user asks about code, both work.

---

### Approach B: MCP Workspace Server

**Concept:** Deus exposes an MCP server that external tools (IDEs, CLI tools) connect to. The MCP server provides Deus's memory, preferences, and evolution capabilities as tools that any MCP-compatible client can call.

#### How It Works

1. Deus runs an MCP server (stdio or SSE transport) alongside its messaging channels
2. Claude Code (or any MCP client) in any directory connects to this server
3. MCP tools exposed:
   - `deus_memory_query` -- semantic search over all Deus memory
   - `deus_memory_store` -- save something to memory
   - `deus_preferences` -- retrieve user preferences/persona
   - `deus_evolution_reflect` -- get reflections for a prompt
   - `deus_evolution_log` -- log an interaction for scoring
   - `deus_schedule` -- schedule a task
   - `deus_notify` -- send a message to a channel
4. User runs `claude` in `~/client-repo`, with Deus MCP server configured
5. Claude Code has full codebase context (its native capability) + Deus memory/preferences via MCP

#### Implementation Changes

| File | Change |
|------|--------|
| New: `src/mcp-server.ts` | MCP server implementation (stdio + SSE) |
| New: `src/mcp-tools.ts` | Tool definitions for memory, evolution, preferences |
| `src/index.ts` | Start MCP server alongside channels |
| `scripts/memory_indexer.py` | May need HTTP API wrapper for MCP server to call |
| `evolution/cli.py` | Already has CLI interface, MCP server shells out to it |

#### Detailed Analysis

**Implementation complexity:** Medium-High. MCP server implementation itself is straightforward (Anthropic provides SDKs), but bridging to the memory indexer (Python) and evolution system requires careful IPC design. The MCP server needs to be a long-running process within the Deus host.

**User experience:** Requires the user to configure MCP in their IDE/tool. Two-step: (1) start Deus, (2) configure MCP client. For Claude Code users, this means adding to `.claude/settings.json`. The user gets Deus's memory everywhere but loses the messaging-first simplicity. They must use Claude Code (or another MCP client) directly -- cannot ask via WhatsApp.

**Security:** MCP server runs on localhost. No external directories are mounted -- the IDE handles filesystem access. Lower attack surface than mount-based approaches. However, the MCP server has access to all of Deus's memory, which could leak information between projects if not carefully scoped.

**Memory/context management:** Memory is global by default. To scope per-project, the MCP tools would need a `project_id` parameter, adding complexity. Cross-project learning is natural (which can be a feature or a bug).

**Evolution loop:** Requires the MCP client to call `deus_evolution_log` explicitly. Claude Code won't do this automatically -- it would need to be a hook or the user would need to configure it. Friction.

**Scalability:** Excellent -- the MCP server is stateless per-request. Multiple IDEs/tools can connect simultaneously. No container overhead per project.

**Channel compatibility:** Poor for this specific use case. WhatsApp/Telegram users cannot benefit from the MCP server -- they still go through the container path. This approach only serves IDE users.

---

### Approach C: Claude Code Extension / Hook Integration

**Concept:** Instead of running a separate system, Deus integrates into Claude Code's hook system. Users install Deus as a set of hooks and settings that enhance Claude Code in any directory.

#### How It Works

1. User installs Deus hooks globally: `~/.claude/settings.json` gets hook definitions
2. Hooks fire on Claude Code events:
   - `PreToolUse` -- inject Deus memory/preferences context
   - `PostToolUse` -- log tool usage for evolution
   - `Stop` -- save session to Deus memory vault
   - `PreCompact` -- archive conversation before compaction
3. Deus runs as a background daemon that hooks communicate with via HTTP/IPC
4. Claude Code operates normally in any directory, but every session benefits from Deus's memory layer

#### Implementation Changes

| File | Change |
|------|--------|
| New: `src/daemon.ts` | HTTP daemon for hook communication |
| New: `hooks/pre-tool-use.sh` | Shell script hook that calls daemon |
| New: `hooks/stop.sh` | Session save hook |
| New: `hooks/post-tool-use.sh` | Evolution logging hook |
| New: `scripts/install-hooks.sh` | Installer that configures `~/.claude/settings.json` |
| Existing memory/evolution systems | Exposed via daemon HTTP API |

#### Detailed Analysis

**Implementation complexity:** High. Claude Code's hook system is designed for simple shell scripts, not complex integrations. Each hook invocation is a separate process -- maintaining state between hooks requires a daemon. The daemon needs to handle concurrent requests from multiple Claude Code sessions. Hook latency directly impacts Claude Code's response time.

**User experience:** Seamless once installed -- Claude Code "just works better" in every project. No separate tool to run. However, it is tightly coupled to Claude Code specifically. If the user switches to Cursor or another tool, none of this works.

**Security:** Hooks run on the host with user permissions. No container isolation. The daemon has access to everything the user can access. This is actually simpler security-wise (no mount concerns), but loses the sandboxing benefit.

**Memory/context management:** Excellent -- hooks fire in the context of whatever project Claude Code is working on. Project detection is natural (read `package.json`, `Cargo.toml`, etc. from cwd). Memory can be tagged per-project automatically.

**Evolution loop:** Can work, but hooks add latency. The `PostToolUse` hook fires after every tool call -- logging each one is expensive. Better to log at session end via `Stop` hook.

**Scalability:** Limited by hook overhead. Each hook invocation is a subprocess. Multiple concurrent Claude Code sessions all hitting the daemon could be slow.

**Channel compatibility:** None. This only works for Claude Code. WhatsApp/Telegram users get no benefit.

---

### Approach D: Lightweight Agent Dispatch

**Concept:** Deus spawns lightweight agents that operate directly in the target directory. Instead of the full container isolation, these agents run as Claude Code sessions on the host, inheriting Deus's memory and preferences but working in the external project's context.

#### How It Works

1. User asks via any channel: "fix the failing tests in client-repo"
2. Deus spawns a Claude Code session with:
   - `cwd` set to `~/client-repo`
   - System prompt includes Deus memory, preferences, reflections
   - `CLAUDE.md` from both the project and Deus's global config
3. Session runs, makes changes, reports back via channel
4. Interaction logged to evolution loop

#### Implementation Changes

| File | Change |
|------|--------|
| `src/container-runner.ts` | Add non-container execution path (direct claude-code CLI) |
| New: `src/host-agent.ts` | Spawns claude-code CLI directly on host |
| `src/index.ts` | Route project-associated groups to host-agent vs container |
| `src/mount-security.ts` | Needs equivalent for host execution (allowed directories) |

#### Detailed Analysis

**Implementation complexity:** Low for basic version. Spawn `claude` CLI with the right cwd and pipe results. The Claude Agent SDK already supports this. However, losing container isolation is a major tradeoff.

**User experience:** Fastest path to "it works." The agent operates in the actual project directory, can run tests, use project tools, etc. No mount mapping confusion. But the user must trust the agent with host-level access.

**Security:** No container isolation. The agent runs with full user permissions. This is the biggest concern. A misbehaving agent could `rm -rf ~/` or exfiltrate credentials. Mitigations: use Claude Code's built-in permission system (ask before destructive operations), but this conflicts with Deus's `bypassPermissions` mode.

**Memory/context management:** Same as Approach A -- Deus injects context into the prompt. The project's native `CLAUDE.md` is automatically loaded since cwd is the project.

**Evolution loop:** Works the same as current system -- log interaction, score, reflect.

**Scalability:** Unlimited by containers, but each agent is an uncontained process. Resource usage harder to control.

**Channel compatibility:** Excellent -- any channel can trigger it, just like current container agents.

---

### Comparison Matrix

| Criterion | A: Mount Override | B: MCP Server | C: Hooks | D: Host Agent |
|-----------|:-:|:-:|:-:|:-:|
| Implementation effort | Medium | Medium-High | High | Low |
| Messaging channel support | Yes | No | No | Yes |
| Container isolation preserved | Yes | N/A (no container) | No | No |
| Works in any IDE | N/A | Yes | Claude Code only | N/A |
| Existing infra reuse | High | Medium | Low | Medium |
| Memory per-project | Natural | Requires scoping | Natural | Natural |
| Evolution integration | Automatic | Manual | Possible | Automatic |
| Multiple simultaneous projects | Yes | Yes | Yes | Yes |
| User setup friction | Low | Medium | Medium | Low |

---

## 3. Key Technical Challenges

### Container Security with Arbitrary User Directories

**Read-only vs read-write:** Most coding tasks require read-write (the agent needs to edit files, run tests, install deps). But granting read-write to arbitrary directories is risky.

**Mitigations:**
- Git-branch enforcement: agent must create a branch before making changes; never commits to main directly
- Snapshot/restore: create a git stash or tarball before the agent starts; auto-restore on failure
- `.env`/secrets shadow: extend the current `.env` shadow pattern to detect and shadow common secret files (`.env.local`, `credentials.json`, `config/secrets.yml`)
- The existing `mount-security.ts` blocked patterns already cover `.ssh`, `.gnupg`, `.aws`, etc.
- Per-project `readonly` override in the mount allowlist

**Recommended policy:**
```json
{
  "allowedRoots": [
    { "path": "~/projects", "allowReadWrite": true },
    { "path": "~/work", "allowReadWrite": true }
  ],
  "blockedPatterns": [".env", ".env.local", ".env.production", "credentials", "secrets"],
  "nonMainReadOnly": true
}
```

### Project Type Detection and Auto-Configuration

Detecting the project type enables automatic domain preset loading and tool configuration.

```typescript
interface ProjectType {
  language: string;        // "typescript" | "python" | "rust" | "go" | ...
  framework?: string;      // "next.js" | "django" | "actix" | ...
  packageManager?: string; // "npm" | "yarn" | "pnpm" | "pip" | "cargo" | ...
  testRunner?: string;     // "jest" | "pytest" | "cargo test" | ...
  buildTool?: string;      // "tsc" | "webpack" | "vite" | ...
}

function detectProjectType(projectPath: string): ProjectType {
  // Check for marker files
  if (exists('package.json'))     -> language: 'typescript' or 'javascript'
  if (exists('Cargo.toml'))       -> language: 'rust'
  if (exists('go.mod'))           -> language: 'go'
  if (exists('pyproject.toml'))   -> language: 'python'
  if (exists('requirements.txt')) -> language: 'python'
  if (exists('Gemfile'))          -> language: 'ruby'

  // Framework detection from package.json dependencies
  if (deps.includes('next'))      -> framework: 'next.js'
  if (deps.includes('react'))     -> framework: 'react'
  if (deps.includes('express'))   -> framework: 'express'
  // etc.
}
```

This could be done once at project registration time and cached. The detected type feeds into domain preset selection (an engineering preset with language-specific guidance).

### Per-Environment Memory Isolation vs Cross-Environment Learning

Two competing needs:
1. **Isolation:** What I learn working on Project A should not leak into Project B (different codebases, different conventions, different clients)
2. **Cross-pollination:** General programming insights, user preferences, and style should carry across all projects

**Solution: Layered memory**

```
Global memory (user preferences, general knowledge)
  |
  +-- Project memory (project-specific patterns, architecture decisions)
       |
       +-- Session memory (current conversation context)
```

Implementation: tag memory entries with `project_id`. Memory queries default to `global + current_project`. The evolution loop scores interactions per-project but extracts general reflections to global scope.

### Token Budget Management

Injecting project context competes with message history, memory, reflections, and domain presets for the context window.

Current token budget breakdown (approximate):
- System prompt (CLAUDE.md): ~2K tokens
- Domain presets: ~800 tokens (MAX_PRESET_CHARS = 3200 chars)
- Reflections: ~500 tokens (top 3, bounded)
- User message + history: variable
- Project context (new): needs budget

**Strategy:** Project context should be injected via the SDK's native mechanisms (CLAUDE.md loading from cwd, `additionalDirectories`), not manually concatenated into the prompt. This leverages the SDK's own context management and compaction. The only manual injection should be a brief project-type hint (e.g., "This is a Next.js project with Jest tests, use pnpm").

### Git Integration

For coding tasks, the agent needs to understand the project's git state:

```typescript
interface GitContext {
  branch: string;
  isDirty: boolean;
  recentCommits: string[];    // Last 5 commit messages
  activePR?: { number: number; title: string; };
  remoteUrl?: string;
}
```

This context can be gathered pre-dispatch (on the host, before spawning the container) and injected into the prompt. The container already has `git` installed.

**Branch safety:** The agent should be instructed (via CLAUDE.md or system prompt) to always create a feature branch. The host can enforce this post-dispatch by checking if the agent committed to a protected branch.

### Evolution Loop with Multi-Project Scoring

Current evolution loop tags interactions by `group_folder`. If projects map 1:1 to groups, this works naturally. The judge can score project-related interactions with domain-specific criteria (e.g., "did the code change compile?" "did tests pass?").

For DSPy optimization, per-project prompt tuning makes sense -- different projects need different system prompts. The optimizer already supports `--domain` filtering.

### Concurrent Work on Multiple Projects

The existing `GroupQueue` already handles concurrent groups with one container per group. Multiple project-associated groups can run simultaneously, limited by `MAX_CONCURRENT_CONTAINERS` (default 5).

No new concurrency challenges -- each project-group pair is independent.

---

## 4. What Other Tools Do

### Claude Code

Claude Code's native approach is directory-centric: you run `claude` in a directory and it reads the project's `CLAUDE.md`. For multi-repo work, it supports `--add-dir` to add directories and `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1` to load their CLAUDE.md files. The "Spine Pattern" (community-developed) uses a hierarchical `CLAUDE.md` structure: workspace-level context at the root that references project-level contexts, acting as a routing layer.

**Relevance to Deus:** Deus already uses the SDK's `additionalDirectories` feature. The key insight is that the SDK handles multi-directory context natively -- Deus just needs to set the right `cwd` and `additionalDirectories`.

### Cursor / Windsurf

Both use semantic codebase indexing: they chunk source files, compute embeddings, store them in a vector database, and retrieve relevant chunks based on the user's query. Cursor sends chunks to a remote embedding service (with privacy guarantees -- raw code is not stored). Windsurf's "Fast Context" builds a real-time understanding of project structure, dependencies, and edit patterns.

**Relevance to Deus:** Deus's memory indexer (`scripts/memory_indexer.py`) already does semantic search over session logs. Extending it to index source code is possible but is a large undertaking and duplicates what the Claude SDK already does internally. Better to rely on the SDK's native codebase understanding (it reads files on demand) rather than building a separate indexer.

### Devin

Devin clones a repository into a sandboxed VM, spins up a full development environment, and works autonomously. It creates branches, implements changes, runs tests, and opens PRs. DeepWiki (2025) generates architecture maps of repositories. Fleet mode runs multiple Devins on multiple repos in parallel.

**Relevance to Deus:** Devin's architecture is closest to Deus's container model. The key difference is that Devin clones the repo into the container, while Deus should mount it (faster, no clone step, changes are immediately visible). Devin's "plan then execute" pattern is worth adopting -- the agent should analyze the repo structure before diving into changes.

### aider

aider builds a "repo map" -- a concise representation of the repository's structure using AST analysis and graph ranking. It sends only the most relevant portions to the LLM based on the current conversation. It is designed for single-repo workflows, with `.aiderignore` for scoping and `--subtree-only` for large repos.

**Relevance to Deus:** aider's repo-map concept is elegant but deeply integrated into aider's architecture. For Deus, the equivalent is letting the Claude SDK handle file discovery (it has its own repo-map-like capability via Glob/Grep tools) and focusing Deus's value-add on memory, preferences, and the evolution loop.

### OpenHands / SWE-agent

OpenHands allows mounting local repos via `SANDBOX_VOLUMES=host_path:container_path[:mode]`. Agents operate in a Docker sandbox with access only to mounted files. The V1 SDK redesign (2025-2026) moves toward optional sandboxing with `LocalWorkspace` as default for lower friction.

**Relevance to Deus:** OpenHands' mount approach is nearly identical to what Deus already has via `additionalMounts`. The shift toward optional sandboxing in OpenHands V1 validates Approach D (host agent) as a legitimate pattern for trusted environments. Deus can offer both: containerized (safe) and host-direct (fast) modes.

---

## 5. Recommended Approach

### Phased Implementation Plan

#### Phase 1: Project Registry + Mount Override (Approach A) -- MVP

**Why this first:** It reuses the most existing infrastructure (mount system, mount security, container isolation, channel compatibility), has medium implementation effort, and delivers the core value proposition: "talk to Deus about your code projects from any messaging app."

**What to build:**

1. **Project registry** (`src/project-registry.ts`, ~150 lines)
   - `registerProject(name, path, options)` -- validates path, detects type, stores in SQLite
   - `getProject(id)` -- lookup
   - `listProjects()` -- all registered projects
   - `associateGroup(projectId, groupFolder)` -- link a group to a project
   - Project type detection (marker files: package.json, Cargo.toml, etc.)

2. **Database schema** (addition to `src/db.ts`, ~30 lines)
   ```sql
   CREATE TABLE projects (
     id TEXT PRIMARY KEY,
     name TEXT NOT NULL,
     path TEXT NOT NULL,
     type TEXT,           -- detected project type JSON
     config TEXT,         -- JSON: readonly, branch policy, etc.
     created_at TEXT NOT NULL
   );

   -- Add project_id column to registered_groups or use a join table
   ALTER TABLE registered_groups ADD COLUMN project_id TEXT;
   ```

3. **Modified container mounts** (changes to `src/container-runner.ts`, ~50 lines)
   ```typescript
   function buildVolumeMounts(group: RegisteredGroup, isMain: boolean): VolumeMount[] {
     const mounts: VolumeMount[] = [];

     // If group has an associated project, mount it as primary workspace
     if (group.projectId) {
       const project = getProject(group.projectId);
       if (project) {
         mounts.push({
           hostPath: project.path,
           containerPath: '/workspace/project',
           readonly: project.config?.readonly ?? false,
         });
         // Shadow sensitive files in the project
         for (const pattern of ['.env', '.env.local', '.env.production']) {
           const envFile = path.join(project.path, pattern);
           if (fs.existsSync(envFile)) {
             mounts.push({
               hostPath: '/dev/null',
               containerPath: `/workspace/project/${pattern}`,
               readonly: true,
             });
           }
         }
       }
     }

     // Group folder always mounted (Deus-specific memory, conversation archives)
     mounts.push({
       hostPath: groupDir,
       containerPath: '/workspace/group',
       readonly: false,
     });

     // ... rest of existing mounts (sessions, IPC, agent-runner, etc.)
   }
   ```

4. **Agent runner cwd override** (changes to `container/agent-runner/src/index.ts`, ~15 lines)
   ```typescript
   // In runQuery(), change cwd when project mount exists
   const projectDir = '/workspace/project';
   const hasProject = fs.existsSync(projectDir) &&
     fs.readdirSync(projectDir).length > 0;
   const cwd = hasProject ? projectDir : '/workspace/group';

   for await (const message of query({
     prompt: stream,
     options: {
       cwd,
       additionalDirectories: [
         ...(hasProject ? ['/workspace/group'] : []),
         ...extraDirs,
       ],
       // ... rest unchanged
     }
   })) { ... }
   ```

5. **IPC command for project registration** (addition to `src/ipc.ts`, ~30 lines)
   - `register_project` command so the main group's agent can register projects
   - `associate_project` command to link a group to a project

6. **Project context injection** (addition to `src/container-runner.ts`, ~20 lines)
   ```typescript
   // Before dispatch, if group has a project, inject a brief context hint
   if (group.projectId) {
     const project = getProject(group.projectId);
     if (project?.type) {
       const hint = `[Project: ${project.name} (${project.type.language}${project.type.framework ? '/' + project.type.framework : ''})]`;
       input = { ...input, prompt: `${hint}\n\n${input.prompt}` };
     }
   }
   ```

**Estimated effort:** 2-3 days of focused work. ~300 lines of new code, ~50 lines of modified code.

**User workflow after Phase 1:**

```
User (WhatsApp): @Deus register project ~/projects/client-api as "Client API"
Deus: Registered "Client API" (Node.js/Express, Jest tests). Associated with this group.

User: fix the failing tests in the auth module
Deus: [works in ~/projects/client-api, runs tests, fixes code, reports back]

User: what did you change?
Deus: [reviews git diff, summarizes changes]
```

#### Phase 2: Git Safety Layer -- Shortly After MVP

- Pre-dispatch: check git status, create branch if on main/master
- Post-dispatch: verify no commits to protected branches
- Inject git context (branch, recent commits, dirty state) into prompt
- Add `git_context` to evolution interaction log for scoring

#### Phase 3: MCP Server (Approach B) -- For IDE Users

Build the MCP server as an **additional interface**, not a replacement. IDE users get Deus memory and preferences via MCP while using Claude Code natively. Messaging users continue using the container path.

This serves the "I want Deus's memory in my IDE" use case without replacing the primary messaging workflow.

#### Phase 4: Optional Host Agent Mode (Approach D) -- For Power Users

Add a `hostExecution: true` flag to project config. When enabled, the agent runs directly on the host instead of in a container. Faster startup, native tool access, but no sandboxing. For trusted environments where the user is comfortable with the risk.

### Minimum Viable Version (Phase 1)

The MVP unlocks the core value: **programmers can register their projects and interact with them through Deus's messaging channels, with container isolation, memory, and evolution loop all working.**

What it does not include (and does not need to include yet):
- Automatic codebase indexing (the SDK handles file discovery)
- IDE integration (Phase 3)
- Host-direct execution (Phase 4)
- Multi-project in a single message (one group = one project)
- Automatic project detection from git URLs

### Files That Need Modification

| File | Type | Changes |
|------|------|---------|
| `src/types.ts` | Modify | Add `ProjectConfig`, `ProjectType`, add `projectId` to `RegisteredGroup` |
| `src/db.ts` | Modify | Add `projects` table, project CRUD functions |
| `src/container-runner.ts` | Modify | Project-aware mount building, context injection |
| `container/agent-runner/src/index.ts` | Modify | cwd override when project mount exists |
| `src/ipc.ts` | Modify | Add `register_project`, `associate_project` commands |
| `src/mount-security.ts` | No change | Already handles validation for external paths |
| `src/config.ts` | No change | Paths are already configurable |
| New: `src/project-registry.ts` | Create | Project CRUD, type detection |
| New: container skill | Create | `/project` skill for registration/management |

### Security Considerations and Mitigations

1. **Mount allowlist is mandatory** -- projects must be under an allowed root. No path, no mount.
2. **Sensitive file shadow** -- extend the `.env` shadow to cover `.env.*`, `credentials.*`, `secrets/`, `*.pem`, `*.key` in mounted projects.
3. **Git branch enforcement** -- agent instructed to never commit directly to main/master. Host can verify post-dispatch.
4. **Non-main groups read-only by default** -- `nonMainReadOnly: true` in the allowlist means only the main group can write to projects. This prevents untrusted channels from modifying code.
5. **Blocked patterns extended** -- add project-specific patterns: `node_modules/.cache`, `.git/config` (contains credentials for some setups), `*.sqlite` (to prevent DB corruption).
6. **Rate limiting** -- existing container timeout and max-concurrent-containers limits apply.

---

## 6. Non-Programmer Environments

The project registration model generalizes naturally beyond code repositories.

### Design Workspaces

Register a Figma export directory or asset folder:
```
@Deus register project ~/Design/client-brand as "Client Brand"
```

Project type detection sees image files, `.fig` exports, style guides. Domain preset activates "design" mode. Agent can browse assets, compare versions, organize files, generate documentation.

### Writing Projects

Register a manuscript directory:
```
@Deus register project ~/Writing/novel as "Novel Draft"
```

Agent sees `.md`, `.docx`, `.tex` files. Can help with editing, continuity checking ("did I mention this character's eye color before?"), word count tracking, outline management.

### Data Science

Register a notebook/dataset directory:
```
@Deus register project ~/Research/climate-data as "Climate Analysis"
```

Agent sees `.ipynb`, `.csv`, `.parquet` files. Can run analysis in the container (Python is available), generate visualizations, summarize findings.

### The Generic Abstraction

The key abstraction is:

```typescript
interface Environment {
  path: string;              // Where the files live
  type: EnvironmentType;     // Detected from contents
  capabilities: string[];    // What the agent can do here
  conventions: string;       // How to work in this environment (from CLAUDE.md or detected)
}
```

`EnvironmentType` is not limited to programming languages:

```typescript
type EnvironmentType =
  | { kind: 'code'; language: string; framework?: string; }
  | { kind: 'design'; tools: string[]; }
  | { kind: 'writing'; format: string; }
  | { kind: 'data'; formats: string[]; }
  | { kind: 'mixed'; components: EnvironmentType[]; }
  | { kind: 'unknown'; };
```

The domain preset system already supports non-code domains (marketing, study, writing, strategy). Project type detection just needs to feed into this existing system.

The container already has everything needed for non-code work: filesystem access, web browsing (agent-browser with Chromium), bash for running tools. The only limitation is container size -- large datasets or media files may be slow to access via bind mounts. For those cases, Phase 4's host-agent mode is the answer.

---

## References

### External Tools Research

- [Claude Code Multi-Repo Context Loading](https://blackdoglabs.io/blog/claude-code-decoded-multi-repo-context)
- [The Spine Pattern: Multi-Repo Context for AI Development](https://tsoporan.com/blog/spine-pattern-multi-repo-ai-development/)
- [Claude Code Multi-Repository Feature Request](https://github.com/anthropics/claude-code/issues/23627)
- [Devin AI Guide 2026](https://aitoolsdevpro.com/ai-tools/devin-guide/)
- [Devin 2025 Performance Review](https://cognition.ai/blog/devin-annual-performance-review-2025)
- [How Cursor Actually Indexes Your Codebase](https://towardsdatascience.com/how-cursor-actually-indexes-your-codebase/)
- [Cursor Codebase Indexing Docs](https://docs.cursor.com/context/codebase-indexing)
- [Context Management for Windsurf](https://datalakehousehub.com/blog/2026-03-context-management-windsurf/)
- [aider Repository Map](https://aider.chat/docs/repomap.html)
- [OpenHands Docker Sandbox](https://docs.openhands.dev/openhands/usage/sandboxes/docker)
- [OpenHands Agent SDK Paper](https://arxiv.org/html/2511.03690v1)
- [SWE-ReX: Sandboxed Code Execution](https://github.com/SWE-agent/SWE-ReX)

### Deus Internal References

- `src/container-runner.ts` -- container lifecycle, mount building
- `src/mount-security.ts` -- mount validation and allowlist
- `src/types.ts` -- `AdditionalMount`, `MountAllowlist`, `RegisteredGroup`
- `container/agent-runner/src/index.ts` -- SDK integration, cwd, additionalDirectories
- `src/domain-presets.ts` -- keyword-based domain detection
- `src/evolution-client.ts` -- reflection retrieval, interaction logging
- `groups/global/CLAUDE.md` -- global agent persona
