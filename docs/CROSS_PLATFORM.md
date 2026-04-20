# Cross-Platform Guide

Deus runs on **macOS, Linux, and Windows** (via Docker Desktop + WSL2). Every change to the codebase must work on all three platforms. This document explains what to avoid, what to use instead, and provides a checklist to run before opening a PR.

---

## The Rule

> If your code contains a shell string, a Unix path, or a platform-specific binary, it's probably broken on at least one OS. Check the table below before merging.

---

## Patterns to Avoid (and Their Replacements)

### 1. Shell redirections in `execSync` strings

`2>/dev/null` and `>/dev/null` are Bash/Zsh syntax. Windows `cmd.exe` doesn't understand them.

```ts
// BAD — 2>/dev/null is shell syntax; breaks on Windows
execSync(`docker image inspect deus-agent 2>/dev/null`, { stdio: 'pipe' });

// GOOD — stdio: 'pipe' already captures/suppresses stderr at the Node level
execSync(`docker image inspect deus-agent`, { stdio: 'pipe' });
```

**Rule:** Never put shell redirect syntax in an `execSync` string. Use `stdio: 'pipe'` or `stdio: ['pipe','pipe','pipe']` instead — they silence stderr at the Node.js level and work everywhere.

---

### 2. Single quotes in shell strings passed to `execSync`

Windows `cmd.exe` does not interpret single quotes. They are passed literally to the subprocess, breaking Go templates, Python `-c` args, and similar patterns.

```ts
// BAD — single quotes break on Windows cmd.exe
execSync(`docker ps --format '{{.Names}}'`);

// GOOD — no quoting needed when there are no spaces
execSync(`docker ps --format {{.Names}}`);

// GOOD — use execFileSync with args array to avoid all shell quoting issues
import { execFileSync } from 'child_process';
execFileSync('docker', ['ps', '--format', '{{.Names}}'], { stdio: 'pipe' });
```

**Rule:** Prefer `execFileSync(binary, [args])` over `execSync(shellString)`. With an args array, the shell is never involved and quoting is not an issue.

---

### 3. Hardcoded `/dev/null`

```ts
// BAD — /dev/null doesn't exist on Windows
hostPath: '/dev/null'

// GOOD — os.devNull is '/dev/null' on Unix, '\\.\nul' on Windows
import os from 'os';
hostPath: os.devNull
```

---

### 4. SIGTERM / negative PID kills

`SIGTERM` is not delivered on Windows and negative PIDs (process groups) are a Unix concept.

```ts
// BAD — broken on Windows
process.kill(-pid, 'SIGTERM');
process.kill(pid, 'SIGTERM');

// GOOD — platform-aware (see src/remote-control.ts for the full killProcess() helper)
import os from 'os';
import { execSync } from 'child_process';

function killProcess(pid: number): void {
  if (os.platform() === 'win32') {
    try { execSync(`taskkill /F /T /PID ${pid}`, { stdio: 'pipe' }); } catch {}
    return;
  }
  try { process.kill(-pid, 'SIGTERM'); } catch {
    try { process.kill(pid, 'SIGTERM'); } catch {}
  }
}
```

**Rule:** Never send signals directly without a platform check. Use the shared `killProcess()` helper in `src/remote-control.ts` or copy its pattern.

---

### 5. Hardcoded Unix paths in TypeScript/JavaScript

```ts
// BAD — Unix absolute paths
fs.readFileSync('/proc/version');        // Linux only
const home = '/Users/alice/deus';       // macOS only
const nullDev = '/dev/null';            // Unix only

// GOOD
import os from 'os';
import path from 'path';
const home = os.homedir();                           // cross-platform
const configDir = path.join(home, '.config', 'deus'); // correct separators
const nullDev = os.devNull;                           // cross-platform
// /proc paths: always wrap in try-catch with platform check
if (os.platform() === 'linux') { /* /proc access */ }
```

**Rule:** Never hardcode `/Users/`, `/home/`, `/proc/`, `/dev/`, `/etc/`. Always use `os.homedir()`, `os.tmpdir()`, `os.devNull`, or `path.join()`.

