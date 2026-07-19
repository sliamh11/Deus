#!/usr/bin/env node
/**
 * deus-v2 — bootstrap launcher for a parallel, independent `sliamh11/deus-v2`
 * checkout (LIA-434). Locates or creates a standalone clone at ~/deus-v2,
 * then delegates argv/cwd/stdio/exit-code/signal unchanged to its own
 * deus-cmd.sh/.ps1 — alongside the existing v1 `deus` command, never
 * replacing it. `deus-v2 [args...]` forwards as-is: no reparsing, no shell
 * interpolation, no bare-command translation.
 *
 * Build-free by design (must run before `npm install`/`npm run build` has
 * ever happened, like deus-cmd.sh/scripts/migrate.mjs) — a deliberate,
 * disclosed exception to the platform-abstraction ADR's src/platform.ts
 * requirement; see docs/decisions/platform-abstraction-layer.md.
 *
 * Design patterns: the lock and signal guard are RAII-style (single
 * dispose/release the caller's `finally` always calls); validateCheckout
 * returns a discriminated result; platform dispatch is a plain boolean
 * (exactly two branches, no reuse beyond this file).
 */
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO_URL = 'https://github.com/sliamh11/deus-v2.git';
// Fully anchored (^...$) to the two exact accepted origin forms — a prior
// unanchored version matched "github.com/sliamh11/deus-v2" as a SUBSTRING
// anywhere in the origin, which a code review caught as a real bypass (e.g.
// "https://attacker.example/github.com/sliamh11/deus-v2.git" would have
// passed). Whole-string match only.
const REPO_SLUG_RE =
  /^(?:https:\/\/github\.com\/sliamh11\/deus-v2(?:\.git)?\/?|git@github\.com:sliamh11\/deus-v2(?:\.git)?)$/i;

// How long a second invocation will wait for a concurrent clone to finish
// before giving up (a full clone can legitimately take minutes on a slow
// connection). This is a give-up-and-report-an-actionable-error timeout, NOT
// a staleness threshold — see acquireCloneLock's comment for why the lock is
// never stolen based on elapsed time alone.
const DEFAULT_LOCK_WAIT_TIMEOUT_MS = 10 * 60 * 1000;
const DEFAULT_LOCK_POLL_MS = 1000;

export function getCheckoutPath() {
  return path.join(os.homedir(), 'deus-v2');
}

export function getLockPath() {
  return path.join(os.homedir(), '.deus', 'deus-v2-clone.lock');
}

export function isWindowsPlatform() {
  return os.platform() === 'win32';
}

// ── Path comparison ─────────────────────────────────────────────────────────
//
// Routed through path.win32/path.posix (not the ambient, host-OS-bound `path`
// module) so an injected `isWindows` test flag stays deterministic regardless
// of the real host OS. Also case-folded when isWin: Git for Windows (MSYS2)
// often reports a lowercase drive letter while fs.realpathSync returns the
// OS-canonical uppercase form — separator normalization alone doesn't fix
// that mismatch, only normalize+lowercase together does.

function resolveGitPath(base, rel, isWin) {
  const p = isWin ? path.win32 : path.posix;
  return p.resolve(base, rel);
}

function pathsEqual(a, b, isWin) {
  const p = isWin ? path.win32 : path.posix;
  const norm = (s) => p.normalize(s).replace(/[\\/]+$/, '');
  const na = norm(a);
  const nb = norm(b);
  return isWin ? na.toLowerCase() === nb.toLowerCase() : na === nb;
}

function isUnderDir(child, parentDir, isWin) {
  const p = isWin ? path.win32 : path.posix;
  const c = p.normalize(child).replace(/[\\/]+$/, '');
  const d = p.normalize(parentDir).replace(/[\\/]+$/, '');
  const [cc, dd] = isWin ? [c.toLowerCase(), d.toLowerCase()] : [c, d];
  return cc === dd || cc.startsWith(dd + p.sep);
}

