import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

import {
  getCheckoutPath,
  getLockPath,
  isWindowsPlatform,
  validateCheckout,
  acquireCloneLock,
  releaseLock,
  makeSignalGuard,
  runForwarding,
  ensureCheckout,
  isMainModule,
} from './deus-v2-cmd.mjs';

const LAUNCHER_PATH = fileURLToPath(
  new URL('./deus-v2-cmd.mjs', import.meta.url),
);

// These validateCheckout/ensureCheckout tests exercise REAL, unmocked git/fs
// against real host-native paths — so `isWindows` must reflect the ACTUAL
// host, not a hardcoded value, or the path-comparison logic gets fed data in
// one convention (e.g. real Windows backslashes/drive letters) while being
// told to compare it in the other (POSIX). The entrypoint filename these
// fixtures create/expect must match for the same reason.
const REAL_ENTRYPOINT_NAME = isWindowsPlatform()
  ? 'deus-cmd.ps1'
  : 'deus-cmd.sh';

function git(cwd: string, args: string[]) {
  const result = spawnSync('git', args, { cwd, encoding: 'utf8' });
  if (result.status !== 0) {
    throw new Error(`git ${args.join(' ')} failed: ${result.stderr}`);
  }
  return result;
}

function initRealRepo(dir: string, { origin }: { origin?: string } = {}) {
  fs.mkdirSync(dir, { recursive: true });
  git(dir, ['init', '-q', '-b', 'main']);
  git(dir, ['config', 'user.email', 'test@example.com']);
  git(dir, ['config', 'user.name', 'Test']);
  if (origin) git(dir, ['remote', 'add', 'origin', origin]);
  fs.writeFileSync(path.join(dir, 'README.md'), 'x');
  git(dir, ['add', '.']);
  git(dir, ['commit', '-q', '-m', 'init']);
}

describe('deus-v2-cmd basics', () => {
  it('getCheckoutPath resolves under the home directory', () => {
    expect(getCheckoutPath()).toBe(path.join(os.homedir(), 'deus-v2'));
  });

  it('getLockPath resolves under ~/.deus', () => {
    expect(getLockPath()).toBe(
      path.join(os.homedir(), '.deus', 'deus-v2-clone.lock'),
    );
  });

  it('isWindowsPlatform matches os.platform()', () => {
    expect(isWindowsPlatform()).toBe(os.platform() === 'win32');
  });
});

describe('isMainModule (symlink-invocation regression, code-review-caught bug)', () => {
  // A code review caught a real, function-breaking bug: the original guard
  // (`fileURLToPath(import.meta.url) === path.resolve(argv1)`, copied from
  // scripts/migrate.mjs, which is only ever invoked directly) compares
  // import.meta.url (which Node resolves THROUGH a symlink to the real
  // file) against the raw, unresolved argv[1] — these never match when
  // deus-v2-cmd.mjs is invoked via its installed ~/.local/bin/deus-v2
  // symlink (setup/cli.ts's whole point), so main() would silently never
  // run. Verified empirically in a real Node process (a throwaway symlink
  // + script) before writing the fix; these tests pin the fixed behavior.

  it('returns true when argv1 resolves (through a symlink) to the module URL', () => {
    // fileURLToPath is not injectable, so moduleUrl must be a URL the real
    // fileURLToPath can actually parse on the CURRENT host: a bare
    // "file:///real/module/path.mjs" (no drive letter) is a valid POSIX
    // file URL but not a valid Windows one, so it must be built via
    // pathToFileURL (the inverse operation) from a platform-appropriate
    // absolute path, not hardcoded as a POSIX-style string.
    const realModulePath = isWindowsPlatform()
      ? 'C:\\real\\module\\path.mjs'
      : '/real/module/path.mjs';
    const moduleUrl = pathToFileURL(realModulePath).href;
    const realpathSync = (p: string) => {
      expect(p).toBe('/some/symlink/path');
      return realModulePath;
    };
    expect(isMainModule('/some/symlink/path', moduleUrl, realpathSync)).toBe(
      true,
    );
  });

  it('returns false when argv1 resolves to a different file', () => {
    const realpathSync = () => '/real/other-script.mjs';
    expect(
      isMainModule('/some/path', 'file:///real/module/path.mjs', realpathSync),
    ).toBe(false);
  });

  it('returns false when argv1 is empty (module imported, not run as CLI)', () => {
    expect(isMainModule('', 'file:///real/module/path.mjs')).toBe(false);
  });

  it('returns false (not throws) when argv1 does not exist on disk', () => {
    const realpathSync = () => {
      throw new Error('ENOENT');
    };
    expect(
      isMainModule(
        '/nonexistent',
        'file:///real/module/path.mjs',
        realpathSync,
      ),
    ).toBe(false);
  });

  it(
    'real end-to-end: invoking the actual launcher through a real symlink ' +
      'runs main() (not the plain module-path comparison this replaced)',
    () => {
      // Primary evidence, not just the unit-level check above: a real
      // symlink to the real deus-v2-cmd.mjs file, invoked via `node
      // <symlink>` exactly as setup/cli.ts's installed ~/.local/bin/deus-v2
      // would be. HOME is redirected to an empty temp dir so getCheckoutPath()
      // resolves somewhere controlled with no real checkout — ensureCheckout
      // takes the "missing -> about to clone" path and logs its first-run
      // message BEFORE ever touching the network, which is what proves
      // main() actually executed (the old, broken guard would produce zero
      // output and exit immediately).
      const tmpHome = fs.mkdtempSync(path.join(os.tmpdir(), 'deus-v2-home-'));
      const symlinkDir = fs.mkdtempSync(
        path.join(os.tmpdir(), 'deus-v2-symlink-'),
      );
      const symlinkPath = path.join(symlinkDir, 'deus-v2');
      fs.symlinkSync(LAUNCHER_PATH, symlinkPath);

      const child = spawn(process.execPath, [symlinkPath], {
        env: { ...process.env, HOME: tmpHome },
      });

      let sawCloneStart = false;
      const timedOut = new Promise<boolean>((resolve) => {
        const timer = setTimeout(() => resolve(true), 5000);
        child.stdout.on('data', (chunk: Buffer) => {
          if (chunk.toString().includes('First run: cloning')) {
            sawCloneStart = true;
            clearTimeout(timer);
            child.kill('SIGTERM'); // don't let a real git clone actually run
            resolve(false);
          }
        });
        child.on('exit', () => {
          clearTimeout(timer);
          resolve(false);
        });
      });

      return timedOut.then((didTimeOut) => {
        expect(didTimeOut).toBe(false);
        expect(sawCloneStart).toBe(true);
        fs.rmSync(tmpHome, { recursive: true, force: true });
        fs.rmSync(symlinkDir, { recursive: true, force: true });
      });
    },
    10000,
  );
});