---

### 6. Unix-only environment variables

```ts
// BAD — HOME is not set on Windows (USERPROFILE is)
const home = process.env.HOME;

// GOOD — always falls back correctly
const home = process.env.HOME || os.homedir();  // os.homedir() handles Windows
```

---

### 7. Unix-only process APIs

```ts
// BAD — process.getuid() / process.getgid() don't exist on Windows
const isRoot = process.getuid() === 0;

// GOOD — use optional chaining; undefined means non-root on Windows
const isRoot = process.getuid?.() === 0;
```

---

### 8. Shell-specific scripts called from Node

```ts
// BAD — .sh scripts don't run on Windows
execSync('./scripts/setup.sh');
execSync('bash container/build.sh');

// GOOD — either use the TypeScript equivalent, or document the Windows alternative
// See: setup/service.ts for examples of platform-branched setup code
```

If a script is macOS/Linux-only by design (e.g. `deus-cmd.sh`), document it clearly and provide the Windows equivalent (e.g. `deus-cmd.ps1`).

---

### 9. Platform branches missing Windows

```ts
// BAD — handles darwin and linux but silently does nothing on Windows
if (platform === 'macos') { /* macOS path */ }
else if (platform === 'linux') { /* Linux path */ }
// Windows falls through with no action and no error

// GOOD — always include a Windows branch or an explicit default
if (platform === 'macos') { /* ... */ }
else if (platform === 'linux') { /* ... */ }
else if (platform === 'windows') { /* Windows path */ }
else { throw new Error(`Unsupported platform: ${platform}`); }
```

---

### 10. PATH separator assumptions

```ts
// BAD — : separator is Unix-only
const PATH = `/usr/local/bin:/usr/bin:${home}/.local/bin`;

// GOOD — use path.delimiter
import path from 'path';
const paths = ['/usr/local/bin', '/usr/bin', customBin];
const PATH = paths.join(path.delimiter);  // ':' on Unix, ';' on Windows
```

---

## Platform Detection Reference

**All platform-sensitive code in `src/` must go through `src/platform.ts`.** Direct calls to `os.platform()`, `process.platform`, and `process.env.HOME` are banned by ESLint outside that file. See `docs/decisions/platform-abstraction-layer.md`.

```ts
// ── src/ code — use platform.ts ──────────────────────────────
import { IS_WINDOWS, IS_MACOS, IS_LINUX, IS_WSL } from './platform.js';
import { homeDir, killProcess, forceKillProcess, processExists } from './platform.js';
import { detectProxyBindHost, hostGatewayArgs, containerBuildHint } from './platform.js';

// ── setup/ code — use setup/platform.ts ──────────────────────
import { getPlatform, isWSL } from './setup/platform.js';

// ── Raw Node.js reference (DO NOT use directly in src/) ──────
os.platform()     // 'darwin' | 'linux' | 'win32'
os.homedir()      // /Users/name | /home/name | C:\Users\name
os.tmpdir()       // /tmp | C:\Users\name\AppData\Local\Temp
os.devNull        // /dev/null | \\.\nul
path.sep          // '/' | '\'
path.delimiter    // ':' | ';'
```

---

## Known Platform-Specific Files

These files are intentionally platform-specific and are **not** expected to be cross-platform:

| File | Platform | Notes |
|------|----------|-------|
| `deus-cmd.sh` | macOS/Linux | CLI launcher; Windows equivalent is `deus-cmd.ps1` |
| `deus-cmd.ps1` | Windows | CLI launcher; install via the setup skill |
| `scripts/rename-repo.sh` | macOS/Linux | One-time repo renaming utility; not part of the runtime |
| `container/build.sh` | macOS/Linux | Use `docker build -t deus-agent ./container` on Windows |
| `setup.sh` | macOS/Linux | Initial bootstrap; use `npm run setup` on Windows |

---

## PR Checklist: Cross-Platform Review

Before opening a PR, run through this checklist for every file you changed:

```
[ ] No shell redirect syntax in execSync strings (2>/dev/null, >/dev/null)
[ ] No single-quoted strings in shell commands run via execSync
[ ] No hardcoded /dev/null — use os.devNull (CI-enforced)
[ ] No SIGTERM/process.kill without platform check — use killProcess() helper (CI-enforced)
[ ] No hardcoded Unix paths (/Users/, /home/, /proc/, /dev/, /etc/)
[ ] No process.getuid() without optional chaining (?.)
[ ] No process.env.HOME without os.homedir() fallback
[ ] No platform branches that handle darwin+linux but skip win32
[ ] No PATH strings using ':' separator — use path.delimiter
[ ] No .sh scripts invoked from Node.js without a Windows alternative
[ ] New exec calls use execFileSync(binary, args) not execSync(shellString)
[ ] No .replace('file://', '') — use fileURLToPath() (CI-enforced)
[ ] No new URL(`file://${path}`) — use pathToFileURL() (CI-enforced)
[ ] No sqlite3 CLI or other Unix-only binaries — use Node.js alternatives
```

---

### 11. `file://` URL to filesystem path conversion

```ts
// BAD — strips file:// prefix but leaves /C:/... on Windows
const filePath = import.meta.resolve('pkg').replace('file://', '');
const filePath = new URL(`file://${process.argv[1]}`).pathname;

// GOOD — handles drive letters, encoding, and all platforms
import { fileURLToPath, pathToFileURL } from 'url';
const filePath = fileURLToPath(import.meta.resolve('pkg'));
const isMatch = new URL(import.meta.url).href === pathToFileURL(process.argv[1]).href;
```

**Rule:** Never manually strip `file://` from URLs or manually construct `file://` URLs. Always use `fileURLToPath()` and `pathToFileURL()` from the `url` module.

---

### 12. `sqlite3` CLI and other Unix-only binaries

```ts
// BAD — sqlite3 CLI is not installed on Windows
execSync(`sqlite3 "${dbPath}" "SELECT COUNT(*) FROM ..."`)

// GOOD — use the Node.js binding (better-sqlite3) which is already a dependency
execSync(`node -e "const D=require('better-sqlite3');..."`)
// Or: use better-sqlite3 directly if async is acceptable
```

**Rule:** Never depend on CLI tools that aren't installed by default on all platforms. If you need SQLite, use `better-sqlite3` (an npm dependency). If you need other tools, check availability or provide a Node.js alternative.

---

## Testing on Multiple Platforms

The CI runs on `ubuntu-latest`, `macos-latest`, and `windows-latest`. The `test-windows` job runs lint, typecheck, and all tests — including automated cross-platform pattern detection tests in `src/cross-platform.test.ts`.

If your change touches anything in the checklist above, you should:

1. Look at the CI output for the `windows-latest` job in your PR
2. If you don't have a Windows machine, annotate your PR with `needs-windows-test` and describe what you expect might fail

For the Windows service path (NSSM/Servy integration), a real Windows machine smoke test is required — see `docs/windows-setup.md`.

### Automated Enforcement

The following patterns are caught automatically by `src/cross-platform.test.ts`, which runs on every CI platform:

| Pattern | Detection |
|---------|-----------|
| `.replace('file://', '')` | Caught — use `fileURLToPath()` |
| `new URL('file://' + path)` | Caught — use `pathToFileURL()` |
| `'/dev/null'` in code (not comments) | Caught — use `os.devNull` |
| `.kill('SIGKILL')` / `.kill('SIGTERM')` without platform check | Caught — use `killProcess()` helper |

These tests scan all `.ts` files in `src/` and fail the build if violations are found. To add new patterns, edit `src/cross-platform.test.ts`.

---

## Adding a New Platform-Specific Feature

If you're adding a feature that truly requires different implementations per platform:

1. Add a branch in `setup/platform.ts` — that's the single source of truth for platform detection
2. Add the Windows case in `setup/service.ts` if it's service-related
3. Update this document with the new pattern
4. Add tests for each platform branch (see `src/checks.test.ts` for examples of mocking `os.platform()`)