// ── Checkout validation ─────────────────────────────────────────────────────
//
// Returns { valid: true } or { valid: false, reason }, where reason is one of:
//   'missing'                    — nonexistent path OR an existing-but-empty
//                                   directory. Both are safe to `git clone`
//                                   into (git accepts an empty target dir),
//                                   so both map to the same auto-clone-safe
//                                   reason.
//   'invalid-not-a-directory'    — checkoutPath exists but is a plain file.
//   'invalid-not-a-git-repo'     | 'invalid-nested-repo'
//   | 'invalid-linked-worktree'  | 'invalid-wrong-origin'
//   | 'invalid-missing-entrypoint:<name>'
//
// Every 'invalid-*' reason is a HARD FAIL — the caller must never delete,
// overwrite, or repurpose the directory for any of these; only 'missing' is
// ever auto-cloned into. All OS/fs primitives are injectable for testability:
// a genuine Windows-mode unit test must also fake fs.realpathSync's return
// value, since the real one can't produce a "C:\..." string on a non-Windows
// test runner.
export function validateCheckout(checkoutPath, opts = {}) {
  const {
    isWindows = isWindowsPlatform(),
    existsSync = fs.existsSync,
    readdirSync = fs.readdirSync,
    realpathSync = fs.realpathSync,
    statSync = fs.statSync,
    runGit = (args) =>
      spawnSync('git', ['-C', checkoutPath, ...args], { encoding: 'utf8' }),
  } = opts;

  if (!existsSync(checkoutPath)) return { valid: false, reason: 'missing' };

  const topStat = statSync(checkoutPath);
  if (!topStat.isDirectory()) {
    return { valid: false, reason: 'invalid-not-a-directory' };
  }
  if (readdirSync(checkoutPath).length === 0) {
    return { valid: false, reason: 'missing' };
  }

  const inside = runGit(['rev-parse', '--is-inside-work-tree']);
  if (inside.status !== 0 || inside.stdout.trim() !== 'true') {
    return { valid: false, reason: 'invalid-not-a-git-repo' };
  }

  const toplevel = runGit(['rev-parse', '--show-toplevel']);
  if (toplevel.status !== 0) {
    return { valid: false, reason: 'invalid-not-a-git-repo' };
  }
  const resolvedToplevel = resolveGitPath(
    checkoutPath,
    toplevel.stdout.trim(),
    isWindows,
  );
  if (!pathsEqual(resolvedToplevel, realpathSync(checkoutPath), isWindows)) {
    return { valid: false, reason: 'invalid-nested-repo' };
  }

  const gitDir = runGit(['rev-parse', '--git-dir']);
  const commonDir = runGit(['rev-parse', '--git-common-dir']);
  if (gitDir.status !== 0 || commonDir.status !== 0) {
    return { valid: false, reason: 'invalid-not-a-git-repo' };
  }
  const resolvedGitDir = resolveGitPath(
    checkoutPath,
    gitDir.stdout.trim(),
    isWindows,
  );
  const resolvedCommonDir = resolveGitPath(
    checkoutPath,
    commonDir.stdout.trim(),
    isWindows,
  );
  if (
    !pathsEqual(resolvedGitDir, resolvedCommonDir, isWindows) ||
    !isUnderDir(resolvedCommonDir, checkoutPath, isWindows)
  ) {
    // Mirrors deus-cmd.sh's existing linked-worktree check (its own `deploy`
    // subcommand): a linked worktree has git-dir under the MAIN repo's
    // .git/worktrees/<name>, with common-dir pointing elsewhere entirely.
    // Cross-checked against scripts/drift_check.py's _in_linked_worktree()
    // for algorithm consistency (same anchoring rationale, different
    // language — not reusable directly, written fresh here).
    return { valid: false, reason: 'invalid-linked-worktree' };
  }

  const origin = runGit(['remote', 'get-url', 'origin']);
  if (origin.status !== 0 || !REPO_SLUG_RE.test(origin.stdout.trim())) {
    return { valid: false, reason: 'invalid-wrong-origin' };
  }

  const entrypointName = isWindows ? 'deus-cmd.ps1' : 'deus-cmd.sh';
  if (!existsSync(path.join(checkoutPath, entrypointName))) {
    return {
      valid: false,
      reason: `invalid-missing-entrypoint:${entrypointName}`,
    };
  }

  return { valid: true };
}

// ── Locking ──────────────────────────────────────────────────────────────
//
// Wait-and-steal, unlike src/auth-refresh.ts's fail-fast lock: a clone in
// progress should be waited out, not treated as already-handled. Stealing is
// decided by PID liveness (process.kill(pid, 0), a signal-free existence
// probe), never by elapsed time — a live holder is never stolen from
// regardless of how slow its clone is, and this also rules out an ABA hazard
// where a dead owner's lock gets reused by a third process before the
// original owner's own cleanup runs.
function isPidAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    // EPERM means the process exists but is owned by another user — still
    // alive, just not signalable by us. Any other error (typically ESRCH)
    // means it's gone.
    return err.code === 'EPERM';
  }
}