describe('validateCheckout', () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'deus-v2-validate-'));
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it('reports missing for a nonexistent path', () => {
    const target = path.join(tmpDir, 'does-not-exist');
    expect(validateCheckout(target)).toEqual({
      valid: false,
      reason: 'missing',
    });
  });

  it('reports missing for an existing empty directory', () => {
    const target = path.join(tmpDir, 'empty');
    fs.mkdirSync(target);
    expect(validateCheckout(target)).toEqual({
      valid: false,
      reason: 'missing',
    });
  });

  it('reports invalid-not-a-directory for a plain file', () => {
    const target = path.join(tmpDir, 'a-file');
    fs.writeFileSync(target, 'not a dir');
    expect(validateCheckout(target)).toEqual({
      valid: false,
      reason: 'invalid-not-a-directory',
    });
  });

  it('reports valid for a real standalone clone with correct origin and entrypoint', () => {
    const target = path.join(tmpDir, 'repo');
    initRealRepo(target, { origin: 'https://github.com/sliamh11/deus-v2.git' });
    fs.writeFileSync(
      path.join(target, REAL_ENTRYPOINT_NAME),
      '#!/bin/sh\necho hi\n',
    );
    expect(
      validateCheckout(target, { isWindows: isWindowsPlatform() }),
    ).toEqual({
      valid: true,
    });
  });

  it('accepts an SSH-form origin remote', () => {
    const target = path.join(tmpDir, 'repo-ssh');
    initRealRepo(target, { origin: 'git@github.com:sliamh11/deus-v2.git' });
    fs.writeFileSync(
      path.join(target, REAL_ENTRYPOINT_NAME),
      '#!/bin/sh\necho hi\n',
    );
    expect(
      validateCheckout(target, { isWindows: isWindowsPlatform() }),
    ).toEqual({
      valid: true,
    });
  });

  it('reports invalid-wrong-origin for a repo pointed at a different remote', () => {
    const target = path.join(tmpDir, 'wrong-origin');
    initRealRepo(target, {
      origin: 'https://github.com/someone-else/other-repo.git',
    });
    fs.writeFileSync(path.join(target, 'deus-cmd.sh'), '#!/bin/sh\necho hi\n');
    expect(
      validateCheckout(target, { isWindows: isWindowsPlatform() }),
    ).toEqual({
      valid: false,
      reason: 'invalid-wrong-origin',
    });
  });

  it('rejects a host-prefix bypass attempt on the origin (code-review-caught security bug)', () => {
    // Regression test for a real bug a GPT-backend code review caught: an
    // earlier, unanchored REPO_SLUG_RE matched "github.com/sliamh11/deus-v2"
    // as a SUBSTRING anywhere in the origin URL, so an attacker-controlled
    // host with the real slug appended as a path segment would have passed
    // validation and had its checkout entrypoint executed.
    const target = path.join(tmpDir, 'origin-bypass-attempt');
    initRealRepo(target, {
      origin: 'https://attacker.example/github.com/sliamh11/deus-v2.git',
    });
    fs.writeFileSync(path.join(target, 'deus-cmd.sh'), '#!/bin/sh\necho hi\n');
    expect(
      validateCheckout(target, { isWindows: isWindowsPlatform() }),
    ).toEqual({
      valid: false,
      reason: 'invalid-wrong-origin',
    });
  });

  it('reports invalid-missing-entrypoint when deus-cmd.sh is absent', () => {
    const target = path.join(tmpDir, 'no-entrypoint');
    initRealRepo(target, { origin: 'https://github.com/sliamh11/deus-v2.git' });
    expect(
      validateCheckout(target, { isWindows: isWindowsPlatform() }),
    ).toEqual({
      valid: false,
      reason: `invalid-missing-entrypoint:${REAL_ENTRYPOINT_NAME}`,
    });
  });

  it('reports invalid-not-a-git-repo for a populated non-git directory', () => {
    const target = path.join(tmpDir, 'not-git');
    fs.mkdirSync(target);
    fs.writeFileSync(path.join(target, 'marker.txt'), 'hello');
    expect(
      validateCheckout(target, { isWindows: isWindowsPlatform() }),
    ).toEqual({
      valid: false,
      reason: 'invalid-not-a-git-repo',
    });
  });

  it('reports invalid-linked-worktree for a REAL git worktree add (not a fake)', () => {
    const mainRepo = path.join(tmpDir, 'main-repo');
    initRealRepo(mainRepo);
    const linked = path.join(tmpDir, 'linked-worktree');
    git(mainRepo, ['worktree', 'add', '-b', 'side-branch', linked]);
    expect(
      validateCheckout(linked, { isWindows: isWindowsPlatform() }),
    ).toEqual({
      valid: false,
      reason: 'invalid-linked-worktree',
    });
  });

  it('never mutates a partial/broken checkout it refuses to touch', () => {
    const target = path.join(tmpDir, 'partial');
    // Simulates an interrupted/partial clone: a real .git dir plus one
    // arbitrary marker file, but no origin remote and no entrypoint —
    // genuinely broken, not a valid checkout.
    initRealRepo(target);
    fs.writeFileSync(path.join(target, 'MARKER'), 'partial-clone-marker');

    const before = fs.readdirSync(target, { recursive: true }).sort();

    const rmSyncSpy = vi.spyOn(fs, 'rmSync');
    const rmdirSyncSpy = vi.spyOn(fs, 'rmdirSync');
    const unlinkSyncSpy = vi.spyOn(fs, 'unlinkSync');

    const result = validateCheckout(target, { isWindows: isWindowsPlatform() });

    expect(result.valid).toBe(false);
    expect(result.reason).not.toBe('missing');

    const after = fs.readdirSync(target, { recursive: true }).sort();
    expect(after).toEqual(before);
    expect(rmSyncSpy).not.toHaveBeenCalled();
    expect(rmdirSyncSpy).not.toHaveBeenCalled();
    expect(unlinkSyncSpy).not.toHaveBeenCalled();

    rmSyncSpy.mockRestore();
    rmdirSyncSpy.mockRestore();
    unlinkSyncSpy.mockRestore();
  });

  it('Windows-mode regression test: lowercase-drive-letter git output vs uppercase realpathSync must still compare equal', () => {
    // This is the exact bug this design fixes: Git for Windows (MSYS2) often
    // reports a lowercase drive letter ("c:/Users/fake/deus-v2") while
    // fs.realpathSync returns the OS-canonical uppercase form
    // ("C:\Users\fake\deus-v2"). A naive case-fold-only comparison (without
    // separator normalization via path.win32) fails this; so does a
    // separator-only fix without case-folding. Only the combination
    // (resolveGitPath via path.win32 + pathsEqual's normalize+lowercase)
    // passes.
    const checkoutPath = 'C:\\Users\\fake\\deus-v2';
    const runGit = vi.fn((args: string[]) => {
      if (args.includes('--is-inside-work-tree')) {
        return { status: 0, stdout: 'true\n' };
      }
      if (args.includes('--show-toplevel')) {
        return { status: 0, stdout: 'c:/Users/fake/deus-v2\n' };
      }
      if (args.includes('--git-dir')) {
        return { status: 0, stdout: 'c:/Users/fake/deus-v2/.git\n' };
      }
      if (args.includes('--git-common-dir')) {
        return { status: 0, stdout: 'c:/Users/fake/deus-v2/.git\n' };
      }
      if (args[0] === 'remote') {
        return {
          status: 0,
          stdout: 'https://github.com/sliamh11/deus-v2.git\n',
        };
      }
      throw new Error(`unexpected git args: ${args.join(' ')}`);
    });

    const result = validateCheckout(checkoutPath, {
      isWindows: true,
      existsSync: () => true,
      readdirSync: () => ['deus-cmd.ps1'],
      statSync: () => ({ isDirectory: () => true }) as fs.Stats,
      realpathSync: () => 'C:\\Users\\fake\\deus-v2',
      runGit,
    });

    expect(result).toEqual({ valid: true });
  });

  it('resolves a short-form (8.3) checkoutPath against git output via realpathSync canonicalization (Windows 8.3-short-name regression)', () => {
    // Regression test for a real Windows-only bug the above test cannot
    // catch: its realpathSync mock returns a FIXED string regardless of
    // input, so it can't discriminate "did the code actually canonicalize
    // both sides" from "did it coincidentally return the same thing
    // either way." This test's mock instead maps each DISTINCT input to
    // its own distinct (but correctly related) canonical output, and
    // throws on any unexpected argument — so it fails loudly if the fix
    // ever regresses to comparing an un-realpath'd value against a
    // realpath'd one, the exact shape of the original bug: os.tmpdir()
    // (and so checkoutPath, in real usage) is commonly reported in the
    // legacy 8.3 short-name form ("RUNNER~1") on GH Actions Windows
    // runners; git's own --show-toplevel/--git-dir/--git-common-dir echo
    // back whatever they were invoked with, so a naive comparison against
    // only a realpath'd checkoutPath (this function's pre-fix behavior)
    // compares a short-form value against a long-form one and spuriously
    // fails with invalid-nested-repo / invalid-linked-worktree.
    const shortForm = 'C:\\Users\\RUNNER~1\\AppData\\Local\\Temp\\deus-v2';
    const longForm = 'C:\\Users\\runneradmin\\AppData\\Local\\Temp\\deus-v2';
    const runGit = vi.fn((args: string[]) => {
      if (args.includes('--is-inside-work-tree')) {
        return { status: 0, stdout: 'true\n' };
      }
      if (args.includes('--show-toplevel')) {
        return { status: 0, stdout: `${shortForm}\n` };
      }
      if (args.includes('--git-dir') || args.includes('--git-common-dir')) {
        return { status: 0, stdout: `${shortForm}\\.git\n` };
      }
      if (args[0] === 'remote') {
        return {
          status: 0,
          stdout: 'https://github.com/sliamh11/deus-v2.git\n',
        };
      }
      throw new Error(`unexpected git args: ${args.join(' ')}`);
    });
    const realpathSync = vi.fn((p: string) => {
      if (p === shortForm) return longForm;
      if (p === `${shortForm}\\.git`) return `${longForm}\\.git`;
      throw new Error(`unexpected realpathSync arg: ${p}`);
    });

    const result = validateCheckout(shortForm, {
      isWindows: true,
      existsSync: () => true,
      readdirSync: () => ['deus-cmd.ps1'],
      statSync: () => ({ isDirectory: () => true }) as fs.Stats,
      realpathSync,
      runGit,
    });

    expect(result).toEqual({ valid: true });
    expect(realpathSync).toHaveBeenCalledWith(shortForm);
    expect(realpathSync).toHaveBeenCalledWith(`${shortForm}\\.git`);
  });
});

