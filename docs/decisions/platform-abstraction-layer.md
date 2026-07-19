# ADR: Platform Abstraction Layer

**Date:** 2026-04-07
**Status:** Accepted
**Scope:** All source code in `src/`, `setup/`, `packages/`

## Context

Deus runs on macOS, Linux, and Windows. Platform-sensitive code was scattered across 10+ files with inconsistent patterns: some used `os.platform()`, others `process.platform`, some had Windows branches, many didn't. This led to critical bugs on Windows:

- `file://` URL stripping broke all channel adapter paths (PR #101)
- `main()` never executed due to incorrect `file://` URL construction (PR #101)
- `SIGKILL` threw `ERR_UNKNOWN_SIGNAL` on Windows (PR #101)
- `sqlite3` CLI not available on Windows (PR #101)

The recurring root cause: every developer (human or AI) makes their own platform decisions inline, often forgetting Windows.

## Decision

**All platform-sensitive logic is centralized in `src/platform.ts`.** No other source file may call `os.platform()`, `process.platform`, `process.env.HOME`, or send raw signals. This is enforced by ESLint `no-restricted-syntax` rules that fail the build.

### Architecture: Three-Tier Model

```
Tier 1: src/platform.ts    — ONLY file with raw OS calls
Tier 2: src/config.ts      — imports from platform.ts, exports derived paths
Tier 3: everything else    — zero raw os/process calls, ESLint enforced
```

Inspired by VS Code's `platform.ts` (single source of truth for all OS detection) and the Strategy pattern (one implementation per platform, resolved once at startup).

### What `platform.ts` exports

| Category | Exports |
|----------|---------|
| Detection | `IS_WINDOWS`, `IS_MACOS`, `IS_LINUX`, `IS_WSL` |
| Directories | `homeDir`, `configDir` |
| Process mgmt | `killProcess()`, `processExists()` |
| Utilities | `devNull` (re-export of `os.devNull`) |
| Container | `detectProxyBindHost()`, `hostGatewayArgs()` |

### ESLint enforcement

The following raw calls are banned outside `src/platform.ts` via `no-restricted-syntax`:

- `process.platform`
- `os.platform()`
- `process.env.HOME` (use `homeDir` from platform.ts)

Violations fail lint, which runs on all three CI platforms (ubuntu, macos, windows).

### CI integration

The lint step runs on every PR via `.github/workflows/ci.yml` (`npm run lint`) on both `ubuntu-latest` and `windows-latest` runners. The rules are defined in `eslint.config.js` lines 28–54 — a dedicated config block scoped to `src/**/*.ts` with `src/platform.ts` in `ignores`.

**Scope gap:** `container/` is excluded from ESLint (`ignores` at line 7). This is intentional — the agent-runner always runs inside a Linux container, so platform guards are unnecessary there. Any future code added to `container/` that needs to run on multiple OSes should be moved to `src/` or explicitly documented here.

## Consequences

- **New platform differences** are added in one place — the compiler and lint show every call site
- **Testing** is simplified: mock `platform.ts` to test any OS combination on any CI runner
- **Container-runtime.ts** retains its own platform detection since it's container-specific logic (Docker network topology), not general OS behavior
- **`process.getuid?.()` / `process.getgid?.()`** remain inline in container-runner.ts — these are Unix-specific container UID mapping, not general platform logic
- **Top-level build-free launcher scripts are a standing, permanent exception**, not covered by the Tier 3 ESLint enforcement (scoped to `src/**/*.ts`) and not expected to ever be: `deus-cmd.sh` and `deus-v2-cmd.mjs` (LIA-434) both do their own OS detection (`$OSTYPE` / `os.platform()`) because they must run correctly before `npm install`/`npm run build` has ever happened — there is no `dist/` to import `src/platform.ts` from, and no `tsx`/`tsc` step for a plain `.mjs`/`.sh` entrypoint. `deus-v2-cmd.mjs` additionally does its own path-comparison logic (`path.win32`/`path.posix`, drive-letter case-folding) for validating a standalone git checkout — this is real platform-sensitive logic, disclosed here rather than silently scattered. If Tier 3 enforcement is ever extended beyond `src/` (see `project_windows_sot_plan` for that possibility), it needs an explicit carve-out for this category of file, not just an `ignores` entry copied from elsewhere.

## Alternatives Considered

1. **Effect-TS platform abstractions** — too heavy for a Node.js server; requires adopting the Effect runtime
2. **Branded types for HostPath/ContainerPath** — good idea for future, deferred to avoid scope creep
3. **Build-time platform elimination** — not useful for server-side Node.js (runtime detection has zero overhead)
4. **tsconfig path aliases (`~path`)** — adds indirection without clear benefit over direct imports

## References

- VS Code `src/vs/base/common/platform.ts` — centralized detection + ESLint enforcement
- [ehmicky/cross-platform-node-guide](https://github.com/ehmicky/cross-platform-node-guide)
- [Effect-TS Platform Abstractions](https://deepwiki.com/Effect-TS/effect/4-platform)
- PR #101 — the bug fix that motivated this ADR