export async function acquireCloneLock(lockPath, opts = {}) {
  const {
    waitTimeoutMs = DEFAULT_LOCK_WAIT_TIMEOUT_MS,
    pollMs = DEFAULT_LOCK_POLL_MS,
    isAlive = isPidAlive,
  } = opts;
  fs.mkdirSync(path.dirname(lockPath), { recursive: true });
  const deadline = Date.now() + waitTimeoutMs;
  while (Date.now() < deadline) {
    try {
      const fd = fs.openSync(lockPath, 'wx');
      fs.writeSync(fd, String(process.pid));
      return { fd, path: lockPath };
    } catch (err) {
      if (err.code !== 'EEXIST') throw err;
    }
    // Hold an open read fd on the lock file for the rest of this iteration
    // instead of re-`statSync`-ing the path twice. This closes a real TOCTOU
    // gap two prior code reviews narrowed but couldn't fully close: a bare
    // `statSync(path)` holds nothing, so the OS is free to reuse its inode
    // number the instant a concurrent process unlinks it — a LATER
    // `statSync(path)` can then observe a coincidentally-matching dev+ino on
    // a genuinely different, freshly-recreated file. POSIX (and, per libuv,
    // Node's own default Windows open flags: FILE_SHARE_READ|WRITE|DELETE)
    // guarantees an inode is never reclaimed while any process holds it
    // open — so keeping `holderFd` open across the isAlive() check FORCES a
    // concurrent steal onto a different inode, making the dev+ino
    // comparison below (where `isAlive` returns false) deterministic rather
    // than merely unlikely to collide.
    let holderFd;
    try {
      holderFd = fs.openSync(lockPath, 'r');
    } catch {
      continue; // vanished between our failed open and this one — retry immediately
    }
    try {
      let holderStat, holderContent;
      try {
        holderStat = fs.fstatSync(holderFd);
        holderContent = fs.readFileSync(holderFd, 'utf8').trim();
      } catch {
        continue; // vanished after we opened it — retry immediately
      }
      const holderPid = parseInt(holderContent, 10);
      if (!Number.isInteger(holderPid)) {
        // Unparseable content most likely means we read the lock file in the
        // narrow window between another process's openSync (empty file) and
        // its writeSync (PID written) — not a dead holder, just one still
        // being created. Wait rather than steal.
        await new Promise((resolve) => setTimeout(resolve, pollMs));
        continue;
      }
      if (!isAlive(holderPid)) {
        // Verify by INODE IDENTITY (not content) immediately before
        // deleting: two code reviews caught real problems here in
        // succession. First: an unconditional unlink could delete a
        // DIFFERENT, freshly re-acquired lock a concurrent stealer already
        // created. Second: re-verifying by CONTENT string (an earlier fix)
        // is still wrong, because PIDs get reused by the OS — a
        // coincidentally-identical PID string on a genuinely different
        // (fresh, live) lock file would pass a content check and still get
        // wrongly deleted. `holderStat` came from the still-open `holderFd`
        // (not a fresh path stat), which is what makes this comparison
        // deterministic — see the comment above `holderFd`'s declaration.
        try {
          const currentStat = fs.statSync(lockPath);
          if (
            currentStat.ino === holderStat.ino &&
            currentStat.dev === holderStat.dev
          ) {
            fs.unlinkSync(lockPath);
          }
        } catch {
          // Vanished or already replaced by someone else — fine either way.
        }
        continue; // retry immediately — no reason to wait for a dead holder
      }
      await new Promise((resolve) => setTimeout(resolve, pollMs));
    } finally {
      try {
        fs.closeSync(holderFd);
      } catch {
        // Nothing else in this scope closes holderFd first, so reaching
        // here would mean something genuinely unexpected — swallow rather
        // than mask the real error this finally is cleaning up after.
      }
    }
  }
  return null; // timed out waiting for a live holder to finish
}

export function releaseLock(lock) {
  try {
    fs.closeSync(lock.fd);
  } catch {
    // already closed
  }
  try {
    fs.unlinkSync(lock.path);
  } catch {
    // already removed
  }
}

