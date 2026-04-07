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

## Consequences

- **New platform differences** are added in one place — the compiler and lint show every call site
- **Testing** is simplified: mock `platform.ts` to test any OS combination on any CI runner
- **Container-runtime.ts** retains its own platform detection since it's container-specific logic (Docker network topology), not general OS behavior
- **`process.getuid?.()` / `process.getgid?.()`** remain inline in container-runner.ts — these are Unix-specific container UID mapping, not general platform logic

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
