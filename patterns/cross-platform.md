---
governs:
  - src/platform.ts
  - src/cross-platform.test.ts
  - src/config.ts
last_verified: "2026-04-25"
test_tasks:
  - "Add a new HOME directory lookup in src/ that must go through src/platform.ts"
  - "Add a shell command helper under src/ that works on macOS, Linux, and Windows"
  - "Fix a path.join separator bug in src/ that only appears on Windows"
  - "Add a new OS-specific path accessor to src/platform.ts"
---
# Pattern: cross-platform

## CI-enforced violations (tests will fail)

These four patterns are caught by `src/cross-platform.test.ts` on every CI platform:

| Violation | Fix |
|-----------|-----|
| `'/dev/null'` in code | `os.devNull` |
| `.replace('file://', '')` | `fileURLToPath()` from `'url'` |
| `new URL('file://' + path)` | `pathToFileURL()` from `'url'` |
| `.kill('SIGKILL')` / `.kill('SIGTERM')` without platform check | `killProcess()` from `./platform.js` |

## Additional enforced rules

- **All platform-sensitive code in `src/` must go through `src/platform.ts`** — ESLint bans direct `os.platform()` / `process.platform` / `process.env.HOME` outside that file. See ADR `platform-abstraction-layer.md`.
- Use `execFileSync(binary, [args])` not `execSync(shellString)` — shell quoting breaks on Windows.
- Never use `:` as PATH separator — use `path.delimiter`.
- Never call `process.getuid()` without optional chaining (`process.getuid?.()`).

## Three-tier model

Platform detection uses a strict three-tier hierarchy:

```
Tier 1: src/platform.ts    — ONLY file with raw OS calls (os.platform, process.platform, process.env.HOME)
Tier 2: src/config.ts      — imports from platform.ts, exports derived paths and values
Tier 3: everything else    — zero raw OS calls, ESLint enforced
```

When adding a new OS-dependent value: add raw detection to `platform.ts`, the derived value to `config.ts`, and consume it from `config.ts` elsewhere. Never skip a tier (e.g., calling `platform.ts` directly from a feature file instead of through `config.ts`).

## Exceptions (ADR-approved)

These patterns are intentional — do not flag them as violations:

- **`container/`** is excluded from ESLint (`ignores` at line 7 of `eslint.config.js`). Agent code always runs inside Linux containers, so platform guards are unnecessary. If code added to `container/` ever needs multi-OS support, move it to `src/` first.
- **`process.getuid?.()` / `process.getgid?.()`** remain inline in `src/container-runner.ts`. These handle Unix-specific container UID mapping and are exempt by ADR. Do not "fix" them.

## Import reference

```typescript
// src/ code
import { IS_WINDOWS, IS_MACOS, homeDir, killProcess } from './platform.js';

// setup/ code
import { getPlatform, isWSL } from './setup/platform.js';
```

## Checklist before PR

```
[ ] No /dev/null, file:// strip, SIGKILL/SIGTERM, new URL('file://...') violations
[ ] All platform detection via src/platform.ts (not os.platform() directly)
[ ] New derived paths/values go in src/config.ts, not directly in consuming files
[ ] execFileSync(binary, args) used instead of execSync(shellString)
[ ] No PATH strings with hardcoded ':' separator
```