// ── Signal-aware child process runner ───────────────────────────────────────
//
// Exactly one process-level listener per signal for a guard's lifetime, fed
// by a mutable box that callers update — runForwarding never registers its
// own listeners, only sets/clears `box.child` around the spawn — so there's
// never more than one listener per signal, and no registration-order race
// between "forward to child" and "clean up and re-raise" (both branches live
// in the same handler).
export function makeSignalGuard(
  forwardSignals = ['SIGINT', 'SIGTERM', 'SIGHUP'],
) {
  const box = { child: null, cleanup: null };
  // INVARIANT: cleanup (via setCleanup) must be fully synchronous — Node
  // fires same-signal listeners with no interleaving only as long as no
  // listener body yields; releaseLock() below already satisfies this.
  //
  // KNOWN, ACCEPTED LIMITATION (evaluated and reverted a "fix" for this —
  // see runForwarding's comment): on POSIX, a non-detached child shares
  // this launcher's own foreground process group, so a terminal-generated
  // signal (e.g. Ctrl+C) reaches the child directly AND gets forwarded a
  // second time here. A prior attempt to close this via spawning the child
  // detached (its own process group) was reverted because Node's
  // `detached: true` actually calls setsid() — a NEW SESSION, not just a
  // new process group — which detaches the child from the controlling
  // terminal entirely and can break real interactive use (job control,
  // reading from the tty) for delegated commands that are genuinely
  // interactive (deus-cmd.sh has real `read -r` prompts; `deus-v2 chat` is
  // an interactive REPL). That regression is a near-certain break of this
  // launcher's core use case; the double-signal risk it would have "fixed"
  // is unconfirmed for the actual delegate (deus-cmd.sh/.ps1 have zero
  // signal-trap logic today, verified via grep) and is a lower-probability,
  // lower-severity tradeoff to accept instead.
  const handler = (sig) => {
    if (box.child) {
      // Windows: kill() is a hard-terminate with no SIGHUP delivery (Node/OS
      // limitation, not a regression — matches a raw Ctrl+C with no launcher
      // in between). POSIX targets get real cooperative forwarding here —
      // possibly a second copy of a terminal-originated signal (see above),
      // which the default (unhandled) OS disposition treats identically to
      // one copy, and is also needed as the ONLY delivery mechanism for a
      // programmatic `kill <this-pid>` that doesn't reach the child at all
      // otherwise.
      box.child.kill(sig);
      return; // the child's 'exit' event drives runForwarding's normal resolution
    }
    const cleanup = box.cleanup;
    box.cleanup = null;
    if (cleanup) cleanup();
    dispose();
    process.kill(process.pid, sig); // re-raise now that we're clean and no listener remains
  };
  function dispose() {
    for (const sig of forwardSignals) process.off(sig, handler);
  }
  for (const sig of forwardSignals) process.on(sig, handler);
  return {
    setChild(child) {
      box.child = child;
    },
    clearChild() {
      box.child = null;
    },
    setCleanup(fn) {
      box.cleanup = fn;
    },
    dispose,
  };
}

// Spawns `command` with argv passed as a real array — no shell:true anywhere,
// so args reach the child via execve unchanged, never through a shell string
// (satisfies "no reparsing/shell interpolation" for both the clone step and
// final delegation). Used identically by the one-time clone step and the
// steady-state delegate step, via the shared `guard`. Deliberately NOT
// spawned detached — see makeSignalGuard's comment for why that would trade
// a narrower double-signal risk for a more certain interactive-terminal
// regression on the delegate commands this launcher exists to run.
export async function runForwarding(command, args, { cwd }, guard) {
  const child = spawn(command, args, { cwd, stdio: 'inherit' });
  guard.setChild(child);
  try {
    return await new Promise((resolve, reject) => {
      child.on('error', reject);
      child.on('exit', (code, signal) => resolve({ code, signal }));
    });
  } finally {
    guard.clearChild();
  }
}

function exitLike({ code, signal }) {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exitCode = code ?? 1;
}

// Thrown, never process.exit()'d directly: exit() skips enclosing `finally`
// blocks (verified — it doesn't unwind the JS stack), which would orphan the
// clone lock on ordinary failure, not just signals. main()'s top-level catch
// turns this into a process exit only after cleanup has already run.
class LauncherError extends Error {
  constructor(message, { signal } = {}) {
    super(message);
    this.name = 'LauncherError';
    this.signal = signal;
  }
}

function fail(msg, opts) {
  throw new LauncherError(`deus-v2: ${msg}`, opts);
}