describe('acquireCloneLock / releaseLock', () => {
  let tmpDir: string;
  let lockPath: string;

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'deus-v2-lock-'));
    lockPath = path.join(tmpDir, 'nested', 'deus-v2-clone.lock');
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it('acquires a free lock', async () => {
    const lock = await acquireCloneLock(lockPath, {
      waitTimeoutMs: 500,
      pollMs: 20,
    });
    expect(lock).not.toBeNull();
    expect(fs.existsSync(lockPath)).toBe(true);
    releaseLock(lock!);
    expect(fs.existsSync(lockPath)).toBe(false);
  });

  it('times out and returns null when the lock is held by a live process', async () => {
    // PID-liveness design (post GPT plan-review fix): a lock held by a live
    // process — however long — must NEVER be stolen. Simulate "live" via an
    // injected isAlive that always returns true, so this test doesn't depend
    // on real elapsed time or a real long-running holder process.
    const holder = await acquireCloneLock(lockPath, {
      waitTimeoutMs: 500,
      pollMs: 20,
    });
    expect(holder).not.toBeNull();

    const second = await acquireCloneLock(lockPath, {
      waitTimeoutMs: 150,
      pollMs: 20,
      isAlive: () => true,
    });
    expect(second).toBeNull();
    // The holder's lock must be untouched — proves no steal happened.
    expect(fs.existsSync(lockPath)).toBe(true);

    releaseLock(holder!);
  });

  it('steals immediately from a provably dead PID, regardless of lock age', async () => {
    fs.mkdirSync(path.dirname(lockPath), { recursive: true });
    fs.writeFileSync(lockPath, '999999'); // a PID essentially guaranteed not to exist
    // Freshly created (not aged) — proves the steal is PID-liveness-driven,
    // not time-driven: the old staleMs design would have refused to steal
    // a lock this fresh.
    const lock = await acquireCloneLock(lockPath, {
      waitTimeoutMs: 500,
      pollMs: 20,
      isAlive: (pid: number) => pid !== 999999,
    });
    expect(lock).not.toBeNull();
    releaseLock(lock!);
  });

  it('never steals from a live PID even when the lock is old', async () => {
    // Direct regression test for the GPT-backend plan-review finding: age
    // alone must never trigger a steal. A lock "aged" via utimes but backed
    // by an isAlive that reports true must still block, not be stolen.
    fs.mkdirSync(path.dirname(lockPath), { recursive: true });
    fs.writeFileSync(lockPath, String(process.pid));
    const oldTime = new Date(Date.now() - 60 * 60 * 1000); // 1 hour old
    fs.utimesSync(lockPath, oldTime, oldTime);

    const result = await acquireCloneLock(lockPath, {
      waitTimeoutMs: 150,
      pollMs: 20,
      isAlive: () => true,
    });
    expect(result).toBeNull();
    expect(fs.existsSync(lockPath)).toBe(true);
  });

  it('does not delete a lock replaced by a new inode between the dead-check and the steal (code-review-caught race)', async () => {
    // Regression test for a real TOCTOU race two rounds of GPT-backend code
    // review caught in succession. Round 1: process A determines a lock is
    // dead, then before A's unlink runs, a different process B has already
    // stolen and replaced it with a fresh, live lock — A's unconditional
    // unlink would delete B's valid lock. Round 2: re-verifying by CONTENT
    // STRING (the first fix) is still wrong, because PIDs get reused by the
    // OS — a coincidentally-identical PID string on a genuinely different,
    // live lock file would pass a content check and still get wrongly
    // deleted. The fix compares dev+ino (actual filesystem identity)
    // immediately before deleting; this test simulates process B replacing
    // the lock with a NEW INODE (unlink + recreate, not an in-place
    // content overwrite) that happens to contain the exact same PID string
    // as the dead holder — the scenario a content-only check would miss.
    fs.mkdirSync(path.dirname(lockPath), { recursive: true });
    fs.writeFileSync(lockPath, '999999'); // will be read as the "dead" holder

    const unlinkSpy = vi.spyOn(fs, 'unlinkSync');
    let mutated = false;
    const isAlive = () => {
      if (!mutated) {
        mutated = true;
        // Simulate a concurrent process B winning the steal race: it
        // deletes the dead lock and writes a NEW file at the same path —
        // a genuinely different inode — that happens to contain the exact
        // same PID string ('999999', simulating OS PID reuse) as the one
        // we just read. A content-only check would wrongly treat this as
        // "unchanged" and delete B's live lock; an inode check must not.
        fs.unlinkSync(lockPath);
        fs.writeFileSync(lockPath, '999999');
        return false; // still report the ORIGINAL pid as dead, triggering our steal attempt
      }
      return true; // the new (different-inode) lock is now treated as live — just wait
    };

    const result = await acquireCloneLock(lockPath, {
      waitTimeoutMs: 200,
      pollMs: 20,
      isAlive,
    });

    expect(result).toBeNull(); // never acquired — the "concurrent" lock was respected
    // Our own unlink attempt on the ORIGINAL inode must never have gone
    // through as a successful deletion of process B's replacement — the
    // file must still exist afterward.
    expect(fs.existsSync(lockPath)).toBe(true);
    expect(fs.readFileSync(lockPath, 'utf8').trim()).toBe('999999'); // B's file, untouched by us

    unlinkSpy.mockRestore();
  });

  it('does not steal a lock with unparseable content (freshly created, not yet PID-written)', async () => {
    // A lock file mid-creation by another process (openSync succeeded,
    // writeSync of the PID hasn't landed yet) reads as empty/unparseable —
    // must be treated as "wait and retry", not "dead, steal it".
    fs.mkdirSync(path.dirname(lockPath), { recursive: true });
    fs.writeFileSync(lockPath, '');
    const isAlive = vi.fn(() => false);

    const result = await acquireCloneLock(lockPath, {
      waitTimeoutMs: 100,
      pollMs: 20,
      isAlive,
    });

    expect(result).toBeNull();
    expect(isAlive).not.toHaveBeenCalled(); // never asked — no parseable PID to check
    expect(fs.existsSync(lockPath)).toBe(true); // untouched
  });
});

