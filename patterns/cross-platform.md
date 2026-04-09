---
governs:
  - src/platform.ts
  - src/cross-platform.test.ts
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
[ ] execFileSync(binary, args) used instead of execSync(shellString)
[ ] No PATH strings with hardcoded ':' separator
```