function explainInvalid(reason, checkoutPath) {
  const base = `${checkoutPath} exists but failed validation (${reason}) — leaving it untouched.`;
  if (reason === 'invalid-linked-worktree') {
    return (
      `${base} It looks like a linked worktree (shares .git with another ` +
      `checkout), not a standalone clone. Remove or relocate it, then retry ` +
      `— deus-v2 will clone fresh.`
    );
  }
  if (reason === 'invalid-wrong-origin') {
    return `${base} Its 'origin' remote doesn't point at sliamh11/deus-v2. Inspect manually.`;
  }
  if (reason.startsWith('invalid-missing-entrypoint')) {
    return `${base} Missing the platform entrypoint — the clone may be incomplete or from an unexpected branch. Inspect manually.`;
  }
  if (reason === 'invalid-not-a-directory') {
    return `${base} A file (not a directory) exists at that path. Inspect manually.`;
  }
  return `${base} Inspect manually before retrying.`;
}

// `opts.spawnClone` is injectable so a test can simulate a failing clone
// (proving the lock is released on failure) without touching the network.
// It receives (guard, targetPath) — see the staging-directory note below
// for why targetPath is never checkoutPath itself.
export async function ensureCheckout(checkoutPath, opts = {}) {
  const {
    lockPath = getLockPath(),
    validateOpts = {},
    lockOpts = {},
    spawnClone = (guard, targetPath) =>
      runForwarding(
        'git',
        ['clone', '--origin', 'origin', REPO_URL, targetPath],
        { cwd: process.cwd() },
        guard,
      ),
  } = opts;

  let result = validateCheckout(checkoutPath, validateOpts);
  if (result.valid) return;
  if (result.reason !== 'missing')
    fail(explainInvalid(result.reason, checkoutPath));

  const lock = await acquireCloneLock(lockPath, lockOpts);
  const guard = makeSignalGuard();
  if (lock) guard.setCleanup(() => releaseLock(lock));
  try {
    // Double-checked: another process may have finished cloning while we
    // waited for the lock.
    result = validateCheckout(checkoutPath, validateOpts);
    if (result.valid) return;
    if (result.reason !== 'missing') {
      fail(explainInvalid(result.reason, checkoutPath));
    }
    if (!lock) {
      fail(
        `timed out waiting for another 'deus-v2' invocation to finish cloning ` +
          `(lock: ${lockPath}). If nothing is actually cloning, remove the ` +
          `stale lock file and retry.`,
      );
    }
    console.log(`First run: cloning sliamh11/deus-v2 into ${checkoutPath} ...`);

    // Clone into a per-process-unique staging directory, then atomically
    // rename() it into place, rather than cloning directly into
    // checkoutPath. This is the real fix for a corruption risk a code
    // review kept correctly pressing on: acquireCloneLock's dead-holder
    // eviction is a check-then-act pattern (stat identity, then unlink) and
    // — like any userspace advisory lock without OS-level flock, which
    // Node's core fs doesn't expose and this file's build-free constraint
    // rules out reaching for via a native module — cannot be made
    // PERFECTLY race-free. Rather than keep chasing an unreachable "closed"
    // bar on the lock itself, this removes what actually matters: with a
    // uniquely-named staging directory per attempt, even if two processes
    // both wrongly believe they hold the lock, each writes into its own
    // directory — there is never shared mutable state during the clone, so
    // there is no interleaved-write corruption to risk. Only the final
    // rename (POSIX-atomic; requires the destination to not exist or be
    // empty) decides a winner. Losing that race is a harmless no-op
    // cleanup, never data loss — the lock still avoids the common case of
    // redundant concurrent clones, but is no longer safety-critical.
    const stagingPath = `${checkoutPath}.staging.${process.pid}.${Date.now()}`;
    const { code, signal } = await spawnClone(guard, stagingPath);
    if (code !== 0 || signal) {
      try {
        fs.rmSync(stagingPath, { recursive: true, force: true });
      } catch {
        // best-effort cleanup
      }
      fail(
        `git clone failed or was interrupted (exit ${code}${signal ? `, signal ${signal}` : ''}). ` +
          `${checkoutPath} was never touched (the clone only ever wrote to a ` +
          `disposable staging path) — safe to simply retry.`,
        { signal },
      );
    }

    try {
      // Windows' MoveFileExW (which fs.renameSync uses under the hood)
      // cannot replace an existing directory destination — even an empty
      // one — unlike POSIX rename(2), which explicitly permits that. But
      // checkoutPath may legitimately exist as an empty directory here:
      // validateCheckout's 'missing' reason covers both nonexistent AND
      // empty-directory destinations as equally safe to clone into. A code
      // review caught that this combination silently broke publication on
      // Windows. Clear an empty destination first so the rename always
      // targets a nonexistent path, which both platforms handle
      // identically. Unconditionally safe: rmdirSync only ever succeeds on
      // an ACTUALLY empty directory (the OS enforces this) — it can never
      // silently discard populated content. If checkoutPath doesn't exist
      // (ENOENT) or is already non-empty (ENOTEMPTY, meaning someone else
      // already published), this simply no-ops and the rename below runs
      // or fails accordingly.
      try {
        fs.rmdirSync(checkoutPath);
      } catch {
        // ENOENT or ENOTEMPTY — either way, proceed to the rename attempt.
      }
      fs.renameSync(stagingPath, checkoutPath);
    } catch {
      // Someone else already published a valid checkout at this path first
      // (they won the publish race) — that's fine, their result is exactly
      // what we wanted too. Discard our redundant staging clone and fall
      // through to re-validate checkoutPath below.
      try {
        fs.rmSync(stagingPath, { recursive: true, force: true });
      } catch {
        // best-effort cleanup
      }
    }

    const postClone = validateCheckout(checkoutPath, validateOpts);
    if (!postClone.valid) {
      fail(
        `clone completed but failed validation (${postClone.reason}) — please investigate.`,
      );
    }
  } finally {
    guard.dispose();
    if (lock) releaseLock(lock);
  }
}