describe('runForwarding / makeSignalGuard', () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'deus-v2-forward-'));
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it('passes argv and cwd through unchanged and reports the real exit code', async () => {
    const outFile = path.join(tmpDir, 'out.json');
    const script = path.join(tmpDir, 'echo-argv.mjs');
    fs.writeFileSync(
      script,
      [
        "import fs from 'node:fs';",
        'fs.writeFileSync(process.argv[3], JSON.stringify({',
        '  args: process.argv.slice(2),',
        '  cwd: process.cwd(),',
        '}));',
        'process.exit(7);',
      ].join('\n'),
    );
    const cwd = tmpDir;
    const trickyArg = 'has spaces and "quotes" and $vars';
    const guard = makeSignalGuard();
    try {
      const result = await runForwarding(
        process.execPath,
        [script, trickyArg, outFile],
        { cwd },
        guard,
      );
      expect(result.code).toBe(7);
      expect(result.signal).toBeNull();
    } finally {
      guard.dispose();
    }

    const written = JSON.parse(fs.readFileSync(outFile, 'utf8'));
    expect(written.args[0]).toBe(trickyArg);
    // realpathSync, not path.resolve: macOS's os.tmpdir() lives under a
    // symlink (/var -> /private/var), and the spawned child's process.cwd()
    // resolves through it, so a plain string/path.resolve comparison of the
    // two would spuriously fail.
    expect(fs.realpathSync(written.cwd)).toBe(fs.realpathSync(cwd));
  });

  it.skipIf(process.platform === 'win32')(
    'forwards SIGTERM to the child process',
    async () => {
      const script = path.join(tmpDir, 'trap-sigterm.mjs');
      const outFile = path.join(tmpDir, 'trapped.txt');
      fs.writeFileSync(
        script,
        [
          "import fs from 'node:fs';",
          "process.on('SIGTERM', () => {",
          "  fs.writeFileSync(process.argv[2], 'trapped');",
          '  process.exit(0);',
          '});',
          'setTimeout(() => {}, 5000);',
        ].join('\n'),
      );
      const guard = makeSignalGuard();
      try {
        const resultPromise = runForwarding(
          process.execPath,
          [script, outFile],
          { cwd: tmpDir },
          guard,
        );
        // Give the child a moment to install its SIGTERM handler, then
        // deliver SIGTERM to our own process — the guard should forward it
        // to the child rather than let Node's default disposition kill us.
        await new Promise((resolve) => setTimeout(resolve, 300));
        process.emit('SIGTERM' as NodeJS.Signals, 'SIGTERM' as NodeJS.Signals);
        const result = await resultPromise;
        expect(result.code).toBe(0);
      } finally {
        guard.dispose();
      }
      expect(fs.readFileSync(outFile, 'utf8')).toBe('trapped');
    },
  );

  it('dispose() removes the guard listener so a later signal does not fire it', () => {
    const before = process.listenerCount('SIGTERM');
    const guard = makeSignalGuard();
    expect(process.listenerCount('SIGTERM')).toBe(before + 1);
    guard.dispose();
    expect(process.listenerCount('SIGTERM')).toBe(before);
  });

  it.skipIf(process.platform === 'win32')(
    "real end-to-end: a delegated child spawned WITHOUT detached shares this process's process group (accepted-tradeoff regression guard)",
    async () => {
      // A prior round of code review flagged the plain (non-detached)
      // forwarding above as risking a double-delivered signal when a
      // terminal sends one to the whole foreground process group. A fix
      // spawning the child detached (its own process group) was
      // implemented, then REVERTED after discovering Node's
      // `detached: true` on POSIX actually calls setsid() — a new SESSION,
      // not just a new process group — which would have broken real
      // interactive delegate commands (deus-cmd.sh has genuine `read -r`
      // prompts; `deus-v2 chat` is an interactive REPL) by detaching them
      // from the controlling terminal. This test pins the REVERTED
      // (current, intentional) behavior — the child shares this process's
      // process group (verified via `ps -o pgid=`, the same technique used
      // to derive the fix-then-revert empirically) — as a regression guard
      // against re-introducing `detached: true` by mistake.
      const ownPgid = spawnSync(
        'ps',
        ['-o', 'pgid=', '-p', String(process.pid)],
        { encoding: 'utf8' },
      ).stdout.trim();

      let capturedChildPid = -1;
      const guard = makeSignalGuard();
      const realSetChild = guard.setChild;
      guard.setChild = ((child: { pid: number }) => {
        capturedChildPid = child.pid;
        return realSetChild(child);
      }) as typeof guard.setChild;

      const resultPromise = runForwarding(
        'sleep',
        ['5'],
        { cwd: os.tmpdir() },
        guard,
      );

      await new Promise((resolve) => setTimeout(resolve, 150));
      expect(capturedChildPid).toBeGreaterThan(0);

      const childPgid = spawnSync(
        'ps',
        ['-o', 'pgid=', '-p', String(capturedChildPid)],
        { encoding: 'utf8' },
      ).stdout.trim();
      expect(childPgid).toBe(ownPgid); // same group — NOT detached into its own

      process.emit('SIGTERM' as NodeJS.Signals, 'SIGTERM' as NodeJS.Signals);
      const result = await resultPromise;
      guard.dispose();

      expect(result.signal).toBe('SIGTERM');
    },
  );
});

describe('ensureCheckout', () => {
  let tmpDir: string;
  let checkoutPath: string;
  let lockPath: string;

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'deus-v2-ensure-'));
    checkoutPath = path.join(tmpDir, 'deus-v2');
    lockPath = path.join(tmpDir, 'lock', 'deus-v2-clone.lock');
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it('releases the lock when the (injected) clone fails — regression test for the process.exit()-skips-finally bug', async () => {
    // Earlier version of this file called process.exit(1) from fail(),
    // which skips enclosing `finally` blocks (verified empirically — it
    // does not unwind the JS stack). That orphaned the lock on ordinary
    // clone failure (network error, bad git config), not just on a signal.
    // fail() now throws instead, so ensureCheckout's finally always runs.
    // No real network call here — spawnClone is injected to simulate a
    // failing clone.
    const spawnClone = vi.fn(async (guard) => {
      // Mirror real usage: a live "child" is set during the simulated
      // clone, then cleared, matching runForwarding's own contract.
      guard.setChild({ kill: () => {} });
      guard.clearChild();
      return { code: 1, signal: null };
    });

    await expect(
      ensureCheckout(checkoutPath, {
        lockPath,
        spawnClone,
        lockOpts: { waitTimeoutMs: 500, pollMs: 20 },
      }),
    ).rejects.toThrow(/git clone failed/);

    expect(spawnClone).toHaveBeenCalledTimes(1);
    expect(fs.existsSync(lockPath)).toBe(false);
    // checkoutPath itself must never have been touched — the "clone" only
    // ever writes to a disposable staging path (LIA-434 staging+rename
    // design), so a failure here leaves nothing at checkoutPath to clean up.
    expect(fs.existsSync(checkoutPath)).toBe(false);
  });

  it('cleans up the staging directory and reports it as never touching checkoutPath, on clone failure', async () => {
    // Confirms the specific staging-path contract: spawnClone receives a
    // path distinct from checkoutPath, and that path is removed on failure.
    let stagingSeen = '';
    const spawnClone = vi.fn(async (guard, targetPath: string) => {
      stagingSeen = targetPath;
      fs.mkdirSync(targetPath, { recursive: true });
      fs.writeFileSync(path.join(targetPath, 'partial'), 'x');
      return { code: 1, signal: null };
    });

    await expect(
      ensureCheckout(checkoutPath, {
        lockPath,
        spawnClone,
        lockOpts: { waitTimeoutMs: 500, pollMs: 20 },
      }),
    ).rejects.toThrow(/git clone failed/);

    expect(stagingSeen).not.toBe('');
    expect(stagingSeen).not.toBe(checkoutPath);
    expect(fs.existsSync(stagingSeen)).toBe(false); // cleaned up
    expect(fs.existsSync(checkoutPath)).toBe(false); // never touched
  });

  it('publishes the staged clone into checkoutPath via an atomic rename on success', async () => {
    const spawnClone = vi.fn(async (_guard, targetPath: string) => {
      // Simulate a real, valid clone landing in the staging dir.
      initRealRepo(targetPath, {
        origin: 'https://github.com/sliamh11/deus-v2.git',
      });
      fs.writeFileSync(
        path.join(targetPath, REAL_ENTRYPOINT_NAME),
        '#!/bin/sh\necho hi\n',
      );
      return { code: 0, signal: null };
    });

    await expect(
      ensureCheckout(checkoutPath, {
        lockPath,
        spawnClone,
        validateOpts: { isWindows: isWindowsPlatform() },
        lockOpts: { waitTimeoutMs: 500, pollMs: 20 },
      }),
    ).resolves.toBeUndefined();

    expect(fs.existsSync(path.join(checkoutPath, REAL_ENTRYPOINT_NAME))).toBe(
      true,
    );
    expect(fs.existsSync(lockPath)).toBe(false);
    // No stray staging directories left behind next to the real checkout.
    const siblings = fs.readdirSync(tmpDir);
    expect(siblings.filter((n) => n.includes('.staging.'))).toHaveLength(0);
  });

  it('publishes successfully when checkoutPath already exists as an empty directory (code-review-caught Windows bug)', async () => {
    // Regression test for a real cross-platform bug a GPT-backend code
    // review caught: validateCheckout's 'missing' reason (safe to
    // auto-clone into) covers BOTH a nonexistent path AND an existing,
    // empty directory — but on Windows, fs.renameSync (MoveFileExW) cannot
    // replace an existing directory destination even when it's empty,
    // unlike POSIX rename(2). This test can't exercise the actual Windows
    // code path on this host, but it does exercise the exact precondition
    // (checkoutPath pre-existing as an empty directory before publish) that
    // triggered the bug, proving the fix's rmdirSync-first step doesn't
    // regress the ordinary (POSIX) case either.
    fs.mkdirSync(checkoutPath, { recursive: true }); // pre-existing, empty

    const spawnClone = vi.fn(async (_guard, targetPath: string) => {
      initRealRepo(targetPath, {
        origin: 'https://github.com/sliamh11/deus-v2.git',
      });
      fs.writeFileSync(
        path.join(targetPath, REAL_ENTRYPOINT_NAME),
        '#!/bin/sh\necho hi\n',
      );
      return { code: 0, signal: null };
    });

    await expect(
      ensureCheckout(checkoutPath, {
        lockPath,
        spawnClone,
        validateOpts: { isWindows: isWindowsPlatform() },
        lockOpts: { waitTimeoutMs: 500, pollMs: 20 },
      }),
    ).resolves.toBeUndefined();

    expect(fs.existsSync(path.join(checkoutPath, REAL_ENTRYPOINT_NAME))).toBe(
      true,
    );
    expect(fs.existsSync(lockPath)).toBe(false);
  });

  it('releases the lock when post-clone validation fails', async () => {
    const spawnClone = vi.fn(async (_guard, targetPath: string) => {
      // Simulate git reporting success but landing an empty/invalid
      // directory in staging — post-rename validateCheckout should see
      // this as invalid, which ensureCheckout treats as an unexpected
      // failure rather than silently succeeding.
      fs.mkdirSync(targetPath, { recursive: true });
      return { code: 0, signal: null };
    });
    await expect(
      ensureCheckout(checkoutPath, {
        lockPath,
        spawnClone,
        lockOpts: { waitTimeoutMs: 500, pollMs: 20 },
      }),
    ).rejects.toThrow(/failed validation/);

    expect(fs.existsSync(lockPath)).toBe(false);
  });

  it('does nothing (no lock, no clone) when the checkout is already valid', async () => {
    initRealRepo(checkoutPath, {
      origin: 'https://github.com/sliamh11/deus-v2.git',
    });
    fs.writeFileSync(
      path.join(checkoutPath, REAL_ENTRYPOINT_NAME),
      '#!/bin/sh\necho hi\n',
    );

    const spawnClone = vi.fn();
    await expect(
      ensureCheckout(checkoutPath, {
        lockPath,
        spawnClone,
        validateOpts: { isWindows: isWindowsPlatform() },
      }),
    ).resolves.toBeUndefined();

    expect(spawnClone).not.toHaveBeenCalled();
    expect(fs.existsSync(lockPath)).toBe(false);
  });

  it('never touches an existing invalid checkout (hard-fail, no lock taken)', async () => {
    fs.mkdirSync(checkoutPath, { recursive: true });
    fs.writeFileSync(path.join(checkoutPath, 'MARKER'), 'not a git repo');

    const spawnClone = vi.fn();
    await expect(
      ensureCheckout(checkoutPath, { lockPath, spawnClone }),
    ).rejects.toThrow(/failed validation/);

    expect(spawnClone).not.toHaveBeenCalled();
    expect(fs.existsSync(lockPath)).toBe(false);
    expect(fs.existsSync(path.join(checkoutPath, 'MARKER'))).toBe(true);
  });

  it.skipIf(process.platform === 'win32')(
    'real end-to-end: a signal during an in-progress clone releases the lock and cleans up listeners',
    async () => {
      // Unlike the other ensureCheckout tests (fake spawnClone, no real
      // child), this one routes spawnClone through the REAL runForwarding
      // against a real long-running child process, so the guard's
      // "forward to a live child" branch is genuinely exercised end-to-end
      // — not just asserted by tracing the code, per the code-review request
      // to verify this path executably rather than by inspection alone.
      const slowScript = path.join(tmpDir, 'slow-clone.mjs');
      fs.writeFileSync(slowScript, 'setTimeout(() => {}, 5000);\n');

      const listenersBefore = process.listenerCount('SIGTERM');

      const spawnClone = (guard: Parameters<typeof runForwarding>[3]) =>
        runForwarding(process.execPath, [slowScript], { cwd: tmpDir }, guard);

      const resultPromise = ensureCheckout(checkoutPath, {
        lockPath,
        spawnClone,
        lockOpts: { waitTimeoutMs: 2000, pollMs: 20 },
      });

      // Give the child time to actually spawn and the lock to be taken,
      // then simulate an incoming signal the same way the runForwarding
      // test above does (process.emit, not a real self-kill).
      await new Promise((resolve) => setTimeout(resolve, 300));
      expect(fs.existsSync(lockPath)).toBe(true); // lock held mid-clone

      process.emit('SIGTERM' as NodeJS.Signals, 'SIGTERM' as NodeJS.Signals);

      await expect(resultPromise).rejects.toThrow();

      expect(fs.existsSync(lockPath)).toBe(false); // released after the interrupt
      expect(process.listenerCount('SIGTERM')).toBe(listenersBefore); // guard disposed
    },
  );
});