function resolveEntrypoint(checkoutPath) {
  return path.join(
    checkoutPath,
    isWindowsPlatform() ? 'deus-cmd.ps1' : 'deus-cmd.sh',
  );
}

async function delegate(checkoutPath, args) {
  const entrypoint = resolveEntrypoint(checkoutPath);
  if (!fs.existsSync(entrypoint)) {
    fail(
      `${entrypoint} not found — the deus-v2 checkout at ${checkoutPath} looks broken (not auto-repaired).`,
    );
  }
  const guard = makeSignalGuard();
  try {
    let result;
    if (isWindowsPlatform()) {
      result = await runForwarding(
        'powershell',
        [
          '-NoProfile',
          '-ExecutionPolicy',
          'Bypass',
          '-File',
          entrypoint,
          ...args,
        ],
        { cwd: process.cwd() },
        guard,
      );
    } else {
      try {
        fs.chmodSync(entrypoint, 0o755);
      } catch {
        // Defensive; exec bit should already survive a normal git clone.
      }
      result = await runForwarding(
        entrypoint,
        args,
        { cwd: process.cwd() },
        guard,
      );
    }
    exitLike(result);
  } finally {
    guard.dispose();
  }
}

async function main() {
  const checkoutPath = getCheckoutPath();
  await ensureCheckout(checkoutPath);
  await delegate(checkoutPath, process.argv.slice(2));
}

// Symlink-safe main-module check. The naive `fileURLToPath(import.meta.url)
// === path.resolve(argv1)` (mirroring scripts/migrate.mjs, which is only
// ever invoked directly via `node scripts/migrate.mjs`) is broken for THIS
// script's actual primary invocation path: setup/cli.ts installs deus-v2 as
// a symlink (~/.local/bin/deus-v2 -> this file), and Node resolves
// import.meta.url through the symlink to the real file while leaving
// argv[1] as the invoked symlink path — the two never match, so main()
// would silently never run. Verified empirically before fixing (a code
// review caught this as a real, function-breaking bug). fs.realpathSync
// resolves argv[1] through the symlink the same way import.meta.url
// already is, restoring the comparison.
export function isMainModule(argv1, moduleUrl, realpathSync = fs.realpathSync) {
  if (!argv1) return false;
  try {
    return realpathSync(argv1) === fileURLToPath(moduleUrl);
  } catch {
    return false;
  }
}

if (isMainModule(process.argv[1], import.meta.url)) {
  main().catch((err) => {
    if (err instanceof LauncherError) {
      console.error(err.message);
      if (err.signal) {
        process.kill(process.pid, err.signal);
        return;
      }
      process.exitCode = 1;
    } else {
      console.error(err);
      process.exitCode = 1;
    }
  });
}
